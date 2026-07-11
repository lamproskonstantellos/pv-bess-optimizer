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
* **BESS-origin flows** (including ``bess_charge_grid_kwh``, whose
  throughput is bounded by the fading battery) are multiplied by
  ``bess_factor[y]``.
* **SOC** is also multiplied by ``bess_factor[y]``.
* **Load and grid prices** are unchanged from Year 1;
  ``grid_to_load_kwh`` passes through Year-1 values (its financial
  scaling lives in :mod:`pvbess_opt.economics`).
* **Mixed totals** (``grid_export_total_kwh``,
  ``grid_injection_total_kwh``) are recomputed from their scaled
  components so the identity ``export_total = pv_to_grid +
  bess_dis_grid`` holds in every projected year even when the PV and
  BESS fade curves diverge.

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

from .io import PROJECT_SHEET_DEFAULTS, parse_augmentation_years
from .kpis import require_economic_columns

__all__ = [
    "aggregate_lifetime_to_yearly",
    "bess_capacity_factors",
    "bess_capacity_factors_pooled",
    "build_lifetime_dispatch",
    "effective_bess_replacement_year",
    "resolve_augmentation_config",
    "resolve_bess_replacement_year",
    "warranty_cycle_utilisation",
]

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
    "bess_dis_load_green_kwh",
    "bess_dis_grid_green_kwh",
    "soc_kwh",
    "soc_green_kwh",
)

# Mixed totals rebuilt from their scaled components after the per-origin
# scaling loops (a single factor cannot scale a PV + BESS sum once the
# two fade curves diverge).  Maps total -> component columns.
_RECOMPUTED_TOTAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "grid_export_total_kwh": ("pv_to_grid_kwh", "bess_dis_grid_kwh"),
}

# EUR-per-step columns added by :func:`pvbess_opt.kpis.add_economic_columns`.
# Scaling convention: PV-origin revenue degrades on the PV production
# factor, BESS-origin revenue on the BESS capacity factor.  The
# ``expense_charge_bess_grid_eur`` column is bundled into the BESS-DAM
# stream by convention -- see ``pvbess_opt/conventions.md`` -- so it
# scales on ``bess_factor`` here AND in :func:`pvbess_opt.economics.build_yearly_cashflow`.
_PV_REVENUE_COLUMNS: tuple[str, ...] = (
    "profit_load_from_pv_eur",
    "profit_export_from_pv_eur",
    # PPA contract leg + covered-volume DAM value: both ride on PV
    # export, so they degrade on the PV production factor.
    "revenue_pv_ppa_eur",
    "ppa_covered_dam_value_eur",
)
_BESS_REVENUE_COLUMNS: tuple[str, ...] = (
    "profit_load_from_bess_eur",
    "profit_export_from_bess_eur",
    "expense_charge_bess_grid_eur",
)

# Balancing reservation columns scale with BESS capacity fade.
_BALANCING_RESERVATION_COLUMNS: tuple[str, ...] = (
    "bm_reservation_fcr_kw",
    "bm_reservation_afrr_up_kw",
    "bm_reservation_afrr_dn_kw",
    "bm_reservation_mfrr_up_kw",
    "bm_reservation_mfrr_dn_kw",
)


