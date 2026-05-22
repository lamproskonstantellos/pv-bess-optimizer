"""Multi-year hourly dispatch projection by analytical degradation scaling.

The MILP is solved **once** for Year 1; Years 2..N are derived from
the Year-1 dispatch by applying:

* a PV degradation curve (initial light-induced + linear),
* a BESS capacity-fade curve (linear),
* a revenue-side inflation index, and
* an OPEX inflation index.

This matches the recipe used by Gridcog, Aurora, and HOMER in their
"fast" / "quick" mode.

Scaling rules (per year ``y`` for ``y >= 1``)
---------------------------------------------

* **PV-origin flows** are multiplied by ``pv_factor[y]``.
* **BESS-origin flows** are multiplied by ``bess_factor[y]``.
* **SOC** is also multiplied by ``bess_factor[y]``.
* **Load and grid prices** are unchanged from Year 1.
* **Mixed flows** (``grid_to_load_kwh``, ``bess_charge_grid_kwh``)
  pass through Year-1 values; their financial scaling lives in
  :mod:`pvbess_opt.economics`.

Timestamps are shifted year-by-year so each year of the lifetime
DataFrame carries a plausible datetime aligned with the
``calendar_year`` column.

Reconciliation invariant
------------------------

``tests/test_lifetime.py`` asserts:

    sum(pv_kwh in lifetime[y]) / sum(pv_kwh in Year 1) ≈ pv_factor[y]

within 0.1 % for every year.  See the lifetime-scaling note under
``docs/source/technical.documentation/lifetime_scaling.rst`` for the
derivation.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from dateutil.relativedelta import relativedelta

from .io import PROJECT_SHEET_DEFAULTS
from .kpis import require_economic_columns

# Columns scaled by the PV degradation curve.
_PV_ORIGIN_COLUMNS: tuple[str, ...] = (
    "pv_kwh",
    "pv_to_load_kwh",
    "pv_to_grid_kwh",
    "pv_curtail_kwh",
    "pv_to_bess_kwh",
)

# Columns scaled by the BESS capacity-fade curve.
_BESS_ORIGIN_COLUMNS: tuple[str, ...] = (
    "bess_dis_load_kwh",
    "bess_dis_grid_kwh",
    "bess_charge_grid_kwh",
    "grid_export_total_kwh",
    "bess_dis_load_green_kwh",
    "bess_dis_grid_green_kwh",
    "soc_kwh",
    "soc_green_kwh",
)

# EUR-per-step columns added by :func:`pvbess_opt.kpis.add_economic_columns`.
# Scaling convention: PV-origin revenue degrades on the PV production
# factor, BESS-origin revenue on the BESS capacity factor.  This matches
# the per-stream degradation in
# :func:`pvbess_opt.economics.build_yearly_cashflow`.
_PV_REVENUE_COLUMNS: tuple[str, ...] = (
    "profit_load_from_pv_eur",
    "profit_export_from_pv_eur",
)
_BESS_REVENUE_COLUMNS: tuple[str, ...] = (
    "profit_load_from_bess_eur",
    "profit_export_from_bess_eur",
    "expense_charge_bess_grid_eur",
)


def _pv_factor(y: int, lid: float, d_annual: float) -> float:
    """Return the PV production factor for project year ``y``."""
    if y < 1:
        return 1.0
    if y == 1:
        return 1.0
    return (1.0 - lid) * (1.0 - d_annual) ** (y - 2)


def _bess_factor(
    y: int,
    d_bess_annual: float,
    replacement_year: int = 0,
    *,
    d_bess_per_cycle: float = 0.0,
    cumulative_cycles_through: float = 0.0,
) -> float:
    """Return the BESS capacity factor for project year ``y``.

    Combines the unchanged multiplicative calendar fade with an optional
    linear cycle-fade term::

        factor = max(0.0,
            (1 - d_annual)^years_since  -  d_per_cycle * cumulative_cycles
        )

    When ``replacement_year > 0`` and ``y >= replacement_year``, the
    calendar factor resets to 1.0 at year ``replacement_year`` and
    degrades fresh from there (linear at ``d_bess_annual`` per year).

    With the cycle keyword-only parameters left at 0 the result is the
    multiplicative calendar fade alone.
    """
    if y < 1:
        return 1.0
    if replacement_year > 0 and y >= replacement_year:
        years_since_install = y - replacement_year
    else:
        years_since_install = y - 1
    calendar = (1.0 - d_bess_annual) ** years_since_install
    cycle = d_bess_per_cycle * cumulative_cycles_through
    return max(0.0, calendar - cycle)


def build_lifetime_dispatch(
    res_year1: pd.DataFrame,
    econ: dict[str, Any],
    capacities: dict[str, float],
    *,
    year1_discharge_mwh: float | None = None,
) -> pd.DataFrame:
    """Project the Year-1 hourly dispatch across the full project horizon.

    ``year1_discharge_mwh`` overrides the value derived from ``res_year1``
    when supplied — used to keep the cycle-counter symmetric with
    ``build_yearly_cashflow`` which already reads the post-derate value
    from ``year1_kpis``.  Falls back to the in-frame sum for
    backward-compat callers that don't plumb KPIs through.

    Requires ``compute_kpis`` to have been called first so the per-step
    EUR columns are present on ``res_year1``; raises otherwise rather
    than silently projecting zero revenue across the horizon.
    """
    if "timestamp" not in res_year1.columns:
        raise ValueError(
            "build_lifetime_dispatch requires a 'timestamp' column on "
            "res_year1."
        )
    if not pd.api.types.is_datetime64_any_dtype(res_year1["timestamp"]):
        raise ValueError(
            "build_lifetime_dispatch requires res_year1['timestamp'] to "
            "be a datetime column."
        )
    require_economic_columns(res_year1, context="build_lifetime_dispatch")

    raw_n_years = econ.get(
        "project_lifecycle_years",
        PROJECT_SHEET_DEFAULTS["project_lifecycle_years"],
    )
    if raw_n_years is None:
        raw_n_years = PROJECT_SHEET_DEFAULTS["project_lifecycle_years"]
    n_years = int(raw_n_years)
    if n_years < 1:
        raise ValueError(
            f"project_lifecycle_years must be >= 1, got {n_years!r}"
        )
    project_start_year = int(
        econ.get("project_start_year", PROJECT_SHEET_DEFAULTS["project_start_year"])
        or PROJECT_SHEET_DEFAULTS["project_start_year"]
    )
    lid = float(econ.get("pv_degradation_year1_pct", 0.0)) / 100.0
    d_annual = float(econ.get("pv_degradation_annual_pct", 0.0)) / 100.0
    d_bess = float(econ.get("bess_degradation_annual_pct", 0.0)) / 100.0
    d_bess_per_cycle = float(
        econ.get("bess_degradation_pct_per_cycle", 0.0) or 0.0
    ) / 100.0
    bess_repl_year = int(econ.get("bess_replacement_year", 0) or 0)

    # Full equivalent cycle convention: discharge-only FEC, matching
    # ``compute_financial_kpis`` (bess_lifetime_cycles = discharge MWh /
    # capacity MWh in pvbess_opt/economics.py).
    capacity_mwh = float(capacities.get("bess_kwh", 0.0) or 0.0) / 1000.0
    if year1_discharge_mwh is None:
        _dis_cols = [
            c for c in ("bess_dis_load_kwh", "bess_dis_grid_kwh")
            if c in res_year1.columns
        ]
        if _dis_cols:
            year1_discharge_mwh = float(
                res_year1[_dis_cols].to_numpy(dtype=float).sum()
            ) / 1000.0
        else:
            year1_discharge_mwh = 0.0
    else:
        year1_discharge_mwh = float(year1_discharge_mwh)

    pv_cols = [c for c in _PV_ORIGIN_COLUMNS if c in res_year1.columns]
    bess_cols = [c for c in _BESS_ORIGIN_COLUMNS if c in res_year1.columns]
    pv_rev_cols = [c for c in _PV_REVENUE_COLUMNS if c in res_year1.columns]
    bess_rev_cols = [c for c in _BESS_REVENUE_COLUMNS if c in res_year1.columns]

    # Anchor the timestamp shift to ``project_start_year`` so the
    # lifetime DataFrame's ``timestamp`` column is internally consistent
    # with its ``calendar_year`` column.
    input_first_year = int(
        pd.to_datetime(res_year1["timestamp"]).dt.year.iloc[0]
    )

    chunks: list[pd.DataFrame] = []
    cumulative_cycles = 0.0  # reset at project start AND at replacement_year
    for y in range(1, n_years + 1):
        if bess_repl_year > 0 and y == bess_repl_year:
            cumulative_cycles = 0.0
        pv_f = _pv_factor(y, lid, d_annual)
        bess_f = _bess_factor(
            y, d_bess, replacement_year=bess_repl_year,
            d_bess_per_cycle=d_bess_per_cycle,
            cumulative_cycles_through=cumulative_cycles,
        )
        if capacity_mwh > 1e-12:
            cumulative_cycles += (year1_discharge_mwh * bess_f) / capacity_mwh
        chunk = res_year1.copy()
        chunk["project_year"] = int(y)
        target_calendar_year = int(project_start_year + y - 1)
        chunk["calendar_year"] = target_calendar_year
        # dateutil.relativedelta is leap-day safe: shifting Feb-29 by
        # N years lands on Feb-28 in non-leap target years instead of
        # rolling over to Mar-1 like pd.DateOffset(years=N) does.
        # The element-wise apply is materially slower than the
        # vectorised DateOffset so we only take the safe path when the
        # input actually contains a Feb-29 timestamp; otherwise both
        # are equivalent.
        n_years_shift = target_calendar_year - input_first_year
        if n_years_shift:
            ts_in = chunk["timestamp"]
            has_feb29 = bool(
                ((ts_in.dt.month == 2) & (ts_in.dt.day == 29)).any()
            )
            if has_feb29:
                shift = relativedelta(years=n_years_shift)
                chunk["timestamp"] = ts_in.apply(lambda t, shift=shift: t + shift)
            else:
                chunk["timestamp"] = ts_in + pd.DateOffset(
                    years=n_years_shift,
                )
        for col in pv_cols:
            chunk[col] = chunk[col].astype(float) * pv_f
        for col in bess_cols:
            chunk[col] = chunk[col].astype(float) * bess_f
        for col in pv_rev_cols:
            chunk[col] = chunk[col].astype(float) * pv_f
        for col in bess_rev_cols:
            chunk[col] = chunk[col].astype(float) * bess_f
        # soc_pct stays unchanged: SOC and E_cap both scale by bess_factor.
        chunks.append(chunk)

    lifetime = pd.concat(chunks, ignore_index=True)

    leading = ["project_year", "calendar_year", "timestamp"]
    rest = [c for c in res_year1.columns if c not in leading]
    ordered = leading + [c for c in rest if c in lifetime.columns]
    return lifetime[ordered]


def aggregate_lifetime_to_yearly(lifetime_df: pd.DataFrame) -> pd.DataFrame:
    """Sum lifetime hourly columns by calendar year for cross-checks.

    Requires the per-step EUR columns produced by ``compute_kpis`` /
    ``build_lifetime_dispatch`` to be present; raises rather than
    silently aggregating zero revenue.
    """
    if lifetime_df.empty:
        return pd.DataFrame(
            columns=[
                "project_year", "calendar_year",
                "pv_generation_mwh", "pv_to_load_mwh", "pv_to_grid_mwh",
                "bess_charge_mwh", "bess_discharge_mwh",
                "import_to_load_mwh", "export_total_mwh",
                "revenue_eur_total",
            ],
        )
    require_economic_columns(lifetime_df, context="aggregate_lifetime_to_yearly")

    df = lifetime_df.copy()
    grouped = df.groupby("calendar_year")
    year_index = grouped.size().index

    def _sum_kwh(col: str) -> pd.Series:
        """Return per-year MWh sums reindexed against the year axis.

        Missing columns yield 0.0 instead of NaN so the multi-path
        combinations below (e.g. ``bess_charge_mwh = pv_to_bess +
        grid_to_bess``) stay symmetric across all KPIs.
        """
        if col in df.columns:
            s = grouped[col].sum() / 1000.0
        else:
            s = pd.Series(dtype=float)
        return s.reindex(year_index, fill_value=0.0)

    revenue_cols = [
        c for c in (
            "profit_load_from_pv_eur",
            "profit_load_from_bess_eur",
            "profit_export_from_pv_eur",
            "profit_export_from_bess_eur",
        ) if c in df.columns
    ]
    expense_cols = [
        c for c in ("expense_charge_bess_grid_eur",) if c in df.columns
    ]
    if revenue_cols:
        revenue = df[revenue_cols].sum(axis=1)
    else:
        revenue = pd.Series(0.0, index=df.index, dtype=float)
    if expense_cols:
        expense = df[expense_cols].sum(axis=1)
    else:
        expense = pd.Series(0.0, index=df.index, dtype=float)
    df["_net_revenue_per_step"] = revenue - expense

    out = pd.DataFrame(
        {
            "project_year": grouped["project_year"].first().astype(int),
            "pv_generation_mwh": _sum_kwh("pv_kwh"),
            "pv_to_load_mwh": _sum_kwh("pv_to_load_kwh"),
            "pv_to_grid_mwh": _sum_kwh("pv_to_grid_kwh"),
            "bess_charge_mwh": (
                _sum_kwh("pv_to_bess_kwh")
                + _sum_kwh("bess_charge_grid_kwh")
            ),
            "bess_discharge_mwh": (
                _sum_kwh("bess_dis_load_kwh")
                + _sum_kwh("bess_dis_grid_kwh")
            ),
            "import_to_load_mwh": _sum_kwh("grid_to_load_kwh"),
            "export_total_mwh": _sum_kwh("grid_export_total_kwh"),
            "revenue_eur_total": grouped["_net_revenue_per_step"].sum(),
        }
    )
    out = out.reset_index()
    cols = ["project_year", "calendar_year"] + [
        c for c in out.columns if c not in ("project_year", "calendar_year")
    ]
    return out[cols].sort_values("calendar_year").reset_index(drop=True)
