"""Parametrized energy-balance + 9-invariant coverage across all
six mode x asset combinations.

Every mode (self_consumption / merchant) crossed with every asset configuration
(hybrid / pv_only / bess_only) runs through verify_energy_balance and
verify_dispatch_invariants at real scale (35,040 steps).

A fast-lane (1-day) variant runs in the default ``-m "not slow"`` lane;
the full-year variant carries the ``slow`` marker.
"""

from __future__ import annotations

import importlib

import pytest

from pvbess_opt.io import read_inputs
from pvbess_opt.kpis import ENERGY_TOLERANCE, verify_energy_balance
from pvbess_opt.optimization import run_scenario, verify_dispatch_invariants

_COMBOS = [
    (mode, asset)
    for mode in ("self_consumption", "merchant")
    for asset in ("hybrid", "pv_only", "bess_only")
]


def _highs_available() -> bool:
    try:
        importlib.import_module("highspy")
    except ImportError:
        return False
    return True


def _params_for(mode: str, asset: str, base_params: dict) -> dict:
    """Apply the Appendix-3 asset recipe to a copy of base_params."""
    params = dict(base_params)
    params["mode"] = mode
    if asset == "pv_only":
        params["bess_power_kw"] = 0.0
        params["bess_capacity_kwh"] = 0.0
    elif asset == "bess_only":
        params["pv_nameplate_kwp"] = 0.0
        params["allow_bess_grid_charging"] = True
    return params


def _assert_combo(mode: str, asset: str, params: dict, res_full):
    # Energy balance must not raise.
    verify_energy_balance(res_full, params, raise_on_failure=True)

    # All 9 invariants within tolerance.
    inv = verify_dispatch_invariants(res_full, params, mode=mode)
    for name, value in inv.items():
        assert value <= ENERGY_TOLERANCE, f"{mode}/{asset}: {name}={value:g}"

    # Combo-specific zero-flow guarantees.
    if asset == "pv_only":
        bess_flow = float(
            res_full["pv_to_bess_kwh"].abs().sum()
            + res_full["bess_charge_grid_kwh"].abs().sum()
            + res_full["bess_dis_load_kwh"].abs().sum()
            + res_full["bess_dis_grid_kwh"].abs().sum()
        )
        assert bess_flow < 1e-6, f"{mode}/pv_only: BESS flows nonzero ({bess_flow})"
    elif asset == "bess_only":
        pv_flow = float(
            res_full["pv_to_load_kwh"].abs().sum()
            + res_full["pv_to_bess_kwh"].abs().sum()
            + res_full["pv_to_grid_kwh"].abs().sum()
            + res_full["pv_curtail_kwh"].abs().sum()
        )
        assert pv_flow < 1e-6, f"{mode}/bess_only: PV flows nonzero ({pv_flow})"
        assert float(res_full["pv_kwh"].sum()) == 0.0


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
@pytest.mark.parametrize("mode,asset", _COMBOS)
def test_realscale_combo_fastlane(mode, asset):
    """1-day slice — runs in the default fast lane for PR feedback."""
    base_params, ts = read_inputs("inputs/input.xlsx")
    steps_per_day = round(24 * 60 / base_params["dt_minutes"])
    ts = ts.iloc[:steps_per_day].reset_index(drop=True)
    params = _params_for(mode, asset, base_params)
    _res, _solver, res_full = run_scenario(
        params, ts, solver_name="highs", mip_gap=0.01,
        time_limit_seconds=300, return_unrounded=True,
    )
    _assert_combo(mode, asset, params, res_full)


@pytest.mark.slow
@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
@pytest.mark.parametrize("mode,asset", _COMBOS)
def test_realscale_combo_full_year(mode, asset):
    """Full 35,040-step solve for every mode x asset combination."""
    base_params, ts = read_inputs("inputs/input.xlsx")
    params = _params_for(mode, asset, base_params)
    _res, _solver, res_full = run_scenario(
        params, ts, solver_name="highs", mip_gap=0.01,
        time_limit_seconds=1800, return_unrounded=True,
    )
    _assert_combo(mode, asset, params, res_full)
