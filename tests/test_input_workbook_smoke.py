"""Smoke tests for the case-study ``inputs/input.xlsx`` workbook.

These tests guard the acceptance criterion that a fresh clone can run
``python main.py inputs/input.xlsx --solver highs`` end-to-end, in both
``self_consumption`` and ``merchant`` modes.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _highs_available() -> bool:
    try:
        import highspy
    except ImportError:
        return False
    return bool(highspy)


def test_repo_input_xlsx_exists():
    assert (ROOT / "inputs" / "input.xlsx").exists()


def test_repo_input_xlsx_has_all_sheets():
    sheets = pd.ExcelFile(ROOT / "inputs" / "input.xlsx").sheet_names
    assert set(sheets) == {
        "timeseries", "project", "pv", "bess", "economics",
        "simulation", "balancing", "max_injection_profile",
        "max_injection_profile_pv", "max_injection_profile_bess",
        "sizing", "scenarios",
    }


def test_repo_input_xlsx_has_35040_timeseries_rows():
    ts = pd.read_excel(ROOT / "inputs" / "input.xlsx", sheet_name="timeseries")
    assert len(ts) == 35040


def test_repo_input_xlsx_site_lump_sums_default_to_zero():
    """The shipped workbook carries the site-wide lump-sum keys at 0.0,
    so headline KPIs match the pre-feature baseline."""
    from pvbess_opt.io import read_workbook
    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    assert float(typed["project"]["site_capex_eur"]) == 0.0
    assert float(typed["project"]["site_devex_eur"]) == 0.0


def test_repo_input_xlsx_has_negative_dam_hours():
    """Spec: 4 negative-price hours seeded so the no-sim-IO logic and
    the sign-aware noise actually exercise.  At 15-minute cadence each
    hour expands to 4 steps, so the 4 seeded hours give 16 negative
    steps; we allow a small tolerance for any noise that lands in the
    same bucket as a seeded hour."""
    ts = pd.read_excel(ROOT / "inputs" / "input.xlsx", sheet_name="timeseries")
    n_neg = int((ts["dam_price_eur_per_mwh"] < 0).sum())
    assert 12 <= n_neg <= 20


def test_max_injection_pct_is_no_curtailment_in_production_workbook():
    # Every row of the canonical max_injection_pct column in the
    # production workbook is 100 — the no-curtailment default.  Users
    # opt in to curtailment by editing the sheet; the shipped workbook
    # imposes no per-hour export cap beyond the regulatory nameplate.
    profile = pd.read_excel(
        ROOT / "inputs" / "input.xlsx",
        sheet_name="max_injection_profile",
    )["max_injection_pct"]
    assert (profile == 100.0).all()


def test_read_workbook_round_trip_after_build_script():
    from pvbess_opt.io import read_workbook
    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    assert typed["dt_minutes"] == 15
    assert typed["project"]["mode"] == "self_consumption"
    assert typed["project"]["project_lifecycle_years"] == 20
    assert "load_kwh" in typed["ts"].columns


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_main_self_consumption_short_horizon(tmp_path, monkeypatch):
    """End-to-end smoke: main.py on a short window, self_consumption mode."""
    from pvbess_opt.io import read_workbook, write_workbook
    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)  # 1 day @ 15 min
    short_xlsx = tmp_path / "short.xlsx"
    write_workbook(typed, short_xlsx)

    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(ROOT))
    import main as main_module
    rc = main_module.main([
        str(short_xlsx),
        "--solver", "highs",
        "--mip-gap", "0.05",
        "--time-limit", "180",
    ])
    assert rc == 0


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_main_merchant_short_horizon(tmp_path, monkeypatch):
    """End-to-end smoke: main.py on a short window, merchant mode."""
    from pvbess_opt.io import read_workbook, write_workbook
    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)  # 1 day @ 15 min
    typed["project"]["mode"] = "merchant"
    short_xlsx = tmp_path / "short_merchant.xlsx"
    write_workbook(typed, short_xlsx)

    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(ROOT))
    import main as main_module
    rc = main_module.main([
        str(short_xlsx),
        "--mode", "merchant",
        "--solver", "highs",
        "--mip-gap", "0.05",
        "--time-limit", "180",
    ])
    assert rc == 0


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_repo_input_xlsx_headline_kpis_pinned():
    """End-to-end pin of headline year-1 KPIs against the stored
    baseline on inputs/input.xlsx (perfect-foresight, self_consumption mode, full
    year).

    Tight tolerances pick up any sign error or fixture drift.
    """
    from pvbess_opt.availability import apply_unavailability_derate
    from pvbess_opt.economics import (
        build_yearly_cashflow,
        compute_financial_kpis,
        derive_asset_capacities,
        read_economic_params,
    )
    from pvbess_opt.io import read_inputs
    from pvbess_opt.kpis import compute_kpis
    from pvbess_opt.lifetime import (
        aggregate_lifetime_to_yearly,
        build_lifetime_dispatch,
    )
    from pvbess_opt.optimization import run_scenario

    excel_path = ROOT / "inputs" / "input.xlsx"
    params, ts = read_inputs(excel_path)
    econ = read_economic_params(excel_path)

    res, _solver = run_scenario(
        params, ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=600,
    )
    kpis = compute_kpis(res, params, verify_balance=False)
    kpis = apply_unavailability_derate(
        kpis, float(params.get("unavailability_pct", 0.0) or 0.0),
    )
    capacities = derive_asset_capacities(econ, params, ts)
    year1_for_cycles = float(kpis.get("bess_total_discharge_mwh", 0.0) or 0.0)
    yearly_cf = build_yearly_cashflow(kpis, econ, capacities)
    lifetime_df = build_lifetime_dispatch(
        res, econ, capacities, year1_discharge_mwh=year1_for_cycles,
    )
    lifetime_yearly = aggregate_lifetime_to_yearly(lifetime_df)
    avail_factor = max(
        0.0,
        min(1.0, 1.0 - float(econ.get("unavailability_pct", 0.0) or 0.0) / 100.0),
    )
    if avail_factor < 1.0 and not lifetime_yearly.empty:
        for col in (
            "pv_generation_mwh", "bess_discharge_mwh", "bess_charge_mwh",
            "pv_to_load_mwh", "pv_to_grid_mwh", "import_to_load_mwh",
            "export_total_mwh", "revenue_eur_dam_retail",
        ):
            if col in lifetime_yearly.columns:
                lifetime_yearly[col] = (
                    lifetime_yearly[col].astype(float) * avail_factor
                )
    fin_kpis = compute_financial_kpis(
        yearly_cf, econ,
        capacities=capacities,
        lifetime_yearly=lifetime_yearly,
        year1_kpis=kpis,
    )

    # Baseline (perfect-foresight, MIP gap 0.01, HiGHS, no-curtailment
    # default).
    assert abs(float(kpis["pv_generation_mwh"]) - 22_275.0) < 1.0e-2
    assert abs(
        float(kpis["bess_total_discharge_mwh"]) - 9_512.872_875
    ) < 1.0e-2
    assert abs(float(kpis["profit_total_eur"]) - 2_824_793.2746) < 1.0
    assert abs(float(fin_kpis["npv_eur"]) - 8_014_317.02) < 1.0
    assert abs(float(fin_kpis["irr_pct"]) - 15.1402) < 1.0e-2
