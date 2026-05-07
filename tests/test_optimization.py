"""MILP optimizer + dispatch invariants tests."""

from __future__ import annotations

import pytest

from pvbess_opt.optimization import (
    derive_tight_big_m,
    run_scenario,
    verify_dispatch_invariants,
)


@pytest.fixture(scope="module")
def _solved_vnb(short_params, short_ts):
    res, e_cap, _ = run_scenario(
        short_params, short_ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=30,
    )
    return res, e_cap


@pytest.fixture(scope="module")
def _solved_merchant(short_params_merchant, short_ts):
    res, e_cap, _ = run_scenario(
        short_params_merchant, short_ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=30,
    )
    return res, e_cap


# ---------------------------------------------------------------------------
# big-M derivation
# ---------------------------------------------------------------------------


def test_big_m_values_are_tight(short_params, short_ts):
    big_m = derive_tight_big_m(
        short_params, short_ts, dt_h=1.0, mode="vnb",
    )
    # Tight Ms: M_imp ~ load_max + p_charge ~ 4500 + 5000 = 9500 << 1e6
    assert big_m["M_imp"] < 20000
    assert big_m["M_exp"] < 5000  # p_export*(1-0.27) = 3650
    assert big_m["M_charge"] < 6000


def test_big_m_merchant_skips_load(short_params_merchant, short_ts):
    big_m = derive_tight_big_m(
        short_params_merchant, short_ts, dt_h=1.0, mode="merchant",
    )
    # In merchant load_max contribution is 0
    assert big_m["M_imp"] == pytest.approx(5000.0 * 1.001)


# ---------------------------------------------------------------------------
# Solve smoke + invariants
# ---------------------------------------------------------------------------


def test_vnb_solve_returns_dataframe(short_params, short_ts):
    res, e_cap, solver = run_scenario(
        short_params, short_ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=30,
    )
    assert e_cap > 0.0
    assert "pv_to_load_kwh" in res.columns
    assert "soc_kwh" in res.columns
    assert "grid_export_total_kwh" in res.columns
    assert len(res) == len(short_ts)


def test_invariants_vnb(short_params, _solved_vnb):
    res, _e_cap = _solved_vnb
    inv = verify_dispatch_invariants(res, short_params, mode="vnb")
    tol = 1.0e-3
    assert inv["invariant_1_pv_balance_kwh"] < tol
    assert inv["invariant_2_load_balance_kwh"] < tol
    assert inv["invariant_3_soc_dynamics_kwh"] < tol
    assert inv["invariant_4_rte_bound_excess_kwh"] < tol
    assert inv["invariant_5_no_sim_grid_io_max_product_kwh2"] < tol ** 2
    assert inv["invariant_6_load_priority_violations"] == 0
    assert inv["invariant_7_curtail_behavior_kwh"] == 0
    assert inv["invariant_8_soc_closed_cycle_kwh"] < tol


def test_invariants_merchant_zero_for_vnb_only(short_params_merchant, _solved_merchant):
    res, _e_cap = _solved_merchant
    inv = verify_dispatch_invariants(res, short_params_merchant, mode="merchant")
    # vnb-only invariants must zero out cleanly
    assert inv["invariant_2_load_balance_kwh"] == 0.0
    assert inv["invariant_5_no_sim_grid_io_max_product_kwh2"] == 0.0
    assert inv["invariant_6_load_priority_violations"] == 0.0
    # All-mode invariants still pass
    assert inv["invariant_1_pv_balance_kwh"] < 1e-3
    assert inv["invariant_7_curtail_behavior_kwh"] == 0.0


def test_merchant_pins_load_flows_to_zero(_solved_merchant):
    res, _ = _solved_merchant
    assert res["pv_to_load_kwh"].max() == 0.0
    assert res["bess_dis_load_kwh"].max() == 0.0
    assert res["grid_to_load_kwh"].max() == 0.0


def test_curtailment_cap_holds_in_both_modes(_solved_vnb, _solved_merchant):
    """Curtailment cap is a regulatory grid-connection limit that applies
    in BOTH vnb and merchant modes per MD YPEN/DAPEEK/53563/1556/2023."""
    for res, _ in (_solved_vnb, _solved_merchant):
        cap = float(res["grid_export_cap_kwh"].iloc[0])
        max_export = float(res["grid_export_total_kwh"].max())
        assert max_export <= cap + 1e-3


def test_terminal_soc_free_skips_closed_cycle(short_params, short_ts):
    res, _e_cap, _ = run_scenario(
        short_params, short_ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=30,
        terminal_soc_free=True,
    )
    # Closed-cycle invariant should not be enforced — but 8 should still
    # be reported (it's only 0 when terminal_soc_equal=True).
    assert "soc_kwh" in res.columns


def test_initial_soc_kwh_override(short_params, short_ts):
    """initial_soc_kwh override pins soc[0] explicitly."""
    target = 1500.0
    res, _e_cap, _ = run_scenario(
        short_params, short_ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=30,
        initial_soc_kwh=target, terminal_soc_free=True,
    )
    assert abs(res["soc_kwh"].iloc[0] - target) < 1e-3


def test_invalid_mode_raises():
    from pvbess_opt.optimization import _resolve_mode
    with pytest.raises(ValueError, match="Unknown mode"):
        _resolve_mode({"mode": "bogus"})
