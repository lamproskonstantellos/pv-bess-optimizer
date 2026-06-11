"""Dispatch-invariant and solver-status hardening tests."""

from __future__ import annotations

import importlib
import logging

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.io import _normalise_timeseries
from pvbess_opt.optimization import (
    SolverStatus,
    TerminationCondition,
    _check_solver_status,
    run_scenario,
    verify_dispatch_invariants,
)


def _highs_available() -> bool:
    try:
        importlib.import_module("highspy")
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# Invariants computed on unrounded model values
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_invariant4_unrounded_full_year_bess_only():
    """Full-year self_consumption BESS-only: invariant_4 must stay below 1e-4.

    The sum-based invariant_4 is computed on unrounded model values so it
    does not accumulate round(4) error across 35,040 rows and trip
    --strict at 1e-3.
    """
    from pvbess_opt.io import read_inputs

    params, ts = read_inputs("inputs/input.xlsx")
    params = dict(params)
    params["pv_nameplate_kwp"] = 0.0
    params["allow_bess_grid_charging"] = True
    params["mode"] = "self_consumption"

    _res, _, res_full = run_scenario(
        params, ts, solver_name="highs", mip_gap=0.01, time_limit_seconds=1800,
        return_unrounded=True,
    )
    inv = verify_dispatch_invariants(res_full, params, mode="self_consumption")
    assert inv["invariant_4_rte_bound_excess_kwh"] < 1e-4, inv


# ---------------------------------------------------------------------------
# NaN-fill warning on the timeseries
# ---------------------------------------------------------------------------


def test_nan_fill_emits_warning_with_location(caplog):
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2027-04-02 00:00", periods=8, freq="h"),
        "pv_kwh": [1.0] * 8,
        "load_kwh": [10.0] * 8,
        "dam_price_eur_per_mwh": [50.0, 51.0, np.nan, np.nan, np.nan, 55.0, 56.0, 57.0],
    })
    with caplog.at_level(logging.WARNING):
        out = _normalise_timeseries(ts, mode="self_consumption")

    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("dam_price_eur_per_mwh" in m and "3 NaN" in m for m in msgs), msgs
    assert any("2027-04-02 02:00" in m for m in msgs), msgs
    # NaNs are still filled.
    assert not out["dam_price_eur_per_mwh"].isna().any()


def test_no_warning_when_timeseries_clean(caplog):
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2027-04-02 00:00", periods=4, freq="h"),
        "pv_kwh": [1.0] * 4,
        "load_kwh": [10.0] * 4,
        "dam_price_eur_per_mwh": [50.0, 51.0, 52.0, 53.0],
    })
    with caplog.at_level(logging.WARNING):
        _normalise_timeseries(ts, mode="self_consumption")
    assert not [r for r in caplog.records if "NaN" in r.getMessage()]


# ---------------------------------------------------------------------------
# Solver soft limit without a feasible incumbent must raise
# ---------------------------------------------------------------------------


class _FakeSolverInfo:
    def __init__(self, status, condition):
        self.status = status
        self.termination_condition = condition


class _FakeResult:
    def __init__(self, status, condition):
        self.solver = _FakeSolverInfo(status, condition)


def test_time_limit_no_incumbent_raises():
    result = _FakeResult(SolverStatus.aborted, TerminationCondition.maxTimeLimit)
    with pytest.raises(RuntimeError, match="no feasible incumbent"):
        _check_solver_status(result, "highs", model=None)


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_time_limit_with_incumbent_accepted():
    """A model carrying a loaded solution under maxTimeLimit is accepted."""
    from pvbess_opt.optimization import build_model, solve_model

    params = {
        "dt_minutes": 60, "efficiency_charge": 0.97, "efficiency_discharge": 0.97,
        "soc_min_frac": 0.20, "soc_max_frac": 0.95, "initial_soc_frac": 0.50,
        "terminal_soc_equal": True, "max_cycles_per_day": 1.0,
        "p_grid_export_max_kw": 5000.0, "pv_nameplate_kwp": 4500.0,
        "bess_power_kw": 5000.0, "bess_capacity_kwh": 20000.0,
        "retail_tariff_eur_per_mwh": 120.0,
        "mode": "self_consumption", "allow_bess_grid_charging": False, "show_titles": False,
    }
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2027-04-02 00:00", periods=24, freq="h"),
        "pv_kwh": np.maximum(np.sin(np.linspace(0, np.pi, 24)) * 2000.0, 0.0),
        "load_kwh": np.full(24, 1500.0),
        "dam_price_eur_per_mwh": np.full(24, 80.0),
    })
    model, _ = solve_model(
        build_model(params, ts), "highs", mip_gap=0.01, time_limit_seconds=60,
    )
    # The model now carries a loaded incumbent; a maxTimeLimit verdict on it
    # must be accepted (no raise).
    result = _FakeResult(SolverStatus.aborted, TerminationCondition.maxTimeLimit)
    _check_solver_status(result, "highs", model=model)
