"""End-to-end pipeline run with the price-scenario engine armed (slow).

One full hourly-year run through ``pipeline.run`` with a parametric
scenario: asserts the auto-trajectories reach the cashflow, the
scenario_price_paths sheet and both figures are emitted, and the
SUMMARY digest carries the scenario lines.  Slow lane — the fast lane
covers the same mechanics at the unit level
(tests/test_pricedata_engine.py, tests/test_pricedata_io.py).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _highs_available() -> bool:
    try:
        import highspy
    except ImportError:
        return False
    return bool(highspy)


@pytest.mark.slow
@pytest.mark.skipif(not _highs_available(), reason="HiGHS not installed")
def test_full_run_with_armed_reprice_engine(tmp_path):
    from pvbess_opt.io import read_workbook, write_workbook
    from pvbess_opt.pipeline import RunConfig, run

    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    ts = typed["ts"].set_index("timestamp")
    energy = [c for c in ts.columns if c.endswith("_kwh")]
    hourly = ts.resample("60min").agg(
        {c: ("sum" if c in energy else "mean") for c in ts.columns},
    ).reset_index()
    typed["ts"] = hourly
    typed["project"]["project_lifecycle_years"] = 5
    typed["simulation"]["plot_daily_scope"] = "none"
    typed["simulation"]["plot_monthly_scope"] = "none"
    typed["scenario_engine"]["price_scenarios_enabled"] = True
    typed["price_scenarios"] = [{
        "name": "Central", "provider": "parametric", "vintage": "v",
        "weight_pct": 100.0, "store_path": "central", "notes": "",
    }]
    store = tmp_path / "central"
    store.mkdir()
    (store / "meta.yaml").write_text(
        yaml.safe_dump({
            "provider": "parametric",
            "parametric": {"dam_level_pct_per_yr": -10.0},
        }),
        encoding="utf-8",
    )
    workbook = tmp_path / "armed.xlsx"
    write_workbook(typed, workbook)

    run(RunConfig(
        excel=workbook, solver="highs", outdir=tmp_path / "out",
        mip_gap=0.05, time_limit=300,
    ))

    run_dirs = list((tmp_path / "out").glob("armed_*"))
    assert len(run_dirs) == 1
    out_dir = run_dirs[0]
    results = out_dir / "03_results.xlsx"
    sheet = pd.read_excel(results, sheet_name="scenario_price_paths")
    assert set(sheet["scenario"]) == {"Central"}
    assert sheet["applied"].all()
    # The parametric level drift reaches the factors verbatim.
    year2 = sheet[sheet["project_year"] == 2].iloc[0]
    assert year2["g_revenue_dam_pv"] == pytest.approx(0.9, rel=1e-6)
    # And the cashflow's DAM revenue declines faster than degradation
    # alone: year-2 revenue < year-1 revenue x 0.95.
    cashflow = pd.read_excel(results, sheet_name="cashflow_yearly")
    dam = cashflow.set_index("project_year")["revenue_dam_eur"]
    assert dam.loc[2] < 0.95 * dam.loc[1]

    summary = (out_dir / "00_summary" / "SUMMARY.md").read_text(
        encoding="utf-8",
    )
    assert "Price scenarios" in summary and "Central" in summary
    plots = out_dir / "04_financial_plots"
    assert (plots / "price_path_fan.pdf").exists()
    assert (plots / "capture_kpis.pdf").exists()


@pytest.mark.slow
@pytest.mark.skipif(not _highs_available(), reason="HiGHS not installed")
def test_full_run_with_resolve_mode(tmp_path):
    """Tier-2 support-year re-solves through ``pipeline.run``: the
    delta sheet lands in the results workbook, the paths sheet stays
    Tier-1, and the SUMMARY digest names the re-solve years."""
    from pvbess_opt.io import read_workbook, write_workbook
    from pvbess_opt.pipeline import RunConfig, run

    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    ts = typed["ts"].set_index("timestamp")
    energy = [c for c in ts.columns if c.endswith("_kwh")]
    hourly = ts.resample("60min").agg(
        {c: ("sum" if c in energy else "mean") for c in ts.columns},
    ).reset_index()
    typed["ts"] = hourly
    typed["project"]["project_lifecycle_years"] = 5
    typed["simulation"]["plot_daily_scope"] = "none"
    typed["simulation"]["plot_monthly_scope"] = "none"
    typed["scenario_engine"]["price_scenarios_enabled"] = True
    typed["scenario_engine"]["scenario_projection_mode"] = "resolve"
    typed["scenario_engine"]["scenario_resolve_years"] = "1,3"
    typed["scenario_engine"]["scenario_resolve_resolution"] = 60
    typed["price_scenarios"] = [{
        "name": "Central", "provider": "parametric", "vintage": "v",
        "weight_pct": 100.0, "store_path": "central", "notes": "",
    }]
    store = tmp_path / "central"
    store.mkdir()
    (store / "meta.yaml").write_text(
        yaml.safe_dump({
            "provider": "parametric",
            "parametric": {"dam_level_pct_per_yr": -10.0},
        }),
        encoding="utf-8",
    )
    workbook = tmp_path / "resolve.xlsx"
    write_workbook(typed, workbook)

    run(RunConfig(
        excel=workbook, solver="highs", outdir=tmp_path / "out",
        mip_gap=0.05, time_limit=300,
    ))

    run_dirs = list((tmp_path / "out").glob("resolve_*"))
    assert len(run_dirs) == 1
    results = run_dirs[0] / "03_results.xlsx"
    delta = pd.read_excel(results, sheet_name="scenario_resolve_delta")
    assert set(delta["stream"]) == {
        "revenue_dam_pv", "revenue_dam_bess_export",
        "expense_dam_bess_charge",
    }
    assert set(delta["project_year"]) == {1, 3}
    # Year 1 anchors both tiers at exactly 1.
    year1 = delta[delta["project_year"] == 1]
    assert (year1["g_tier1_reprice"] == 1.0).all()
    assert (year1["g_tier2_resolve"] == 1.0).all()
    # The parametric level drift is a uniform scale, so the Tier-1
    # year-3 factor on the live streams is exactly 0.9^2; the shipped
    # workbook never charges the BESS from the grid, so the charge
    # stream rides the zero-volume guard (flat 1.0).  The Tier-2
    # factors re-solved the dispatch and only need to be positive here
    # (their closed-form contracts live in
    # tests/test_pricedata_resolve.py).
    year3 = delta[delta["project_year"] == 3].set_index("stream")
    assert year3.loc[
        "revenue_dam_pv", "g_tier1_reprice"
    ] == pytest.approx(0.81, rel=1e-6)
    assert year3.loc[
        "revenue_dam_bess_export", "g_tier1_reprice"
    ] == pytest.approx(0.81, rel=1e-6)
    assert year3.loc[
        "expense_dam_bess_charge", "g_tier1_reprice"
    ] == pytest.approx(1.0)
    assert (year3["g_tier2_resolve"] > 0.0).all()
    # The paths sheet stays Tier-1 — the fan compares every scenario
    # on the same frozen-dispatch footing.
    sheet = pd.read_excel(results, sheet_name="scenario_price_paths")
    year2 = sheet[sheet["project_year"] == 2].iloc[0]
    assert year2["g_revenue_dam_pv"] == pytest.approx(0.9, rel=1e-6)

    summary = (run_dirs[0] / "00_summary" / "SUMMARY.md").read_text(
        encoding="utf-8",
    )
    assert "`resolve`" in summary and "Tier-2 re-solves" in summary
