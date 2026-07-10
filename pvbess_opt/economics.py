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
* BESS CAPEX ~250 EUR/kWh of nameplate energy capacity (full installed
  cost: cells + PCS + BOP + EPC; EU-utility, 2024) — Lazard *Levelized
  Cost of Storage v9* (2024), band 215-315 EUR/kWh.
  ``capex_bess_eur_per_kwh`` multiplies ``bess_capacity_kwh`` directly;
  BESS DEVEX and OPEX stay per kW of the power block.
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

from .availability import availability_factor
from .constants import (
    BENCHMARK_LCOE_HIGH_EUR_PER_MWH,
    BENCHMARK_LCOE_LOW_EUR_PER_MWH,
    BENCHMARK_LCOS_HIGH_EUR_PER_MWH,
    BENCHMARK_LCOS_LOW_EUR_PER_MWH,
)
from .io import PROJECT_SHEET_DEFAULTS, read_workbook
from .kpis import require_economic_columns
from .lifetime import bess_capacity_factors, effective_bess_replacement_year

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

    # Bracket the valid IRR domain down to the same floor the Newton path
    # guards against (rate <= -0.999), so an extreme negative IRR in
    # (-0.999, -0.99) is still bracketed and the design-doc statement
    # "(-0.999, 10]" matches the implementation.
    low, high = -0.999, 10.0
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


_DEFAULT_DEBT_INTEREST_RATE_PCT = 5.0
_DEFAULT_DEBT_TENOR_YEARS = 15


def _financing_params(econ: dict[str, Any]) -> tuple[float, float, int, str]:
    """Return ``(gearing, interest_rate, tenor, repayment)`` from ``econ``;
    gearing and rate as fractions."""
    gearing = float(econ.get("gearing_pct", 0.0) or 0.0) / 100.0
    rate = float(
        econ.get("debt_interest_rate_pct", _DEFAULT_DEBT_INTEREST_RATE_PCT) or 0.0
    ) / 100.0
    tenor = int(econ.get("debt_tenor_years", _DEFAULT_DEBT_TENOR_YEARS) or 0)
    repayment = str(econ.get("debt_repayment", "annuity") or "annuity").strip().lower()
    return gearing, rate, tenor, repayment


def _amortization_schedule(
    debt: float, rate: float, tenor: int, repayment: str,
) -> list[dict[str, float]]:
    """Per-year (interest, principal, debt_service, balance) for years 1..tenor.

    ``annuity`` keeps debt service level; ``linear`` keeps principal level.
    The balance amortises to ~0 at ``tenor`` for both profiles.
    """
    rows: list[dict[str, float]] = []
    debt = float(debt)
    tenor = int(tenor)
    if debt <= 0.0 or tenor <= 0:
        return rows
    service = (
        debt * rate / (1.0 - (1.0 + rate) ** (-tenor)) if rate > 0.0
        else debt / tenor
    )
    balance = debt
    for year in range(1, tenor + 1):
        interest = balance * rate
        if repayment == "linear":
            principal = debt / tenor
            svc = principal + interest
        else:
            svc = service
            principal = svc - interest
        balance = max(0.0, balance - principal)
        rows.append({
            "year": float(year),
            "interest_eur": interest,
            "principal_eur": principal,
            "debt_service_eur": svc,
            "debt_balance_eur": balance,
        })
    return rows


def _leverage_kpis(
    net_cashflow_eur: np.ndarray, econ: dict[str, Any],
) -> tuple[float, float]:
    """Return ``(equity_irr_pct, min_dscr)``; ``(nan, nan)`` when all-equity.

    Debt funds ``gearing`` of the Year-0 investment; equity cashflow is the
    project cashflow net of debt service over the tenor.  DSCR is the
    operating cashflow over the debt service per year.
    """
    gearing, rate, tenor, repayment = _financing_params(econ)
    net_cf = np.asarray(net_cashflow_eur, dtype=float)
    if gearing <= 0.0 or net_cf.size < 2:
        return float("nan"), float("nan")
    initial_investment = -float(net_cf[0])
    if initial_investment <= 0.0:
        return float("nan"), float("nan")
    debt = gearing * initial_investment
    schedule = _amortization_schedule(debt, rate, tenor, repayment)
    if not schedule:
        return float("nan"), float("nan")
    equity_cf = net_cf.copy()
    equity_cf[0] = net_cf[0] + debt
    dscrs: list[float] = []
    for row in schedule:
        y = int(row["year"])
        svc = row["debt_service_eur"]
        if y < equity_cf.size:
            equity_cf[y] -= svc
            if svc > 0.0:
                dscrs.append(float(net_cf[y]) / svc)
    eq_irr = calculate_irr(equity_cf)
    equity_irr_pct = float("nan") if np.isnan(eq_irr) else eq_irr * 100.0
    min_dscr = float(min(dscrs)) if dscrs else float("nan")
    return equity_irr_pct, min_dscr


def build_debt_schedule(
    yearly_cf: pd.DataFrame, econ: dict[str, Any],
) -> pd.DataFrame | None:
    """Per-year debt schedule + equity cashflow + DSCR; None when all-equity."""
    gearing, rate, tenor, repayment = _financing_params(econ)
    if gearing <= 0.0 or "net_cashflow_eur" not in yearly_cf.columns:
        return None
    net_cf = yearly_cf["net_cashflow_eur"].to_numpy(dtype=float)
    if net_cf.size < 2 or net_cf[0] >= 0.0:
        return None
    schedule = _amortization_schedule(
        gearing * (-float(net_cf[0])), rate, tenor, repayment,
    )
    if not schedule:
        return None
    rows: list[dict[str, float]] = []
    for row in schedule:
        y = int(row["year"])
        op_cf = float(net_cf[y]) if y < net_cf.size else float("nan")
        svc = row["debt_service_eur"]
        rows.append({
            "year": float(y),
            "interest_eur": round(row["interest_eur"], 2),
            "principal_eur": round(row["principal_eur"], 2),
            "debt_service_eur": round(svc, 2),
            "debt_balance_eur": round(row["debt_balance_eur"], 2),
            "operating_cf_eur": round(op_cf, 2),
            "equity_cf_eur": round(op_cf - svc, 2),
            "dscr": round(op_cf / svc, 4) if svc > 0.0 else float("nan"),
        })
    return pd.DataFrame(rows)


def read_economic_params(xlsx_path: str | Path) -> dict[str, Any]:
    """Read the project / pv / bess / economics / simulation / balancing
    / ppa sheets.

    Returns a single flat dict combining every key from the seven
    parameter sheets — the financial helpers downstream expect a flat
    mapping (e.g. ``econ['discount_rate_pct']``,
    ``econ['capex_pv_eur_per_kw']``, ``econ['ppa_term_years']``).
    Key names are unique across sheets by construction
    (:data:`pvbess_opt.io._KEY_TO_SHEET`), so the flat merge is lossless.
    """
    typed = read_workbook(xlsx_path)
    merged: dict[str, Any] = {}
    for section in (
        "project", "pv", "bess", "economics", "simulation", "balancing",
        "ppa",
    ):
        merged.update(typed[section])
    # The per-year trajectory block (Eq. E24) rides along under a
    # reserved non-kv key: kv-sheet keys are lowercase snake_case
    # scalars validated by the loader, so the flat merge stays lossless.
    merged["trajectories"] = typed.get("trajectories")
    return merged


# ---------------------------------------------------------------------------
# Per-stream escalation (Eq. E24)
# ---------------------------------------------------------------------------


def _escalation_series(
    stream: str,
    inflation_frac: float,
    n_years: int,
    trajectories: dict[str, dict[str, Any]] | None,
) -> list[float]:
    """Per-year escalation factors ``g_y`` for ``stream`` (Eq. E24).

    Index 0 is operating year 1.  Without a trajectory the series is the
    flat scalar index ``(1 + i)^(y-1)``; a ``replace``-mode trajectory
    substitutes its multipliers ``m_y``; ``overlay`` multiplies them on
    top of the scalar index.  Both the yearly cashflow and the LCOE /
    LCOS OPEX numerators MUST source their escalation from this one
    helper so the metric and cashflow OPEX can never diverge (E24a).

    The loader (``io.validate_workbook_params``) enforces full
    1..project_lifecycle_years coverage and the ``m_1 == 1`` anchor; for
    hand-built ``econ`` dicts that bypass it, a short vector holds its
    LAST multiplier flat for the remaining years (predictable, never
    silent-zero).
    """
    scalar = [(1.0 + inflation_frac) ** y for y in range(n_years)]
    spec = (trajectories or {}).get(stream)
    if not spec:
        return scalar
    values = [float(v) for v in spec["values"]][:n_years]
    if len(values) < n_years:
        values = values + [values[-1]] * (n_years - len(values))
    if str(spec.get("mode", "replace")) == "replace":
        return values
    return [s * m for s, m in zip(scalar, values, strict=True)]


