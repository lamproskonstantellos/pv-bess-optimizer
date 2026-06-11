"""Mode x grid-charging x grid-cap x asset-config matrix semantics.

Complements ``test_dispatch_matrix_robustness.py`` (which sweeps the
8-cell switch matrix for invariant cleanliness) with the asset-config
axis and the behavioural guarantees the audit verified:

* KPI aggregates agree with the dispatch frame in every cell;
* merchant mode never carries load flows and zeroes the
  self-consumption revenue aggregate;
* a BESS-only project with grid charging disabled has no energy source,
  so the battery stays idle in BOTH modes (closed SOC cycle);
* ``grid_cap_includes_load`` is a clean — and WARNED — no-op in
  merchant mode: the dispatch frame is identical with the flag on/off.
"""

from __future__ import annotations

import itertools
import logging

import numpy as np
import pandas as pd
import pytest

import pvbess_opt.optimization as opt
from pvbess_opt.kpis import compute_kpis
from pvbess_opt.optimization import run_scenario

SOLVER_KW = {"solver_name": "highs", "mip_gap": 0.0, "time_limit_seconds": 120}


def _matrix_ts() -> pd.DataFrame:
    """24 h with a PV bell, bimodal load, and a negative-DAM step."""
    h = np.arange(24)
    pv = np.clip(10.0 * np.sin(np.pi * (h - 6) / 12.0), 0.0, None)
    pv[(h < 6) | (h > 18)] = 0.0
    load = (
        3.0
        + 5.0 * np.exp(-((h - 19.0) ** 2) / 6.0)
        + 2.5 * np.exp(-((h - 8.0) ** 2) / 6.0)
    )
    dam = 40.0 + 25.0 * np.sin(np.pi * (h - 15.0) / 12.0)
    dam[12] = -10.0
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=24, freq="h"),
        "pv_kwh": pv.astype(float),
        "load_kwh": load.astype(float),
        "dam_price_eur_per_mwh": dam.astype(float),
    })


def _params(mode: str, grid_chg: bool, cap_incl: bool, asset: str) -> dict:
    return {
        "dt_minutes": 60,
        "efficiency_charge": 0.95,
        "efficiency_discharge": 0.95,
        "soc_min_frac": 0.10,
        "soc_max_frac": 0.95,
        "initial_soc_frac": 0.50,
        "terminal_soc_equal": True,
        "max_cycles_per_day": 3.0,
        "p_grid_export_max_kw": 6.0,
        "pv_nameplate_kwp": 1000.0 if asset in ("hybrid", "pv_only") else 0.0,
        "bess_power_kw": 5.0 if asset in ("hybrid", "bess_only") else 0.0,
        "bess_capacity_kwh": 10.0 if asset in ("hybrid", "bess_only") else 0.0,
        "retail_tariff_eur_per_mwh": 180.0,
        "mode": mode,
        "allow_bess_grid_charging": grid_chg,
        "grid_cap_includes_load": cap_incl,
        "show_titles": False,
    }


_CELLS = list(itertools.product(
    ("self_consumption", "merchant"),
    (False, True),               # allow_bess_grid_charging
    (False, True),               # grid_cap_includes_load
    ("hybrid", "pv_only", "bess_only"),
))


