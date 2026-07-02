"""MILP optimizer + dispatch invariants tests."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.optimization import (
    build_model,
    derive_tight_big_m,
    run_scenario,
    verify_dispatch_invariants,
)


def test_build_model_self_consumption_requires_load_column(short_params, short_ts):
    """A self_consumption build with no ``load_kwh`` column must raise,
    not silently optimise against zero load — the contract stated in
    docs/self_consumption_design.md (the workbook loader raises the same
    error earlier in io._normalise_timeseries)."""
    ts_no_load = short_ts.drop(columns=["load_kwh"])
    with pytest.raises(ValueError, match="load_kwh"):
        build_model(short_params, ts_no_load)


@pytest.fixture(scope="module")
def _solved_self_consumption(short_params, short_ts):
    res, _solver = run_scenario(
        short_params, short_ts, solver_name="highs",
        mip_gap=0.001, time_limit_seconds=60,
    )
    return res


@pytest.fixture(scope="module")
def _solved_merchant(short_params_merchant, short_ts):
    res, _solver = run_scenario(
        short_params_merchant, short_ts, solver_name="highs",
        mip_gap=0.001, time_limit_seconds=60,
    )
    return res


# ---------------------------------------------------------------------------
# big-M derivation
# ---------------------------------------------------------------------------


def test_big_m_values_are_tight(short_params, short_ts):
    # The short_params fixture omits max_injection_profile so the
    # resolver's no-cap fallback is 1.0; supply an explicit 73 % cap
    # so M_exp can be tight.
    params = dict(short_params)
    params["max_injection_profile"] = np.full(24, 73.0, dtype=float)
    big_m = derive_tight_big_m(
        params, short_ts, dt_h=1.0, mode="self_consumption",
    )
    # Tight Ms: M_imp ~ load_max + bess_power ~ 4500 + 5000 = 9500 << 1e6
    assert big_m["M_imp"] < 20000
    assert big_m["M_exp"] < 5000  # p_export * 0.73 = 3650
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


def test_self_consumption_solve_returns_dataframe(short_params, short_ts):
    res, _solver = run_scenario(
        short_params, short_ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=30,
    )
    assert "pv_to_load_kwh" in res.columns
    assert "soc_kwh" in res.columns
    assert "grid_export_total_kwh" in res.columns
    assert len(res) == len(short_ts)


def test_invariants_self_consumption(short_params, _solved_self_consumption):
    inv = verify_dispatch_invariants(
        _solved_self_consumption, short_params, mode="self_consumption",
    )
    tol = 1.0e-3
    assert inv["invariant_1_pv_balance_kwh"] < tol
    assert inv["invariant_2_load_balance_kwh"] < tol
    assert inv["invariant_3_soc_dynamics_kwh"] < tol
    assert inv["invariant_4_rte_bound_excess_kwh"] < tol
    assert inv["invariant_5_no_sim_grid_io_max_product_kwh2"] < tol ** 2
    assert inv["invariant_6_load_priority_violations"] == 0
    assert inv["invariant_7_curtail_behavior_count"] == 0
    assert inv["invariant_8_soc_closed_cycle_kwh"] < tol
    assert inv["invariant_9_pv_load_priority_kwh"] < tol


def test_invariants_merchant_zero_for_self_consumption_only(
    short_params_merchant, _solved_merchant,
):
    inv = verify_dispatch_invariants(
        _solved_merchant, short_params_merchant, mode="merchant",
    )
    assert inv["invariant_2_load_balance_kwh"] == 0.0
    assert inv["invariant_5_no_sim_grid_io_max_product_kwh2"] == 0.0
    assert inv["invariant_6_load_priority_violations"] == 0.0
    assert inv["invariant_9_pv_load_priority_kwh"] == 0.0
    assert inv["invariant_1_pv_balance_kwh"] < 1e-3
    assert inv["invariant_7_curtail_behavior_count"] == 0.0


def test_merchant_pins_load_flows_to_zero(_solved_merchant):
    res = _solved_merchant
    assert res["pv_to_load_kwh"].max() == 0.0
    assert res["bess_dis_load_kwh"].max() == 0.0
    assert res["grid_to_load_kwh"].max() == 0.0


def test_curtailment_cap_holds_in_both_modes(_solved_self_consumption, _solved_merchant):
    """Curtailment cap is a regulatory grid-connection limit that applies
    in BOTH self_consumption and merchant modes per MD YPEN/DAPEEK/53563/1556/2023."""
    for res in (_solved_self_consumption, _solved_merchant):
        cap = float(res["grid_export_cap_kwh"].iloc[0])
        max_export = float(res["grid_export_total_kwh"].max())
        assert max_export <= cap + 1e-3


def test_terminal_soc_free_skips_closed_cycle(short_params, short_ts):
    res, _ = run_scenario(
        short_params, short_ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=30,
        terminal_soc_free=True,
    )
    assert "soc_kwh" in res.columns


def test_initial_soc_kwh_override(short_params, short_ts):
    """initial_soc_kwh override pins soc[0] explicitly.

    Target must lie inside [soc_min_frac, soc_max_frac] * bess_capacity_kwh.
    With the conftest defaults (capacity 20 MWh, min 20 %, max 95 %) the
    feasible band is [4 000, 19 000] kWh.
    """
    target = 8000.0
    res, _ = run_scenario(
        short_params, short_ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=30,
        initial_soc_kwh=target, terminal_soc_free=True,
    )
    assert abs(res["soc_kwh"].iloc[0] - target) < 1e-3


def test_invalid_mode_raises():
    from pvbess_opt.modes import resolve_mode
    with pytest.raises(ValueError, match="Unknown mode"):
        resolve_mode({"mode": "bogus"})


# ---------------------------------------------------------------------------
# Spec rule coverage — Sections 2, 4, 6
# ---------------------------------------------------------------------------


def test_pv_priority_over_bess_for_load(short_params):
    """Section 2: pv_to_load[t] == min(pv[t], load[t]) exactly."""
    n = 24
    timestamps = pd.date_range("2026-06-01 00:00", periods=n, freq="h")
    pv = np.zeros(n, dtype=float)
    pv[:12] = 1000.0
    load = np.full(n, 1000.0, dtype=float)
    dam = np.full(n, 100.0, dtype=float)
    ts = pd.DataFrame({
        "timestamp": timestamps,
        "pv_kwh": pv,
        "load_kwh": load,
        "dam_price_eur_per_mwh": dam,
    })
    params = dict(short_params)
    params["initial_soc_frac"] = 0.80

    res, _ = run_scenario(
        params, ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=30,
    )

    pv_to_load = res["pv_to_load_kwh"].to_numpy()
    pv_arr = res["pv_kwh"].to_numpy()
    load_arr = res["load_kwh"].to_numpy()
    expected = np.minimum(pv_arr, load_arr)
    diffs = np.abs(pv_to_load - expected)
    assert diffs.max() < 1e-3
    assert float(res["bess_dis_load_kwh"].iloc[:12].sum()) < 1e-3


def test_no_charge_discharge_simultaneity(_solved_self_consumption):
    """Section 4: y_charge[t] + y_dis[t] <= 1 ⇒ products zero."""
    res = _solved_self_consumption
    pv_to_bess = res["pv_to_bess_kwh"].to_numpy()
    grid_to_bess = res["bess_charge_grid_kwh"].to_numpy()
    bess_dis_load = res["bess_dis_load_kwh"].to_numpy()
    bess_dis_grid = res["bess_dis_grid_kwh"].to_numpy()
    assert ((pv_to_bess > 1e-3) & (bess_dis_grid > 1e-3)).sum() == 0
    charge = pv_to_bess + grid_to_bess
    discharge = bess_dis_load + bess_dis_grid
    assert ((charge > 1e-3) & (discharge > 1e-3)).sum() == 0


def test_grid_charge_only_when_pv_zero(short_params, short_ts):
    """Section 6: grid_to_bess > 0 ⇒ pv ≈ 0 in the same step."""
    params = dict(short_params)
    params["allow_bess_grid_charging"] = True
    res, _ = run_scenario(
        params, short_ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=30,
    )
    pv = res["pv_kwh"].to_numpy()
    grid_to_bess = res["bess_charge_grid_kwh"].to_numpy()
    assert ((pv > 1e-3) & (grid_to_bess > 1e-3)).sum() == 0