def resolve_bess_replacement_year(
    econ: dict[str, Any],
    *,
    year1_discharge_mwh: float,
    capacity_mwh: float,
) -> tuple[int, str, int]:
    """Resolve ``bess_replacement_year`` to one effective project year.

    Three-way semantics (see the workbook help text):

    * a positive integer N schedules the replacement in year N and the
      SOH threshold ``bess_eol_soh_pct`` is ignored completely;
    * the ``auto`` sentinel (blank cell or the literal string) replaces
      in the first project year whose SOH falls to
      ``bess_eol_soh_pct``, resolved analytically with
      :func:`bess_capacity_factors`; no crossing within the lifecycle
      means no replacement;
    * 0 never replaces.

    Returns ``(effective_year, source, second_crossing_year)`` where
    ``source`` is ``"scheduled"``, ``"soh_threshold"``,
    ``"soh_threshold_not_reached"`` or ``"never"``, and
    ``second_crossing_year`` is the first year the POST-replacement pack
    would cross the threshold again (0 = none).  Only one replacement is
    ever charged; callers surface the second crossing as a warning.
    The resolved value is stored by the pipeline as
    ``econ['bess_replacement_year_effective']`` so every consumer
    (cashflow, lifetime projection, LCOS, degradation report) reads one
    source of truth via :func:`effective_bess_replacement_year`.
    """
    raw = econ.get("bess_replacement_year", 0)
    n_years = int(
        econ.get(
            "project_lifecycle_years",
            PROJECT_SHEET_DEFAULTS["project_lifecycle_years"],
        )
        or PROJECT_SHEET_DEFAULTS["project_lifecycle_years"]
    )
    d_annual = float(econ.get("bess_degradation_annual_pct", 0.0) or 0.0) / 100.0
    d_cycle = float(
        econ.get("bess_degradation_pct_per_cycle", 0.0) or 0.0
    ) / 100.0
    threshold = float(econ.get("bess_eol_soh_pct", 80.0) or 80.0) / 100.0

    def _first_crossing(replacement_year: int, from_year: int) -> int:
        factors = bess_capacity_factors(
            n_years,
            d_bess_annual=d_annual,
            d_bess_per_cycle=d_cycle,
            year1_discharge_mwh=float(year1_discharge_mwh),
            capacity_mwh=float(capacity_mwh),
            replacement_year=replacement_year,
        )
        for y in range(from_year, n_years + 1):
            if factors[y - 1] <= threshold + 1e-12:
                return y
        return 0

    is_auto = isinstance(raw, str) and raw.strip().lower() == "auto"
    if not is_auto:
        scheduled = int(raw or 0)
        if scheduled > 0:
            return scheduled, "scheduled", 0
        return 0, "never", 0

    effective = _first_crossing(0, 1)
    if effective <= 0:
        return 0, "soh_threshold_not_reached", 0
    second = _first_crossing(effective, effective + 1)
    return effective, "soh_threshold", second


def effective_bess_replacement_year(econ: dict[str, Any]) -> int:
    """Return the effective replacement year for finance-layer consumers.

    Prefers the pipeline-resolved ``bess_replacement_year_effective``;
    falls back to a plain integer ``bess_replacement_year``.  An
    unresolved ``auto`` sentinel raises: the AUTO resolution must happen
    exactly once (:func:`resolve_bess_replacement_year`), never
    implicitly inside a consumer.
    """
    if "bess_replacement_year_effective" in econ:
        return int(econ.get("bess_replacement_year_effective") or 0)
    raw = econ.get("bess_replacement_year", 0)
    if isinstance(raw, str):
        if raw.strip().lower() == "auto":
            raise ValueError(
                "bess_replacement_year='auto' has not been resolved; call "
                "resolve_bess_replacement_year() and store the result as "
                "econ['bess_replacement_year_effective'] before the "
                "finance layer runs."
            )
        return int(float(raw))
    return int(raw or 0)


def _pv_factor(y: int, lid: float, d_annual: float) -> float:
    """Return the PV production factor for project year ``y``."""
    if y < 1:
        return 1.0
    if y == 1:
        return 1.0
    return (1.0 - lid) * (1.0 - d_annual) ** (y - 2)


def bess_capacity_factors(
    n_years: int,
    *,
    d_bess_annual: float,
    d_bess_per_cycle: float = 0.0,
    year1_discharge_mwh: float = 0.0,
    capacity_mwh: float = 0.0,
    replacement_year: int = 0,
) -> list[float]:
    """BESS capacity factors (SOH fractions) for project years 1..N.

    The single source of truth for the reset-at-replacement cycle-fade
    accumulator that was previously hand-copied across
    :func:`build_lifetime_dispatch`,
    :func:`pvbess_opt.economics.build_yearly_cashflow` and
    :func:`pvbess_opt.degradation.build_degradation_report`.  Per year::

        factor(y) = _bess_factor(y, ..., cumulative_cycles_through=K)
        K += year1_discharge_mwh * factor(y) / capacity_mwh

    with ``K`` reset to zero at project start and at
    ``replacement_year``.  ``factors[y - 1]`` is the capacity factor
    (equivalently the SOH fraction) of project year ``y`` given a
    replacement in ``replacement_year`` (0 = no replacement), which is
    exactly the question the SOH-threshold replacement resolver asks.
    """
    factors: list[float] = []
    cumulative_cycles = 0.0
    for y in range(1, int(n_years) + 1):
        if replacement_year > 0 and y == replacement_year:
            cumulative_cycles = 0.0
        factor = _bess_factor(
            y, d_bess_annual, replacement_year=replacement_year,
            d_bess_per_cycle=d_bess_per_cycle,
            cumulative_cycles_through=cumulative_cycles,
        )
        if capacity_mwh > 1e-12:
            cumulative_cycles += (year1_discharge_mwh * factor) / capacity_mwh
        factors.append(factor)
    return factors


