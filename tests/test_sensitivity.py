"""Sensitivity tornado tests."""

from __future__ import annotations

import pytest

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
        "retail_inflation_pct": 2.0,
        "dam_inflation_pct": 2.0,
        "capex_pv_eur_per_kw": 525.0,
        # 200 EUR/kW x 5000 kW / 20000 kWh: total BESS CAPEX unchanged.
        "capex_bess_eur_per_kwh": 50.0,
        "devex_pv_eur_per_kw": 60.0,
        "devex_bess_eur_per_kw": 30.0,
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


def test_capex_driver_value_is_year0_outlay():
    """The tornado CAPEX driver VALUE equals the Year-0 outlay (matching
    the Year-0 stack in the charts and initial_investment_eur), even
    when a scheduled BESS replacement adds CAPEX later in the horizon.
    The perturbation itself still scales the replacement row too."""
    kpis = {"profit_total_eur": 500_000.0}
    caps = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    econ = {**_econ(), "bess_replacement_year": 5}

    cf = build_yearly_cashflow(kpis, econ, caps)
    fin = compute_financial_kpis(cf, econ)
    y0 = cf["project_year"] == 0
    year0_outlay = float(
        cf.loc[y0, "capex_eur"].sum() + cf.loc[y0, "devex_eur"].sum()
    )
    lifecycle_total = float(cf["capex_eur"].sum() + cf["devex_eur"].sum())
    assert lifecycle_total < year0_outlay  # replacement makes it larger (more negative)

    sens = run_sensitivity_analysis(kpis, econ, caps, fin)
    capex_rows = sens.loc[sens["variable"] == "CAPEX"].set_index("scenario")
    assert float(capex_rows.loc["base", "value"]) == pytest.approx(year0_outlay)
    assert float(capex_rows.loc["base", "value"]) == pytest.approx(
        fin["initial_investment_eur"], abs=0.01,
    )
    assert float(capex_rows.loc["low", "value"]) == pytest.approx(
        year0_outlay * 0.9,
    )
    assert float(capex_rows.loc["high", "value"]) == pytest.approx(
        year0_outlay * 1.1,
    )
