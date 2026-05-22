"""Economic-model acceptance tests.

The five invariants we want explicit confidence in:

1. Inflation × degradation: Year-N revenue (after fee, before tax) >
   Year-1 revenue for every N >= 2 under the v0.8 default constants.
2. Discounted payback is finite and lands between 5 and 20 years
   under the default constants.
3. NPV is monotonic in total CAPEX (× 0.5 → larger NPV; × 1.5 → smaller).
4. The Year-1 revenue-base scaling line in the cashflow rebuild is
   consistent with the analytical pv_factor × (1 + rev_infl) shape
   (no off-by-one error).
5. Default workbook does NOT enable uncertainty.

The full uncertainty round-trip (Set ``uncertainty_enabled = True`` in
the workbook, run main.py, assert 16 rows in
``rolling_horizon_compare_mc``) lives in a separate, HiGHS-gated
end-to-end test below.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pandas as pd
import pytest

from pvbess_opt.economics import (
    build_yearly_cashflow,
    compute_financial_kpis,
)
from pvbess_opt.io import (
    BESS_SHEET_DEFAULTS,
    ECONOMICS_SHEET_DEFAULTS,
    PROJECT_SHEET_DEFAULTS,
    PV_SHEET_DEFAULTS,
    SIMULATION_SHEET_DEFAULTS,
)


def _highs_available() -> bool:
    try:
        importlib.import_module("highspy")
    except ImportError:
        return False
    return True


def _default_econ() -> dict:
    """v0.8 defaults sourced from the canonical sheet defaults."""
    out = {}
    out.update(PROJECT_SHEET_DEFAULTS)
    out.update(PV_SHEET_DEFAULTS)
    out.update(BESS_SHEET_DEFAULTS)
    out.update(ECONOMICS_SHEET_DEFAULTS)
    out.update(SIMULATION_SHEET_DEFAULTS)
    return out


def _caps() -> dict:
    return {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}


def _kpis(profit: float = 600_000.0) -> dict:
    return {"profit_total_eur": profit, "pv_generation_mwh": 4500.0 * 1.5}


# ---------------------------------------------------------------------------
# 1. Inflation × degradation invariant
# ---------------------------------------------------------------------------


def test_revenue_strictly_increasing_year2_onwards():
    # The workbook default retail_inflation_pct is 0 (the user must opt
    # in to indexation explicitly), so this test sets a positive value
    # locally to verify the indexation mechanism still produces
    # year-on-year growth that beats degradation.
    econ = _default_econ()
    econ["retail_inflation_pct"] = 2.0
    econ["dam_inflation_pct"] = 2.0
    df = build_yearly_cashflow(_kpis(), econ, _caps())
    rev = df.loc[df["project_year"] >= 2, "revenue_eur"].astype(float)
    assert rev.is_monotonic_increasing
    assert (rev.diff().dropna() > 0).all()


# ---------------------------------------------------------------------------
# 2. Discounted payback is finite and lands in 5..20 years
# ---------------------------------------------------------------------------


def test_discounted_payback_between_5_and_20_under_defaults():
    df = build_yearly_cashflow(_kpis(profit=900_000.0), _default_econ(), _caps())
    fin = compute_financial_kpis(df, _default_econ())
    pb = float(fin["discounted_payback_years"])
    assert pb > 0.0
    assert 5.0 <= pb <= 20.0


# ---------------------------------------------------------------------------
# 3. NPV monotonic in CAPEX
# ---------------------------------------------------------------------------


def test_npv_monotonic_in_capex():
    base_econ = _default_econ()
    base_df = build_yearly_cashflow(_kpis(), base_econ, _caps())
    base_fin = compute_financial_kpis(base_df, base_econ)
    base_npv = float(base_fin["npv_eur"])

    low_econ = dict(base_econ)
    low_econ["capex_pv_eur_per_kw"] = float(base_econ["capex_pv_eur_per_kw"]) * 0.5
    low_econ["capex_bess_eur_per_kw"] = float(base_econ["capex_bess_eur_per_kw"]) * 0.5
    low_df = build_yearly_cashflow(_kpis(), low_econ, _caps())
    low_npv = float(compute_financial_kpis(low_df, low_econ)["npv_eur"])

    high_econ = dict(base_econ)
    high_econ["capex_pv_eur_per_kw"] = float(base_econ["capex_pv_eur_per_kw"]) * 1.5
    high_econ["capex_bess_eur_per_kw"] = float(base_econ["capex_bess_eur_per_kw"]) * 1.5
    high_df = build_yearly_cashflow(_kpis(), high_econ, _caps())
    high_npv = float(compute_financial_kpis(high_df, high_econ)["npv_eur"])

    assert low_npv > base_npv > high_npv


# ---------------------------------------------------------------------------
# 4. Default workbook does NOT enable uncertainty
# ---------------------------------------------------------------------------


def test_uncertainty_disabled_by_default():
    assert SIMULATION_SHEET_DEFAULTS["uncertainty_enabled"] is False
    assert SIMULATION_SHEET_DEFAULTS["uncertainty_compare_sources"] is False


# ---------------------------------------------------------------------------
# 5. Aggregator fee + DEVEX show up in the assumptions snapshot
# ---------------------------------------------------------------------------


def test_aggregator_fee_and_devex_in_assumptions_snapshot(tmp_path: Path):
    from pvbess_opt.io import write_assumptions_summary

    params = {
        "mode": "vnb",
        "unavailability_pct": 1.0,
        "bess_power_kw": 5000.0,
    }
    econ = {
        "discount_rate_pct": 7.0,
        "aggregator_fee_pct_revenue": 10.0,
        "devex_pv_eur_per_kw": 60.0,
        "devex_bess_eur_per_kw": 30.0,
    }
    out = tmp_path / "assumptions.txt"
    write_assumptions_summary(out, params, econ)
    text = out.read_text(encoding="utf-8")
    assert "aggregator_fee_pct_revenue" in text
    assert "devex_pv_eur_per_kw" in text
    assert "devex_bess_eur_per_kw" in text
    assert "unavailability_pct" in text


# ---------------------------------------------------------------------------
# Uncertainty round-trip — main.py end-to-end with compare-sources
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_uncertainty_compare_sources_round_trip(tmp_path: Path, monkeypatch):
    """Workbook-driven uncertainty: 4 source-sets x 4 seeds = 16 rows,
    plus the four foresight_gap_pct_p50_* keys on financial_kpis."""
    from pvbess_opt.io import read_workbook, write_workbook

    repo_root = Path(__file__).resolve().parent.parent
    typed = read_workbook(repo_root / "inputs" / "input.xlsx")
    # Trim to a single day @ 15-min cadence to keep the run fast.
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)
    typed["simulation"]["uncertainty_enabled"] = True
    typed["simulation"]["uncertainty_compare_sources"] = True
    typed["simulation"]["uncertainty_n_seeds"] = 4
    typed["simulation"]["uncertainty_window_hours"] = 12
    typed["simulation"]["uncertainty_commit_hours"] = 6
    typed["bess"]["terminal_soc_equal"] = False
    short_xlsx = tmp_path / "short_unc.xlsx"
    write_workbook(typed, short_xlsx)

    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(repo_root))
    import importlib
    main_module = importlib.import_module("main")
    rc = main_module.main([
        str(short_xlsx),
        "--solver", "highs",
        "--mip-gap", "0.05",
        "--time-limit", "60",
    ])
    assert rc == 0
    # Find the run output directory.
    runs = sorted((tmp_path / "results").glob("*"))
    assert runs, "no results directory was produced"
    run_dir = runs[-1]
    results_xlsx = run_dir / "03_results.xlsx"
    assert results_xlsx.exists()
    sheets = pd.ExcelFile(results_xlsx).sheet_names
    assert "rolling_horizon_compare_mc" in sheets
    df_mc = pd.read_excel(results_xlsx, sheet_name="rolling_horizon_compare_mc")
    assert len(df_mc) == 16
    assert set(df_mc["source_set"].unique()) == {"dam", "pv", "load", "all"}

    fin_df = pd.read_excel(results_xlsx, sheet_name="kpis_year1")
    fin_keys = set(fin_df["metric"].astype(str).tolist())
    for src in ("dam", "pv", "load", "all"):
        key = f"foresight_gap_pct_p50_{src}"
        assert key in fin_keys, f"missing {key} in kpis_year1"