def resolve_augmentation_config(
    econ: dict[str, Any],
) -> tuple[float, tuple[int, ...], str, float]:
    """Parse the augmentation / overbuild surface from the flat params.

    Returns ``(overbuild_frac, augmentation_years, mode, added_kwh)``
    with the all-off default ``(0.0, (), 'top_up', 0.0)``.  Range and
    exclusivity validation happens at load time
    (``io.validate_workbook_params``); this resolver only normalises,
    so every consumer (cashflow, lifetime projection, degradation
    report) reads one parse of the same keys.
    """
    overbuild_frac = max(
        0.0, float(econ.get("bess_overbuild_pct", 0.0) or 0.0) / 100.0
    )
    years = parse_augmentation_years(econ.get("bess_augmentation_years"))
    mode = str(
        econ.get("bess_augmentation_mode", "top_up") or "top_up"
    ).strip().lower()
    added_kwh = max(
        0.0, float(econ.get("bess_augmentation_kwh", 0.0) or 0.0)
    )
    return overbuild_frac, years, mode, added_kwh


def bess_capacity_factors_pooled(
    n_years: int,
    *,
    d_bess_annual: float,
    d_bess_per_cycle: float = 0.0,
    year1_discharge_mwh: float = 0.0,
    capacity_mwh: float = 0.0,
    replacement_year: int = 0,
    overbuild_frac: float = 0.0,
    augmentation_years: tuple[int, ...] = (),
    augmentation_mode: str = "top_up",
    augmentation_kwh: float = 0.0,
) -> tuple[list[float], dict[int, float]]:
    """Per-pool BESS capacity factors with overbuild + staged additions.

    The plant is a set of installed pools.  Pool ``i`` of size ``E_i``
    (MWh) installed in project year ``a_i`` fades on its own
    calendar-plus-cycle curve (Eq. E50)::

        phi_i(y) = max(0, (1 - d_cal)^(y - a_i) - d_cyc * K_i(y))

    where ``K_i`` accumulates the pool's PRO-RATA share of the plant
    throughput (apportioned by surviving pool capacity — the modelling
    choice documented in ``docs/economics_design.md``; at equal fade
    rates the apportionment is irrelevant).  The plant factor is the
    nameplate-clamped pool sum::

        f_y = min(1, sum_i E_i * phi_i(y) / E_N)

    so a day-1 DC overbuild (pool 0 sized ``(1 + ob) * E_N``, Eq. E52)
    holds the usable capacity at nameplate until fade consumes the
    margin, and a ``top_up`` augmentation event restores it to exactly
    nameplate in the event year.  Returns ``(factors, added_mwh)``
    where ``added_mwh`` maps each event year to the energy actually
    installed (the Eq. E51 CAPEX base; ``top_up`` events that find the
    plant still above nameplate add nothing).

    With no overbuild and no events the call **delegates** to
    :func:`bess_capacity_factors` (bit-identity, including the
    replacement reset).  The pooled path never coexists with a
    replacement — the loader rejects the combination — and a
    zero-capacity plant has nothing to pool, so both delegate too.
    """
    events = tuple(sorted({int(y) for y in augmentation_years}))
    active = (overbuild_frac > 0.0 or bool(events)) and capacity_mwh > 1e-12
    if not active:
        return bess_capacity_factors(
            n_years,
            d_bess_annual=d_bess_annual,
            d_bess_per_cycle=d_bess_per_cycle,
            year1_discharge_mwh=year1_discharge_mwh,
            capacity_mwh=capacity_mwh,
            replacement_year=replacement_year,
        ), {}
    if replacement_year > 0:
        raise ValueError(
            "bess_replacement_year cannot combine with augmentation / "
            "overbuild (the pooled capacity engine supersedes the single "
            "replacement); set bess_replacement_year = 0."
        )
    mode = str(augmentation_mode or "top_up").strip().lower()
    # Each pool: [install_year, size_mwh, cumulative_cycles].
    pools: list[list[float]] = [[1.0, capacity_mwh * (1.0 + overbuild_frac), 0.0]]

    def _phi(pool: list[float], y: int) -> float:
        calendar = (1.0 - d_bess_annual) ** (y - int(pool[0]))
        return max(0.0, calendar - d_bess_per_cycle * pool[2])

    factors: list[float] = []
    added_mwh: dict[int, float] = {}
    for y in range(1, int(n_years) + 1):
        if y in events:
            surviving = sum(p[1] * _phi(p, y) for p in pools)
            if mode == "fixed_kwh":
                delta = augmentation_kwh / 1000.0
            else:
                delta = max(0.0, capacity_mwh - surviving)
            if delta > 1e-12:
                pools.append([float(y), delta, 0.0])
                added_mwh[y] = delta
        survivals = [p[1] * _phi(p, y) for p in pools]
        available = sum(survivals)
        factor = min(1.0, available / capacity_mwh)
        # Pro-rata throughput apportionment: the plant discharges
        # D_1 * f_y this year; each pool cycles its share over its own
        # NAMEPLATE size (the same nameplate-cycle convention as the
        # single-pool accumulator in bess_capacity_factors).
        if available > 1e-12:
            discharge = year1_discharge_mwh * factor
            for pool, surv in zip(pools, survivals, strict=True):
                if pool[1] > 1e-12:
                    pool[2] += discharge * (surv / available) / pool[1]
        factors.append(factor)
    return factors, added_mwh


