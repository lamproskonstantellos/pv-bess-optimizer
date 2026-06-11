"""KPI calculations and energy-balance verification.

Energy-flow conventions (all per timestep, kWh):

* PV split::

    pv_kwh = pv_to_load_kwh + pv_to_bess_kwh
           + pv_to_grid_kwh + pv_curtail_kwh

* Load balance (self_consumption only)::

    load_kwh = pv_to_load_kwh + bess_dis_load_kwh + grid_to_load_kwh

* BESS state-of-charge dynamics::

    soc_kwh[t+1] - soc_kwh[t] =
        efficiency_charge * (pv_to_bess_kwh + bess_charge_grid_kwh)
      - (bess_dis_load_kwh + bess_dis_grid_kwh) / efficiency_discharge

* Grid export (subject to export-cap constraint)::

    grid_export_total_kwh = pv_to_grid_kwh + bess_dis_grid_kwh

In ``mode == "merchant"`` the load-balance check, all load-coverage
ratios, and the ``profit_load_*`` revenue components are skipped or
zeroed.

KPI keys are lowercase snake_case throughout.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from .balancing import (
    PRODUCTS_ALL,
    PRODUCTS_DN,
    PRODUCTS_UP,
    PRODUCTS_WITH_ACTIVATION,
    acceptance_probability,
    activation_probability,
    resolve_balancing_config,
)
from .modes import resolve_mode
from .ppa import resolve_ppa_config
from .timeutils import dt_hours_from

logger = logging.getLogger(__name__)

__all__ = [
    "ECONOMIC_COLUMNS",
    "ENERGY_TOLERANCE",
    "add_economic_columns",
    "attribute_green_discharge",
    "compute_kpis",
    "compute_monthly_kpis",
    "require_economic_columns",
    "verify_energy_balance",
]

ENERGY_TOLERANCE: float = 1.0e-3  # kWh per timestep


def _balancing_soc_drift(
    res: pd.DataFrame, params: dict[str, Any],
) -> np.ndarray | None:
    """Return the per-step expected-activation SOC drift, or None.

    The drift is positive when downward activation (charging) dominates
    and negative for upward activation (discharging). Returns ``None``
    when the balancing block did not fire so callers can keep their
    pre-feature numerical behaviour bit-identical.
    """
    raw_cfg = params.get("balancing") or {}
    cfg = resolve_balancing_config(raw_cfg)
    if not cfg.balancing_enabled:
        return None
    if not all(f"bm_reservation_{p}_kw" in res.columns for p in PRODUCTS_UP + PRODUCTS_DN):
        return None

    dt_h = dt_hours_from(params)
    eta_c = float(params.get("efficiency_charge", 1.0) or 1.0)
    eta_d = float(params.get("efficiency_discharge", 1.0) or 1.0)
    drift = np.zeros(len(res), dtype=float)
    for product in PRODUCTS_DN:
        r = res[f"bm_reservation_{product}_kw"].to_numpy(dtype=float)
        alpha_beta = (
            acceptance_probability(cfg, product)
            * activation_probability(cfg, product)
        )
        drift = drift + eta_c * dt_h * alpha_beta * r
    for product in PRODUCTS_UP:
        r = res[f"bm_reservation_{product}_kw"].to_numpy(dtype=float)
        alpha_beta = (
            acceptance_probability(cfg, product)
            * activation_probability(cfg, product)
        )
        drift = drift - (dt_h / eta_d) * alpha_beta * r
    return drift


# ---------------------------------------------------------------------------
# Energy-flow verification
# ---------------------------------------------------------------------------


def verify_energy_balance(
    res: pd.DataFrame,
    params: dict[str, Any],
    *,
    raise_on_failure: bool = False,
) -> dict[str, float]:
    """Verify the per-step energy balances against the dispatch DataFrame.

    Pass the full-precision frame from ``run_scenario(return_unrounded=True)``
    to avoid round(4) accumulation in the per-step residuals.
    """
    mode = resolve_mode(params)

    pv_residual = np.abs(
        res["pv_kwh"].to_numpy(dtype=float)
        - (
            res["pv_to_load_kwh"].to_numpy(dtype=float)
            + res["pv_to_bess_kwh"].to_numpy(dtype=float)
            + res["pv_to_grid_kwh"].to_numpy(dtype=float)
            + res["pv_curtail_kwh"].to_numpy(dtype=float)
        )
    )
    if mode == "self_consumption":
        load_residual = np.abs(
            res["load_kwh"].to_numpy(dtype=float)
            - (
                res["pv_to_load_kwh"].to_numpy(dtype=float)
                + res["bess_dis_load_kwh"].to_numpy(dtype=float)
                + res["grid_to_load_kwh"].to_numpy(dtype=float)
            )
        )
    else:
        load_residual = np.zeros_like(pv_residual)
    export_residual = np.abs(
        res["grid_export_total_kwh"].to_numpy(dtype=float)
        - (
            res["pv_to_grid_kwh"].to_numpy(dtype=float)
            + res["bess_dis_grid_kwh"].to_numpy(dtype=float)
        )
    )

    eta_c = float(params["efficiency_charge"])
    eta_d = float(params["efficiency_discharge"])
    soc = res["soc_kwh"].to_numpy(dtype=float)
    expected_delta = (
        eta_c * (res["pv_to_bess_kwh"] + res["bess_charge_grid_kwh"])
        - (res["bess_dis_load_kwh"] + res["bess_dis_grid_kwh"]) / eta_d
    ).to_numpy(dtype=float)
    # Include the deterministic expected-activation drift when the
    # balancing block fired (the MILP added it to the SOC recursion).
    bm_drift = _balancing_soc_drift(res, params)
    if bm_drift is not None:
        expected_delta = expected_delta + bm_drift
    soc_residual = np.zeros_like(soc)
    if len(soc) >= 2:
        soc_residual[:-1] = np.abs(soc[1:] - soc[:-1] - expected_delta[:-1])

    residuals = {
        "max_pv_split_residual_kwh": float(pv_residual.max(initial=0.0)),
        "max_load_balance_residual_kwh": float(load_residual.max(initial=0.0)),
        "max_export_definition_residual_kwh": float(export_residual.max(initial=0.0)),
        "max_soc_dynamics_residual_kwh": float(soc_residual.max(initial=0.0)),
    }

    if raise_on_failure:
        for name, value in residuals.items():
            if value > ENERGY_TOLERANCE:
                raise AssertionError(
                    f"Energy-balance check '{name}' violated: "
                    f"max residual {value:.6g} kWh > tolerance {ENERGY_TOLERANCE} kWh"
                )
    else:
        for name, value in residuals.items():
            if value > ENERGY_TOLERANCE:
                logger.warning(
                    "Energy-balance %s exceeded tolerance: %.6g kWh", name, value,
                )

    return residuals


# ---------------------------------------------------------------------------
# Green-energy attribution inside the BESS
# ---------------------------------------------------------------------------


def attribute_green_discharge(
    res: pd.DataFrame, params: dict[str, Any],
) -> pd.DataFrame:
    """Annotate ``res`` with the PV-origin component of BESS discharge.

    Adds three columns:
        ``bess_dis_load_green_kwh``
        ``bess_dis_grid_green_kwh``
        ``soc_green_kwh``

    Single running balance: PV charge adds to green stock; discharge
    draws proportionally from green stock first.  Initial SOC is treated
    as green (worst-case for reporting honesty).
    """
    eta_c = float(params.get("efficiency_charge", 1.0))
    eta_d = float(params.get("efficiency_discharge", 1.0))

    n = len(res)
    if n == 0:
        for col in (
            "bess_dis_load_green_kwh", "bess_dis_grid_green_kwh", "soc_green_kwh",
        ):
            res[col] = []
        return res

    e_ch_pv = res["pv_to_bess_kwh"].to_numpy(dtype=float)
    e_dis_load = res["bess_dis_load_kwh"].to_numpy(dtype=float)
    e_dis_grid = res["bess_dis_grid_kwh"].to_numpy(dtype=float)

    green_soc = float(res["soc_kwh"].iloc[0])
    e_dis_load_green = np.zeros(n, dtype=float)
    e_dis_grid_green = np.zeros(n, dtype=float)
    green_soc_trace = np.zeros(n, dtype=float)

    for t in range(n):
        green_soc += eta_c * e_ch_pv[t]
        out_total = e_dis_load[t] + e_dis_grid[t]
        draw_from_soc = out_total / eta_d if eta_d > 0 else 0.0
        green_draw = min(green_soc, draw_from_soc)
        green_out = green_draw * eta_d

        if out_total > 1.0e-12:
            frac_load = e_dis_load[t] / out_total
            frac_grid = e_dis_grid[t] / out_total
        else:
            frac_load = frac_grid = 0.0

        e_dis_load_green[t] = green_out * frac_load
        e_dis_grid_green[t] = green_out * frac_grid
        green_soc -= green_draw
        green_soc_trace[t] = max(green_soc, 0.0)

    res["bess_dis_load_green_kwh"] = e_dis_load_green
    res["bess_dis_grid_green_kwh"] = e_dis_grid_green
    res["soc_green_kwh"] = green_soc_trace
    return res


# ---------------------------------------------------------------------------
# Per-step EUR columns
# ---------------------------------------------------------------------------


def add_economic_columns(
    res: pd.DataFrame, params: dict[str, Any],
) -> pd.DataFrame:
    """Add per-step EUR columns derived from prices and the retail tariff.

    Column names use lowercase snake_case:

    * ``profit_load_from_pv_eur``        — retail × pv_to_load / 1000.
    * ``profit_load_from_bess_eur``      — retail × bess_dis_load / 1000.
    * ``profit_export_from_pv_eur``      — DAM × pv_to_grid / 1000
      (under a physical PPA: DAM × the UNCOVERED share only).
    * ``profit_export_from_bess_eur``    — DAM × bess_dis_grid / 1000.
    * ``expense_charge_bess_grid_eur``   — DAM × bess_charge_grid / 1000.

    When a pay-as-produced PPA is active (``params['ppa']`` — see
    :mod:`pvbess_opt.ppa` and ``docs/ppa_design.md``) two further
    columns are written; they are absent otherwise so disabled runs
    stay bit-identical:

    * ``revenue_pv_ppa_eur`` — the contract leg on the covered share of
      PV export: ``covered × strike`` under physical settlement,
      ``covered × (strike − DAM)`` under CfD (negative when DAM exceeds
      the strike).
    * ``ppa_covered_dam_value_eur`` — the counterfactual DAM value of
      the covered volume (``covered × DAM``), carried for the
      multi-year cashflow's post-term reversion.
    """
    retail_default = float(params.get("retail_tariff_eur_per_mwh", 0.0) or 0.0)
    if "retail_price_eur_per_mwh" in res.columns:
        retail_series = res["retail_price_eur_per_mwh"].fillna(retail_default)
    else:
        retail_series = pd.Series(retail_default, index=res.index)
    if "dam_price_eur_per_mwh" in res.columns:
        dam_series = res["dam_price_eur_per_mwh"].fillna(0.0)
    else:
        dam_series = pd.Series(0.0, index=res.index)

    res["profit_load_from_pv_eur"] = (
        res["pv_to_load_kwh"] / 1000.0 * retail_series
    )
    res["profit_load_from_bess_eur"] = (
        res["bess_dis_load_kwh"] / 1000.0 * retail_series
    )
    res["profit_export_from_pv_eur"] = (
        res["pv_to_grid_kwh"] / 1000.0 * dam_series
    )
    res["profit_export_from_bess_eur"] = (
        res["bess_dis_grid_kwh"] / 1000.0 * dam_series
    )
    res["expense_charge_bess_grid_eur"] = (
        res["bess_charge_grid_kwh"].fillna(0.0) / 1000.0 * dam_series
    )

    ppa_cfg = resolve_ppa_config(params.get("ppa"))
    if ppa_cfg.active:
        share = ppa_cfg.share_frac
        strike = float(ppa_cfg.ppa_price_eur_per_mwh)
        covered_mwh = share * res["pv_to_grid_kwh"] / 1000.0
        res["ppa_covered_dam_value_eur"] = covered_mwh * dam_series
        if ppa_cfg.ppa_settlement == "physical":
            # The covered volume is paid the strike and never touches
            # the DAM; the market column keeps the uncovered share only.
            res["revenue_pv_ppa_eur"] = covered_mwh * strike
            res["profit_export_from_pv_eur"] = (
                (1.0 - share) * res["pv_to_grid_kwh"] / 1000.0 * dam_series
            )
        else:  # cfd — two-way settlement on top of full DAM exposure.
            res["revenue_pv_ppa_eur"] = covered_mwh * (strike - dam_series)
    return res


# Per-step EUR columns that :func:`add_economic_columns` (called inside
# :func:`compute_kpis`) writes onto the dispatch frame.  The downstream
# financial pipeline reads these; running it before ``compute_kpis`` would
# otherwise silently default revenue to zero.  The two PPA columns are
# written only when a pay-as-produced contract is active (disabled runs
# stay bit-identical); the five DAM/retail columns are always written
# together.
ECONOMIC_COLUMNS: tuple[str, ...] = (
    "profit_load_from_pv_eur",
    "profit_load_from_bess_eur",
    "profit_export_from_pv_eur",
    "profit_export_from_bess_eur",
    "expense_charge_bess_grid_eur",
    "revenue_pv_ppa_eur",
    "ppa_covered_dam_value_eur",
)


def require_economic_columns(df: pd.DataFrame, *, context: str) -> None:
    """Raise if none of the per-step EUR columns are present.

    Enforces the ordering contract: :func:`compute_kpis` (or
    :func:`add_economic_columns`) must run before the financial pipeline
    so revenue is never silently defaulted to zero.  ``add_economic_columns``
    always writes the full :data:`ECONOMIC_COLUMNS` set together, so the
    absence of *all* of them means it was never called.
    """
    if not any(c in df.columns for c in ECONOMIC_COLUMNS):
        raise ValueError(
            f"{context}: no economic columns present (expected the "
            f"compute_kpis outputs {ECONOMIC_COLUMNS}). compute_kpis() must "
            "be called before the financial pipeline (derive_monthly_cashflow "
            "/ build_lifetime_dispatch / aggregate_lifetime_to_yearly); "
            "revenue must not default to zero."
        )


# ---------------------------------------------------------------------------
# Aggregate KPIs
# ---------------------------------------------------------------------------


def _sum_mwh(res: pd.DataFrame, col: str) -> float:
    if col not in res.columns:
        return 0.0
    return float(res[col].sum()) / 1000.0


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if abs(denominator) > 1e-9 else 0.0


def compute_kpis(
    res: pd.DataFrame,
    params: dict[str, Any],
    *,
    verify_balance: bool = True,
) -> dict[str, Any]:
    """Compute the headline KPI dictionary for a solved scenario.

    ``e_cap`` is not a decision variable — the BESS energy capacity is
    pinned to ``params['bess_capacity_kwh']``.
    """
    if verify_balance:
        verify_energy_balance(res, params, raise_on_failure=False)
    attribute_green_discharge(res, params)
    add_economic_columns(res, params)

    mode = resolve_mode(params)
    e_cap_kwh = float(params.get("bess_capacity_kwh", 0.0) or 0.0)

    pv_gen = _sum_mwh(res, "pv_kwh")
    load_en = _sum_mwh(res, "load_kwh") if mode == "self_consumption" else 0.0
    pv_direct = _sum_mwh(res, "pv_to_load_kwh") if mode == "self_consumption" else 0.0
    bess_to_load = _sum_mwh(res, "bess_dis_load_kwh") if mode == "self_consumption" else 0.0
    curtailed = _sum_mwh(res, "pv_curtail_kwh")

    pv_to_bess = _sum_mwh(res, "pv_to_bess_kwh")
    bess_charge_grid = _sum_mwh(res, "bess_charge_grid_kwh")
    total_charge = pv_to_bess + bess_charge_grid
    total_discharge = (
        bess_to_load + _sum_mwh(res, "bess_dis_grid_kwh")
    )

    total_export = (
        _sum_mwh(res, "pv_to_grid_kwh") + _sum_mwh(res, "bess_dis_grid_kwh")
    )
    total_import = (
        _sum_mwh(res, "grid_to_load_kwh") + _sum_mwh(res, "bess_charge_grid_kwh")
    )

    bess_green_to_load = float(res["bess_dis_load_green_kwh"].sum()) / 1000.0
    system_green = pv_direct + bess_green_to_load

    pv_direct_self_consumption = _safe_div(pv_direct, pv_gen)
    bess_from_pv_self_consumption = _safe_div(bess_green_to_load, pv_gen)
    system_pv_self_consumption = _safe_div(system_green, pv_gen)

    if mode == "self_consumption":
        pv_load_cov = _safe_div(pv_direct, load_en)
        load_coverage_bess = _safe_div(bess_green_to_load, load_en)
        system_load_green_coverage = _safe_div(system_green, load_en)
        load_coverage_bess_total = _safe_div(bess_to_load, load_en)
    else:
        pv_load_cov = 0.0
        load_coverage_bess = 0.0
        system_load_green_coverage = 0.0
        load_coverage_bess_total = 0.0

    if e_cap_kwh > 1e-9:
        soc_min_pct = float(res["soc_pct"].min())
        soc_max_pct = float(res["soc_pct"].max())
        soc_avg_pct = float(res["soc_pct"].mean())
    else:
        soc_min_pct = soc_max_pct = soc_avg_pct = 0.0

    days_count = (
        int(pd.to_datetime(res["timestamp"]).dt.date.nunique())
        if pd.api.types.is_datetime64_any_dtype(res["timestamp"])
        else 1
    )

    eq_cycles_total = (
        (total_discharge * 1000.0) / e_cap_kwh if e_cap_kwh > 1e-9 else 0.0
    )
    eq_cycles_per_day = eq_cycles_total / days_count if days_count > 0 else 0.0

    rte = _safe_div(total_discharge, total_charge)

    soc_initial_kwh = float(res["soc_kwh"].iloc[0]) if len(res) else 0.0
    soc_final_kwh = float(res["soc_kwh"].iloc[-1]) if len(res) else 0.0
    net_soc_change_kwh = soc_final_kwh - soc_initial_kwh
    rte_theoretical = float(params["efficiency_charge"]) * float(params["efficiency_discharge"])

    profit_load_pv = float(res["profit_load_from_pv_eur"].sum())
    profit_load_bess = float(res["profit_load_from_bess_eur"].sum())
    profit_export_pv = float(res["profit_export_from_pv_eur"].sum())
    profit_export_bess = float(res["profit_export_from_bess_eur"].sum())
    expense_charge_grid = float(res["expense_charge_bess_grid_eur"].sum())
    # PPA contract leg (0.0 when no pay-as-produced contract is active —
    # the columns are then absent by design).
    revenue_ppa = (
        float(res["revenue_pv_ppa_eur"].sum())
        if "revenue_pv_ppa_eur" in res.columns else 0.0
    )
    ppa_covered_dam_value = (
        float(res["ppa_covered_dam_value_eur"].sum())
        if "ppa_covered_dam_value_eur" in res.columns else 0.0
    )
    profit_total = (
        profit_load_pv + profit_load_bess + profit_export_pv + profit_export_bess
        - expense_charge_grid
        + revenue_ppa
    )

    initial_soc_pct = params["initial_soc_frac"] * 100.0

    kpis: dict[str, Any] = {
        "mode": mode,
        "allow_bess_grid_charging": bool(params.get("allow_bess_grid_charging", False)),
        "e_cap_mwh": round(e_cap_kwh / 1000.0, 4),
        "system_total_import_mwh": round(total_import, 4),
        "system_total_export_mwh": round(total_export, 4),
        "bess_total_charge_mwh": round(total_charge, 4),
        "pv_to_bess_mwh": round(pv_to_bess, 4),
        "bess_charge_grid_mwh": round(bess_charge_grid, 4),
        "bess_total_discharge_mwh": round(total_discharge, 4),
        "pv_generation_mwh": round(pv_gen, 4),
        "load_energy_mwh": round(load_en, 4),

        "pv_direct_to_load_mwh": round(pv_direct, 4),
        "bess_to_load_mwh": round(bess_to_load, 4),
        "bess_green_to_load_mwh": round(bess_green_to_load, 4),
        "system_green_to_load_mwh": round(system_green, 4),

        "pv_direct_self_consumption_frac": round(pv_direct_self_consumption, 4),
        "bess_from_pv_self_consumption_frac": round(bess_from_pv_self_consumption, 4),
        "system_pv_self_consumption_frac": round(system_pv_self_consumption, 4),
        "load_coverage_from_pv_frac": round(pv_load_cov, 4),
        "load_coverage_from_bess_frac": round(load_coverage_bess, 4),
        "load_coverage_from_bess_total_frac": round(load_coverage_bess_total, 4),
        "system_load_green_coverage_frac": round(system_load_green_coverage, 4),

        "soc_initial_pct": round(initial_soc_pct, 2),
        "soc_min_pct": round(soc_min_pct, 2),
        "soc_max_pct": round(soc_max_pct, 2),
        "soc_avg_pct": round(soc_avg_pct, 2),

        "bess_equivalent_cycles_total": round(eq_cycles_total, 4),
        "bess_equivalent_cycles_per_day": round(eq_cycles_per_day, 4),
        "bess_roundtrip_eff_est": round(rte, 4),
        "bess_roundtrip_eff_theoretical": round(rte_theoretical, 4),
        "bess_net_soc_change_mwh": round(net_soc_change_kwh / 1000.0, 4),

        "pv_energy_curtailed_mwh": round(curtailed, 4),

        "profit_load_from_pv_eur": round(profit_load_pv, 2),
        "profit_load_from_bess_eur": round(profit_load_bess, 2),
        "profit_export_from_pv_eur": round(profit_export_pv, 2),
        "profit_export_from_bess_eur": round(profit_export_bess, 2),
        "expense_charge_bess_grid_eur": round(expense_charge_grid, 2),
        # PPA contract leg + the covered volume's counterfactual DAM
        # value (both 0.0 without an active contract; always emitted so
        # the dict shape stays stable for downstream consumers).
        "revenue_pv_ppa_eur": round(revenue_ppa, 2),
        "ppa_covered_dam_value_eur": round(ppa_covered_dam_value, 2),
        "profit_total_eur": round(profit_total, 2),
    }

    # ---------------------------------------------------------------------
    # BESS utilisation diagnostics (Year-1 throughput vs. theoretical max).
    # Lets the run log explain *why* a project ends up with low lifetime
    # cycles — typically PV surplus << load with grid charging disabled.
    # ---------------------------------------------------------------------
    if e_cap_kwh > 1e-9:
        max_cycles_per_day = float(params.get("max_cycles_per_day", 0.0) or 0.0)
        bess_discharge_load_mwh = bess_to_load
        bess_discharge_grid_mwh = _sum_mwh(res, "bess_dis_grid_kwh")
        max_cycles_year = max_cycles_per_day * 365.0
        actual_cycles_year1 = (
            (bess_discharge_load_mwh + bess_discharge_grid_mwh) * 1000.0
            / e_cap_kwh
        )
        utilisation_pct = (
            100.0 * actual_cycles_year1 / max_cycles_year
            if max_cycles_year > 1e-9 else 0.0
        )
        # Note: this nested-dict diagnostic is NOT derated by
        # unavailability (unlike the headline MWh keys that
        # apply_unavailability_derate scales) — it reports the raw
        # Year-1 dispatch utilisation.
        kpis["bess_utilization_diagnostics"] = {
            "bess_charge_pv_surplus_mwh": round(pv_to_bess, 4),
            "bess_charge_grid_mwh": round(bess_charge_grid, 4),
            "bess_discharge_load_mwh": round(bess_discharge_load_mwh, 4),
            "bess_discharge_grid_mwh": round(bess_discharge_grid_mwh, 4),
            "bess_capacity_mwh": round(e_cap_kwh / 1000.0, 4),
            "bess_max_cycles_per_year_theoretical": round(max_cycles_year, 2),
            "bess_actual_cycles_year1": round(actual_cycles_year1, 2),
            "bess_utilization_pct": round(utilisation_pct, 1),
        }

    # ---------------------------------------------------------------------
    # Balancing market KPIs (FCR / aFRR / mFRR).
    # Always emitted, with zero values when the master switch is off so
    # downstream code (plotting, lifetime, economics) can read the keys
    # unconditionally.
    # ---------------------------------------------------------------------
    kpis.update(_compute_balancing_kpis(res, params))

    # ---------------------------------------------------------------------
    # Canonical revenue aggregates for the financial-plot stack.  These
    # split DAM revenue between PV-direct exports and BESS-arbitrage
    # exports, and aggregate each balancing product's capacity +
    # activation streams into a single per-product key.  Always emitted.
    # ---------------------------------------------------------------------
    kpis.update(_compute_canonical_revenue_aggregates(kpis, mode))

    return kpis


def _compute_canonical_revenue_aggregates(
    kpis: dict[str, Any], mode: str,
) -> dict[str, float]:
    """Return 8 of the 9 canonical revenue aggregate keys used by the
    financial-plot stack and the BESS-revenue waterfall / split plots
    (the ninth, ``revenue_pv_ppa_eur``, is summed directly from its
    per-step column in :func:`compute_kpis`).

    * ``revenue_pv_dam_eur``        — PV → DAM exports (under a
      physical PPA: the uncovered share only).
    * ``revenue_bess_dam_eur``      — BESS-DAM arbitrage net of the
      grid-charging expense.
    * ``revenue_self_consumption_eur`` — load coverage from PV-direct
      and BESS-discharge; 0 in merchant mode.
    * ``revenue_bess_fcr_eur``      — FCR capacity payment.
    * ``revenue_bess_afrr_up_eur``  — aFRR-up capacity + activation.
    * ``revenue_bess_afrr_dn_eur``  — aFRR-dn capacity + activation.
    * ``revenue_bess_mfrr_up_eur``  — mFRR-up capacity + activation.
    * ``revenue_bess_mfrr_dn_eur``  — mFRR-dn capacity + activation.

    CONTRACT -- two parallel revenue-key families:

    Per-product balancing raws (written by
    :func:`_compute_balancing_kpis`):
        ``bm_<product>_capacity_revenue_eur``
        ``bm_<product>_activation_revenue_eur``

    Per-product canonical aggregates (written here, sum of the two
    raws above for every product that earns activation; capacity-only
    for FCR):
        ``revenue_bess_<product>_eur``

    Top-level balancing totals (written by
    :func:`_compute_balancing_kpis`):
        ``bm_total_capacity_revenue_eur``
        ``bm_total_activation_revenue_eur``
        ``bm_total_balancing_revenue_eur``

    Consumers:
        * :func:`pvbess_opt.plotting.bess_revenue.plot_bess_revenue_waterfall`
          reads the per-product canonical aggregates.
        * :func:`pvbess_opt.plotting.lifecycle.plot_revenue_stack_yearly`
          reads the per-product canonical aggregates.
        * :func:`pvbess_opt.economics.build_yearly_cashflow`
          reads ``bm_total_capacity_revenue_eur`` and
          ``bm_total_activation_revenue_eur``.

    :func:`pvbess_opt.availability.apply_unavailability_derate` scales
    every key in both families by the same availability factor so the
    aggregates and their components stay in lockstep.
    """
    rev_pv_dam = float(kpis.get("profit_export_from_pv_eur", 0.0) or 0.0)
    rev_bess_dam = (
        float(kpis.get("profit_export_from_bess_eur", 0.0) or 0.0)
        - float(kpis.get("expense_charge_bess_grid_eur", 0.0) or 0.0)
    )
    if mode == "self_consumption":
        rev_self = (
            float(kpis.get("profit_load_from_pv_eur", 0.0) or 0.0)
            + float(kpis.get("profit_load_from_bess_eur", 0.0) or 0.0)
        )
    else:
        rev_self = 0.0

    def _bm(product: str, with_activation: bool) -> float:
        cap = float(kpis.get(f"bm_{product}_capacity_revenue_eur", 0.0) or 0.0)
        act = (
            float(kpis.get(f"bm_{product}_activation_revenue_eur", 0.0) or 0.0)
            if with_activation else 0.0
        )
        return cap + act

    return {
        "revenue_pv_dam_eur": round(rev_pv_dam, 2),
        "revenue_bess_dam_eur": round(rev_bess_dam, 2),
        "revenue_self_consumption_eur": round(rev_self, 2),
        "revenue_bess_fcr_eur": round(_bm("fcr", False), 2),
        "revenue_bess_afrr_up_eur": round(_bm("afrr_up", True), 2),
        "revenue_bess_afrr_dn_eur": round(_bm("afrr_dn", True), 2),
        "revenue_bess_mfrr_up_eur": round(_bm("mfrr_up", True), 2),
        "revenue_bess_mfrr_dn_eur": round(_bm("mfrr_dn", True), 2),
    }


def _compute_balancing_kpis(
    res: pd.DataFrame, params: dict[str, Any],
) -> dict[str, Any]:
    """Compute the per-product and aggregate balancing KPIs.

    When the balancing block did not fire (sheet absent / switch off /
    no BESS) every key is set to 0.0 so the dict shape stays stable.

    CONTRACT -- emitted keys:

    Per-product raws (one pair per balancing product):
        ``bm_<product>_capacity_revenue_eur``   (every product)
        ``bm_<product>_activation_revenue_eur`` (products in
        :data:`pvbess_opt.balancing.PRODUCTS_WITH_ACTIVATION`)

    Per-product diagnostics:
        ``bm_reservation_avg_kw_<product>``     (every product)

    Top-level aggregates:
        ``bm_total_capacity_revenue_eur``    = sum of all
        ``bm_<p>_capacity_revenue_eur``.
        ``bm_total_activation_revenue_eur``  = sum of all
        ``bm_<p>_activation_revenue_eur``.
        ``bm_total_balancing_revenue_eur``   = capacity total +
        activation total.
        ``bm_expected_activation_energy_up_kwh`` /
        ``bm_expected_activation_energy_dn_kwh``  — deterministic
        expected-activation throughput.
        ``bm_revenue_share_pct``             — share of total
        revenue contributed by balancing.

    The canonical per-product aggregates ``revenue_bess_<product>_eur``
    are written by :func:`_compute_canonical_revenue_aggregates` from
    the raws above.  See its CONTRACT block for the full key map.
    """

    out: dict[str, Any] = {}
    for product in PRODUCTS_ALL:
        out[f"bm_{product}_capacity_revenue_eur"] = 0.0
        out[f"bm_reservation_avg_kw_{product}"] = 0.0
    for product in PRODUCTS_WITH_ACTIVATION:
        out[f"bm_{product}_activation_revenue_eur"] = 0.0
    out["bm_total_capacity_revenue_eur"] = 0.0
    out["bm_total_activation_revenue_eur"] = 0.0
    out["bm_total_balancing_revenue_eur"] = 0.0
    out["bm_expected_activation_energy_up_kwh"] = 0.0
    out["bm_expected_activation_energy_dn_kwh"] = 0.0
    out["bm_revenue_share_pct"] = 0.0

    raw_cfg = params.get("balancing") or {}
    cfg = resolve_balancing_config(raw_cfg)
    if not cfg.balancing_enabled:
        return out

    res_columns_have_reservations = all(
        f"bm_reservation_{p}_kw" in res.columns for p in PRODUCTS_ALL
    )
    if not res_columns_have_reservations:
        # Switch was on but the dispatch frame does not carry the
        # reservation columns (e.g. a BESS-absent run). Leave zeros.
        return out

    dt_h = dt_hours_from(params)
    if dt_h <= 0.0:
        return out

    total_capacity = 0.0
    total_activation = 0.0
    # Note: reservation columns come from the rounded dispatch frame
    # (model_to_dataframe(round_output=True) rounds to 4 dp), so
    # sub-0.5 mW reservations are zero here.  See the rounding section
    # of pvbess_opt/conventions.md for the full contract.
    for product in PRODUCTS_ALL:
        r_kw = res[f"bm_reservation_{product}_kw"].to_numpy(dtype=float)
        alpha = acceptance_probability(cfg, product)
        cap_col = f"{product}_capacity_price_eur_per_mwh"
        if cap_col in res.columns:
            cap_price = res[cap_col].to_numpy(dtype=float)
            cap_rev = float(
                (alpha * dt_h / 1000.0) * float((cap_price * r_kw).sum())
            )
        else:
            cap_rev = 0.0
        out[f"bm_{product}_capacity_revenue_eur"] = round(cap_rev, 2)
        out[f"bm_reservation_avg_kw_{product}"] = round(float(r_kw.mean()), 4)
        total_capacity += cap_rev

    for product in PRODUCTS_WITH_ACTIVATION:
        r_kw = res[f"bm_reservation_{product}_kw"].to_numpy(dtype=float)
        alpha = acceptance_probability(cfg, product)
        beta = activation_probability(cfg, product)
        act_col = f"{product}_activation_price_eur_per_mwh"
        if act_col in res.columns:
            act_price = res[act_col].to_numpy(dtype=float)
            act_rev = float(
                (alpha * beta * dt_h / 1000.0) * float((act_price * r_kw).sum())
            )
        else:
            act_rev = 0.0
        out[f"bm_{product}_activation_revenue_eur"] = round(act_rev, 2)
        total_activation += act_rev

    total = total_capacity + total_activation
    out["bm_total_capacity_revenue_eur"] = round(total_capacity, 2)
    out["bm_total_activation_revenue_eur"] = round(total_activation, 2)
    out["bm_total_balancing_revenue_eur"] = round(total, 2)

    # Expected activation energies (kWh) — the deterministic drift that
    # the MILP added to the SOC recursion.
    eta_c = float(params.get("efficiency_charge", 1.0) or 1.0)
    eta_d = float(params.get("efficiency_discharge", 1.0) or 1.0)
    e_act_up = 0.0
    e_act_dn = 0.0
    for product in PRODUCTS_UP:
        r_kw = res[f"bm_reservation_{product}_kw"].to_numpy(dtype=float)
        alpha = acceptance_probability(cfg, product)
        beta = activation_probability(cfg, product)
        e_act_up += float(alpha * beta * dt_h / eta_d * r_kw.sum())
    for product in PRODUCTS_DN:
        r_kw = res[f"bm_reservation_{product}_kw"].to_numpy(dtype=float)
        alpha = acceptance_probability(cfg, product)
        beta = activation_probability(cfg, product)
        e_act_dn += float(alpha * beta * dt_h * eta_c * r_kw.sum())
    out["bm_expected_activation_energy_up_kwh"] = round(e_act_up, 4)
    out["bm_expected_activation_energy_dn_kwh"] = round(e_act_dn, 4)

    # Denominator for bm_revenue_share_pct: every non-balancing revenue
    # stream the project earns (retail-load coverage + DAM exports net of
    # grid-charging expense) plus total balancing revenue.  The name
    # "non-balancing" makes explicit that this is NOT just DAM exports --
    # the retail-load coverage from PV-direct and BESS-discharge is the
    # bulk of it in any self-consumption project.  Used downstream to
    # report what fraction of total revenue the balancing block
    # contributes.
    non_balancing_revenue_eur = (
        float(res.get("profit_load_from_pv_eur", pd.Series(0.0)).sum())
        + float(res.get("profit_load_from_bess_eur", pd.Series(0.0)).sum())
        + float(res.get("profit_export_from_pv_eur", pd.Series(0.0)).sum())
        + float(res.get("profit_export_from_bess_eur", pd.Series(0.0)).sum())
        - float(res.get("expense_charge_bess_grid_eur", pd.Series(0.0)).sum())
    )
    # The construction is safe today because balancing revenue does NOT
    # enter ``profit_*_eur`` (those columns are driven by DAM / retail
    # only).  If a future change folds balancing into profit_total_eur,
    # the denominator would double-count balancing -- guard regression
    # is tests/test_balancing_kpi_denominator_non_overlap.py.
    denom = non_balancing_revenue_eur + total
    if abs(denom) > 1e-9:
        out["bm_revenue_share_pct"] = round(100.0 * total / denom, 4)
    return out


# ---------------------------------------------------------------------------
# Monthly KPI roll-up
# ---------------------------------------------------------------------------


def compute_monthly_kpis(res: pd.DataFrame) -> pd.DataFrame:
    """Compute monthly self-consumption / coverage ratios."""
    if not pd.api.types.is_datetime64_any_dtype(res["timestamp"]):
        return pd.DataFrame()

    month_key = pd.to_datetime(res["timestamp"]).dt.to_period("M")
    grouped_kwh = res.groupby(month_key).agg({
        "pv_kwh": "sum",
        "load_kwh": "sum",
        "pv_to_load_kwh": "sum",
        "bess_dis_load_kwh": "sum",
        "bess_dis_load_green_kwh": "sum",
        "pv_to_bess_kwh": "sum",
    })
    grouped_mwh = grouped_kwh / 1000.0
    bess_green_load_mwh = grouped_kwh["bess_dis_load_green_kwh"].to_numpy() / 1000.0
    pv_mwh = grouped_mwh["pv_kwh"].to_numpy()
    user_mwh = grouped_mwh["load_kwh"].to_numpy()
    pv_load_mwh = grouped_mwh["pv_to_load_kwh"].to_numpy()
    bess_load_mwh = grouped_mwh["bess_dis_load_kwh"].to_numpy()

    def _ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            out = np.where(np.abs(den) > 1e-9, num / den, 0.0)
        return out

    df = pd.DataFrame(
        {
            "pv_direct_self_consumption_frac": _ratio(pv_load_mwh, pv_mwh),
            "bess_from_pv_self_consumption_frac": _ratio(
                bess_green_load_mwh, pv_mwh,
            ),
            "system_pv_self_consumption_frac": _ratio(
                pv_load_mwh + bess_green_load_mwh, pv_mwh,
            ),
            "load_coverage_from_pv_frac": _ratio(pv_load_mwh, user_mwh),
            "load_coverage_from_bess_frac": _ratio(bess_green_load_mwh, user_mwh),
            "load_coverage_from_bess_total_frac": _ratio(bess_load_mwh, user_mwh),
            "system_load_green_coverage_frac": _ratio(
                pv_load_mwh + bess_green_load_mwh, user_mwh,
            ),
        },
        index=grouped_mwh.index.astype(str),
    ).round(4)

    return df
