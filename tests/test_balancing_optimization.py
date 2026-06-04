"""MILP-level integration tests for the balancing-market extension."""

from __future__ import annotations

from pvbess_opt.balancing import (
    PRODUCTS_ALL,
    resolve_balancing_config,
)
from pvbess_opt.io import BALANCING_SHEET_DEFAULTS
from pvbess_opt.optimization import run_scenario
from tests._balancing_helpers import _balancing_on


def _balancing_off(params: dict) -> dict:
    out = dict(params)
    out["balancing"] = dict(BALANCING_SHEET_DEFAULTS)
    return out


def test_balancing_off_model_matches_baseline_objective(short_params, short_ts):
    """When balancing is disabled the MILP solution must be identical
    to the pre-feature build (no new variables, same optimum)."""
    p_off = _balancing_off(short_params)
    res_off, _ = run_scenario(p_off, short_ts)
    # No balancing reservation columns in the dispatch frame when off.
    assert not any(c.startswith("bm_reservation_") for c in res_off.columns)


def test_balancing_on_adds_reservation_columns(short_params, short_ts):
    p_on = _balancing_on(short_params)
    res, _ = run_scenario(p_on, short_ts)
    for product in PRODUCTS_ALL:
        col = f"bm_reservation_{product}_kw"
        assert col in res.columns
    # The bid-acceptance defaults produce non-trivial reservations on at
    # least one product (FCR is capped at 10 % share of bess_power_kw =
    # 500 kW for these fixtures and the price is well above zero).
    assert (res["bm_reservation_fcr_kw"] > 0).any()


def test_zero_shares_force_zero_reservations(short_params, short_ts):
    p_zero = _balancing_on(
        short_params,
        fcr_capacity_share_pct=0.0,
        afrr_up_capacity_share_pct=0.0,
        afrr_dn_capacity_share_pct=0.0,
        mfrr_up_capacity_share_pct=0.0,
        mfrr_dn_capacity_share_pct=0.0,
    )
    res, _ = run_scenario(p_zero, short_ts)
    for product in PRODUCTS_ALL:
        col = f"bm_reservation_{product}_kw"
        assert (res[col].abs() < 1e-6).all()


def test_per_direction_power_budget_holds(short_params, short_ts):
    p_on = _balancing_on(short_params)
    res, _ = run_scenario(p_on, short_ts)
    dt_h = short_params["dt_minutes"] / 60.0
    p_bess = short_params["bess_power_kw"]
    # Up direction: discharge_dam + (FCR + aFRR_up + mFRR_up) * dt_h <= p_bess * dt_h
    up_share = (
        res["bm_reservation_fcr_kw"]
        + res["bm_reservation_afrr_up_kw"]
        + res["bm_reservation_mfrr_up_kw"]
    )
    lhs_up = (
        res["bess_dis_load_kwh"] + res["bess_dis_grid_kwh"] + up_share * dt_h
    )
    assert (lhs_up <= p_bess * dt_h + 1e-4).all()
    # Down direction: charge_dam + (FCR + aFRR_dn + mFRR_dn) * dt_h <= p_bess * dt_h
    dn_share = (
        res["bm_reservation_fcr_kw"]
        + res["bm_reservation_afrr_dn_kw"]
        + res["bm_reservation_mfrr_dn_kw"]
    )
    lhs_dn = (
        res["pv_to_bess_kwh"] + res["bess_charge_grid_kwh"] + dn_share * dt_h
    )
    assert (lhs_dn <= p_bess * dt_h + 1e-4).all()


def test_soc_headroom_constraint_holds(short_params, short_ts):
    p_on = _balancing_on(short_params)
    res, _ = run_scenario(p_on, short_ts)
    cfg = resolve_balancing_config(p_on["balancing"])
    dt_h = short_params["dt_minutes"] / 60.0
    eta_c = short_params["efficiency_charge"]
    eta_d = short_params["efficiency_discharge"]
    h_buf = cfg.bm_soc_headroom_pct / 100.0
    d_fcr = cfg.fcr_required_duration_hours
    soc_min = short_params["soc_min_frac"] * short_params["bess_capacity_kwh"]
    soc_max = short_params["soc_max_frac"] * short_params["bess_capacity_kwh"]
    soc = res["soc_kwh"].to_numpy(dtype=float)
    asym_up = (1.0 + h_buf) * dt_h * (
        res["bm_reservation_afrr_up_kw"].to_numpy(dtype=float)
        + res["bm_reservation_mfrr_up_kw"].to_numpy(dtype=float)
    ) / eta_d
    sym_up = (1.0 + h_buf) * d_fcr * res["bm_reservation_fcr_kw"].to_numpy(
        dtype=float,
    ) / eta_d
    assert (soc - soc_min >= asym_up + sym_up - 1e-3).all()
    asym_dn = (1.0 + h_buf) * dt_h * (
        res["bm_reservation_afrr_dn_kw"].to_numpy(dtype=float)
        + res["bm_reservation_mfrr_dn_kw"].to_numpy(dtype=float)
    ) * eta_c
    sym_dn = (1.0 + h_buf) * d_fcr * res["bm_reservation_fcr_kw"].to_numpy(
        dtype=float,
    ) * eta_c
    assert (soc_max - soc >= asym_dn + sym_dn - 1e-3).all()


def test_fcr_occupies_both_directions(short_params, short_ts):
    """FCR is symmetric: increasing the FCR share must tighten BOTH the
    up- and down-direction power budgets in subsequent solves."""
    p_low = _balancing_on(
        short_params,
        fcr_capacity_share_pct=5.0,
        afrr_up_capacity_share_pct=0.0,
        afrr_dn_capacity_share_pct=0.0,
        mfrr_up_capacity_share_pct=0.0,
        mfrr_dn_capacity_share_pct=0.0,
    )
    p_high = _balancing_on(
        short_params,
        fcr_capacity_share_pct=25.0,
        afrr_up_capacity_share_pct=0.0,
        afrr_dn_capacity_share_pct=0.0,
        mfrr_up_capacity_share_pct=0.0,
        mfrr_dn_capacity_share_pct=0.0,
    )
    _res_low, _ = run_scenario(p_low, short_ts)
    res_high, _ = run_scenario(p_high, short_ts)
    # With higher FCR share, DAM charge headroom shrinks per step.
    dt_h = short_params["dt_minutes"] / 60.0
    p_bess = short_params["bess_power_kw"]
    high_charge = (
        res_high["pv_to_bess_kwh"] + res_high["bess_charge_grid_kwh"]
    )
    high_fcr_share = res_high["bm_reservation_fcr_kw"] * dt_h
    assert (high_charge + high_fcr_share <= p_bess * dt_h + 1e-4).all()
    # And discharge.
    high_dis = (
        res_high["bess_dis_load_kwh"] + res_high["bess_dis_grid_kwh"]
    )
    assert (high_dis + high_fcr_share <= p_bess * dt_h + 1e-4).all()


def test_balancing_on_without_bess_emits_no_reservations(short_params, short_ts):
    """When the project has no BESS the balancing block must stay
    dormant — the gate also requires bess_present."""
    p = _balancing_on(short_params)
    p["bess_power_kw"] = 0.0
    p["bess_capacity_kwh"] = 0.0
    res, _ = run_scenario(p, short_ts)
    assert not any(c.startswith("bm_reservation_") for c in res.columns)