def _contract_phase(
    y: int, year_from: int, year_to: int, n_years: int,
) -> bool:
    """Contract phase-window indicator chi_y (Eq. E25).

    True when operating year ``y`` lies in ``[year_from, year_to]``
    inclusive; ``year_to = 0`` means end-of-life (``n_years``),
    generalising the ``y <= ppa_term`` in-term gating the PPA stream
    already uses.  Year 0 (construction) is never inside any phase.
    Callers validate ``year_from >= 1`` and effective
    ``year_to >= year_from`` at load; this helper is pure.
    """
    if y < 1:
        return False
    effective_to = n_years if int(year_to) == 0 else int(year_to)
    return int(year_from) <= int(y) <= effective_to


def _opex_escalation_series(
    leg: str,
    inflation_frac: float,
    n_years: int,
    trajectories: dict[str, dict[str, Any]] | None,
) -> list[float]:
    """OPEX escalation for one asset leg (Eq. E24a).

    ``leg`` is ``opex_pv`` or ``opex_bess``.  When either per-asset
    split stream is declared, each leg escalates on its own series (an
    absent split leg falls back to the flat scalar); otherwise both legs
    share the ``opex`` stream.  The yearly cashflow's OPEX row and the
    LCOE / LCOS discounted-OPEX numerators all route through here — one
    source, no metric drift.
    """
    if trajectories and (
        "opex_pv" in trajectories or "opex_bess" in trajectories
    ):
        return _escalation_series(leg, inflation_frac, n_years, trajectories)
    return _escalation_series("opex", inflation_frac, n_years, trajectories)


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

    ``econ`` contract: a FLAT mapping as produced by
    :func:`read_economic_params`, which merges every workbook sheet into
    one dict — the PPA knobs are read as ``econ['ppa_enabled']``,
    ``econ['ppa_settlement']``, ``econ['ppa_term_years']`` and
    ``econ['ppa_inflation_pct']``, NOT as a nested ``econ['ppa']`` block
    (that nested shape belongs to the dispatch-side
    :func:`pvbess_opt.ppa.resolve_ppa_config` consumers).
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
    bess_kwh = float(capacities["bess_kwh"])

    capex_pv_y0 = -float(econ["capex_pv_eur_per_kw"]) * pv_kwp
    # BESS CAPEX is an energy-basis cost: EUR/kWh x nameplate kWh.
    # DEVEX and OPEX stay on the power basis (development, permitting
    # and fixed O&M scale with the power block).
    capex_bess_y0 = -float(econ["capex_bess_eur_per_kwh"]) * bess_kwh
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
    # Optional, separate route-to-market (BSP / balancing-aggregator) fee on
    # GROSS balancing revenue.  Default 0.0 ⇒ fee-free balancing, bit-identical
    # to a workbook without the key.  Clamped to [0, 1] exactly like the energy
    # aggregator fee above (a non-negative deduction; never a rebate).
    balancing_aggregator_fee_pct = float(
        econ.get("balancing_aggregator_fee_pct_revenue", 0.0) or 0.0
    )
    balancing_aggregator_fee_frac = max(
        0.0, min(1.0, balancing_aggregator_fee_pct / 100.0)
    )
    # Structural market-access fees (Eq. E13c / E13d in
    # docs/economics_design.md), both default-off:
    # * route-to-market fee — EUR/MWh of grid-EXPORTED energy (the FoSE /
    #   Direktvermarktung representation charge).  Flat over the project
    #   life; the exported MWh themselves fade on the per-origin
    #   degradation curves.  Clamped non-negative (a fee, never a rebate).
    # * optimizer revenue share — a percentage of the POSITIVE annual BESS
    #   wholesale trading margin (export minus grid charging), the
    #   merchant / floor+share structure of BESS optimizers.
    # Charging-side grid fee (Eqs. E26/E27): the Year-1 wedge actually
    # paid comes from the KPI — already availability-derated and zeroed
    # under the exemption switch — and the charged grid-to-BESS volume
    # fades on the BESS capacity curve (the flat-rate convention of the
    # route-to-market fee E13c: regulated charges are quoted per MWh,
    # not indexed).
    grid_charging_fee_1 = float(
        year1_kpis.get("expense_grid_charging_fee_eur", 0.0) or 0.0
    )
    # Imbalance settlement (Eq. E28): the Year-1 base is the
    # availability-derated Monte Carlo MEAN (unbiased expected-value
    # estimate; the percentiles carry the distribution).  The deviation
    # volume is PV-forecast-error-driven, so it fades on the PV curve,
    # and the settlement prices ride the DAM series.
    imbalance_cost_1 = float(
        year1_kpis.get("imbalance_cost_year1_eur", 0.0) or 0.0
    )
    route_to_market_fee_rate = max(0.0, float(
        econ.get("route_to_market_fee_eur_per_mwh", 0.0) or 0.0
    ))
    optimizer_share_frac = max(0.0, min(1.0, float(
        econ.get("optimizer_revenue_share_pct", 0.0) or 0.0
    ) / 100.0))
    # Optimizer floor + share-above-floor (Eqs. E30/E30a): with the
    # floor enabled the share applies to the margin ABOVE the
    # guaranteed floor and shortfalls are topped up; disabled (default)
    # the plain E13d share applies unchanged.  A shared term window
    # (default whole life) gates BOTH share and floor.  The floor is
    # gated by the explicit enable switch — a floor VALUE of zero with
    # the switch on still guarantees a non-negative margin — so a zero
    # floor value alone never silently converts losses into top-ups.
    optimizer_floor_enabled = bool(
        econ.get("optimizer_floor_enabled", False)
    )
    optimizer_floor_rate = max(0.0, float(
        econ.get("optimizer_floor_eur_per_kw_year", 0.0) or 0.0
    ))
    _raw_opt_from = econ.get("optimizer_term_year_from", 1)
    opt_term_year_from = int(1 if _raw_opt_from is None else _raw_opt_from)
    _raw_opt_to = econ.get("optimizer_term_year_to", 0)
    opt_term_year_to = int(0 if _raw_opt_to is None else _raw_opt_to)
    optimizer_margin_basis = str(
        econ.get("optimizer_margin_basis", "dam") or "dam"
    ).strip().lower()
    # Year-1 exported MWh by origin (availability-derated upstream, like the
    # EUR bases).  Older KPI dicts without the split charge no RTM fee.
    pv_export_mwh_1 = float(year1_kpis.get("pv_export_mwh", 0.0) or 0.0)
    bess_export_mwh_1 = float(year1_kpis.get("bess_export_mwh", 0.0) or 0.0)

    # BESS tolling agreement (Eqs. E29/E29a): a fixed EUR/MW/yr payment
    # for dispatch rights over a phase window (Eq. E25).  The toll is a
    # NEW stream (not derived from the derated Year-1 KPIs), so the
    # availability factor applies here — once, per the E8 single-derate
    # principle — and there is deliberately no bess_factor fade (the
    # payment is on the contracted power block, not delivered energy).
    toll_rate = max(0.0, float(
        econ.get("bess_toll_eur_per_mw_year", 0.0) or 0.0
    ))
    _raw_toll_from = econ.get("bess_toll_year_from", 1)
    toll_year_from = int(1 if _raw_toll_from is None else _raw_toll_from)
    _raw_toll_to = econ.get("bess_toll_year_to", 0)
    toll_year_to = int(0 if _raw_toll_to is None else _raw_toll_to)
    toll_treatment = str(
        econ.get("bess_toll_merchant_treatment", "zeroed") or "zeroed"
    ).strip().lower()
    toll_infl = float(
        econ.get("bess_toll_indexation_pct", 0.0) or 0.0
    ) / 100.0
    # One availability factor for every contracted stream that is NOT
    # derived from the already-derated Year-1 KPIs (toll, optimizer
    # floor, ...) — applied once per the E8 single-derate principle.
    contract_avail = availability_factor(
        float(econ.get("unavailability_pct", 0.0) or 0.0)
    )
    # Guaranteed floor level (Eq. E30): EUR/kW/yr on the power block,
    # availability-scaled, flat nominal (no capacity-fade scaling and
    # no indexation — the floor is a contractual level).
    optimizer_floor_level = optimizer_floor_rate * bess_kw * contract_avail
    # State support with two-way clawback (Eqs. E31/E31a): a fixed
    # EUR/MW/yr support (availability-scaled, no fade) netted two-way
    # against realised market revenue relative to an indexed threshold.
    ss_rate = max(0.0, float(
        econ.get("state_support_eur_per_mw_year", 0.0) or 0.0
    ))
    _raw_ss_from = econ.get("state_support_year_from", 1)
    ss_year_from = int(1 if _raw_ss_from is None else _raw_ss_from)
    _raw_ss_to = econ.get("state_support_year_to", 0)
    ss_year_to = int(0 if _raw_ss_to is None else _raw_ss_to)
    ss_threshold = max(0.0, float(econ.get(
        "state_support_clawback_threshold_eur_per_mw_year", 0.0,
    ) or 0.0))
    ss_share_frac = max(0.0, min(1.0, float(econ.get(
        "state_support_clawback_share_pct", 0.0,
    ) or 0.0) / 100.0))
    ss_infl = float(
        econ.get("state_support_indexation_pct", 0.0) or 0.0
    ) / 100.0
    _ss_repayment_years: list[int] = []
    # Capacity-market payment (Eq. E32): paid on the DERATED power
    # block over a contract window, availability-scaled, no fade; the
    # revenue counts toward the E31a netting base.
    cm_rate = max(0.0, float(
        econ.get("capacity_market_eur_per_mw_year", 0.0) or 0.0
    ))
    cm_derating_frac = max(0.0, min(1.0, float(
        econ.get("capacity_market_derating_pct", 100.0) or 0.0
    ) / 100.0))
    _raw_cm_from = econ.get("capacity_market_year_from", 1)
    cm_year_from = int(1 if _raw_cm_from is None else _raw_cm_from)
    _raw_cm_to = econ.get("capacity_market_year_to", 0)
    cm_year_to = int(0 if _raw_cm_to is None else _raw_cm_to)
    cm_infl = float(
        econ.get("capacity_market_indexation_pct", 0.0) or 0.0
    ) / 100.0

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
        # profit_total_eur it should equal retail + DAM (+ the PPA
        # contract leg, which compute_kpis folds into the total) within
        # rounding.
        if "profit_total_eur" in year1_kpis:
            profit_total = float(year1_kpis["profit_total_eur"] or 0.0)
            # profit_total also nets the charging-side grid fee
            # (Eq. E26), which is NOT part of the revenue streams.
            split_total = revenue_1_gross + float(
                year1_kpis.get("revenue_pv_ppa_eur", 0.0) or 0.0
            ) - float(
                year1_kpis.get("expense_grid_charging_fee_eur", 0.0) or 0.0
            )
            if abs(profit_total - split_total) > max(
                1.0, abs(profit_total) * 1e-6,
            ):
                logger.warning(
                    "Year-1 revenue split drift: profit_total_eur=%.2f vs "
                    "retail+dam+ppa=%.2f. Using component sum.",
                    profit_total, split_total,
                )
    else:
        # When year1_kpis carries only profit_total_eur with no
        # per-stream breakdown, index the whole revenue as retail
        # (CPI-linked); this coincides with the per-stream result
        # whenever retail_inflation_pct == dam_inflation_pct.  The PPA
        # contract leg (folded into profit_total_eur by compute_kpis)
        # is carved out: it flows through its own fee-free
        # ``ppa_revenue_eur`` column, so leaving it in the gross here
        # would double-count it AND wrongly charge it the aggregator
        # fee.
        # The charging-side grid fee (Eq. E26) is likewise carved out:
        # profit_total_eur already nets it, but it flows through its own
        # grid_charging_fee_eur column below — leaving it netted here
        # would double-count the deduction.
        revenue_1_gross = float(
            year1_kpis.get("profit_total_eur", 0.0) or 0.0
        ) - float(year1_kpis.get("revenue_pv_ppa_eur", 0.0) or 0.0) + float(
            year1_kpis.get("expense_grid_charging_fee_eur", 0.0) or 0.0
        )
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

    # A tolled grid-scale battery has no retail leg, so the self-
    # consumption BESS stream (profit_load_from_bess_eur) is
    # deliberately NOT zeroed by the toll (Eq. E29a) — flag the
    # combination instead of silently mis-modelling it.
    if toll_rate > 0.0 and abs(rev1_retail_bess) > 1e-9:
        logger.warning(
            "A BESS toll is active while the battery also serves retail "
            "load (profit_load_from_bess_eur = %.2f EUR): the retail "
            "stream is NOT zeroed in toll years (Eq. E29a). A tolled "
            "grid-scale battery normally has no retail leg — check the "
            "configuration.",
            rev1_retail_bess,
        )

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

    # Per-stream escalation series (Eq. E24): flat scalar indices unless
    # a trajectory reshapes the stream.  The CfD DAM leg, the post-term
    # PPA reversion and the optimizer-fee base (E13d) all ride the SAME
    # DAM series as the merchant DAM revenue; the PPA strike escalates
    # contractually (ppa_inflation_pct, no trajectory by design).
    trajectories = econ.get("trajectories") or None
    g_retail = _escalation_series(
        "revenue_retail", retail_infl, n_years, trajectories,
    )
    g_dam = _escalation_series("revenue_dam", dam_infl, n_years, trajectories)
    g_bm_cap = _escalation_series(
        "balancing_capacity", bm_infl, n_years, trajectories,
    )
    g_bm_act = _escalation_series(
        "balancing_activation", bm_infl, n_years, trajectories,
    )
    # Per-asset OPEX decomposition (Eq. E24a) — shared with the LCOE /
    # LCOS numerators through _opex_escalation_series.  The split branch
    # is entered ONLY when a per-asset stream is declared: the shared
    # path keeps the historical -(pv+bess) * g grouping so a run without
    # split trajectories stays bit-identical (float multiplication does
    # not distribute exactly).
    _split_opex = bool(trajectories) and bool(
        {"opex_pv", "opex_bess"} & set(trajectories or {}),
    )
    _g_opex_pv = _opex_escalation_series(
        "opex_pv", opex_infl, n_years, trajectories,
    )
    _g_opex_bess = _opex_escalation_series(
        "opex_bess", opex_infl, n_years, trajectories,
    )
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

    # PPA stream (docs/ppa_design.md).  Year-1 bases come from the KPI
    # dict (already availability-derated): the contract leg
    # ``revenue_pv_ppa_eur`` and the covered volume's counterfactual DAM
    # value.  The strike leg escalates at the contract's own
    # ``ppa_inflation_pct``; the CfD's DAM leg at ``dam_inflation_pct``;
    # and after ``ppa_term_years`` the stream ends — under physical
    # settlement the covered volume's DAM value then rejoins the DAM
    # revenue stream (market revenue: the aggregator fee applies to it).
    ppa_enabled = bool(econ.get("ppa_enabled", False))
    ppa_settlement = (
        str(econ.get("ppa_settlement", "physical") or "physical")
        .strip().lower()
    )
    ppa_term = int(econ.get("ppa_term_years", 0) or 0)
    ppa_infl = float(econ.get("ppa_inflation_pct", 0.0) or 0.0) / 100.0
    # Covered share of the PV export — the route-to-market fee exempts it
    # while a physical (sleeved) contract is in term (Eq. E13c).
    ppa_share_frac = max(0.0, min(1.0, float(
        econ.get("ppa_volume_share_pct", 0.0) or 0.0
    ) / 100.0))
    # Negative-price suspension clause (Eqs. P6/P7): with the clause on,
    # the fee-exempt covered export is the EXACT per-step KPI (suspended
    # steps settle at spot and are NOT exempt), not the share-based
    # approximation.  Without the clause the share-based algebra below
    # is exact and stays bit-identical.
    ppa_negative_rule = str(
        econ.get("ppa_negative_price_rule", "none") or "none"
    ).strip().lower()
    ppa_exempt_export_mwh_1 = year1_kpis.get("ppa_fee_exempt_export_mwh")
    rev1_ppa = float(year1_kpis.get("revenue_pv_ppa_eur", 0.0) or 0.0)
    ppa_covered_dam_1 = float(
        year1_kpis.get("ppa_covered_dam_value_eur", 0.0) or 0.0
    )
    if ppa_settlement == "cfd":
        # The CfD leg is (strike - DAM) x covered, so the strike-only
        # leg reconstructs as contract leg + covered DAM value.
        ppa_strike_value_1 = rev1_ppa + ppa_covered_dam_1
    else:
        ppa_strike_value_1 = rev1_ppa

    bess_repl_year = effective_bess_replacement_year(econ)
    bess_repl_cost_pct = float(econ.get("bess_replacement_cost_pct", 0.0) or 0.0)

    # BESS capacity factors from the shared reset-at-replacement
    # cycle-fade accumulator (single source of truth in lifetime.py).
    # Cycle convention matches compute_financial_kpis'
    # bess_lifetime_cycles (discharge MWh / capacity MWh).
    capacity_mwh = float(capacities.get("bess_kwh", 0.0) or 0.0) / 1000.0
    year1_discharge_mwh = float(
        year1_kpis.get("bess_total_discharge_mwh", 0.0) or 0.0
    )
    bess_factors = bess_capacity_factors(
        n_years,
        d_bess_annual=bess_deg_annual,
        d_bess_per_cycle=bess_deg_per_cycle,
        year1_discharge_mwh=year1_discharge_mwh,
        capacity_mwh=capacity_mwh,
        replacement_year=bess_repl_year,
    )

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
            balancing_aggregator_fee_y = 0.0
            route_to_market_fee_y = 0.0
            optimizer_fee_y = 0.0
            optimizer_floor_topup_y = 0.0
            grid_charging_fee_y = 0.0
            imbalance_cost_y = 0.0
            ppa_y = 0.0
            bess_market_rev_y = 0.0
            toll_revenue_y = 0.0
            state_support_y = 0.0
            state_support_clawback_y = 0.0
            capacity_market_rev_y = 0.0
        else:
            if y == 1:
                pv_factor = 1.0
            else:
                pv_factor = (1.0 - pv_deg_y1) * (1.0 - pv_deg_annual) ** (y - 2)
            bess_factor = bess_factors[y - 1]
            # Toll revenue (Eq. E29): availability-conditioned payment
            # on the contracted power block, indexed contractually,
            # gated by the phase window (Eq. E25).  No bess_factor fade.
            toll_in_phase = toll_rate > 0.0 and _contract_phase(
                y, toll_year_from, toll_year_to, n_years,
            )
            if toll_in_phase:
                toll_revenue_y = (
                    toll_rate * (bess_kw / 1000.0) * contract_avail
                    * (1.0 + toll_infl) ** (y - 1)
                )
            else:
                toll_revenue_y = 0.0
            # Merchant zeroing (Eq. E29a): in toll years under 'zeroed'
            # treatment the toller holds dispatch rights, so every
            # BESS-origin merchant base is substituted with zero FOR
            # THE YEAR — the Year-1 bases themselves are never mutated,
            # so the Year-1 revenue-split reconciliation stays intact
            # and non-toll years reuse the exact original floats
            # (bit-identity when the toll is off).  The charging-side
            # grid fee follows the grid-charging cost it accompanies
            # (both are dispatch costs the toller bears); PV-origin
            # streams, the retail/self-consumption stream (warned
            # above) and the PV-forecast-error-driven imbalance cost
            # are untouched.
            _toll_zeroed = toll_in_phase and toll_treatment == "zeroed"
            if _toll_zeroed:
                _rev1_dam_bess_y = 0.0
                _bm_cap_y1_y = 0.0
                _bm_act_y1_y = 0.0
                _bess_export_mwh_1_y = 0.0
                _grid_charging_fee_1_y = 0.0
            else:
                _rev1_dam_bess_y = rev1_dam_bess
                _bm_cap_y1_y = bm_cap_y1
                _bm_act_y1_y = bm_act_y1
                _bess_export_mwh_1_y = bess_export_mwh_1
                _grid_charging_fee_1_y = grid_charging_fee_1
            # Degrade PV-origin revenue on pv_factor and BESS-origin
            # revenue on bess_factor, mirroring build_lifetime_dispatch's
            # per-year factor loop so the
            # two sheets in 03_results.xlsx agree.  Inflation is applied
            # per stream (retail vs DAM index).
            revenue_retail_y = (
                rev1_retail_pv * pv_factor + rev1_retail_bess * bess_factor
            ) * g_retail[y - 1]
            revenue_dam_y = (
                rev1_dam_pv * pv_factor + _rev1_dam_bess_y * bess_factor
            ) * g_dam[y - 1]
            # PPA stream: in-term contract leg, or the post-term
            # physical reversion of the covered volume to the DAM
            # stream (where the fee below applies to it).
            if ppa_enabled and y <= ppa_term:
                strike_leg = (
                    ppa_strike_value_1 * pv_factor
                    * (1.0 + ppa_infl) ** (y - 1)
                )
                if ppa_settlement == "cfd":
                    ppa_y = strike_leg - (
                        ppa_covered_dam_1 * pv_factor * g_dam[y - 1]
                    )
                else:
                    ppa_y = strike_leg
            else:
                ppa_y = 0.0
                if ppa_enabled and ppa_settlement != "cfd":
                    revenue_dam_y += (
                        ppa_covered_dam_1 * pv_factor * g_dam[y - 1]
                    )
            revenue_gross_y = revenue_retail_y + revenue_dam_y
            # The aggregator fee is by spec a non-negative deduction
            # (BSPs charge a positive fraction of gross revenue, never
            # rebate negative-gross dispatches).  Clamping the gross at
            # zero stops the fee from flipping to a revenue when
            # revenue_gross_y < 0 (a regime that can occur in pure-
            # arbitrage projects with sustained negative DAM hours).
            aggregator_fee_y = -max(revenue_gross_y, 0.0) * aggregator_fee_frac
            if _split_opex:
                opex_y = -(
                    opex_pv_1 * _g_opex_pv[y - 1]
                    + opex_bess_1 * _g_opex_bess[y - 1]
                )
            else:
                opex_y = opex_1 * _g_opex_pv[y - 1]
            if bess_repl_year > 0 and y == bess_repl_year:
                capex_y = capex_bess_y0 * (bess_repl_cost_pct / 100.0)
            else:
                capex_y = 0.0
            devex_y = 0.0
            balancing_capacity_y = (
                _bm_cap_y1_y * bess_factor * g_bm_cap[y - 1]
            )
            balancing_activation_y = (
                _bm_act_y1_y * bess_factor * g_bm_act[y - 1]
            )
            # Optional route-to-market (BSP) fee on GROSS balancing revenue.
            # A non-negative deduction, clamped at a zero-gross floor exactly
            # like the energy aggregator fee.  The gross is already escalated
            # (bess_factor x (1+bm_infl)^(y-1)), so the fee escalates with it.
            balancing_aggregator_fee_y = -max(
                balancing_capacity_y + balancing_activation_y, 0.0,
            ) * balancing_aggregator_fee_frac

            # Route-to-market fee (Eq. E13c): EUR/MWh on the year's exported
            # energy, each origin fading on its own curve.  The fee level is
            # flat (representation charges are quoted per MWh, not indexed);
            # the charged MWh shrink with degradation.  While a PHYSICAL
            # (sleeved) PPA is in term its covered PV-export share is routed
            # by the offtaker, not the aggregator, so that share is exempt;
            # a CfD sells the full volume at DAM through the aggregator and
            # is not exempt.  Post-term the full export pays the fee.
            _exemption_applies = (
                ppa_enabled and ppa_settlement != "cfd" and y <= ppa_term
            )
            if (
                _exemption_applies
                and ppa_negative_rule == "suspend"
                and ppa_exempt_export_mwh_1 is not None
            ):
                # Exact per-step exemption base under the suspension
                # clause (Eqs. P6/P7): suspended-step export pays the
                # fee, so the exempt volume is below share x export.
                route_to_market_fee_y = -route_to_market_fee_rate * (
                    max(
                        (pv_export_mwh_1 - float(ppa_exempt_export_mwh_1))
                        * pv_factor,
                        0.0,
                    )
                    + _bess_export_mwh_1_y * bess_factor
                )
            else:
                _ppa_exempt_share = (
                    ppa_share_frac if _exemption_applies else 0.0
                )
                route_to_market_fee_y = -route_to_market_fee_rate * (
                    pv_export_mwh_1 * pv_factor * (1.0 - _ppa_exempt_share)
                    + _bess_export_mwh_1_y * bess_factor
                )
            # Optimizer revenue share (Eq. E13d) / floor+share
            # (Eqs. E30/E30a), gated by the shared term window (default
            # whole life, preserving the historical all-years share).
            # Plain share: a percentage of the POSITIVE BESS wholesale
            # trading margin (export minus grid charging, already
            # netted in rev1_dam_bess), clamped at zero — an optimizer
            # never invoices a share of a trading loss.  Floor+share:
            # the share applies to the margin ABOVE the guaranteed
            # floor, and any shortfall below the floor is topped up by
            # the optimizer (a separate >= 0 column so the fee column
            # keeps its <= 0 sign contract).  The trailing +0.0
            # normalises the -0.0 produced when a clamp binds.
            _opt_in_term = _contract_phase(
                y, opt_term_year_from, opt_term_year_to, n_years,
            )
            if not _opt_in_term:
                optimizer_fee_y = 0.0
                optimizer_floor_topup_y = 0.0
            elif optimizer_floor_enabled:
                # Margin basis (Eq. E30a): the E13d DAM margin, or the
                # full E25a base when the optimizer also manages the
                # ancillary revenue (share after the BSP fee — fees
                # never compound).
                if optimizer_margin_basis == "dam_plus_balancing":
                    _opt_margin = (
                        _rev1_dam_bess_y * bess_factor * g_dam[y - 1]
                        + balancing_capacity_y + balancing_activation_y
                        + balancing_aggregator_fee_y
                    )
                else:
                    _opt_margin = (
                        _rev1_dam_bess_y * bess_factor * g_dam[y - 1]
                    )
                optimizer_fee_y = -optimizer_share_frac * max(
                    _opt_margin - optimizer_floor_level, 0.0,
                ) + 0.0
                optimizer_floor_topup_y = max(
                    optimizer_floor_level - _opt_margin, 0.0,
                ) + 0.0
            else:
                optimizer_fee_y = -optimizer_share_frac * max(
                    _rev1_dam_bess_y * bess_factor * g_dam[y - 1],
                    0.0,
                ) + 0.0
                optimizer_floor_topup_y = 0.0
            # Charging-side grid fee (Eq. E27): flat regulated rate on a
            # charged volume that fades on the BESS capacity curve.
            grid_charging_fee_y = -_grid_charging_fee_1_y * bess_factor
            # Imbalance settlement (Eq. E28): PV-error-driven volume on
            # the PV curve, prices on the DAM escalation series.
            imbalance_cost_y = (
                -imbalance_cost_1 * pv_factor * g_dam[y - 1]
            )
            # BESS market-revenue base (Eq. E25a): the battery's
            # wholesale trading margin (the E13d base, UNclamped) plus
            # balancing revenue net of the BSP fee.  Informational only
            # — the single netting base the contracted structures
            # (tolling / floor+share / state-support clawback) read; it
            # is NOT summed into net_cashflow_eur.  Availability-derated
            # by construction (every input already carries A per E8).
            bess_market_rev_y = (
                _rev1_dam_bess_y * bess_factor * g_dam[y - 1]
                + balancing_capacity_y + balancing_activation_y
                + balancing_aggregator_fee_y
            )
            # Capacity-market payment (Eq. E32) — computed BEFORE the
            # state-support netting because the capacity revenue counts
            # as realised market revenue in its base (Eq. E31a).
            _cm_in_phase = cm_rate > 0.0 and _contract_phase(
                y, cm_year_from, cm_year_to, n_years,
            )
            if _cm_in_phase:
                capacity_market_rev_y = (
                    cm_rate * (bess_kw / 1000.0) * cm_derating_frac
                    * contract_avail * (1.0 + cm_infl) ** (y - 1)
                )
            else:
                capacity_market_rev_y = 0.0
            # State support (Eq. E31) and the two-way netting
            # (Eq. E31a): the gross support is availability-conditioned
            # on the power block (no fade), and the netting settles the
            # realised market revenue (the E25a base plus the
            # capacity-market revenue) against the indexed threshold —
            # clawback above it, compensation below it, both at the
            # same share.  No floor is applied: a year whose netted
            # support turns negative is a net repayment (collected and
            # flagged once after the loop).
            _ss_in_phase = ss_rate > 0.0 and _contract_phase(
                y, ss_year_from, ss_year_to, n_years,
            )
            if _ss_in_phase:
                _ss_g = (1.0 + ss_infl) ** (y - 1)
                state_support_y = (
                    ss_rate * (bess_kw / 1000.0) * contract_avail * _ss_g
                )
                if ss_share_frac > 0.0:
                    _ss_theta_y = (
                        ss_threshold * (bess_kw / 1000.0) * _ss_g
                    )
                    state_support_clawback_y = -ss_share_frac * (
                        bess_market_rev_y + capacity_market_rev_y
                        - _ss_theta_y
                    ) + 0.0
                else:
                    state_support_clawback_y = 0.0
                if state_support_y + state_support_clawback_y < 0.0:
                    _ss_repayment_years.append(int(y))
            else:
                state_support_y = 0.0
                state_support_clawback_y = 0.0

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
        # Balancing revenue carries NO energy-aggregator fee (that fee covers
        # the DAM/retail streams only).  It MAY carry an optional, separate
        # route-to-market (BSP / balancing-aggregator) fee when participation
        # is routed through an aggregator that keeps a share — off by default
        # (balancing_aggregator_fee_frac == 0) so the column is all-zero and
        # the net cashflow is bit-identical to today.  The PPA stream carries
        # neither fee (a bilateral offtake settles directly with the
        # offtaker).  ``balancing_revenue_eur`` stays GROSS; the fee is its
        # own negative column, mirroring ``aggregator_fee_eur``.
        net_cf = (
            revenue_net_y + balancing_revenue_y + balancing_aggregator_fee_y
            + route_to_market_fee_y + optimizer_fee_y
            + optimizer_floor_topup_y
            + grid_charging_fee_y + imbalance_cost_y
            + toll_revenue_y
            + state_support_y + state_support_clawback_y
            + capacity_market_rev_y
            + ppa_y + opex_y + capex_y + devex_y
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
                "route_to_market_fee_eur": float(route_to_market_fee_y),
                "optimizer_fee_eur": float(optimizer_fee_y),
                "optimizer_floor_topup_eur": float(optimizer_floor_topup_y),
                "grid_charging_fee_eur": float(grid_charging_fee_y),
                "imbalance_cost_eur": float(imbalance_cost_y),
                "balancing_capacity_revenue_eur": float(balancing_capacity_y),
                "balancing_activation_revenue_eur": float(balancing_activation_y),
                "balancing_revenue_eur": float(balancing_revenue_y),
                "balancing_aggregator_fee_eur": float(balancing_aggregator_fee_y),
                "bess_market_revenue_eur": float(bess_market_rev_y),
                "toll_revenue_eur": float(toll_revenue_y),
                "state_support_eur": float(state_support_y),
                "state_support_clawback_eur": float(
                    state_support_clawback_y
                ),
                "capacity_market_revenue_eur": float(
                    capacity_market_rev_y
                ),
                "ppa_revenue_eur": float(ppa_y),
                "opex_eur": float(opex_y),
                "capex_eur": float(capex_y),
                "devex_eur": float(devex_y),
                "net_cashflow_eur": float(net_cf),
                "discount_factor": float(discount_factor),
                "discounted_cf_eur": float(net_cf * discount_factor),
            }
        )

    if _ss_repayment_years:
        logger.warning(
            "[state support] The two-way netting turns the combined "
            "support negative (a net repayment) in project year(s) %s "
            "— realised market revenue exceeded the threshold by more "
            "than the support level; no floor is applied by design "
            "(Eq. E31a).",
            _ss_repayment_years,
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

    The informational ``bess_market_revenue_eur`` yearly column
    (Eq. E25a) deliberately has NO monthly counterpart: it is a netting
    base composed of streams that already reconcile individually, not a
    cash flow of its own, so the monthly/yearly reconciliation contract
    covers it implicitly through its components.

    Output frame columns
    --------------------

    * ``project_year`` / ``calendar_year`` / ``period`` / ``period_type``
      — period descriptors. ``period`` is the month (1..12) or quarter
      (1..4) and ``period_type`` is ``"month"`` or ``"quarter"``.
    * ``pv_production_mwh`` — Year-1 monthly PV energy scaled by the
      year's PV degradation factor and derated by the availability
      factor, so the per-year sums reconcile with
      ``kpis_year1['pv_generation_mwh']`` and the
      ``lifetime_dispatch_yearly`` sheet (both derated upstream).
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
    * ``balancing_aggregator_fee_eur`` — per-month allocation of
      ``yearly_cf['balancing_aggregator_fee_eur']`` (the optional BSP /
      route-to-market fee), weighted by the same reservation profile as
      ``balancing_revenue_eur``.  Because ``balancing_revenue_eur`` is
      GROSS, this fee (signed negative) is part of ``net_cashflow_eur``
      here; it is identically zero when
      ``balancing_aggregator_fee_pct_revenue`` is 0.
    * ``aggregator_fee_eur`` — per-month allocation of
      ``yearly_cf['aggregator_fee_eur']``, weighted by the monthly
      ``revenue_eur`` share so each month carries its proportional
      slice of the fee that has already been deducted from
      ``revenue_eur`` (informational; NOT re-added to the net).
    * ``toll_revenue_eur`` — flat ``1/12`` allocation of the yearly
      toll payment (Eq. E29; a level contractual stream, so the flat
      split is exact).  Part of ``net_cashflow_eur`` here.
    * ``opex_eur`` — Year-1 ``opex`` split evenly across months, scaled
      by the year's opex inflation factor.
    * ``capex_eur`` / ``devex_eur`` — the year's investment events
      (e.g. the scheduled BESS replacement CAPEX), booked in month 12.
      End-of-year placement matches the yearly sheet's ``1/(1+r)^y``
      discounting exactly (December of year ``y`` carries that same
      factor), so the monthly and yearly DCFs agree on the event.
    * ``net_cashflow_eur`` — ``revenue_eur + balancing_revenue_eur +
      balancing_aggregator_fee_eur + ppa_revenue_eur + opex_eur +
      capex_eur + devex_eur``. Sums to
      ``yearly_cf['net_cashflow_eur']`` row-for-row in EVERY operating
      year, including a BESS-replacement year.  (Year 0 is not part of
      the monthly frame; the initial outlay stays on the yearly sheet.)
    * ``discounted_cf_eur`` — ``net_cashflow_eur`` discounted at
      ``econ['discount_rate_pct']`` to the start of the project,
      end-of-month convention: month ``m`` of year ``y`` lands at
      ``t = (y - 1) + m/12`` years, so December of year ``y`` carries
      exactly the yearly row's ``1/(1+r)^y`` factor.

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

    # Derate the physical PV column by the availability factor so the
    # monthly sheet reconciles with kpis_year1['pv_generation_mwh'] and
    # the lifetime_dispatch_yearly sheet (both already derated upstream).
    avail_factor = availability_factor(
        float(econ.get("unavailability_pct", 0.0) or 0.0)
    )
    monthly_pv_mwh_y1 = monthly_pv_kwh_y1 / 1000.0 * avail_factor

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

    # Per-month PPA share — weighted by the magnitude of the Year-1
    # per-step contract-leg column when present (mirrors the balancing
    # reservation weighting; magnitudes keep the weights stable when a
    # CfD leg flips sign across months), flat 1/12 otherwise.  The
    # shares sum to one, so monthly sums reconcile to the yearly column
    # exactly either way.
    if "revenue_pv_ppa_eur" in res.columns:
        monthly_ppa_abs = (
            res["revenue_pv_ppa_eur"].abs().groupby(month_idx).sum()
            .reindex(range(1, 13), fill_value=0.0)
            .astype(float)
        )
        ppa_abs_sum = float(monthly_ppa_abs.sum())
        if ppa_abs_sum > 1e-9:
            ppa_share = monthly_ppa_abs / ppa_abs_sum
        else:
            ppa_share = pd.Series(1.0 / 12.0, index=range(1, 13), dtype=float)
    else:
        ppa_share = pd.Series(1.0 / 12.0, index=range(1, 13), dtype=float)

    # Aggregator-fee share — proportional to the monthly post-fee
    # ``revenue_eur`` so each month carries its slice of the fee that
    # has already been deducted from ``revenue_eur``.
    rev_y1_total = float(monthly_revenue_y1.sum())
    if abs(rev_y1_total) > 1e-9:
        fee_share = monthly_revenue_y1 / rev_y1_total
    else:
        fee_share = pd.Series(1.0 / 12.0, index=range(1, 13), dtype=float)

    # Charging-side grid fee share — weighted by the Year-1 per-step
    # fee column when present (the wedge is paid when the BESS actually
    # grid-charges, a strongly seasonal shape), flat 1/12 otherwise;
    # shares sum to one, so monthly sums reconcile exactly (PPA-share
    # pattern).
    if "expense_grid_charging_fee_eur" in res.columns:
        monthly_gcf = (
            res["expense_grid_charging_fee_eur"].groupby(month_idx).sum()
            .reindex(range(1, 13), fill_value=0.0)
            .astype(float)
        )
        gcf_sum = float(monthly_gcf.sum())
        if gcf_sum > 1e-9:
            gcf_share = monthly_gcf / gcf_sum
        else:
            gcf_share = pd.Series(
                1.0 / 12.0, index=range(1, 13), dtype=float,
            )
    else:
        gcf_share = pd.Series(1.0 / 12.0, index=range(1, 13), dtype=float)

    has_balancing_col = "balancing_revenue_eur" in yearly_cf.columns
    has_bal_fee_col = "balancing_aggregator_fee_eur" in yearly_cf.columns
    has_fee_col = "aggregator_fee_eur" in yearly_cf.columns
    has_rtm_col = "route_to_market_fee_eur" in yearly_cf.columns
    has_opt_col = "optimizer_fee_eur" in yearly_cf.columns
    has_gcf_col = "grid_charging_fee_eur" in yearly_cf.columns
    has_imb_col = "imbalance_cost_eur" in yearly_cf.columns
    has_toll_col = "toll_revenue_eur" in yearly_cf.columns
    has_topup_col = "optimizer_floor_topup_eur" in yearly_cf.columns
    has_ss_col = "state_support_eur" in yearly_cf.columns
    has_ss_cb_col = "state_support_clawback_eur" in yearly_cf.columns
    has_cm_col = "capacity_market_revenue_eur" in yearly_cf.columns
    has_ppa_col = "ppa_revenue_eur" in yearly_cf.columns
    has_capex_col = "capex_eur" in yearly_cf.columns
    has_devex_col = "devex_eur" in yearly_cf.columns

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
        bal_fee_y = (
            float(yearly_indexed.loc[y, "balancing_aggregator_fee_eur"])
            if has_bal_fee_col else 0.0
        )
        fee_y = (
            float(yearly_indexed.loc[y, "aggregator_fee_eur"])
            if has_fee_col else 0.0
        )
        rtm_fee_y = (
            float(yearly_indexed.loc[y, "route_to_market_fee_eur"])
            if has_rtm_col else 0.0
        )
        opt_fee_y = (
            float(yearly_indexed.loc[y, "optimizer_fee_eur"])
            if has_opt_col else 0.0
        )
        gcf_fee_y = (
            float(yearly_indexed.loc[y, "grid_charging_fee_eur"])
            if has_gcf_col else 0.0
        )
        imb_y = (
            float(yearly_indexed.loc[y, "imbalance_cost_eur"])
            if has_imb_col else 0.0
        )
        toll_y = (
            float(yearly_indexed.loc[y, "toll_revenue_eur"])
            if has_toll_col else 0.0
        )
        topup_y = (
            float(yearly_indexed.loc[y, "optimizer_floor_topup_eur"])
            if has_topup_col else 0.0
        )
        ss_y = (
            float(yearly_indexed.loc[y, "state_support_eur"])
            if has_ss_col else 0.0
        )
        ss_cb_y = (
            float(yearly_indexed.loc[y, "state_support_clawback_eur"])
            if has_ss_cb_col else 0.0
        )
        cm_y = (
            float(yearly_indexed.loc[y, "capacity_market_revenue_eur"])
            if has_cm_col else 0.0
        )
        ppa_y = (
            float(yearly_indexed.loc[y, "ppa_revenue_eur"])
            if has_ppa_col else 0.0
        )
        capex_y = (
            float(yearly_indexed.loc[y, "capex_eur"]) if has_capex_col else 0.0
        )
        devex_y = (
            float(yearly_indexed.loc[y, "devex_eur"]) if has_devex_col else 0.0
        )

        if abs(yearly_y1_revenue) > 1e-9:
            rev_scale = rev_y / yearly_y1_revenue
            rev_flat_m = 0.0
        else:
            # Degenerate regime: Year-1 net revenue is ~0 (streams can
            # cancel) while a later year is non-zero.  A proportional
            # scale is undefined, so allocate that year's revenue flat
            # across the months — the monthly sum still reconciles to
            # the yearly column exactly.
            rev_scale = 0.0
            rev_flat_m = rev_y / 12.0
        if abs(yearly_y1_opex) > 1e-9:
            opex_scale = opex_y / yearly_y1_opex
            opex_flat_m = 0.0
        else:
            opex_scale = 0.0
            opex_flat_m = opex_y / 12.0

        for m in range(1, 13):
            rev_m = float(monthly_revenue_y1.loc[m]) * rev_scale + rev_flat_m
            opex_m = float(monthly_opex_y1.loc[m]) * opex_scale + opex_flat_m
            pv_mwh_m = float(monthly_pv_mwh_y1.loc[m]) * pv_factor
            balancing_m = float(balancing_share.loc[m]) * balancing_y
            # The balancing-aggregator fee is proportional to balancing
            # revenue, so it rides the same per-month reservation weights;
            # the shares sum to one, so the monthly sum reconciles to the
            # yearly column exactly.
            bal_fee_m = float(balancing_share.loc[m]) * bal_fee_y
            ppa_m = float(ppa_share.loc[m]) * ppa_y
            fee_m = float(fee_share.loc[m]) * fee_y
            # The structural fees ride the same monthly revenue-share
            # weights as the energy-aggregator fee (an approximation of the
            # export/trading shape; the shares sum to one, so each month's
            # slice reconciles the yearly column exactly).  Unlike fee_m
            # they are part of the net here — the yearly net_cashflow_eur
            # carries them as their own columns.
            rtm_fee_m = float(fee_share.loc[m]) * rtm_fee_y
            opt_fee_m = float(fee_share.loc[m]) * opt_fee_y
            # The charging-side fee rides its own Year-1 charging shape.
            gcf_fee_m = float(gcf_share.loc[m]) * gcf_fee_y
            # Imbalance cost rides the PV production shape (Eq. E28a):
            # the deviation volume is PV-forecast-error-driven.  The
            # PV shares sum to one, so the monthly sum reconciles the
            # yearly column exactly.
            pv_y1_total = float(monthly_pv_mwh_y1.sum())
            if pv_y1_total > 1e-9:
                imb_m = float(monthly_pv_mwh_y1.loc[m]) / pv_y1_total * imb_y
            else:
                imb_m = imb_y / 12.0
            # Toll revenue (Eq. E29) is a level contractual payment, so
            # a flat 1/12 allocation is exact (shares sum to one and
            # the monthly sum reconciles the yearly column).
            toll_m = toll_y / 12.0
            # The optimizer floor top-up (Eq. E30) settles ex post
            # against the year's realised margin, so it books in
            # month 12 — the replacement-CAPEX convention, keeping the
            # monthly and yearly DCFs in exact agreement on the event.
            topup_m = topup_y if m == 12 else 0.0
            # State support (Eq. E31) is a level payment (flat 1/12);
            # its two-way netting (Eq. E31a) settles ex post against
            # the year's realised revenue, so it books in month 12.
            ss_m = ss_y / 12.0
            ss_cb_m = ss_cb_y if m == 12 else 0.0
            # The capacity payment (Eq. E32) is a level contractual
            # stream: flat 1/12 is exact.
            cm_m = cm_y / 12.0
            # Investment events (BESS replacement CAPEX, any operating-
            # year DEVEX) book in month 12 so the monthly DCF carries
            # the yearly end-of-year discount factor for them exactly.
            capex_m = capex_y if m == 12 else 0.0
            devex_m = devex_y if m == 12 else 0.0
            # balancing_m is GROSS, so its fee (bal_fee_m, negative) enters
            # the net here — unlike rev_m, which is already net of the energy
            # aggregator fee (fee_m is informational on the monthly frame).
            net_m = (
                rev_m + balancing_m + bal_fee_m + rtm_fee_m + opt_fee_m
                + topup_m
                + gcf_fee_m + imb_m
                + toll_m
                + ss_m + ss_cb_m
                + cm_m
                + ppa_m + opex_m + capex_m + devex_m
            )
            # End-of-month discounting: month m of year y lands at
            # (y - 1) + m/12, so December of year y discounts exactly like
            # the end-of-year yearly row (1 / (1+r)^y) and earlier months
            # discount less — the months of year y occur DURING year y.
            t_years = float(y) - 1.0 + m / 12.0
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
                    "balancing_aggregator_fee_eur": float(bal_fee_m),
                    "route_to_market_fee_eur": float(rtm_fee_m),
                    "optimizer_fee_eur": float(opt_fee_m),
                    "optimizer_floor_topup_eur": float(topup_m),
                    "grid_charging_fee_eur": float(gcf_fee_m),
                    "imbalance_cost_eur": float(imb_m),
                    "toll_revenue_eur": float(toll_m),
                    "state_support_eur": float(ss_m),
                    "state_support_clawback_eur": float(ss_cb_m),
                    "capacity_market_revenue_eur": float(cm_m),
                    "ppa_revenue_eur": float(ppa_m),
                    "aggregator_fee_eur": float(fee_m),
                    "opex_eur": float(opex_m),
                    "capex_eur": float(capex_m),
                    "devex_eur": float(devex_m),
                    "net_cashflow_eur": float(net_m),
                    "discounted_cf_eur": float(net_m * disc_factor),
                }
            )

    monthly_cf = pd.DataFrame(rows)

    monthly_columns = [
        "project_year", "calendar_year", "period",
        "period_type", "pv_production_mwh", "revenue_eur",
        "balancing_revenue_eur", "balancing_aggregator_fee_eur",
        "route_to_market_fee_eur", "optimizer_fee_eur",
        "optimizer_floor_topup_eur",
        "grid_charging_fee_eur", "imbalance_cost_eur",
        "toll_revenue_eur",
        "state_support_eur", "state_support_clawback_eur",
        "capacity_market_revenue_eur",
        "ppa_revenue_eur", "aggregator_fee_eur",
        "opex_eur", "capex_eur", "devex_eur",
        "net_cashflow_eur", "discounted_cf_eur",
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
                    "balancing_revenue_eur", "balancing_aggregator_fee_eur",
                    "route_to_market_fee_eur", "optimizer_fee_eur",
                    "optimizer_floor_topup_eur",
                    "grid_charging_fee_eur", "imbalance_cost_eur",
                    "toll_revenue_eur",
                    "state_support_eur", "state_support_clawback_eur",
                    "capacity_market_revenue_eur",
                    "ppa_revenue_eur", "aggregator_fee_eur",
                    "opex_eur", "capex_eur", "devex_eur",
                    "net_cashflow_eur", "discounted_cf_eur",
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
    :func:`build_yearly_cashflow` is reflected automatically.

    Investment-outlay conventions:

    * ``initial_investment_eur`` — the Year-0 outlay only (per-asset
      CAPEX + DEVEX + site lump sums, signed negative).  This matches
      the Year-0 bar in the financial plots.
    * ``total_capex_eur`` / ``total_capex_devex_eur`` — lifecycle
      totals; with a scheduled BESS replacement these also include the
      replacement CAPEX charged in ``bess_replacement_year``.
    * ``roi_pct`` — sum of operating net cashflow (Years 1..N) over
      ``|initial_investment_eur|``.

    Balancing revenue enters NPV / IRR / ROI / BCR / payback the same
    way — via ``balancing_revenue_eur`` in the yearly cashflow, which is
    included in ``net_cashflow_eur`` by :func:`build_yearly_cashflow` —
    so all five cashflow-derived KPIs already account for the FCR /
    aFRR / mFRR streams when balancing is on.  When an optional
    balancing-aggregator (BSP) fee is set, its negative
    ``balancing_aggregator_fee_eur`` column is also folded into
    ``net_cashflow_eur``, so the five KPIs consume the NET balancing
    revenue; ``lifetime_bm_revenue_total_eur`` stays gross while
    ``lifetime_bm_aggregator_fee_total_eur`` /
    ``lifetime_bm_revenue_net_total_eur`` expose the fee and the net.

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
    devex_y0 = (
        float(df.loc[df[project_year_col] == 0, "devex_eur"].iloc[0])
        if "devex_eur" in df.columns and (df[project_year_col] == 0).any()
        else 0.0
    )
    # The Year-0 outlay (per-asset CAPEX + DEVEX + site lump sums) — the
    # number that matches the Year-0 bar in the financial plots.  It
    # deliberately EXCLUDES the BESS replacement CAPEX charged later in
    # the horizon; the lifecycle totals below include it.
    initial_investment_eur = capex_y0 + devex_y0
    investment_abs = abs(float(initial_investment_eur))

    npv = float(df["discounted_cf_eur"].sum())

    cf_array = df["net_cashflow_eur"].to_numpy(dtype=float)
    irr = calculate_irr(cf_array)
    irr_pct = float("nan") if np.isnan(irr) else irr * 100.0
    gearing_pct_val = float(econ.get("gearing_pct", 0.0) or 0.0)
    equity_irr_pct, min_dscr = _leverage_kpis(cf_array, econ)

    after_y0_cf = df.loc[after_y0_mask, "net_cashflow_eur"]
    # ROI = sum of operating net cashflow (Years 1..N, replacement CAPEX
    # included via the net) over the initial investment |Year-0 CAPEX +
    # DEVEX| — the standard total-return-on-initial-investment form.
    if investment_abs > 1e-9:
        roi_pct = float(after_y0_cf.sum()) / investment_abs * 100.0
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

    # Lifecycle totals: ``capex_eur`` is summed over ALL years, so with a
    # scheduled BESS replacement these include the replacement CAPEX and
    # exceed the Year-0 outlay (``initial_investment_eur``) by exactly
    # ``bess_replacement_cost_pct`` x BESS CAPEX.
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
    total_route_to_market_fee_eur_lifecycle = (
        float(df.loc[after_y0_mask, "route_to_market_fee_eur"].sum())
        if "route_to_market_fee_eur" in df.columns else 0.0
    )
    total_optimizer_fee_eur_lifecycle = (
        float(df.loc[after_y0_mask, "optimizer_fee_eur"].sum())
        if "optimizer_fee_eur" in df.columns else 0.0
    )
    total_grid_charging_fee_eur_lifecycle = (
        float(df.loc[after_y0_mask, "grid_charging_fee_eur"].sum())
        if "grid_charging_fee_eur" in df.columns else 0.0
    )
    total_imbalance_cost_eur_lifecycle = (
        float(df.loc[after_y0_mask, "imbalance_cost_eur"].sum())
        if "imbalance_cost_eur" in df.columns else 0.0
    )
    total_toll_revenue_eur_lifecycle = (
        float(df.loc[after_y0_mask, "toll_revenue_eur"].sum())
        if "toll_revenue_eur" in df.columns else 0.0
    )
    total_optimizer_floor_topup_eur_lifecycle = (
        float(df.loc[after_y0_mask, "optimizer_floor_topup_eur"].sum())
        if "optimizer_floor_topup_eur" in df.columns else 0.0
    )
    total_state_support_eur_lifecycle = (
        float(df.loc[after_y0_mask, "state_support_eur"].sum())
        if "state_support_eur" in df.columns else 0.0
    )
    total_state_support_clawback_eur_lifecycle = (
        float(df.loc[after_y0_mask, "state_support_clawback_eur"].sum())
        if "state_support_clawback_eur" in df.columns else 0.0
    )
    total_capacity_market_revenue_eur_lifecycle = (
        float(df.loc[after_y0_mask, "capacity_market_revenue_eur"].sum())
        if "capacity_market_revenue_eur" in df.columns else 0.0
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
    total_balancing_aggregator_fee_eur_lifecycle = (
        float(df.loc[after_y0_mask, "balancing_aggregator_fee_eur"].sum())
        if "balancing_aggregator_fee_eur" in df.columns else 0.0
    )
    total_ppa_revenue_eur_lifecycle = (
        float(df.loc[after_y0_mask, "ppa_revenue_eur"].sum())
        if "ppa_revenue_eur" in df.columns else 0.0
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
            # upstream in pvbess_opt.pipeline._build_financials).
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
            # The SAME escalation series as the cashflow's OPEX row
            # (Eq. E24a) — an OPEX trajectory must move LCOE identically.
            _g_lcoe_opex = _opex_escalation_series(
                "opex_pv", opex_infl_lcoe,
                int(df.loc[op_mask, project_year_col].max()),
                econ.get("trajectories") or None,
            )
            disc_pv_opex = 0.0
            disc_pv_mwh = 0.0
            for y in df.loc[op_mask, project_year_col]:
                yi = int(y)
                if yi == 0:
                    continue
                disc_y = float(disc_series.loc[yi])
                opex_pv_y = (
                    opex_pv_per_kwp * pv_kwp * _g_lcoe_opex[yi - 1]
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
            # BESS-attributable CAPEX share: BESS energy block + BESS DEVEX.
            bess_capex_y0 = float(econ.get("capex_bess_eur_per_kwh", 0.0)) * bess_kwh
            bess_devex_y0 = (
                float(econ.get("devex_bess_eur_per_kw", 0.0) or 0.0) * bess_kw
            )
            bess_repl_year = effective_bess_replacement_year(econ)
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
            # Same series as the cashflow's BESS OPEX leg (Eq. E24a).
            _g_lcos_opex = _opex_escalation_series(
                "opex_bess", opex_infl,
                int(df.loc[op_mask, project_year_col].max()),
                econ.get("trajectories") or None,
            )
            for y in df.loc[op_mask, project_year_col]:
                yi = int(y)
                if yi == 0:
                    continue
                disc_y = float(disc_series.loc[yi])
                opex_bess_y = (
                    opex_bess_per_kw * bess_kw * _g_lcos_opex[yi - 1]
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
        repl_fade = effective_bess_replacement_year(econ)
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
        "gearing_pct": float(round(gearing_pct_val, 4)),
        "equity_irr_pct": (
            float("nan") if np.isnan(equity_irr_pct)
            else float(round(equity_irr_pct, 4))
        ),
        "min_dscr": (
            float("nan") if np.isnan(min_dscr) else float(round(min_dscr, 4))
        ),
        "initial_investment_eur": float(round(initial_investment_eur, 2)),
        "total_capex_eur": float(round(total_capex_eur, 2)),
        "total_devex_eur": float(round(total_devex_eur, 2)),
        "total_capex_devex_eur": float(round(total_capex_devex_eur, 2)),
        "total_opex_eur_lifecycle": float(round(total_opex_eur_lifecycle, 2)),
        "total_revenue_eur_lifecycle": float(round(total_revenue_eur_lifecycle, 2)),
        "total_aggregator_fee_eur_lifecycle": float(round(
            total_aggregator_fee_eur_lifecycle, 2,
        )),
        # Structural market-access fee totals (Eq. E13c / E13d); both 0 when
        # the knobs are off, and the SUMMARY renders them only when set.
        "total_route_to_market_fee_eur_lifecycle": float(round(
            total_route_to_market_fee_eur_lifecycle, 2,
        )),
        "total_grid_charging_fee_eur_lifecycle": float(round(
            total_grid_charging_fee_eur_lifecycle, 2,
        )),
        "total_imbalance_cost_eur_lifecycle": float(round(
            total_imbalance_cost_eur_lifecycle, 2,
        )),
        "total_optimizer_fee_eur_lifecycle": float(round(
            total_optimizer_fee_eur_lifecycle, 2,
        )),
        # Contracted BESS revenue (Eq. E29); 0 when no toll is set and
        # the SUMMARY renders it only when non-zero.
        "total_toll_revenue_eur_lifecycle": float(round(
            total_toll_revenue_eur_lifecycle, 2,
        )),
        # Optimizer floor guarantee (Eq. E30); >= 0, SUMMARY-optional.
        "total_optimizer_floor_topup_eur_lifecycle": float(round(
            total_optimizer_floor_topup_eur_lifecycle, 2,
        )),
        # State support and its two-way netting (Eqs. E31/E31a);
        # SUMMARY-optional, the netting total is signed.
        "total_state_support_eur_lifecycle": float(round(
            total_state_support_eur_lifecycle, 2,
        )),
        "total_state_support_clawback_eur_lifecycle": float(round(
            total_state_support_clawback_eur_lifecycle, 2,
        )),
        # Capacity-market payment (Eq. E32); SUMMARY-optional.
        "total_capacity_market_revenue_eur_lifecycle": float(round(
            total_capacity_market_revenue_eur_lifecycle, 2,
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
        # Optional BSP / route-to-market fee on balancing revenue.  The gross
        # roll-up (``lifetime_bm_revenue_total_eur``) stays fee-free for the
        # revenue stack; this fee (<= 0) and the net let plots and the DCF
        # agree.  Both are 0 when balancing_aggregator_fee_pct_revenue == 0.
        "lifetime_bm_aggregator_fee_total_eur": float(round(
            total_balancing_aggregator_fee_eur_lifecycle, 2,
        )),
        "lifetime_bm_revenue_net_total_eur": float(round(
            total_balancing_revenue_eur_lifecycle
            + total_balancing_aggregator_fee_eur_lifecycle, 2,
        )),
        "lifetime_ppa_revenue_total_eur": float(round(
            total_ppa_revenue_eur_lifecycle, 2,
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
    # numbers next to the Lazard 2024 reference bands.  Emitted only when
    # the LCOE/LCOS inputs were supplied: the sensitivity perturbations
    # call this function without capacities/lifetime_yearly, and logging
    # "LCOE = n/a" once per perturbed scenario was misleading noise.
    if capacities is not None and lifetime_yearly is not None:
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
