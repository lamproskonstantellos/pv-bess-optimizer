"""BESS-only output frame must not carry phantom PV.

When ``pv_nameplate_kwp == 0`` the model pins every PV flow to zero, so
``model_to_dataframe`` must zero the PV columns in the output frame even
when the workbook ships a populated ``pv_kwh`` timeseries (the default
``inputs/input.xlsx`` does).  Otherwise a per-step energy-balance
residual would breach ``invariant_1`` / ``invariant_9`` and surface
phantom ``pv_generation_mwh`` KPIs.

These tests build a BESS-only solve from a non-zero ``pv_kwh`` column and
assert balance / invariants / KPIs are all clean.
"""

from __future__ import annotations

import importlib

import pytest

from pvbess_opt.io import read_inputs
from pvbess_opt.kpis import ENERGY_TOLERANCE, compute_kpis, verify_energy_balance
from pvbess_opt.optimization import run_scenario, verify_dispatch_invariants


def _highs_available() -> bool:
    try:
        importlib.import_module("highspy")
    except ImportError:
        return False
    return True


def _bess_only_one_day():
    """Default workbook, first day, PV nameplate zeroed but pv_kwh left in."""
    params, ts = read_inputs("inputs/input.xlsx")
    steps_per_day = round(24 * 60 / params["dt_minutes"])
    ts = ts.iloc[:steps_per_day].reset_index(drop=True)
    params = dict(params)
    params["pv_nameplate_kwp"] = 0.0
    params["allow_bess_grid_charging"] = True
    assert float(ts["pv_kwh"].sum()) > 0.0  # populated PV column survives
    return params, ts


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_bess_only_output_frame_balanced():
    params, ts = _bess_only_one_day()
    res, _ = run_scenario(
        params, ts, solver_name="highs", mip_gap=0.01, time_limit_seconds=120,
    )

    # pv_kwh must be zeroed in the output frame, not copied from input.
    assert float(res["pv_kwh"].sum()) == 0.0

    # Energy balance must not raise.
    verify_energy_balance(res, params, raise_on_failure=True)

    # All 9 invariants within tolerance; invariant_1 / invariant_9 near zero.
    inv = verify_dispatch_invariants(res, params, mode="self_consumption")
    for name, value in inv.items():
        assert value <= ENERGY_TOLERANCE, f"{name}={value:g} exceeds tolerance"
    assert inv["invariant_1_pv_balance_kwh"] <= 1e-3
    assert inv["invariant_9_pv_load_priority_kwh"] <= 1e-3

    # No phantom PV generation KPI.
    kpis = compute_kpis(res, params, verify_balance=False)
    assert kpis["pv_generation_mwh"] == 0.0
