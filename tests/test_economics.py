"""Multi-year economics: cashflow projection + financial KPIs + sensitivity."""

from __future__ import annotations

import numpy as np
import pandas as pd

from pvbess_opt.economics import (
    build_yearly_cashflow,
    calculate_irr,
    compute_financial_kpis,
    derive_asset_capacities,
    derive_monthly_cashflow,
    read_economic_params,
)


def _econ() -> dict:
    return {
        "project_lifecycle_years": 5,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 1.0,
        "retail_inflation_pct": 2.0,
        "dam_inflation_pct": 2.0,
        "capex_pv_eur_per_kw": 525.0,
        "capex_bess_eur_per_kw": 200.0,
        "devex_pv_eur_per_kw": 60.0,
        "devex_bess_eur_per_kw": 30.0,
        "opex_pv_eur_per_kwp": 7.0,
        "opex_bess_eur_per_kw": 14.0,
        "pv_degradation_year1_pct": 2.5,
        "pv_degradation_annual_pct": 0.55,
        "bess_degradation_annual_pct": 2.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 50.0,
        "sensitivity_enabled": True,
        "sensitivity_capex_delta_pct": 10.0,
        "sensitivity_opex_delta_pct": 10.0,
        "sensitivity_revenue_delta_pct": 10.0,
        "sensitivity_discount_rate_delta_pp": 2.0,
        "show_titles": False,
        "currency_format": "auto",
        "plot_daily_scope": "year1_only",
        "plot_monthly_scope": "all",
        "plot_yearly_scope": "all",
    }


def test_calculate_irr_simple():
    cf = np.array([-1000.0, 600.0, 600.0])
    irr = calculate_irr(cf)
    # NPV(13.0656...%) = 0
    assert 0.12 < irr < 0.14


def test_calculate_irr_no_root_returns_nan():
    cf = np.array([100.0, 100.0, 100.0])
    assert np.isnan(calculate_irr(cf))


def test_read_economic_params_via_workbook(repo_input_xlsx):
    econ = read_economic_params(repo_input_xlsx)
    assert econ["discount_rate_pct"] == 7.0
    assert econ["project_lifecycle_years"] == 20


def test_derive_asset_capacities():
    params = {
        "dt_minutes": 60,
        "pv_nameplate_kwp": 4500.0,
        "bess_power_kw": 5000.0,
        "bess_capacity_kwh": 20000.0,
    }
    ts = pd.DataFrame({"pv_kwh": [4500.0, 0.0]})
    caps = derive_asset_capacities(_econ(), params, ts)
    assert caps["pv_kwp"] == 4500.0
    assert caps["bess_kw"] == 5000.0
    assert caps["bess_kwh"] == 20000.0


def test_build_yearly_cashflow_lowercase_keys():
    kpis = {"profit_total_eur": 200_000.0}
    caps = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    df = build_yearly_cashflow(kpis, _econ(), caps)
    assert "project_year" in df.columns
    assert "calendar_year" in df.columns
    assert "revenue_eur" in df.columns
    assert "net_cashflow_eur" in df.columns
    assert df.loc[df["project_year"] == 0, "capex_eur"].iloc[0] < 0


def test_calendar_year_convention():
    """Year 0 (CAPEX) is project_start_year - 1; Year 1 is project_start_year."""
    kpis = {"profit_total_eur": 200_000.0}
    caps = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    df = build_yearly_cashflow(kpis, _econ(), caps)
    y0 = int(df.loc[df["project_year"] == 0, "calendar_year"].iloc[0])
    y1 = int(df.loc[df["project_year"] == 1, "calendar_year"].iloc[0])
    assert y0 == 2025
    assert y1 == 2026


def test_compute_financial_kpis_lowercase_keys():
    kpis = {"profit_total_eur": 500_000.0}
    caps = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    yearly_cf = build_yearly_cashflow(kpis, _econ(), caps)
    fin = compute_financial_kpis(yearly_cf, _econ())
    for k in (
        "npv_eur", "irr_pct", "roi_pct", "bcr",
        "simple_payback_years", "discounted_payback_years",
        "total_capex_eur", "project_start_year", "project_end_year",
    ):
        assert k in fin
    # All keys must be lowercase
    for k in fin:
        assert any(ch.isupper() for ch in k) is False, k


def test_monthly_cashflow_sums_to_yearly_y1():
    kpis = {"profit_total_eur": 240_000.0}  # 20k/month average
    caps = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    yearly_cf = build_yearly_cashflow(kpis, _econ(), caps)
    n = 35040
    timestamps = pd.date_range("2026-01-01", periods=n, freq="15min")
    res = pd.DataFrame({
        "timestamp": timestamps,
        "pv_kwh": np.ones(n) * 100.0,
        "profit_load_from_pv_eur": np.ones(n) * (240_000.0 / n),
    })
    monthly_cf, quarterly_cf = derive_monthly_cashflow(res, yearly_cf, _econ())
    y1 = monthly_cf.loc[monthly_cf["project_year"] == 1, "revenue_eur"].sum()
    y1_yearly = float(yearly_cf.loc[yearly_cf["project_year"] == 1, "revenue_eur"].iloc[0])
    assert abs(y1 - y1_yearly) / max(abs(y1_yearly), 1.0) < 1e-3
    # quarterly aggregates
    assert "period_type" in quarterly_cf.columns
