"""Sensitivity tornado tests."""

from __future__ import annotations

from pvbess_opt.economics import build_yearly_cashflow, compute_financial_kpis
from pvbess_opt.sensitivity import (
    run_sensitivity_analysis,
    variables_for_irr_sensitivity,
    variables_for_npv_sensitivity,
)


def _econ() -> dict:
    return {
        "project_lifecycle_years": 10,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 1.0,
        "revenue_inflation_pct": 2.0,
        "capex_pv_eur_per_kw": 525.0,
        "capex_bess_eur_per_kw": 200.0,
        "capex_licenses_eur_per_kw": 90.0,
        "opex_pv_eur_per_kwp": 7.0,
        "opex_bess_eur_per_kw": 14.0,
        "pv_degradation_year1_pct": 2.5,
        "pv_degradation_annual_pct": 0.55,
        "bess_degradation_annual_pct": 2.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 50.0,
        "sensitivity_capex_delta_pct": 10.0,
        "sensitivity_opex_delta_pct": 10.0,
        "sensitivity_revenue_delta_pct": 10.0,
        "sensitivity_discount_rate_delta_pp": 2.0,
    }


def test_npv_variables_include_all_four():
    vars_ = variables_for_npv_sensitivity(_econ())
    names = {v["name"] for v in vars_}
    assert names == {"CAPEX", "OPEX", "Revenue", "DiscountRate"}


def test_irr_variables_drop_discount_rate():
    vars_ = variables_for_irr_sensitivity(_econ())
    names = {v["name"] for v in vars_}
    assert "DiscountRate" not in names
    assert names == {"CAPEX", "OPEX", "Revenue"}


def test_run_sensitivity_returns_lowercase_columns():
    kpis = {"profit_total_eur": 500_000.0}
    caps = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    cf = build_yearly_cashflow(kpis, _econ(), caps)
    base_kpis = compute_financial_kpis(cf, _econ())
    sens = run_sensitivity_analysis(kpis, _econ(), caps, base_kpis)
    for col in ("variable", "scenario", "delta_value", "value", "npv_eur",
                "irr_pct", "payback_years", "delta_npv_eur",
                "delta_irr_pp", "delta_payback_years"):
        assert col in sens.columns


def test_run_sensitivity_capex_delta_changes_npv():
    kpis = {"profit_total_eur": 500_000.0}
    caps = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    cf = build_yearly_cashflow(kpis, _econ(), caps)
    base_kpis = compute_financial_kpis(cf, _econ())
    sens = run_sensitivity_analysis(kpis, _econ(), caps, base_kpis)
    capex_rows = sens.loc[sens["variable"] == "CAPEX"]
    base_npv = float(capex_rows.loc[capex_rows["scenario"] == "base", "npv_eur"].iloc[0])
    high_npv = float(capex_rows.loc[capex_rows["scenario"] == "high", "npv_eur"].iloc[0])
    low_npv = float(capex_rows.loc[capex_rows["scenario"] == "low", "npv_eur"].iloc[0])
    # +10 % CAPEX => more negative cash, lower NPV
    assert high_npv < base_npv < low_npv
