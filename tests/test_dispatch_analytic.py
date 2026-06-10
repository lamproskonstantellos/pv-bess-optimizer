"""Analytically solvable dispatch scenarios — solver vs by-hand optimum.

Each scenario is small enough that the profit-maximising dispatch can be
derived with pencil and paper; the tests assert the MILP reproduces it
exactly (within solver tolerance).  These lock the economics of the
objective, the efficiency placement in the SOC recursion, the export
cap, negative-price handling, charge/discharge mutual exclusion, the
big-M "unlimited export" substitution, and determinism.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.kpis import compute_kpis
from pvbess_opt.optimization import run_scenario, verify_dispatch_invariants

pytest.importorskip("highspy")

_OPTS = {"solver_name": "highs", "mip_gap": 1e-6, "time_limit_seconds": 60}


def _params(**overrides) -> dict:
    base = {
        "dt_minutes": 60,
        "efficiency_charge": 0.9,
        "efficiency_discharge": 0.9,
        "soc_min_frac": 0.0,
        "soc_max_frac": 1.0,
        "initial_soc_frac": 0.0,
        "terminal_soc_equal": True,
        "max_cycles_per_day": 1.0,
        "p_grid_export_max_kw": 100000.0,
        "pv_nameplate_kwp": 0.0,
        "bess_power_kw": 1000.0,
        "bess_capacity_kwh": 2000.0,
        "retail_tariff_eur_per_mwh": 120.0,
        "settlement_minutes": 60,
        "mode": "merchant",
        "allow_bess_grid_charging": True,
        "show_titles": False,
    }
    base.update(overrides)
    return base


def _ts(prices, pv=None, load=None) -> pd.DataFrame:
    n = len(prices)
    out = {
        "timestamp": pd.date_range("2026-06-01", periods=n, freq="h"),
        "pv_kwh": np.asarray(pv if pv is not None else np.zeros(n), dtype=float),
        "dam_price_eur_per_mwh": np.asarray(prices, dtype=float),
    }
    if load is not None:
        out["load_kwh"] = np.asarray(load, dtype=float)
    return pd.DataFrame(out)


# ---------------------------------------------------------------------------
# A — single price spike: charge the trough, discharge the spike
# ---------------------------------------------------------------------------


def test_single_spike_arbitrage_matches_hand_optimum():
    # Prices flat at 10 except one 200 spike. Power cap 1000 kW limits the
    # spike discharge to 1000 kWh; the energy drawn to deliver it is
    # 1000 / (0.9 * 0.9) = 1234.568 kWh, bought at 10 EUR/MWh.
    prices = [10.0, 10.0, 10.0, 200.0, 10.0, 10.0]
    params = _params(max_cycles_per_day=10.0)
    res, _solver, res_full = run_scenario(
        params, _ts(prices), return_unrounded=True, **_OPTS,
    )

    rte = 0.9 * 0.9
    expected_charge_kwh = 1000.0 / rte            # 1234.5679
    expected_profit = 200.0 * 1.0 - 10.0 * expected_charge_kwh / 1000.0

    assert float(res_full["bess_dis_grid_kwh"].iloc[3]) == pytest.approx(
        1000.0, abs=1e-4,
    )
    assert float(res_full["bess_dis_grid_kwh"].sum()) == pytest.approx(
        1000.0, abs=1e-4,
    )
    assert float(res_full["bess_charge_grid_kwh"].sum()) == pytest.approx(
        expected_charge_kwh, abs=1e-3,
    )
    kpis = compute_kpis(res, params, verify_balance=False)
    assert kpis["profit_total_eur"] == pytest.approx(expected_profit, abs=0.01)
    # Terminal SOC closes the cycle.
    assert kpis["bess_net_soc_change_mwh"] == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# B — efficiency bracket: spread below/above the round-trip threshold
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "high_price,should_cycle",
    [
        (123.0, False),  # 123/100 < 1/0.81 = 1.2346 — unprofitable
        (125.0, True),   # 125/100 > 1/0.81 — profitable
    ],
)
def test_round_trip_efficiency_gates_marginal_arbitrage(high_price, should_cycle):
    prices = [100.0, high_price] * 3
    params = _params(max_cycles_per_day=10.0)
    _res, _solver, res_full = run_scenario(
        params, _ts(prices), return_unrounded=True, **_OPTS,
    )
    total_discharge = float(res_full["bess_dis_grid_kwh"].sum())
    if should_cycle:
        assert total_discharge > 100.0
    else:
        assert total_discharge == pytest.approx(0.0, abs=1e-4)


# ---------------------------------------------------------------------------
# C — export cap clips PV injection; the clipped surplus is curtailed
# ---------------------------------------------------------------------------


def test_export_cap_clips_pv_injection():
    params = _params(
        pv_nameplate_kwp=2000.0, bess_power_kw=0.0, bess_capacity_kwh=0.0,
        p_grid_export_max_kw=1000.0, allow_bess_grid_charging=False,
    )
    pv = [2000.0, 2000.0, 2000.0]
    _res, _solver, res_full = run_scenario(
        params, _ts([50.0, 50.0, 50.0], pv=pv), return_unrounded=True, **_OPTS,
    )
    assert np.allclose(res_full["pv_to_grid_kwh"], 1000.0, atol=1e-4)
    assert np.allclose(res_full["pv_curtail_kwh"], 1000.0, atol=1e-4)


# ---------------------------------------------------------------------------
# D — negative price: curtail rather than export at a loss
# ---------------------------------------------------------------------------


def test_negative_price_curtails_instead_of_exporting():
    params = _params(
        pv_nameplate_kwp=1000.0, bess_power_kw=0.0, bess_capacity_kwh=0.0,
        allow_bess_grid_charging=False,
    )
    res, _solver, res_full = run_scenario(
        params, _ts([-50.0, -50.0], pv=[1000.0, 1000.0]),
        return_unrounded=True, **_OPTS,
    )
    assert float(res_full["pv_to_grid_kwh"].sum()) == pytest.approx(0.0, abs=1e-4)
    assert float(res_full["pv_curtail_kwh"].sum()) == pytest.approx(
        2000.0, abs=1e-3,
    )
    kpis = compute_kpis(res, params, verify_balance=False)
    assert kpis["profit_total_eur"] == pytest.approx(0.0, abs=1e-6)


def test_negative_price_charging_earns_when_grid_charging_allowed():
    # Buy at -50 (get paid), sell at +50: profit on both legs.
    prices = [-50.0, 50.0]
    params = _params(max_cycles_per_day=10.0)
    res, _solver, res_full = run_scenario(
        params, _ts(prices), return_unrounded=True, **_OPTS,
    )
    charge = float(res_full["bess_charge_grid_kwh"].iloc[0])
    assert charge == pytest.approx(1000.0, abs=1e-3)  # power-limited
    out = float(res_full["bess_dis_grid_kwh"].iloc[1])
    assert out == pytest.approx(1000.0 * 0.81, abs=1e-3)
    kpis = compute_kpis(res, params, verify_balance=False)
    expected = 50.0 * (1000.0 * 0.81) / 1000.0 + 50.0 * 1000.0 / 1000.0
    assert kpis["profit_total_eur"] == pytest.approx(expected, abs=0.01)


# ---------------------------------------------------------------------------
# E — mode pinning: self_consumption load priority vs merchant zero-load
# ---------------------------------------------------------------------------


def test_self_consumption_pins_pv_to_load_priority():
    params = _params(
        mode="self_consumption", pv_nameplate_kwp=1000.0,
        bess_power_kw=0.0, bess_capacity_kwh=0.0,
        allow_bess_grid_charging=False,
    )
    _res, _solver, res_full = run_scenario(
        params, _ts([50.0, 50.0], pv=[800.0, 300.0], load=[500.0, 500.0]),
        return_unrounded=True, **_OPTS,
    )
    # Step 0: pv 800 > load 500 -> pv_to_load = 500, surplus 300 exported.
    assert float(res_full["pv_to_load_kwh"].iloc[0]) == pytest.approx(500.0, abs=1e-6)
    assert float(res_full["pv_to_grid_kwh"].iloc[0]) == pytest.approx(300.0, abs=1e-4)
    assert float(res_full["grid_to_load_kwh"].iloc[0]) == pytest.approx(0.0, abs=1e-6)
    # Step 1: pv 300 < load 500 -> all PV to load, 200 imported, no export.
    assert float(res_full["pv_to_load_kwh"].iloc[1]) == pytest.approx(300.0, abs=1e-6)
    assert float(res_full["grid_to_load_kwh"].iloc[1]) == pytest.approx(200.0, abs=1e-6)
    assert float(res_full["pv_to_grid_kwh"].iloc[1]) == pytest.approx(0.0, abs=1e-6)


def test_merchant_pins_load_flows_to_zero():
    params = _params(
        mode="merchant", pv_nameplate_kwp=1000.0,
        bess_power_kw=0.0, bess_capacity_kwh=0.0,
        allow_bess_grid_charging=False,
    )
    _res, _solver, res_full = run_scenario(
        params, _ts([50.0, 50.0], pv=[800.0, 300.0], load=[500.0, 500.0]),
        return_unrounded=True, **_OPTS,
    )
    for col in ("pv_to_load_kwh", "bess_dis_load_kwh", "grid_to_load_kwh"):
        assert float(res_full[col].abs().sum()) == pytest.approx(0.0, abs=1e-9)
    assert float(res_full["pv_to_grid_kwh"].sum()) == pytest.approx(1100.0, abs=1e-3)


# ---------------------------------------------------------------------------
# F — independent energy-balance closure + mutual exclusion
# ---------------------------------------------------------------------------


def _assert_balance_closes(res_full: pd.DataFrame, params: dict) -> None:
    eta_c = params["efficiency_charge"]
    eta_d = params["efficiency_discharge"]
    pv_residual = (
        res_full["pv_kwh"]
        - res_full["pv_to_load_kwh"] - res_full["pv_to_bess_kwh"]
        - res_full["pv_to_grid_kwh"] - res_full["pv_curtail_kwh"]
    )
    assert float(pv_residual.abs().max()) < 1e-6
    soc = res_full["soc_kwh"].to_numpy(dtype=float)
    delta = (
        eta_c * (res_full["pv_to_bess_kwh"] + res_full["bess_charge_grid_kwh"])
        - (res_full["bess_dis_load_kwh"] + res_full["bess_dis_grid_kwh"]) / eta_d
    ).to_numpy(dtype=float)
    if len(soc) >= 2:
        assert float(np.abs(soc[1:] - soc[:-1] - delta[:-1]).max()) < 1e-6


def test_energy_balance_closes_and_charge_discharge_exclusive():
    rng = np.random.default_rng(7)
    n = 48
    prices = 80.0 + 60.0 * np.sin(np.arange(n) / 24.0 * 2 * np.pi) + rng.normal(0, 10, n)
    pv = np.maximum(
        0.0, 1500.0 * np.sin((np.arange(n) % 24 - 6) / 12.0 * np.pi)
    )
    load = 600.0 + 200.0 * rng.random(n)
    params = _params(
        mode="self_consumption", pv_nameplate_kwp=1500.0,
        allow_bess_grid_charging=True, max_cycles_per_day=2.0,
    )
    _res, _solver, res_full = run_scenario(
        params, _ts(prices, pv=pv, load=load), return_unrounded=True, **_OPTS,
    )
    _assert_balance_closes(res_full, params)
    charge = (
        res_full["pv_to_bess_kwh"] + res_full["bess_charge_grid_kwh"]
    ).to_numpy(dtype=float)
    discharge = (
        res_full["bess_dis_load_kwh"] + res_full["bess_dis_grid_kwh"]
    ).to_numpy(dtype=float)
    assert float((charge * discharge).max()) < 1e-6
    inv = verify_dispatch_invariants(res_full, params)
    for key, value in inv.items():
        if key == "invariant_5_no_sim_grid_io_max_product_kwh2":
            assert value <= 1e-6, key
        else:
            assert value <= 1e-3, key


# ---------------------------------------------------------------------------
# G — the big-M "unlimited export" bound never changes the optimum
# ---------------------------------------------------------------------------


def test_unlimited_export_big_m_does_not_change_optimum():
    prices = [10.0, 10.0, 200.0, 10.0]
    pv = [1500.0, 1500.0, 1500.0, 0.0]
    base = _params(pv_nameplate_kwp=1500.0, max_cycles_per_day=10.0)
    # A finite cap comfortably above any feasible injection...
    res_loose, _s1, full_loose = run_scenario(
        {**base, "p_grid_export_max_kw": 50_000.0},
        _ts(prices, pv=pv), return_unrounded=True, **_OPTS,
    )
    # ...vs the loader's "unlimited" big-M substitution (1e6 kW).
    res_bigm, _s2, full_bigm = run_scenario(
        {**base, "p_grid_export_max_kw": 1.0e6, "grid_export_unlimited": True},
        _ts(prices, pv=pv), return_unrounded=True, **_OPTS,
    )
    k_loose = compute_kpis(res_loose, base, verify_balance=False)
    k_bigm = compute_kpis(res_bigm, base, verify_balance=False)
    assert k_bigm["profit_total_eur"] == pytest.approx(
        k_loose["profit_total_eur"], abs=0.01,
    )
    for col in ("pv_to_grid_kwh", "bess_dis_grid_kwh", "pv_curtail_kwh"):
        assert float(full_bigm[col].sum()) == pytest.approx(
            float(full_loose[col].sum()), abs=1e-2,
        )


# ---------------------------------------------------------------------------
# H — determinism: identical re-solve
# ---------------------------------------------------------------------------


def test_resolve_is_deterministic():
    prices = [10.0, 150.0, 30.0, 90.0, 10.0, 200.0]
    params = _params(max_cycles_per_day=10.0)
    res_a, _s1 = run_scenario(params, _ts(prices), **_OPTS)
    res_b, _s2 = run_scenario(params, _ts(prices), **_OPTS)
    pd.testing.assert_frame_equal(res_a, res_b)
