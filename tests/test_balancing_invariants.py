"""The six balancing-market dispatch invariants (INV-B1..INV-B6)."""

from __future__ import annotations

import pytest

from pvbess_opt.balancing import (
    PRODUCTS_ALL,
    capacity_share_kw,
    resolve_balancing_config,
)
from pvbess_opt.io import BALANCING_SHEET_DEFAULTS, _validate_balancing_config
from pvbess_opt.optimization import run_scenario, verify_dispatch_invariants


def _balancing_on(params: dict, **overrides) -> dict:
    out = dict(params)
    bm = dict(BALANCING_SHEET_DEFAULTS, balancing_enabled=True)
    bm["bm_settlement_minutes"] = int(out.get("dt_minutes", 60))
    bm.update(overrides)
    out["balancing"] = bm
    return out


def test_invb1_sum_of_shares_capped_at_100_pct():
    bm = dict(BALANCING_SHEET_DEFAULTS, balancing_enabled=True)
    bm["bm_settlement_minutes"] = 15
    total = sum(
        bm[k] for k in (
            "dam_capacity_share_pct",
            "fcr_capacity_share_pct",
            "afrr_up_capacity_share_pct",
            "afrr_dn_capacity_share_pct",
            "mfrr_up_capacity_share_pct",
            "mfrr_dn_capacity_share_pct",
        )
    )
    assert total <= 100.0
    bm["dam_capacity_share_pct"] = 90.0
    bm["fcr_capacity_share_pct"] = 20.0
    with pytest.raises(ValueError, match="exceeds 100 %"):
        _validate_balancing_config(bm, dt_minutes=15)


def test_invb2_reservation_below_share_cap(short_params, short_ts):
    p_on = _balancing_on(short_params)
    res, _ = run_scenario(p_on, short_ts)
    cfg = resolve_balancing_config(p_on["balancing"])
    for product in PRODUCTS_ALL:
        cap = capacity_share_kw(cfg, product, short_params["bess_power_kw"])
        assert (res[f"bm_reservation_{product}_kw"] <= cap + 1e-6).all()


def test_invb3_soc_headroom_up_holds(short_params, short_ts):
    p_on = _balancing_on(short_params)
    res, _ = run_scenario(p_on, short_ts)
    cfg = resolve_balancing_config(p_on["balancing"])
    dt_h = short_params["dt_minutes"] / 60.0
    eta_d = short_params["efficiency_discharge"]
    h_buf = cfg.bm_soc_headroom_pct / 100.0
    d_fcr = cfg.fcr_required_duration_hours
    soc_min = short_params["soc_min_frac"] * short_params["bess_capacity_kwh"]
    headroom_required = (
        (1.0 + h_buf) * dt_h * (
            res["bm_reservation_afrr_up_kw"]
            + res["bm_reservation_mfrr_up_kw"]
        ) / eta_d
        + (1.0 + h_buf) * d_fcr * res["bm_reservation_fcr_kw"] / eta_d
    )
    assert (res["soc_kwh"] - soc_min >= headroom_required - 1e-3).all()


def test_invb4_soc_headroom_dn_holds(short_params, short_ts):
    p_on = _balancing_on(short_params)
    res, _ = run_scenario(p_on, short_ts)
    cfg = resolve_balancing_config(p_on["balancing"])
    dt_h = short_params["dt_minutes"] / 60.0
    eta_c = short_params["efficiency_charge"]
    h_buf = cfg.bm_soc_headroom_pct / 100.0
    d_fcr = cfg.fcr_required_duration_hours
    soc_max = short_params["soc_max_frac"] * short_params["bess_capacity_kwh"]
    headroom_required = (
        (1.0 + h_buf) * dt_h * (
            res["bm_reservation_afrr_dn_kw"]
            + res["bm_reservation_mfrr_dn_kw"]
        ) * eta_c
        + (1.0 + h_buf) * d_fcr * res["bm_reservation_fcr_kw"] * eta_c
    )
    assert (soc_max - res["soc_kwh"] >= headroom_required - 1e-3).all()


def test_invb5_power_budget_per_direction(short_params, short_ts):
    p_on = _balancing_on(short_params)
    res, _ = run_scenario(p_on, short_ts)
    dt_h = short_params["dt_minutes"] / 60.0
    p_bess = short_params["bess_power_kw"]
    up_share = (
        res["bm_reservation_fcr_kw"]
        + res["bm_reservation_afrr_up_kw"]
        + res["bm_reservation_mfrr_up_kw"]
    )
    dn_share = (
        res["bm_reservation_fcr_kw"]
        + res["bm_reservation_afrr_dn_kw"]
        + res["bm_reservation_mfrr_dn_kw"]
    )
    lhs_up = (
        res["bess_dis_load_kwh"] + res["bess_dis_grid_kwh"]
        + up_share * dt_h
    )
    lhs_dn = (
        res["pv_to_bess_kwh"] + res["bess_charge_grid_kwh"]
        + dn_share * dt_h
    )
    assert (lhs_up <= p_bess * dt_h + 1e-4).all()
    assert (lhs_dn <= p_bess * dt_h + 1e-4).all()


def test_invb6_off_preserves_existing_invariants(short_params, short_ts):
    """When balancing is OFF the previous 9 dispatch invariants must
    hold with the same residuals as before the feature landed."""
    res, _ = run_scenario(short_params, short_ts)
    inv = verify_dispatch_invariants(res, short_params)
    for key, value in inv.items():
        # The existing test suite enforces 1e-3 kWh tolerance for the
        # numeric invariants; the priority counts are integer-valued.
        assert value <= 1e-3, f"{key} = {value:g}"
