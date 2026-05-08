"""v0.8 BESS spec rationalisation tests (Phase 2).

The optimizer's surface dropped three v0.7 keys:
``battery_hours``, ``p_charge_max_kw``, ``p_dis_max_kw``.  ``bess_power_kw``
is now the symmetric charge / discharge limit, and ``bess_capacity_kwh``
pins the BESS energy capacity.  ``e_cap`` is no longer a decision
variable; ``run_scenario`` returns ``(res, resolved_solver_name)`` and
the KPI dict carries ``e_cap_mwh`` (renamed from ``e_cap_opt_mwh``).
"""

from __future__ import annotations

import importlib

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.io import _parse_kv_sheet
from pvbess_opt.kpis import compute_kpis
from pvbess_opt.optimization import run_scenario


def _highs_available() -> bool:
    try:
        importlib.import_module("highspy")
    except ImportError:
        return False
    return True


def _ts(n: int = 48, *, with_load: bool = True) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    timestamps = pd.date_range("2026-06-01 00:00", periods=n, freq="h")
    h = np.arange(n).astype(float) % 24
    pv = 4000.0 * np.where((h >= 6) & (h <= 18),
                            np.sin(np.pi * (h - 6) / 12.0), 0.0)
    pv = np.maximum(pv + rng.normal(0, 30, n), 0.0)
    dam = 100.0 - 50.0 * np.sin(np.pi * (h - 6) / 12.0) + rng.normal(0, 5, n)
    df = {"timestamp": timestamps, "pv_kwh": pv, "dam_price_eur_per_mwh": dam}
    if with_load:
        load = 3000.0 + 1500.0 * np.exp(-((h - 9) ** 2) / 8.0)
        df["load_kwh"] = np.maximum(load + rng.normal(0, 50, n), 800.0)
    return pd.DataFrame(df)


def _params(*, pv_kwp: float, bess_kw: float, bess_kwh: float, mode: str) -> dict:
    return {
        "dt_minutes": 60,
        "efficiency_charge": 0.97,
        "efficiency_discharge": 0.97,
        "soc_min_frac": 0.20,
        "soc_max_frac": 0.95,
        "initial_soc_frac": 0.50,
        "terminal_soc_equal": True,
        "max_cycles_per_day": 1.0,
        "p_grid_export_max_kw": 5000.0,
        "pv_nameplate_kwp": pv_kwp,
        "bess_power_kw": bess_kw,
        "bess_capacity_kwh": bess_kwh,
        "curtailment_frac": 0.27,
        "retail_tariff_eur_per_mwh": 132.0,
        "settlement_minutes": 15,
        "mode": mode,
        "allow_bess_grid_charging": False,
        "show_titles": False,
    }


# ---------------------------------------------------------------------------
# Loader rejects dropped keys with a friendly warning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dropped", [
    "battery_hours", "p_charge_max_kw", "p_dis_max_kw",
])
def test_loader_warns_on_v07_bess_keys(dropped, caplog):
    flat = {dropped: 5000.0}
    with caplog.at_level("WARNING"):
        _parse_kv_sheet("bess", flat)
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert dropped in msgs
    assert "bess_power_kw" in msgs or "bess_capacity_kwh" in msgs
    assert "v0.8" in msgs


# ---------------------------------------------------------------------------
# Optimizer end-to-end: BESS-only and hybrid still solve
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_bess_only_run_still_works():
    params = _params(
        pv_kwp=0.0, bess_kw=5000.0, bess_kwh=20000.0, mode="merchant",
    )
    params["allow_bess_grid_charging"] = True
    ts = _ts(with_load=False)
    res, solver = run_scenario(
        params, ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=60,
    )
    # Charge / discharge powers respect the symmetric limit.
    cap = float(params["bess_power_kw"])
    assert res["pv_to_bess_kwh"].max() <= cap + 1e-3
    assert res["bess_charge_grid_kwh"].max() <= cap + 1e-3
    assert res["bess_dis_load_kwh"].max() <= cap + 1e-3
    assert res["bess_dis_grid_kwh"].max() <= cap + 1e-3
    assert isinstance(solver, str) and solver


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_hybrid_run_still_works():
    params = _params(
        pv_kwp=4500.0, bess_kw=5000.0, bess_kwh=20000.0, mode="vnb",
    )
    ts = _ts()
    res, _solver = run_scenario(
        params, ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=60,
    )
    # SOC respects [soc_min_frac, soc_max_frac] * bess_capacity_kwh.
    assert res["soc_kwh"].min() >= params["soc_min_frac"] * params["bess_capacity_kwh"] - 1e-3
    assert res["soc_kwh"].max() <= params["soc_max_frac"] * params["bess_capacity_kwh"] + 1e-3


# ---------------------------------------------------------------------------
# KPI rename: e_cap_opt_mwh → e_cap_mwh
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_kpi_e_cap_mwh_matches_workbook_capacity():
    params = _params(
        pv_kwp=4500.0, bess_kw=5000.0, bess_kwh=20000.0, mode="vnb",
    )
    ts = _ts()
    res, _ = run_scenario(
        params, ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=60,
    )
    kpis = compute_kpis(res, params, verify_balance=False)
    assert "e_cap_mwh" in kpis
    assert "e_cap_opt_mwh" not in kpis
    # In v0.8 the value comes straight from bess_capacity_kwh / 1000.
    assert kpis["e_cap_mwh"] == pytest.approx(20.0, rel=1e-6)


# ---------------------------------------------------------------------------
# run_scenario signature: (res, resolved_solver_name)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_run_scenario_returns_two_tuple():
    params = _params(
        pv_kwp=4500.0, bess_kw=5000.0, bess_kwh=20000.0, mode="vnb",
    )
    ts = _ts()
    out = run_scenario(
        params, ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=60,
    )
    assert len(out) == 2
    res, solver = out
    assert isinstance(res, pd.DataFrame)
    assert isinstance(solver, str)
