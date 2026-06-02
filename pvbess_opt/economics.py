"""Multi-year economic and cash-flow projection for the PV + BESS optimizer.

This module extends the single-year MILP with a long-horizon financial
model.  Given the hourly dispatch produced by :mod:`pvbess_opt.optimization`
and the headline KPI dictionary returned by :func:`pvbess_opt.kpis.compute_kpis`,
the helpers below project yearly, quarterly, and monthly cash-flows and
compute the standard project-finance metrics (NPV, IRR, ROI, BCR, simple
and discounted payback).

Why an analytical scaling and not a re-solve per year?
------------------------------------------------------

Industry practice is to solve the dispatch optimisation **once** for
a representative "Year 1" then derive Years 2..N analytically by
applying a PV degradation curve, a BESS capacity-fade curve, and
inflation indices for revenue and OPEX.

Calendar-year convention
-------------------------------

* **Year 0** carries the upfront CAPEX only.  Its calendar year is
  ``project_start_year - 1`` (CAPEX is paid the year before
  commercial-operations date).
* **Year 1** is the first operating year.  Its calendar year is
  ``project_start_year`` exactly.
* **Year N** is the last operating year, calendar
  ``project_start_year + N - 1``.

A 20-year run with ``project_start_year = 2026`` therefore produces
21 yearly rows: Year 0 = 2025 (CAPEX only), Years 1..20 = 2026..2045.
Year 0 and Year 1 carry distinct calendar values rather than sharing
the same calendar year.

Sign convention
---------------

* **CAPEX** rows are stored as **negative** numbers (cash outflow).
* **OPEX** rows are stored as **negative** numbers (cash outflow).
* **Revenue** rows are stored as **positive** numbers (cash inflow).
* ``net_cashflow = revenue + opex + capex + devex`` (sum of signed
  components).

References for default values
-----------------------------

* PV CAPEX ~525 EUR/kWp (utility-scale ground mount, 2024) — IRENA
  *Renewable Power Generation Costs in 2023* (2024).
* BESS CAPEX ~200 EUR/kW power block (DC + PCS, EU-utility, 2024) —
  Lazard *Levelized Cost of Storage v9* (2024).
* PV degradation 2.5% Year-1 LID + 0.55%/yr linear — Tier-1 module
  warranty terms (Jinko / LONGi / Trina, 25-year linear ≤ 0.55%/yr).
* BESS degradation 2%/yr linear (LFP, ~80% capacity at 10y) — typical
  Tier-1 cell warranty.
* Discount rate 7% — typical EU renewable WACC band 6–8%.
* Retail / DAM indexation — user-supplied annual percentages; the
  workbook defaults to 0 (no indexation) for both so the user has to
  opt in explicitly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .constants import (
    BENCHMARK_LCOE_HIGH_EUR_PER_MWH,
    BENCHMARK_LCOE_LOW_EUR_PER_MWH,
    BENCHMARK_LCOS_HIGH_EUR_PER_MWH,
    BENCHMARK_LCOS_LOW_EUR_PER_MWH,
)
from .io import PROJECT_SHEET_DEFAULTS, read_workbook
from .kpis import require_economic_columns
from .lifetime import _bess_factor

logger = logging.getLogger(__name__)

__all__ = [
    "build_yearly_cashflow",
    "calculate_irr",
    "compute_financial_kpis",
    "derive_asset_capacities",
    "derive_monthly_cashflow",
    "read_economic_params",
]


# ---------------------------------------------------------------------------
# IRR helper
# ---------------------------------------------------------------------------


def calculate_irr(
    cash_flows: np.ndarray,
    *,
    guess: float = 0.1,
    max_iterations: int = 200,
    tolerance: float = 1.0e-7,
) -> float:
    """Compute IRR via Newton-Raphson with a bisection fall-back."""
    cash_flows = np.asarray(cash_flows, dtype=float)
    if cash_flows.size == 0 or np.all(cash_flows >= 0) or np.all(cash_flows <= 0):
        return float("nan")

    def npv(rate: float) -> float:
        return float(sum(cf / (1.0 + rate) ** t for t, cf in enumerate(cash_flows)))

    rate = guess
    for _ in range(max_iterations):
        if rate <= -0.999:
            break
        f = npv(rate)
        df = sum(-t * cf / (1.0 + rate) ** (t + 1) for t, cf in enumerate(cash_flows))
        if abs(df) < 1.0e-12:
            break
        new_rate = rate - f / df
        if abs(new_rate - rate) < tolerance:
            return float(new_rate)
        rate = new_rate

    low, high = -0.99, 10.0
    f_low, f_high = npv(low), npv(high)
    if np.isnan(f_low) or np.isnan(f_high) or f_low * f_high > 0.0:
        return float("nan")
    for _ in range(200):
        mid = 0.5 * (low + high)
        f_mid = npv(mid)
        if abs(f_mid) < tolerance or (high - low) < tolerance:
            return float(mid)
        if f_low * f_mid < 0.0:
            high, f_high = mid, f_mid
        else:
            low, f_low = mid, f_mid
    return float(0.5 * (low + high))


# ---------------------------------------------------------------------------
# Workbook input
# ---------------------------------------------------------------------------


def read_economic_params(xlsx_path: str | Path) -> dict[str, Any]:
    """Read the project / pv / bess / economics / simulation sheets.

    Returns a single flat dict combining every key from the five
    parameter sheets — the financial helpers downstream expect a flat
    mapping (e.g. ``econ['discount_rate_pct']``,
    ``econ['capex_pv_eur_per_kw']``).
    """
    typed = read_workbook(xlsx_path)
    merged: dict[str, Any] = {}
    for section in ("project", "pv", "bess", "economics", "simulation"):
        merged.update(typed[section])
    return merged


# ---------------------------------------------------------------------------
# Asset sizing resolution
# ---------------------------------------------------------------------------


def derive_asset_capacities(
    econ: dict[str, Any],
    params: dict[str, Any],
    ts: pd.DataFrame,
) -> dict[str, float]:
    """Resolve the PV nameplate and BESS sizing that drive EUR/kW math.

    ``pv_nameplate_kwp``, ``bess_power_kw`` and
    ``bess_capacity_kwh`` are workbook inputs (no inference, no
    decision-variable read-back).  ``bess_kwh`` follows ``bess_kw``:
    zero when the BESS is absent, otherwise the workbook value.
    ``econ`` and ``ts`` are kept in the signature for API symmetry.

    Negative inputs are clamped to zero as defense-in-depth: the
    workbook validator rejects them upstream, but a hand-built
    ``params`` dict (or a future caller that bypasses validation) must
    not propagate a negative capacity into the EUR/kW math.
    """
    _ = econ, ts  # accepted for API symmetry
    pv_kwp = max(float(params.get("pv_nameplate_kwp", 0.0) or 0.0), 0.0)
    bess_kw = max(float(params.get("bess_power_kw", 0.0) or 0.0), 0.0)
    bess_kwh = max(float(params.get("bess_capacity_kwh", 0.0) or 0.0), 0.0)
    return {
        "pv_kwp": pv_kwp,
        "bess_kw": bess_kw,
        "bess_kwh": bess_kwh if bess_kw > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Yearly cash-flow
# ---------------------------------------------------------------------------


def build_yearly_cashflow(
    year1_kpis: dict[str, Any],
    econ: dict[str, Any],
    capacities: dict[str, float],
) -> pd.DataFrame:
    """Build the Year-0..N yearly cash-flow projection.

    Year 0 carries the upfront CAPEX and nothing else.  Year 1 uses the
    Year-1 KPI ``profit_total_eur`` as the revenue base.  Years 2..N are
    derived analytically from the PV degradation curve, BESS capacity
    fade, and inflation indices.

    Calendar-year mapping:
    Year 0 (CAPEX paid the year before COD) lands at calendar
    ``project_start_year - 1``; Years 1..N at
    ``project_start_year .. project_start_year + N - 1``.
    """
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

    pv_kwp = float(capacities["pv_kwp"])
    bess_kw = float(capacities["bess_kw"])

    capex_pv_y0 = -float(econ["capex_pv_eur_per_kw"]) * pv_kwp
    capex_bess_y0 = -float(econ["capex_bess_eur_per_kw"]) * bess_kw
    # Site-wide lump-sum CAPEX/DEVEX (substation, grid upgrades,
    # interconnection, environmental studies, ...) are not per-asset, so
    # they fold straight into the Year-0 outflow rows.
    site_capex_y0 = -float(econ.get("site_capex_eur", 0.0) or 0.0)
    site_devex_y0 = -float(econ.get("site_devex_eur", 0.0) or 0.0)
    capex_total_y0 = capex_pv_y0 + capex_bess_y0 + site_capex_y0

    devex_pv_y0 = -float(econ.get("devex_pv_eur_per_kw", 0.0) or 0.0) * pv_kwp
    devex_bess_y0 = -float(econ.get("devex_bess_eur_per_kw", 0.0) or 0.0) * bess_kw
    devex_total_y0 = devex_pv_y0 + devex_bess_y0 + site_devex_y0

    # Revenue is derated by the aggregator fee (Gridcog /
    # merchant-aggregator convention).  The unavailability factor is
    # already baked into ``year1_kpis['profit_total_eur']`` upstream
    # (see :mod:`pvbess_opt.availability`), so it is NOT re-applied here.
    aggregator_fee_pct = float(econ.get("aggregator_fee_pct_revenue", 0.0) or 0.0)
    aggregator_fee_frac = max(0.0, min(1.0, aggregator_fee_pct / 100.0))

    # Split the Year-1 revenue base into retail (load-coverage)
    # and DAM (wholesale export) streams.  Retail revenue is indexed by
    # retail_inflation_pct (CPI-linked PPAs / Self-consumption tariffs).  DAM revenue
    # is indexed by dam_inflation_pct (default 0 — Lazard / Aurora /
    # Gridcog use exogenous price curves, not CPI).  Grid-charging cost
    # (a negative on the revenue side) tracks the DAM index.
    _has_breakdown = any(
        k in year1_kpis for k in (
            "profit_load_from_pv_eur", "profit_load_from_bess_eur",
            "profit_export_from_pv_eur", "profit_export_from_bess_eur",
            "expense_charge_bess_grid_eur",
        )
    )
    if _has_breakdown:
        # PV-origin vs BESS-origin Year-1 revenue (mirrors lifetime.py's
        # _PV_REVENUE_COLUMNS / _BESS_REVENUE_COLUMNS so the two sheets agree).
        rev1_retail_pv = float(year1_kpis.get("profit_load_from_pv_eur", 0.0) or 0.0)
        rev1_retail_bess = float(
            year1_kpis.get("profit_load_from_bess_eur", 0.0) or 0.0
        )
        rev1_dam_pv = float(year1_kpis.get("profit_export_from_pv_eur", 0.0) or 0.0)
        # expense_charge_bess_grid_eur is bundled into the BESS-DAM
        # stream by convention -- see ``pvbess_opt/conventions.md``.
        # The same convention is honoured by ``_BESS_REVENUE_COLUMNS``
        # in ``pvbess_opt/lifetime.py`` so the cashflow and lifetime
        # sheets stay aligned.
        rev1_dam_bess = float(
            year1_kpis.get("profit_export_from_bess_eur", 0.0) or 0.0
        ) - float(year1_kpis.get("expense_charge_bess_grid_eur", 0.0) or 0.0)
        revenue_1_retail = rev1_retail_pv + rev1_retail_bess
        revenue_1_dam = rev1_dam_pv + rev1_dam_bess
        revenue_1_gross = revenue_1_retail + revenue_1_dam
        # Reconciliation guard — when the KPI dict carries
        # profit_total_eur it should equal retail + DAM within rounding.
        if "profit_total_eur" in year1_kpis:
            profit_total = float(year1_kpis["profit_total_eur"] or 0.0)
            if abs(profit_total - revenue_1_gross) > max(
                1.0, abs(profit_total) * 1e-6,
            ):
                logger.warning(
                    "Year-1 revenue split drift: profit_total_eur=%.2f vs "
                    "retail+dam=%.2f. Using component sum.",
                    profit_total, revenue_1_gross,
                )
    else:
        # When year1_kpis carries only profit_total_eur with no
        # per-stream breakdown, index the whole revenue as retail
        # (CPI-linked); this coincides with the per-stream result
        # whenever retail_inflation_pct == dam_inflation_pct.
        revenue_1_gross = float(year1_kpis.get("profit_total_eur", 0.0) or 0.0)
        revenue_1_retail = revenue_1_gross
        revenue_1_dam = 0.0
        # With no per-stream breakdown the whole revenue base is degraded
        # on pv_factor by routing it all to the PV-origin retail component.
        logger.debug(
            "build_yearly_cashflow: year1_kpis lacks per-stream breakdown; "
            "degrading all revenue on pv_factor."
        )
        rev1_retail_pv = revenue_1_gross
        rev1_retail_bess = 0.0
        rev1_dam_pv = 0.0
        rev1_dam_bess = 0.0

    opex_pv_1 = float(econ["opex_pv_eur_per_kwp"]) * pv_kwp
    opex_bess_1 = float(econ["opex_bess_eur_per_kw"]) * bess_kw
    opex_1 = -(opex_pv_1 + opex_bess_1)

    pv_deg_y1 = float(econ["pv_degradation_year1_pct"]) / 100.0
    pv_deg_annual = float(econ["pv_degradation_annual_pct"]) / 100.0
    bess_deg_annual = float(econ["bess_degradation_annual_pct"]) / 100.0
    bess_deg_per_cycle = float(
        econ.get("bess_degradation_pct_per_cycle", 0.0) or 0.0
    ) / 100.0
    retail_infl = float(econ.get("retail_inflation_pct", 0.0) or 0.0) / 100.0
    dam_infl = float(econ.get("dam_inflation_pct", 0.0) or 0.0) / 100.0
    opex_infl = float(econ["opex_inflation_pct"]) / 100.0
    discount_rate = float(econ["discount_rate_pct"]) / 100.0
    bm_infl = float(econ.get("bm_inflation_pct", 0.0) or 0.0) / 100.0
    # Year-1 balancing revenue lines come from the KPI dict; they
    # already carry the BESS degradation factor for Year 1 (which is
    # 1.0) and degrade on the BESS capacity-fade curve via bess_factor
    # in subsequent years, indexed by bm_inflation_pct.
    bm_cap_y1 = float(
        year1_kpis.get("bm_total_capacity_revenue_eur", 0.0) or 0.0
    )
    bm_act_y1 = float(
        year1_kpis.get("bm_total_activation_revenue_eur", 0.0) or 0.0
    )

    bess_repl_year = int(econ.get("bess_replacement_year", 0) or 0)
    bess_repl_cost_pct = float(econ.get("bess_replacement_cost_pct", 0.0) or 0.0)

    # Cumulative full-equivalent-cycle accumulator for the cycle-fade
    # term.  Convention matches compute_financial_kpis' bess_lifetime_cycles
    # (discharge MWh / capacity MWh).  Resets at project start and at
    # bess_replacement_year.
    capacity_mwh = float(capacities.get("bess_kwh", 0.0) or 0.0) / 1000.0
    year1_discharge_mwh = float(
        year1_kpis.get("bess_total_discharge_mwh", 0.0) or 0.0
    )
    cumulative_cycles = 0.0

    rows: list[dict[str, float]] = []
    for y in range(0, n_years + 1):
        if y == 0:
            pv_factor = 1.0
            bess_factor = 1.0
            revenue_retail_y = 0.0
            revenue_dam_y = 0.0
            revenue_gross_y = 0.0
            opex_y = 0.0
            capex_y = capex_total_y0
            devex_y = devex_total_y0
            aggregator_fee_y = 0.0
            balancing_capacity_y = 0.0
            balancing_activation_y = 0.0
        else:
            if y == 1:
                pv_factor = 1.0
            else:
                pv_factor = (1.0 - pv_deg_y1) * (1.0 - pv_deg_annual) ** (y - 2)
            if bess_repl_year > 0 and y == bess_repl_year:
                cumulative_cycles = 0.0
            bess_factor = _bess_factor(
                y, bess_deg_annual, replacement_year=bess_repl_year,
                d_bess_per_cycle=bess_deg_per_cycle,
                cumulative_cycles_through=cumulative_cycles,
            )
            if capacity_mwh > 1e-12:
                cumulative_cycles += (
                    year1_discharge_mwh * bess_factor / capacity_mwh
                )
            # Degrade PV-origin revenue on pv_factor and BESS-origin
            # revenue on bess_factor, mirroring lifetime.py:248-251 so the
            # two sheets in 03_results.xlsx agree.  Inflation is applied
            # per stream (retail vs DAM index).
            revenue_retail_y = (
                rev1_retail_pv * pv_factor + rev1_retail_bess * bess_factor
            ) * (1.0 + retail_infl) ** (y - 1)
            revenue_dam_y = (
                rev1_dam_pv * pv_factor + rev1_dam_bess * bess_factor
            ) * (1.0 + dam_infl) ** (y - 1)
            revenue_gross_y = revenue_retail_y + revenue_dam_y
            # The aggregator fee is by spec a non-negative deduction
            # (BSPs charge a positive fraction of gross revenue, never
            # rebate negative-gross dispatches).  Clamping the gross at
            # zero stops the fee from flipping to a revenue when
            # revenue_gross_y < 0 (a regime that can occur in pure-
            # arbitrage projects with sustained negative DAM hours).
            aggregator_fee_y = -max(revenue_gross_y, 0.0) * aggregator_fee_frac
            opex_y = opex_1 * (1.0 + opex_infl) ** (y - 1)
            if bess_repl_year > 0 and y == bess_repl_year:
                capex_y = capex_bess_y0 * (bess_repl_cost_pct / 100.0)
            else:
                capex_y = 0.0
            devex_y = 0.0
            balancing_capacity_y = (
                bm_cap_y1 * bess_factor * (1.0 + bm_infl) ** (y - 1)
            )
            balancing_activation_y = (
                bm_act_y1 * bess_factor * (1.0 + bm_infl) ** (y - 1)
            )

        revenue_net_y = revenue_gross_y + aggregator_fee_y
        # Split the aggregator fee across the two streams in proportion
        # to their gross contribution so the per-stream net columns add
        # up exactly to revenue_eur.
        if abs(revenue_gross_y) > 1e-12:
            retail_share = revenue_retail_y / revenue_gross_y
        else:
            retail_share = 0.0
        retail_fee_y = aggregator_fee_y * retail_share
        dam_fee_y = aggregator_fee_y - retail_fee_y
        revenue_retail_net_y = revenue_retail_y + retail_fee_y
        revenue_dam_net_y = revenue_dam_y + dam_fee_y
        balancing_revenue_y = balancing_capacity_y + balancing_activation_y
        # Balancing revenue is treated as an offset on the cash inflow
        # side; the existing aggregator fee already covers DAM/retail
        # streams, so we do not derate balancing revenue by it (industry
        # convention: BSPs typically settle ancillary services directly
        # with the TSO, not through the same aggregator).
        net_cf = (
            revenue_net_y + balancing_revenue_y
            + opex_y + capex_y + devex_y
        )
        discount_factor = 1.0 / (1.0 + discount_rate) ** y
        rows.append(
            {
                "project_year": int(y),
                "calendar_year": int(project_start_year + y - 1),
                "pv_production_factor": float(pv_factor),
                "bess_capacity_factor": float(bess_factor),
                "revenue_eur": float(revenue_net_y),
                "revenue_retail_eur": float(revenue_retail_net_y),
                "revenue_dam_eur": float(revenue_dam_net_y),
                "aggregator_fee_eur": float(aggregator_fee_y),
                "balancing_capacity_revenue_eur": float(balancing_capacity_y),
                "balancing_activation_revenue_eur": float(balancing_activation_y),
                "balancing_revenue_eur": float(balancing_revenue_y),
                "opex_eur": float(opex_y),
                "capex_eur": float(capex_y),
                "devex_eur": float(devex_y),
                "net_cashflow_eur": float(net_cf),
                "discount_factor": float(discount_factor),
                "discounted_cf_eur": float(net_cf * discount_factor),
            }
        )

    df = pd.DataFrame(rows)
    df["cumulative_cf_eur"] = df["net_cashflow_eur"].cumsum()
    df["cumulative_dcf_eur"] = df["discounted_cf_eur"].cumsum()
    return df


# ---------------------------------------------------------------------------
# Monthly + quarterly cash-flow
# ---------------------------------------------------------------------------


def derive_monthly_cashflow(
    res: pd.DataFrame,
    yearly_cf: pd.DataFrame,
    econ: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Derive monthly and quarterly cash-flows from the yearly projection.

    Requires ``compute_kpis`` to have been called first so the per-step
    EUR columns are present on ``res``; raises otherwise rather than
    silently defaulting revenue to zero.

    Output frame columns
    --------------------

    * ``project_year`` / ``calendar_year`` / ``period`` / ``period_type``
      — period descriptors. ``period`` is the month (1..12) or quarter
      (1..4) and ``period_type`` is ``"month"`` or ``"quarter"``.
    * ``pv_production_mwh`` — Year-1 monthly PV energy scaled by the
      year's PV degradation factor.
    * ``revenue_eur`` — DAM + retail revenue net of the aggregator fee
      (matches ``yearly_cf['revenue_eur']`` in scope). Balancing is not
      included here; it is surfaced in its own column so callers can
      reconcile against either ``yearly_cf['revenue_eur']`` or
      ``yearly_cf['revenue_eur'] + yearly_cf['balancing_revenue_eur']``.
    * ``balancing_revenue_eur`` — per-month allocation of
      ``yearly_cf['balancing_revenue_eur']``. The Year-1 share comes
      from the aggregate per-month sum of every
      ``bm_reservation_<product>_kw`` column on ``res`` (matching the
      reservation-weighted allocation in
      :func:`plot_bess_revenue_by_month`); when reservations are
      identically zero, falls back to a flat ``1/12`` split.
    * ``aggregator_fee_eur`` — per-month allocation of
      ``yearly_cf['aggregator_fee_eur']``, weighted by the monthly
      ``revenue_eur`` share so each month carries its proportional
      slice of the fee that has already been deducted from
      ``revenue_eur``.
    * ``opex_eur`` — Year-1 ``opex`` split evenly across months, scaled
      by the year's opex inflation factor.
    * ``net_cashflow_eur`` — ``revenue_eur + balancing_revenue_eur
      + opex_eur``. Sums to ``yearly_cf['net_cashflow_eur']`` row-for-
      row in any operating year without replacement / devex events.
    * ``discounted_cf_eur`` — ``net_cashflow_eur`` discounted at
      ``econ['discount_rate_pct']`` to the start of the project.

    The quarterly frame carries the same columns aggregated by
    ``period = ((month - 1) // 3) + 1``.
    """
    if not pd.api.types.is_datetime64_any_dtype(res["timestamp"]):
        raise ValueError(
            "derive_monthly_cashflow requires res['timestamp'] to be a "
            "datetime column."
        )
    require_economic_columns(res, context="derive_monthly_cashflow")

    discount_rate = float(econ["discount_rate_pct"]) / 100.0

    timestamps = pd.to_datetime(res["timestamp"])
    month_idx = timestamps.dt.month

    revenue_cols = [
        c for c in (
            "profit_load_from_pv_eur", "profit_load_from_bess_eur",
            "profit_export_from_pv_eur", "profit_export_from_bess_eur",
        ) if c in res.columns
    ]
    expense_cols = [
        c for c in ("expense_charge_bess_grid_eur",) if c in res.columns
    ]

    monthly_revenue_y1 = pd.Series(0.0, index=range(1, 13), dtype=float)
    monthly_pv_kwh_y1 = pd.Series(0.0, index=range(1, 13), dtype=float)

    if revenue_cols:
        revenue_per_step = res[revenue_cols].sum(axis=1)
    else:
        revenue_per_step = pd.Series(0.0, index=res.index, dtype=float)
    if expense_cols:
        expense_per_step = res[expense_cols].sum(axis=1)
    else:
        expense_per_step = pd.Series(0.0, index=res.index, dtype=float)

    net_revenue_per_step = revenue_per_step - expense_per_step

    grouped_revenue = net_revenue_per_step.groupby(month_idx).sum()
    if "pv_kwh" in res.columns:
        grouped_pv_kwh = res["pv_kwh"].groupby(month_idx).sum()
    else:
        grouped_pv_kwh = pd.Series(dtype=float)

    for m, val in grouped_revenue.items():
        # pandas types the index value as Hashable; the groupby was by
        # integer month so int() is always valid.
        monthly_revenue_y1.loc[int(m)] = float(val)  # type: ignore[call-overload]
    for m, val in grouped_pv_kwh.items():
        monthly_pv_kwh_y1.loc[int(m)] = float(val)  # type: ignore[call-overload]

    yearly_y1_revenue = float(
        yearly_cf.loc[yearly_cf["project_year"] == 1, "revenue_eur"].iloc[0]
    )
    monthly_y1_sum = float(monthly_revenue_y1.sum())
    if abs(monthly_y1_sum) > 1e-9 and abs(yearly_y1_revenue) > 1e-9:
        scale = yearly_y1_revenue / monthly_y1_sum
        monthly_revenue_y1 = monthly_revenue_y1 * scale

    yearly_y1_opex = float(
        yearly_cf.loc[yearly_cf["project_year"] == 1, "opex_eur"].iloc[0]
    )
    monthly_opex_y1 = pd.Series(yearly_y1_opex / 12.0, index=range(1, 13), dtype=float)

    monthly_pv_mwh_y1 = monthly_pv_kwh_y1 / 1000.0

    # Per-month balancing share — aggregate reservation kW across every
    # balancing product, group by month, normalize.  Falls back to a
    # flat 1/12 when no reservation columns are present or when every
    # reservation is identically zero (e.g. balancing toggled on with no
    # bids).  The chosen allocation matches the per-product weighting in
    # ``plot_bess_revenue_by_month``.
    balancing_products = ("fcr", "afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn")
    total_reservation = pd.Series(0.0, index=res.index, dtype=float)
    any_reservation_column = False
    for product in balancing_products:
        rcol = f"bm_reservation_{product}_kw"
        if rcol in res.columns:
            any_reservation_column = True
            total_reservation = total_reservation + res[rcol].astype(float)

    if any_reservation_column:
        monthly_reservation = (
            total_reservation.groupby(month_idx).sum()
            .reindex(range(1, 13), fill_value=0.0)
            .astype(float)
        )
        reservation_sum = float(monthly_reservation.sum())
        if reservation_sum > 1e-9:
            balancing_share = monthly_reservation / reservation_sum
        else:
            logger.debug(
                "derive_monthly_cashflow: reservation columns present but "
                "all zeros; falling back to flat 1/12 balancing allocation."
            )
            balancing_share = pd.Series(
                1.0 / 12.0, index=range(1, 13), dtype=float,
            )
    else:
        balancing_share = pd.Series(
            1.0 / 12.0, index=range(1, 13), dtype=float,
        )

    # Aggregator-fee share — proportional to the monthly post-fee
    # ``revenue_eur`` so each month carries its slice of the fee that
    # has already been deducted from ``revenue_eur``.
    rev_y1_total = float(monthly_revenue_y1.sum())
    if abs(rev_y1_total) > 1e-9:
        fee_share = monthly_revenue_y1 / rev_y1_total
    else:
        fee_share = pd.Series(1.0 / 12.0, index=range(1, 13), dtype=float)

    has_balancing_col = "balancing_revenue_eur" in yearly_cf.columns
    has_fee_col = "aggregator_fee_eur" in yearly_cf.columns

    rows: list[dict[str, Any]] = []
    yearly_indexed = yearly_cf.set_index("project_year")
    for y in yearly_indexed.index:
        if y == 0:
            continue
        rev_y = float(yearly_indexed.loc[y, "revenue_eur"])
        opex_y = float(yearly_indexed.loc[y, "opex_eur"])
        pv_factor = float(yearly_indexed.loc[y, "pv_production_factor"])
        cal_y = int(yearly_indexed.loc[y, "calendar_year"])
        balancing_y = (
            float(yearly_indexed.loc[y, "balancing_revenue_eur"])
            if has_balancing_col else 0.0
        )
        fee_y = (
            float(yearly_indexed.loc[y, "aggregator_fee_eur"])
            if has_fee_col else 0.0
        )

        if abs(yearly_y1_revenue) > 1e-9:
            rev_scale = rev_y / yearly_y1_revenue
        else:
            rev_scale = 0.0
        if abs(yearly_y1_opex) > 1e-9:
            opex_scale = opex_y / yearly_y1_opex
        else:
            opex_scale = 0.0

        for m in range(1, 13):
            rev_m = float(monthly_revenue_y1.loc[m]) * rev_scale
            opex_m = float(monthly_opex_y1.loc[m]) * opex_scale
            pv_mwh_m = float(monthly_pv_mwh_y1.loc[m]) * pv_factor
            balancing_m = float(balancing_share.loc[m]) * balancing_y
            fee_m = float(fee_share.loc[m]) * fee_y
            net_m = rev_m + balancing_m + opex_m
            t_years = float(y) + (m - 1) / 12.0
            disc_factor = 1.0 / (1.0 + discount_rate) ** t_years
            rows.append(
                {
                    "project_year": int(y),
                    "calendar_year": cal_y,
                    "period": int(m),
                    "period_type": "month",
                    "pv_production_mwh": float(pv_mwh_m),
                    "revenue_eur": float(rev_m),
                    "balancing_revenue_eur": float(balancing_m),
                    "aggregator_fee_eur": float(fee_m),
                    "opex_eur": float(opex_m),
                    "net_cashflow_eur": float(net_m),
                    "discounted_cf_eur": float(net_m * disc_factor),
                }
            )

    monthly_cf = pd.DataFrame(rows)

    monthly_columns = [
        "project_year", "calendar_year", "period",
        "period_type", "pv_production_mwh", "revenue_eur",
        "balancing_revenue_eur", "aggregator_fee_eur",
        "opex_eur", "net_cashflow_eur", "discounted_cf_eur",
    ]
    if monthly_cf.empty:
        quarterly_cf = pd.DataFrame(columns=monthly_columns)
    else:
        monthly_cf = monthly_cf[monthly_columns]
        monthly_with_q = monthly_cf.copy()
        monthly_with_q["quarter"] = ((monthly_with_q["period"] - 1) // 3) + 1
        agg = (
            monthly_with_q.groupby(
                ["project_year", "calendar_year", "quarter"], as_index=False,
            )[
                [
                    "pv_production_mwh", "revenue_eur",
                    "balancing_revenue_eur", "aggregator_fee_eur",
                    "opex_eur", "net_cashflow_eur", "discounted_cf_eur",
                ]
            ].sum()
        )
        agg = agg.rename(columns={"quarter": "period"})
        agg["period_type"] = "quarter"
        agg = agg[monthly_columns]
        quarterly_cf = agg.reset_index(drop=True)

    return monthly_cf, quarterly_cf


# ---------------------------------------------------------------------------
# Headline financial KPIs
# ---------------------------------------------------------------------------


def compute_financial_kpis(
    yearly_cf: pd.DataFrame,
    econ: dict[str, Any],
    *,
    capacities: dict[str, float] | None = None,
    lifetime_yearly: pd.DataFrame | None = None,
    year1_kpis: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Compute the headline NPV / IRR / ROI / BCR / payback metrics
    plus the LCOE / LCOS / capacity-factor / cycles metrics when
    ``capacities``, ``lifetime_yearly``, and ``year1_kpis`` are provided.

    KPI keys are lowercase snake_case.

    NPV / IRR / ROI / BCR / payback read ``net_cashflow_eur`` and
    ``discounted_cf_eur`` directly, so any site-wide lump-sum CAPEX/DEVEX
    folded into the Year-0 ``capex_eur`` / ``devex_eur`` rows by
    :func:`build_yearly_cashflow` is reflected automatically.  Balancing
    revenue enters NPV / IRR / ROI / BCR / payback the same way — via
    ``balancing_revenue_eur`` in the yearly cashflow, which is included
    in ``net_cashflow_eur`` by :func:`build_yearly_cashflow` — so all
    five cashflow-derived KPIs already account for the FCR / aFRR /
    mFRR streams when balancing is on.

    LCOE is PV-only and LCOS is BESS-only (IEA / IRENA / NREL ATB /
    Lazard convention): their numerators are built from the per-asset
    CAPEX/DEVEX/OPEX directly, never from the cash-flow ``capex_eur``
    column.  Site-wide lump-sum costs are neither PV-only nor BESS-only
    and are therefore **excluded** from both LCOE and LCOS so the values
    stay Lazard-comparable.  Balancing revenue is also **excluded** from
    LCOE and LCOS by the same convention: balancing is a revenue (not a
    cost), it does not move the LCOS discharge-MWh denominator, and
    Lazard's published bands are revenue-agnostic energy-cost figures.
    Toggling ``balancing_enabled`` with identical capacities and price
    inputs must therefore leave LCOE and LCOS unchanged.
    """
    df = yearly_cf

    project_year_col = "project_year"
    project_years = df[project_year_col].to_numpy(dtype=float)
    after_y0_mask = df[project_year_col] >= 1

    capex_y0 = float(df.loc[df[project_year_col] == 0, "capex_eur"].iloc[0]) \
        if (df[project_year_col] == 0).any() else 0.0
    capex_abs = abs(float(capex_y0))

    npv = float(df["discounted_cf_eur"].sum())

    cf_array = df["net_cashflow_eur"].to_numpy(dtype=float)
    irr = calculate_irr(cf_array)
    irr_pct = float("nan") if np.isnan(irr) else irr * 100.0

    after_y0_cf = df.loc[after_y0_mask, "net_cashflow_eur"]
    if capex_abs > 1e-9:
        roi_pct = float(after_y0_cf.sum()) / capex_abs * 100.0
    else:
        roi_pct = float("nan")

    discounted = df["discounted_cf_eur"].to_numpy(dtype=float)
    dcf_pos = float(np.sum(np.where(discounted > 0, discounted, 0.0)))
    dcf_neg_abs = float(np.sum(np.where(discounted < 0, -discounted, 0.0)))
    if dcf_neg_abs > 1e-9:
        bcr = dcf_pos / dcf_neg_abs
    else:
        bcr = float("nan")

    payback = _payback_year(
        project_years,
        df["cumulative_cf_eur"].to_numpy(dtype=float),
        df["net_cashflow_eur"].to_numpy(dtype=float),
    )
    discounted_payback = _payback_year(
        project_years,
        df["cumulative_dcf_eur"].to_numpy(dtype=float),
        df["discounted_cf_eur"].to_numpy(dtype=float),
    )

    total_capex_eur = float(df["capex_eur"].sum()) if "capex_eur" in df.columns \
        else float(capex_y0)
    total_devex_eur = (
        float(df["devex_eur"].sum()) if "devex_eur" in df.columns else 0.0
    )
    total_capex_devex_eur = total_capex_eur + total_devex_eur
    total_opex_eur_lifecycle = (
        float(df.loc[after_y0_mask, "opex_eur"].sum())
        if "opex_eur" in df.columns else 0.0
    )
    total_revenue_eur_lifecycle = (
        float(df.loc[after_y0_mask, "revenue_eur"].sum())
        if "revenue_eur" in df.columns else 0.0
    )
    total_aggregator_fee_eur_lifecycle = (
        float(df.loc[after_y0_mask, "aggregator_fee_eur"].sum())
        if "aggregator_fee_eur" in df.columns else 0.0
    )
    total_balancing_revenue_eur_lifecycle = (
        float(df.loc[after_y0_mask, "balancing_revenue_eur"].sum())
        if "balancing_revenue_eur" in df.columns else 0.0
    )
    total_balancing_capacity_revenue_eur_lifecycle = (
        float(df.loc[after_y0_mask, "balancing_capacity_revenue_eur"].sum())
        if "balancing_capacity_revenue_eur" in df.columns else 0.0
    )
    total_balancing_activation_revenue_eur_lifecycle = (
        float(df.loc[after_y0_mask, "balancing_activation_revenue_eur"].sum())
        if "balancing_activation_revenue_eur" in df.columns else 0.0
    )

    if "calendar_year" in df.columns and (df["project_year"] >= 1).any():
        first_op_year_row = df.loc[df["project_year"] == 1].iloc[0]
        project_start_year = int(first_op_year_row["calendar_year"])
        project_end_year = int(df["calendar_year"].iloc[-1])
    elif "calendar_year" in df.columns and len(df) > 0:
        project_start_year = int(df["calendar_year"].iloc[0])
        project_end_year = int(df["calendar_year"].iloc[-1])
    else:
        project_start_year = int(
            econ.get("project_start_year",
                     PROJECT_SHEET_DEFAULTS["project_start_year"])
            or PROJECT_SHEET_DEFAULTS["project_start_year"]
        )
        n_years = int(
            econ.get("project_lifecycle_years",
                     PROJECT_SHEET_DEFAULTS["project_lifecycle_years"])
            or PROJECT_SHEET_DEFAULTS["project_lifecycle_years"]
        )
        project_end_year = project_start_year + n_years - 1

    if "calendar_year" in df.columns and (df["project_year"] == 0).any():
        capex_year = int(
            df.loc[df["project_year"] == 0, "calendar_year"].iloc[0]
        )
    else:
        capex_year = int(project_start_year - 1) if project_start_year else 0

    payback_rounded = (
        float("nan") if np.isnan(payback) else float(round(payback, 4))
    )

    # ---- LCOE / LCOS / capacity-factor / cycles --------------------------
    # Balancing capacity and activation revenue do not enter either LCOE
    # or LCOS — both metrics measure cost per delivered MWh, and the
    # balancing streams are revenue (not cost) and do not produce DAM
    # discharge MWh (the LCOS denominator).  They flow into NPV/IRR/payback
    # via build_yearly_cashflow but are deliberately excluded here.
    extras: dict[str, float] = {
        "lcoe_eur_per_mwh": float("nan"),
        "lcos_eur_per_mwh": float("nan"),
        "pv_capacity_factor": float("nan"),
        "bess_lifetime_cycles": float("nan"),
    }
    if capacities is not None and lifetime_yearly is not None:
        pv_kwp = float(capacities.get("pv_kwp", 0.0) or 0.0)
        bess_kw = float(capacities.get("bess_kw", 0.0) or 0.0)
        bess_kwh = float(capacities.get("bess_kwh", 0.0) or 0.0)
        op_mask = df[project_year_col] >= 1

        if pv_kwp > 0.0 and "pv_generation_mwh" in lifetime_yearly.columns:
            # LCOE per IEA / IRENA / NREL ATB: isolate PV-only economics.
            # Numerator must NOT include BESS CAPEX, BESS DEVEX, BESS OPEX
            # or BESS replacement.  Denominator uses derated PV generation
            # (the lifetime_yearly column is already unavailability-derated
            # upstream in main._build_financials).
            ly = lifetime_yearly.set_index("project_year") \
                if "project_year" in lifetime_yearly.columns else None
            disc_series = df.set_index(project_year_col)["discount_factor"]

            disc_y0 = float(
                df.loc[df[project_year_col] == 0, "discount_factor"].iloc[0]
            ) if (df[project_year_col] == 0).any() else 1.0
            capex_pv_y0 = float(econ.get("capex_pv_eur_per_kw", 0.0)) * pv_kwp
            devex_pv_y0 = (
                float(econ.get("devex_pv_eur_per_kw", 0.0) or 0.0) * pv_kwp
            )
            disc_pv_capex = (capex_pv_y0 + devex_pv_y0) * disc_y0

            opex_pv_per_kwp = float(econ.get("opex_pv_eur_per_kwp", 0.0))
            opex_infl_lcoe = float(econ.get("opex_inflation_pct", 0.0) or 0.0) / 100.0
            disc_pv_opex = 0.0
            disc_pv_mwh = 0.0
            for y in df.loc[op_mask, project_year_col]:
                yi = int(y)
                if yi == 0:
                    continue
                disc_y = float(disc_series.loc[yi])
                opex_pv_y = (
                    opex_pv_per_kwp * pv_kwp * (1.0 + opex_infl_lcoe) ** (yi - 1)
                )
                disc_pv_opex += disc_y * opex_pv_y
                if ly is not None and yi in ly.index:
                    # pandas .loc returns a broad Scalar type; the column
                    # is numeric by construction (verified upstream).
                    disc_pv_mwh += disc_y * float(
                        ly.loc[yi, "pv_generation_mwh"],  # type: ignore[arg-type]
                    )

            disc_pv_total = disc_pv_capex + disc_pv_opex
            if disc_pv_mwh > 1e-9:
                extras["lcoe_eur_per_mwh"] = float(
                    round(disc_pv_total / disc_pv_mwh, 4),
                )
            # Expose the discounted components so downstream sensitivity
            # plots can compute the correct LCOE range
            # (disc_capex * (1 +/- capex_d) + disc_opex * (1 +/- opex_d)) / disc_mwh
            # rather than the incorrect base * (1 +/- capex_d)(1 +/- opex_d)
            # multiplicative approximation.
            extras["lcoe_disc_pv_capex_eur"] = float(disc_pv_capex)
            extras["lcoe_disc_pv_opex_eur"] = float(disc_pv_opex)
            extras["lcoe_disc_pv_mwh"] = float(disc_pv_mwh)

        if (
            bess_kw > 0.0 and bess_kwh > 0.0
            and "bess_discharge_mwh" in lifetime_yearly.columns
        ):
            # BESS-attributable CAPEX share: BESS power-block + BESS DEVEX.
            bess_capex_y0 = float(econ.get("capex_bess_eur_per_kw", 0.0)) * bess_kw
            bess_devex_y0 = (
                float(econ.get("devex_bess_eur_per_kw", 0.0) or 0.0) * bess_kw
            )
            bess_repl_year = int(econ.get("bess_replacement_year", 0) or 0)
            bess_repl_pct = float(econ.get("bess_replacement_cost_pct", 0.0) or 0.0)

            disc_y0 = float(
                df.loc[df[project_year_col] == 0, "discount_factor"].iloc[0]
            ) if (df[project_year_col] == 0).any() else 1.0
            disc_bess_capex = (bess_capex_y0 + bess_devex_y0) * disc_y0

            if bess_repl_year > 0 and (
                df[project_year_col] == bess_repl_year
            ).any():
                disc_repl = float(
                    df.loc[df[project_year_col] == bess_repl_year,
                           "discount_factor"].iloc[0]
                )
                disc_bess_capex += (
                    bess_capex_y0 * (bess_repl_pct / 100.0) * disc_repl
                )

            opex_bess_per_kw = float(econ.get("opex_bess_eur_per_kw", 0.0))
            disc_bess_opex = 0.0
            disc_bess_mwh = 0.0
            ly = lifetime_yearly.set_index("project_year") \
                if "project_year" in lifetime_yearly.columns else None
            disc_series = df.set_index(project_year_col)["discount_factor"]
            opex_infl = float(econ.get("opex_inflation_pct", 0.0)) / 100.0
            for y in df.loc[op_mask, project_year_col]:
                yi = int(y)
                if yi == 0:
                    continue
                disc_y = float(disc_series.loc[yi])
                opex_bess_y = (
                    opex_bess_per_kw * bess_kw * (1.0 + opex_infl) ** (yi - 1)
                )
                disc_bess_opex += disc_y * opex_bess_y
                if ly is not None and yi in ly.index:
                    # pandas .loc returns a broad Scalar type; the column
                    # is numeric by construction (verified upstream).
                    disc_bess_mwh += disc_y * float(
                        ly.loc[yi, "bess_discharge_mwh"],  # type: ignore[arg-type]
                    )

            disc_bess_total = disc_bess_capex + disc_bess_opex
            if disc_bess_mwh > 1e-9:
                extras["lcos_eur_per_mwh"] = float(
                    round(disc_bess_total / disc_bess_mwh, 4),
                )
            # Expose the discounted components so the LCOS sensitivity
            # plot can compute the correct range; see the LCOE
            # comment above for the rationale.
            extras["lcos_disc_bess_capex_eur"] = float(disc_bess_capex)
            extras["lcos_disc_bess_opex_eur"] = float(disc_bess_opex)
            extras["lcos_disc_bess_mwh"] = float(disc_bess_mwh)

            # bess_lifetime_cycles: sum of (degraded discharge / nameplate)
            # — discharge is already scaled by bess_factor in lifetime.py.
            if bess_kwh > 0.0:
                cycles = float(
                    lifetime_yearly["bess_discharge_mwh"].sum() * 1000.0
                    / bess_kwh
                )
                extras["bess_lifetime_cycles"] = float(round(cycles, 4))

    if (
        year1_kpis is not None and capacities is not None
        and float(capacities.get("pv_kwp", 0.0) or 0.0) > 0.0
    ):
        pv_gen_y1 = float(year1_kpis.get("pv_generation_mwh", 0.0) or 0.0)
        max_y1 = float(capacities["pv_kwp"]) * 8760.0 / 1000.0
        if max_y1 > 1e-9:
            extras["pv_capacity_factor"] = float(round(pv_gen_y1 / max_y1, 4))

    # ---- BESS capacity-fade decomposition at the final year ---------------
    # Splits the year-N fade into its unchanged multiplicative calendar
    # component and the new additive cycle component.  By construction
    # calendar_fade + cycle_fade == total_fade whenever the max(0, ...)
    # floor in _bess_factor is inactive (the normal case).
    fade: dict[str, float] = {
        "bess_calendar_fade_pct_y_final": float("nan"),
        "bess_cycle_fade_pct_y_final": float("nan"),
        "bess_total_fade_pct_y_final": float("nan"),
    }
    if (df[project_year_col] >= 1).any():
        n_op_years = int(df.loc[df[project_year_col] >= 1, project_year_col].max())
        d_annual_fade = float(econ.get("bess_degradation_annual_pct", 0.0) or 0.0) / 100.0
        d_cycle_fade = float(
            econ.get("bess_degradation_pct_per_cycle", 0.0) or 0.0
        ) / 100.0
        repl_fade = int(econ.get("bess_replacement_year", 0) or 0)
        if repl_fade > 0 and n_op_years >= repl_fade:
            years_since_final = n_op_years - repl_fade
            reset_start = repl_fade
        else:
            years_since_final = n_op_years - 1
            reset_start = 1
        calendar_factor = (1.0 - d_annual_fade) ** years_since_final
        cycles_through_final_minus_1 = 0.0
        if lifetime_yearly is not None and capacities is not None:
            cap_mwh = float(capacities.get("bess_kwh", 0.0) or 0.0) / 1000.0
            if (
                cap_mwh > 1e-12
                and "bess_discharge_mwh" in lifetime_yearly.columns
                and "project_year" in lifetime_yearly.columns
            ):
                disc_by_year = lifetime_yearly.set_index(
                    "project_year",
                )["bess_discharge_mwh"]
                for yy in range(reset_start, n_op_years):
                    if yy in disc_by_year.index:
                        cycles_through_final_minus_1 += float(disc_by_year.loc[yy])
                cycles_through_final_minus_1 /= cap_mwh
        cycle_term = d_cycle_fade * cycles_through_final_minus_1
        factor_final = max(0.0, calendar_factor - cycle_term)
        fade["bess_calendar_fade_pct_y_final"] = (1.0 - calendar_factor) * 100.0
        fade["bess_cycle_fade_pct_y_final"] = cycle_term * 100.0
        fade["bess_total_fade_pct_y_final"] = (1.0 - factor_final) * 100.0

    # ---- Year-1 revenue breakdown -----------------------------------------
    breakdown: dict[str, float] = {}
    if year1_kpis is not None:
        breakdown = {
            "revenue_breakdown_y1_load_pv_eur": float(
                year1_kpis.get("profit_load_from_pv_eur", 0.0) or 0.0,
            ),
            "revenue_breakdown_y1_load_bess_eur": float(
                year1_kpis.get("profit_load_from_bess_eur", 0.0) or 0.0,
            ),
            "revenue_breakdown_y1_export_pv_eur": float(
                year1_kpis.get("profit_export_from_pv_eur", 0.0) or 0.0,
            ),
            "revenue_breakdown_y1_export_bess_eur": float(
                year1_kpis.get("profit_export_from_bess_eur", 0.0) or 0.0,
            ),
            "revenue_breakdown_y1_grid_charge_cost_eur": float(
                year1_kpis.get("expense_charge_bess_grid_eur", 0.0) or 0.0,
            ),
        }

    out: dict[str, Any] = {
        "npv_eur": float(round(npv, 2)),
        "irr_pct": float("nan") if np.isnan(irr_pct) else float(round(irr_pct, 4)),
        "roi_pct": float("nan") if np.isnan(roi_pct) else float(round(roi_pct, 4)),
        "bcr": float("nan") if np.isnan(bcr) else float(round(bcr, 4)),
        "simple_payback_years": payback_rounded,
        "discounted_payback_years": (
            float("nan") if np.isnan(discounted_payback)
            else float(round(discounted_payback, 4))
        ),
        "total_capex_eur": float(round(total_capex_eur, 2)),
        "total_devex_eur": float(round(total_devex_eur, 2)),
        "total_capex_devex_eur": float(round(total_capex_devex_eur, 2)),
        "total_opex_eur_lifecycle": float(round(total_opex_eur_lifecycle, 2)),
        "total_revenue_eur_lifecycle": float(round(total_revenue_eur_lifecycle, 2)),
        "total_aggregator_fee_eur_lifecycle": float(round(
            total_aggregator_fee_eur_lifecycle, 2,
        )),
        "lifetime_bm_revenue_total_eur": float(round(
            total_balancing_revenue_eur_lifecycle, 2,
        )),
        "lifetime_bm_revenue_eur_per_year": (
            [
                float(round(v, 2))
                for v in df.loc[after_y0_mask, "balancing_revenue_eur"].tolist()
            ]
            if "balancing_revenue_eur" in df.columns else []
        ),
        "lifetime_bm_capacity_revenue_total_eur": float(round(
            total_balancing_capacity_revenue_eur_lifecycle, 2,
        )),
        "lifetime_bm_activation_revenue_total_eur": float(round(
            total_balancing_activation_revenue_eur_lifecycle, 2,
        )),
        "capex_year": int(capex_year),
        "project_start_year": int(project_start_year),
        "project_end_year": int(project_end_year),
    }
    out.update(extras)
    out.update(fade)
    out.update(breakdown)

    # ---- LCOE / LCOS audit log --------------------------------------------
    # Single INFO line so the run_log.txt records the headline cost
    # numbers next to the Lazard 2024 reference bands.
    lcoe_bench_low = float(econ.get(
        "benchmark_lcoe_low_eur_per_mwh", BENCHMARK_LCOE_LOW_EUR_PER_MWH))
    lcoe_bench_high = float(econ.get(
        "benchmark_lcoe_high_eur_per_mwh", BENCHMARK_LCOE_HIGH_EUR_PER_MWH))
    lcos_bench_low = float(econ.get(
        "benchmark_lcos_low_eur_per_mwh", BENCHMARK_LCOS_LOW_EUR_PER_MWH))
    lcos_bench_high = float(econ.get(
        "benchmark_lcos_high_eur_per_mwh", BENCHMARK_LCOS_HIGH_EUR_PER_MWH))
    lcoe_val = extras.get("lcoe_eur_per_mwh", float("nan"))
    lcos_val = extras.get("lcos_eur_per_mwh", float("nan"))
    cycles_val = extras.get("bess_lifetime_cycles", float("nan"))

    def _fmt(v: float) -> str:
        return "n/a" if np.isnan(v) else f"{v:.1f}"

    logger.info(
        "[LCOE/LCOS audit] LCOE = %s EUR/MWh (Lazard: %.0f-%.0f) | "
        "LCOS = %s EUR/MWh (Lazard: %.0f-%.0f) | bess_lifetime_cycles = %s",
        _fmt(lcoe_val), lcoe_bench_low, lcoe_bench_high,
        _fmt(lcos_val), lcos_bench_low, lcos_bench_high,
        "n/a" if np.isnan(cycles_val) else f"{cycles_val:.0f}",
    )

    # ---- Site-wide lump-sum CAPEX/DEVEX audit -----------------------------
    site_capex = float(econ.get("site_capex_eur", 0.0) or 0.0)
    site_devex = float(econ.get("site_devex_eur", 0.0) or 0.0)
    if site_capex > 0.0 or site_devex > 0.0:
        logger.info(
            "[site lump-sum] site_capex_eur = %.2f, site_devex_eur = %.2f "
            "(folded into Year-0 CAPEX/DEVEX and the NPV/IRR/ROI/BCR/"
            "payback metrics; NOT folded into LCOE/LCOS — Lazard "
            "convention).",
            site_capex, site_devex,
        )
    return out


def _payback_year(
    years: np.ndarray,
    cumulative: np.ndarray,
    incremental: np.ndarray,
) -> float:
    """Linear-interpolate the project year at which ``cumulative`` first reaches 0.

    The returned value is the number of project years from the CAPEX
    year (project year 0).  A "Simple payback: 0.7 yr" therefore lands
    0.7 years after CAPEX commitment, NOT 0.7 years after the
    Commercial Operation Date.  The downstream plot
    (:func:`pvbess_opt.plotting.financial.plot_payback`) anchors the
    vertical line to the calendar of the CAPEX year so the on-axis
    geometry stays consistent with the scalar value.

    Returns ``float('nan')`` when no crossing exists -- including the
    cumulative-stuck-at-zero edge case (every ``incremental[i]``
    smaller than the rounding epsilon means no defined payback).
    """
    cumulative = np.asarray(cumulative, dtype=float)
    years = np.asarray(years, dtype=float)
    incremental = np.asarray(incremental, dtype=float)
    if cumulative.size == 0:
        return float("nan")

    for i in range(cumulative.size):
        if cumulative[i] >= 0:
            if i == 0:
                # A genuine cross-at-start (cumulative[0] > 0) is a
                # defined payback at years[0].  But cumulative[0] == 0
                # with no positive flow yet (incremental[0] within
                # rounding of zero) is the cumulative-stuck-at-zero
                # edge case the docstring promises NaN for.
                if cumulative[0] > 1e-12:
                    return float(years[0])
                if incremental[0] > 1e-12:
                    return float(years[0])
                return float("nan")
            cum_prev = cumulative[i - 1]
            inc = incremental[i]
            if inc > 1e-12:
                return float(years[i - 1] + (-cum_prev) / inc)
            # Degenerate crossing -- cumulative reaches 0 with a flat
            # incremental column (every year's flow within rounding
            # of zero).  There is no defined payback in that case;
            # surfacing NaN keeps the plot / KPI sheet honest.
            return float("nan")
    return float("nan")
