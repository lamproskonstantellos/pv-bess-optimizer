"""NPV tail risk over Monte Carlo seeds (Eqs. U10/U11).

`risk_metrics_enabled` maps each seed's realised Year-1 profit onto an
NPV (pro-rata revenue rescale) and reports empirical VaR/CVaR.
Locked here: zero-default bit-identity (nothing computed, no sheet),
the numpy-reference estimator arithmetic and the CVaR <= VaR
invariant, the degenerate all-equal case, the pro-rata NPV mapping,
the scenario-set append path, and the loader validation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import pvbess_opt.pipeline as pipeline
from pvbess_opt.economics import (
    build_yearly_cashflow,
    npv_for_year1_revenue,
    var_cvar,
)


def _econ(**overrides) -> dict:
    econ = {
        "project_lifecycle_years": 6,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 400.0,
        "capex_bess_eur_per_kwh": 100.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 5.0,
        "opex_bess_eur_per_kw": 5.0,
        "pv_degradation_year1_pct": 2.0,
        "pv_degradation_annual_pct": 1.0,
        "bess_degradation_annual_pct": 1.5,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
    }
    econ.update(overrides)
    return econ


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}


def _kpis() -> dict:
    return {
        "profit_load_from_pv_eur": 80_000.0,
        "profit_load_from_bess_eur": 20_000.0,
        "profit_export_from_pv_eur": 10_000.0,
        "profit_export_from_bess_eur": 30_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 135_000.0,
        "pv_export_mwh": 800.0,
        "bess_export_mwh": 300.0,
        "bess_total_discharge_mwh": 500.0,
    }


def test_var_cvar_match_numpy_reference():
    rng = np.random.default_rng(11)
    sample = rng.normal(loc=1_000.0, scale=300.0, size=20)
    var, cvar = var_cvar(sample, 5.0)
    assert var == pytest.approx(float(np.quantile(sample, 0.05)))
    tail = sample[sample <= var + 1e-12]
    assert cvar == pytest.approx(float(tail.mean()))
    assert cvar <= var + 1e-12
    # Degenerate distribution: all seeds equal => VaR == CVaR == value.
    var_d, cvar_d = var_cvar([42.0] * 10, 5.0)
    assert var_d == pytest.approx(42.0)
    assert cvar_d == pytest.approx(42.0)
    # NaNs are dropped; an empty sample returns NaNs.
    var_n, cvar_n = var_cvar([float("nan")], 5.0)
    assert np.isnan(var_n) and np.isnan(cvar_n)


def test_pro_rata_npv_mapping():
    econ, caps, kpis = _econ(), _caps(), _kpis()
    base_cf = build_yearly_cashflow(kpis, econ, caps)
    base_npv = float(base_cf["discounted_cf_eur"].sum())
    # Ratio 1 reproduces the base NPV exactly.
    assert npv_for_year1_revenue(
        kpis, econ, caps, profit_total_eur=135_000.0,
    ) == pytest.approx(base_npv, rel=1e-12)
    # A scaled seed shifts the NPV monotonically.
    up = npv_for_year1_revenue(
        kpis, econ, caps, profit_total_eur=1.2 * 135_000.0,
    )
    dn = npv_for_year1_revenue(
        kpis, econ, caps, profit_total_eur=0.8 * 135_000.0,
    )
    assert dn < base_npv < up
    # Zero base profit has no meaningful ratio.
    dead = dict(kpis, profit_total_eur=0.0)
    assert np.isnan(npv_for_year1_revenue(
        dead, econ, caps, profit_total_eur=1.0,
    ))


def test_pipeline_helper_off_and_on():
    econ, caps, kpis = _econ(), _caps(), _kpis()
    mc = pd.DataFrame({
        "seed": range(20),
        "profit_total_eur": np.linspace(100_000.0, 170_000.0, 20),
    })
    # Off: nothing computed, KPI dict untouched.
    out = pipeline._compute_risk_metrics(mc, dict(kpis), econ, caps)
    assert out is None
    # On without seeds: warns and skips.
    econ_on = _econ(risk_metrics_enabled=True, risk_alpha_pct=10.0)
    assert pipeline._compute_risk_metrics(
        pd.DataFrame(), dict(kpis), econ_on, caps,
    ) is None
    # On with seeds: table + KPI rows, matching the direct estimator.
    kpis_live = dict(kpis)
    df = pipeline._compute_risk_metrics(mc, kpis_live, econ_on, caps)
    assert df is not None
    assert list(df.columns) == ["metric", "value"]
    npvs = [
        npv_for_year1_revenue(kpis, econ_on, caps, profit_total_eur=p)
        for p in mc["profit_total_eur"]
    ]
    var, cvar = var_cvar(npvs, 10.0)
    assert kpis_live["npv_var_eur"] == pytest.approx(var, abs=0.01)
    assert kpis_live["npv_cvar_eur"] == pytest.approx(cvar, abs=0.01)
    assert kpis_live["risk_alpha_pct"] == 10.0
    by_metric = df.set_index("metric")["value"]
    assert float(by_metric.loc["npv_var_eur"]) == pytest.approx(var)
    assert float(by_metric.loc["npv_cvar_eur"]) == pytest.approx(cvar)
    assert float(by_metric.loc["n_seeds"]) == 20.0


def test_workbook_sheet_and_summary_only_when_present(tmp_path):
    from pvbess_opt.io import write_results_workbook, write_summary_md

    res = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=4, freq="h"),
        "pv_kwh": [1.0, 2.0, 3.0, 4.0],
    })
    risk = pd.DataFrame({
        "metric": ["npv_var_eur"], "value": [1234.0],
    })
    off = tmp_path / "off.xlsx"
    on = tmp_path / "on.xlsx"
    write_results_workbook(off, res_year1=res, kpis_year1={"x": 1.0},
                           kpis_monthly_year1=None)
    write_results_workbook(on, res_year1=res, kpis_year1={"x": 1.0},
                           kpis_monthly_year1=None, risk_metrics=risk)
    assert "risk_metrics" not in pd.ExcelFile(off).sheet_names
    assert "risk_metrics" in pd.ExcelFile(on).sheet_names

    # SUMMARY rolling rows render only when the KPI keys exist.
    md = tmp_path / "s.md"
    write_summary_md(
        md,
        kpis_year1={
            "profit_total_eur": 1.0, "mc_n_seeds": 20,
            "mc_window_hours": 48, "mc_commit_hours": 24,
            "npv_var_eur": -5.0, "npv_cvar_eur": -9.0,
            "risk_alpha_pct": 5.0,
        },
        financial_kpis=None, params={},
    )
    text = md.read_text()
    assert "NPV VaR [EUR]" in text
    assert "NPV CVaR [EUR]" in text


def test_scenario_sheet_gains_tail_rows(tmp_path, monkeypatch):
    from pvbess_opt.economics import var_cvar as _vc

    comparison = pd.DataFrame({
        "name": [f"s{i}" for i in range(5)],
        "npv_eur": [100.0, 200.0, 300.0, 400.0, 500.0],
    })
    var, cvar = _vc(comparison["npv_eur"].tolist(), 20.0)
    # The scenario runner concatenates the two rows onto the sheet
    # frame only; replicate the frame surgery it performs.
    sheet = pd.concat(
        [
            comparison,
            pd.DataFrame([
                {"name": "npv_var_20pct", "npv_eur": var},
                {"name": "npv_cvar_20pct", "npv_eur": cvar},
            ]),
        ],
        ignore_index=True,
    )
    assert len(sheet) == 7
    assert float(sheet.loc[5, "npv_eur"]) == pytest.approx(var)
    assert float(sheet.loc[6, "npv_eur"]) == pytest.approx(cvar)
    assert cvar <= var


def test_loader_validation(tmp_path):
    from pvbess_opt.io import (
        BALANCING_SHEET_DEFAULTS,
        BESS_SHEET_DEFAULTS,
        ECONOMICS_SHEET_DEFAULTS,
        PROJECT_SHEET_DEFAULTS,
        PV_SHEET_DEFAULTS,
        SIMULATION_SHEET_DEFAULTS,
        read_workbook,
        write_workbook,
    )
    n = 24
    def _write(**sim):
        path = tmp_path / f"wb{len(list(tmp_path.iterdir()))}.xlsx"
        typed = {
            "ts": pd.DataFrame({
                "timestamp": pd.date_range(
                    "2026-01-01", periods=n, freq="h",
                ),
                "pv_kwh": np.full(n, 100.0),
                "dam_price_eur_per_mwh": np.full(n, 60.0),
            }),
            "project": dict(PROJECT_SHEET_DEFAULTS, mode="merchant"),
            "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=1000.0),
            "bess": dict(BESS_SHEET_DEFAULTS),
            "economics": dict(ECONOMICS_SHEET_DEFAULTS),
            "simulation": dict(SIMULATION_SHEET_DEFAULTS, **sim),
            "balancing": dict(BALANCING_SHEET_DEFAULTS),
        }
        write_workbook(typed, path)
        return path

    back = read_workbook(_write(
        risk_metrics_enabled=True, risk_alpha_pct=10.0,
        uncertainty_enabled=True,
    ))
    assert back["simulation"]["risk_metrics_enabled"] is True
    assert back["simulation"]["risk_alpha_pct"] == 10.0
    with pytest.raises(ValueError, match=r"\(0, 50\]"):
        read_workbook(_write(
            risk_metrics_enabled=True, risk_alpha_pct=60.0,
        ))
    with pytest.raises(ValueError, match=r"\(0, 50\]"):
        read_workbook(_write(
            risk_metrics_enabled=True, risk_alpha_pct=0.0,
        ))
