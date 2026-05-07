"""KPI calculations and energy-balance verification.

Energy-flow conventions (all per timestep, kWh):

    PV split:
        pv_kwh = pv_to_load_kwh + pv_to_bess_kwh
              + pv_to_grid_kwh + pv_curtail_kwh
    Load balance (vnb only):
        load_kwh = pv_to_load_kwh + bess_dis_load_kwh + grid_to_load_kwh
    BESS state-of-charge dynamics:
        soc_kwh[t+1] - soc_kwh[t] =
            efficiency_charge * (pv_to_bess_kwh + bess_charge_grid_kwh)
          - (bess_dis_load_kwh + bess_dis_grid_kwh) / efficiency_discharge
    Grid export (subject to export-cap constraint):
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

logger = logging.getLogger(__name__)

ENERGY_TOLERANCE: float = 1.0e-3  # kWh per timestep


# ---------------------------------------------------------------------------
# Energy-flow verification
# ---------------------------------------------------------------------------


def verify_energy_balance(
    res: pd.DataFrame,
    params: dict[str, Any],
    *,
    raise_on_failure: bool = False,
) -> dict[str, float]:
    """Verify the per-step energy balances against the dispatch DataFrame."""
    mode = str(params.get("mode", "vnb") or "vnb").lower()

    pv_residual = np.abs(
        res["pv_kwh"].to_numpy(dtype=float)
        - (
            res["pv_to_load_kwh"].to_numpy(dtype=float)
            + res["pv_to_bess_kwh"].to_numpy(dtype=float)
            + res["pv_to_grid_kwh"].to_numpy(dtype=float)
            + res["pv_curtail_kwh"].to_numpy(dtype=float)
        )
    )
    if mode == "vnb":
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
    * ``profit_export_from_pv_eur``      — DAM × pv_to_grid / 1000.
    * ``profit_export_from_bess_eur``    — DAM × bess_dis_grid / 1000.
    * ``expense_charge_bess_grid_eur``   — DAM × bess_charge_grid / 1000.
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
    return res


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
    e_cap_kwh: float,
    *,
    verify_balance: bool = True,
) -> dict[str, Any]:
    """Compute the headline KPI dictionary for a solved scenario."""
    if verify_balance:
        verify_energy_balance(res, params, raise_on_failure=False)
    attribute_green_discharge(res, params)
    add_economic_columns(res, params)

    mode = str(params.get("mode", "vnb") or "vnb").lower()

    pv_gen = _sum_mwh(res, "pv_kwh")
    load_en = _sum_mwh(res, "load_kwh") if mode == "vnb" else 0.0
    pv_direct = _sum_mwh(res, "pv_to_load_kwh") if mode == "vnb" else 0.0
    bess_to_load = _sum_mwh(res, "bess_dis_load_kwh") if mode == "vnb" else 0.0
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

    if mode == "vnb":
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
    profit_total = (
        profit_load_pv + profit_load_bess + profit_export_pv + profit_export_bess
        - expense_charge_grid
    )

    initial_soc_pct = params["initial_soc_frac"] * 100.0

    kpis: dict[str, Any] = {
        "mode": mode,
        "allow_bess_grid_charging": bool(params.get("allow_bess_grid_charging", False)),
        "e_cap_opt_mwh": round(e_cap_kwh / 1000.0, 4),
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
        "profit_total_eur": round(profit_total, 2),
    }

    return kpis


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