def warranty_cycle_utilisation(
    n_years: int,
    *,
    year1_discharge_mwh: float,
    capacity_mwh: float,
    factors: list[float],
    basis: str = "nameplate",
    max_cycles_per_year: float = 0.0,
) -> tuple[list[float], list[bool]]:
    """Per-year full-equivalent cycles on the chosen basis (Eq. E47).

    Under the analytic scaling recipe the year-``y`` discharge is
    ``D_1 f_y``, so::

        FEC_y = D_1 f_y / E_N          (basis = 'nameplate')
        FEC_y = D_1 f_y / (E_N f_y)
              = D_1 / E_N              (basis = 'faded')

    The faded-basis ratio is constant and the nameplate-basis ratio is
    maximal in Year 1 (or in a replacement reset year), which is why
    the Year-1 MILP constraint (Eq. E46) is sufficient and the
    projected years only need this analytic check.  Returns
    ``(cycles_per_year, exceeds_cap_mask)``; the mask is all-False
    when ``max_cycles_per_year`` is 0 (cap off).
    """
    basis = str(basis or "nameplate").strip().lower()
    cycles: list[float] = []
    exceeds: list[bool] = []
    for y in range(1, int(n_years) + 1):
        factor = float(factors[y - 1]) if y <= len(factors) else 1.0
        if capacity_mwh <= 1e-12:
            fec = 0.0
        elif basis == "faded" and factor > 1e-12:
            fec = year1_discharge_mwh / capacity_mwh
        else:
            fec = year1_discharge_mwh * factor / capacity_mwh
        cycles.append(fec)
        exceeds.append(
            max_cycles_per_year > 0.0 and fec > max_cycles_per_year + 1e-9
        )
    return cycles, exceeds


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
    from ``year1_kpis``.  Falls back to summing the per-step discharge
    columns directly from ``res_year1`` when the override is not given.

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
    bess_repl_year = effective_bess_replacement_year(econ)

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
    # Baseload PPA (Eqs. P9/E45): the contract volume is FIXED, not
    # PV-degrading, so its two EUR columns must not ride the PV fade —
    # they are excluded here and the yearly cashflow's no-fade branch
    # applies the same convention (economics.build_yearly_cashflow).
    if str(
        econ.get("ppa_structure", "pay_as_produced") or "pay_as_produced"
    ).strip().lower() == "baseload":
        pv_rev_cols = [
            c for c in pv_rev_cols
            if c not in ("revenue_pv_ppa_eur", "ppa_covered_dam_value_eur")
        ]
    bess_rev_cols = [c for c in _BESS_REVENUE_COLUMNS if c in res_year1.columns]
    # Balancing reservations live on BESS capacity, so they degrade
    # with the BESS capacity-fade curve.
    balancing_cols = [
        c for c in _BALANCING_RESERVATION_COLUMNS if c in res_year1.columns
    ]

    # Anchor the timestamp shift to ``project_start_year`` so the
    # lifetime DataFrame's ``timestamp`` column is internally consistent
    # with its ``calendar_year`` column.
    input_first_year = int(
        pd.to_datetime(res_year1["timestamp"]).dt.year.iloc[0]
    )

    _ob_frac, _aug_years, _aug_mode, _aug_kwh = resolve_augmentation_config(
        econ,
    )
    bess_factors, _ = bess_capacity_factors_pooled(
        n_years,
        d_bess_annual=d_bess,
        d_bess_per_cycle=d_bess_per_cycle,
        year1_discharge_mwh=year1_discharge_mwh,
        capacity_mwh=capacity_mwh,
        replacement_year=bess_repl_year,
        overbuild_frac=_ob_frac,
        augmentation_years=_aug_years,
        augmentation_mode=_aug_mode,
        augmentation_kwh=_aug_kwh,
    )

    chunks: list[pd.DataFrame] = []
    for y in range(1, n_years + 1):
        pv_f = _pv_factor(y, lid, d_annual)
        bess_f = bess_factors[y - 1]
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
        for col in balancing_cols:
            chunk[col] = chunk[col].astype(float) * bess_f
        # Mixed totals: rebuild from the scaled components so the export
        # identity holds when pv_factor != bess_factor (a single-factor
        # scale of the Year-1 total cannot).
        for total_col, components in _RECOMPUTED_TOTAL_COLUMNS.items():
            if total_col in chunk.columns:
                chunk[total_col] = sum(
                    chunk[c].astype(float) for c in components
                    if c in chunk.columns
                )
        if "grid_injection_total_kwh" in chunk.columns:
            # The injection total's basis depends on the cap mode:
            # surplus export by default, total plant injection under
            # grid_cap_includes_load.  All four candidate components are
            # already scaled, so rebuild on the same basis Year 1 used.
            surplus = sum(
                chunk[c].astype(float)
                for c in ("pv_to_grid_kwh", "bess_dis_grid_kwh")
                if c in chunk.columns
            )
            if bool(econ.get("grid_cap_includes_load", False)) and str(
                econ.get("mode", "self_consumption"),
            ).lower() == "self_consumption":
                chunk["grid_injection_total_kwh"] = surplus + sum(
                    chunk[c].astype(float)
                    for c in ("pv_to_load_kwh", "bess_dis_load_kwh")
                    if c in chunk.columns
                )
            else:
                chunk["grid_injection_total_kwh"] = surplus
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

    ``revenue_eur_dam_retail`` is the per-step DAM + retail **gross**
    aggregate — revenue at the dispatch prices minus the grid-charging
    expense, BEFORE the aggregator fee (the fee is a project-level
    deduction applied only in the cashflow).  It reconciles against the
    cashflow as ``revenue_eur - aggregator_fee_eur`` (the fee column is
    signed negative) whenever ``retail_inflation_pct`` and
    ``dam_inflation_pct`` are zero; with non-zero indexation the
    cashflow escalates per stream while this frame stays at Year-1
    prices by construction.  It also deliberately **excludes**
    balancing revenue — the lifetime frame is per-step physics, while
    balancing settles per window via reservation × probability × price
    (see :func:`pvbess_opt.economics.build_yearly_cashflow`).  Callers
    that want a true project-total revenue should use the cashflow
    columns.
    """
    if lifetime_df.empty:
        return pd.DataFrame(
            columns=[
                "project_year", "calendar_year",
                "pv_generation_mwh", "pv_to_load_mwh", "pv_to_grid_mwh",
                "bess_charge_mwh", "bess_discharge_mwh",
                "import_to_load_mwh", "export_total_mwh",
                "revenue_eur_dam_retail",
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
            "revenue_eur_dam_retail": grouped["_net_revenue_per_step"].sum(),
        }
    )
    out = out.reset_index()
    cols = ["project_year", "calendar_year"] + [
        c for c in out.columns if c not in ("project_year", "calendar_year")
    ]
    return out[cols].sort_values("calendar_year").reset_index(drop=True)
