"""Battery degradation: Rainflow counting, wear cost, SOH fade, no double-count."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from pvbess_opt.degradation import (
    build_degradation_report,
    derive_wear_cost_eur_per_mwh,
    equivalent_full_cycles,
    rainflow_cycles,
)

ROOT = Path(__file__).resolve().parent.parent


def _highs_available() -> bool:
    try:
        import highspy
    except ImportError:
        return False
    return bool(highspy)


def test_rainflow_counts_full_cycles():
    # Two full 0->1 swings == 2.0 equivalent full cycles.
    assert equivalent_full_cycles([0, 1, 0, 1, 0], 1.0) == pytest.approx(2.0)
    # One swing == 1.0.
    assert equivalent_full_cycles([0, 1, 0], 1.0) == pytest.approx(1.0)


def test_equivalent_full_cycles_half_depth():
    # A half-depth swing counts as half a full cycle.
    assert equivalent_full_cycles([0.0, 0.5, 0.0], 1.0) == pytest.approx(0.5)
    # Scaling by the usable amplitude.
    assert equivalent_full_cycles([0.0, 500.0, 0.0], 1000.0) == pytest.approx(0.5)


def test_rainflow_returns_range_count_pairs():
    cycles = rainflow_cycles([0, 2, 0, 2, 0])
    assert all(len(c) == 2 for c in cycles)
    assert sum(rng * count for rng, count in cycles) == pytest.approx(4.0)


def test_derive_wear_cost():
    # 100 000 EUR replacement / (5000 cycles x 10 MWh) = 2.0 EUR/MWh.
    assert derive_wear_cost_eur_per_mwh(100_000.0, 5000.0, 10.0) == pytest.approx(2.0)
    assert derive_wear_cost_eur_per_mwh(100_000.0, 0.0, 10.0) == 0.0
    assert derive_wear_cost_eur_per_mwh(100_000.0, 5000.0, 0.0) == 0.0


def test_build_degradation_report_fades_and_replaces():
    soc = np.append(np.tile([0.0, 1000.0], 50), 0.0)  # 50 full cycles, closed
    report = build_degradation_report(
        soc,
        capacity_kwh=1000.0, soc_min_frac=0.0, soc_max_frac=1.0,
        degradation_pct_per_cycle=0.1, project_years=10, start_year=2026,
        end_of_life_soh_pct=80.0,
    )
    assert len(report) == 10
    assert report["equivalent_full_cycles"].iloc[0] == pytest.approx(50.0)
    # SOH falls year on year; 50 cycles x 0.1 % = 5 %/yr -> EoL in year 4.
    assert report["soh_pct"].iloc[0] == pytest.approx(95.0)
    assert bool(report["replacement"].any())
    assert (report["soh_pct"] >= 0.0).all()


def test_degradation_report_none_without_bess():
    report = build_degradation_report(
        [0.0, 0.0], capacity_kwh=0.0, soc_min_frac=0.0, soc_max_frac=1.0,
        degradation_pct_per_cycle=0.5, project_years=3, start_year=2026,
    )
    # capacity 0 => usable 0 => zero equivalent cycles, no fade.
    assert (report["equivalent_full_cycles"] == 0.0).all()


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_wear_cost_suppresses_cycles_and_is_not_double_counted(tmp_path):
    from pvbess_opt.io import read_inputs, read_workbook, write_workbook
    from pvbess_opt.kpis import compute_kpis
    from pvbess_opt.optimization import run_scenario

    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)
    typed["project"]["mode"] = "merchant"
    typed["project"]["allow_bess_grid_charging"] = True
    short = tmp_path / "short.xlsx"
    write_workbook(typed, short)

    params, ts = read_inputs(short)
    opts = {"solver_name": "highs", "mip_gap": 0.05, "time_limit_seconds": 180}

    res0, _s, _f = run_scenario(
        {**params, "bess_wear_cost_eur_per_mwh": 0.0}, ts,
        return_unrounded=True, **opts,
    )
    res1, _s2, _f2 = run_scenario(
        {**params, "bess_wear_cost_eur_per_mwh": 100_000.0}, ts,
        return_unrounded=True, **opts,
    )
    d0 = compute_kpis(res0, params, verify_balance=False)["bess_total_discharge_mwh"]
    d1 = compute_kpis(res1, params, verify_balance=False)["bess_total_discharge_mwh"]
    assert d0 > 0.0           # baseline arbitrages / cycles
    assert d1 < d0            # a steep wear cost suppresses marginal cycling

    # No double-count: the reported profit is computed from the dispatch
    # flows and is independent of the wear-cost shadow price.
    a = compute_kpis(
        res1, {**params, "bess_wear_cost_eur_per_mwh": 0.0}, verify_balance=False,
    )
    b = compute_kpis(
        res1, {**params, "bess_wear_cost_eur_per_mwh": 9999.0}, verify_balance=False,
    )
    assert a["profit_total_eur"] == b["profit_total_eur"]