@pytest.mark.parametrize("mode,grid_chg,cap_incl,asset", _CELLS)
def test_kpis_agree_with_dispatch_frame(mode, grid_chg, cap_incl, asset):
    """Every cell: the headline KPI aggregates re-derive from the frame."""
    params = _params(mode, grid_chg, cap_incl, asset)
    res, _ = run_scenario(params, _matrix_ts(), **SOLVER_KW)
    k = compute_kpis(res, params, verify_balance=False)

    frame_profit = float(
        res["profit_load_from_pv_eur"].sum()
        + res["profit_load_from_bess_eur"].sum()
        + res["profit_export_from_pv_eur"].sum()
        + res["profit_export_from_bess_eur"].sum()
        - res["expense_charge_bess_grid_eur"].sum()
    )
    assert k["profit_total_eur"] == pytest.approx(frame_profit, abs=0.05)
    assert k["pv_generation_mwh"] == pytest.approx(
        res["pv_kwh"].sum() / 1000.0, abs=1e-3,
    )
    assert k["bess_total_discharge_mwh"] == pytest.approx(
        (res["bess_dis_load_kwh"].sum() + res["bess_dis_grid_kwh"].sum())
        / 1000.0, abs=1e-3,
    )
    assert k["system_total_export_mwh"] == pytest.approx(
        (res["pv_to_grid_kwh"].sum() + res["bess_dis_grid_kwh"].sum())
        / 1000.0, abs=1e-3,
    )
    assert k["pv_energy_curtailed_mwh"] == pytest.approx(
        res["pv_curtail_kwh"].sum() / 1000.0, abs=1e-3,
    )
    if mode == "merchant":
        assert float(
            res[["pv_to_load_kwh", "bess_dis_load_kwh", "grid_to_load_kwh"]]
            .to_numpy().max()
        ) == 0.0
        assert k["revenue_self_consumption_eur"] == 0.0
    if asset == "pv_only":
        assert float(
            res[["pv_to_bess_kwh", "bess_charge_grid_kwh",
                 "bess_dis_load_kwh", "bess_dis_grid_kwh"]].to_numpy().max()
        ) == 0.0
    if asset == "bess_only":
        assert float(
            res[["pv_to_load_kwh", "pv_to_bess_kwh", "pv_to_grid_kwh",
                 "pv_curtail_kwh"]].to_numpy().max()
        ) == 0.0


@pytest.mark.parametrize("mode", ["self_consumption", "merchant"])
def test_bess_only_without_grid_charging_is_idle(mode):
    """No PV and no grid charging leaves the battery with no energy
    source; with the closed SOC cycle it must not discharge at all."""
    params = _params(mode, grid_chg=False, cap_incl=False, asset="bess_only")
    res, _ = run_scenario(params, _matrix_ts(), **SOLVER_KW)
    k = compute_kpis(res, params, verify_balance=False)
    assert k["bess_total_discharge_mwh"] == 0.0
    assert k["bess_total_charge_mwh"] == 0.0


@pytest.mark.parametrize("grid_chg", [False, True])
@pytest.mark.parametrize("asset", ["hybrid", "pv_only", "bess_only"])
def test_merchant_cap_flag_is_a_clean_noop(grid_chg, asset):
    """grid_cap_includes_load flips nothing in merchant mode — the
    dispatch frames are identical with the flag on and off."""
    ts = _matrix_ts()
    res_off, _ = run_scenario(
        _params("merchant", grid_chg, False, asset), ts, **SOLVER_KW,
    )
    res_on, _ = run_scenario(
        _params("merchant", grid_chg, True, asset), ts, **SOLVER_KW,
    )
    pd.testing.assert_frame_equal(res_off, res_on)


def test_merchant_cap_flag_noop_is_warned(monkeypatch, caplog):
    """Setting grid_cap_includes_load in merchant mode warns (once)."""
    monkeypatch.setattr(opt, "_MERCHANT_CAP_FLAG_WARNED", False)
    params = _params("merchant", False, True, "hybrid")
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.optimization"):
        opt.build_model(params, _matrix_ts())
    assert any(
        "grid_cap_includes_load" in rec.getMessage()
        and "merchant" in rec.getMessage()
        for rec in caplog.records
    )
    # Latched: a second build does not repeat the warning.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.optimization"):
        opt.build_model(params, _matrix_ts())
    assert not any(
        "grid_cap_includes_load" in rec.getMessage() for rec in caplog.records
    )


def test_self_consumption_cap_flag_never_warns(monkeypatch, caplog):
    """The flag is meaningful in self_consumption — no warning there."""
    monkeypatch.setattr(opt, "_MERCHANT_CAP_FLAG_WARNED", False)
    params = _params("self_consumption", False, True, "hybrid")
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.optimization"):
        opt.build_model(params, _matrix_ts())
    assert not any(
        "grid_cap_includes_load" in rec.getMessage() for rec in caplog.records
    )
