"""Sensitivity-driver monotonicity under the balancing-on cashflow.

The Revenue, CAPEX and OPEX drivers must produce monotone NPV / IRR
movements relative to the base case:

* Revenue: NPV(low) < NPV(base) < NPV(high); same for IRR.
* CAPEX:   NPV(high) < NPV(base) < NPV(low); same for IRR.
* OPEX:    NPV(high) < NPV(base) < NPV(low); same for IRR.

Before the balancing-revenue fix the Revenue driver only scaled the
DAM + retail stack and left ``balancing_revenue_eur`` untouched, so a
"+10 % Revenue" scenario could still produce a lower NPV than the
base when balancing dominated.  This regression test pins all three
drivers under a balancing-on cashflow that mirrors the canonical
workbook proportions (the audit run reports balancing carrying
roughly a fifth of revenue at the case-study scale).
"""

from __future__ import annotations

from pvbess_opt.economics import build_yearly_cashflow, compute_financial_kpis
from pvbess_opt.sensitivity import run_sensitivity_analysis


def _econ() -> dict:
    return {
        "project_lifecycle_years": 20,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 1.0,
        "retail_inflation_pct": 2.0,
        "dam_inflation_pct": 2.0,
        "bm_inflation_pct": 2.0,
        "capex_pv_eur_per_kw": 525.0,
        # 300 EUR/kW x 5000 kW / 20000 kWh: total BESS CAPEX unchanged.
        "capex_bess_eur_per_kwh": 75.0,
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
        "aggregator_fee_pct_revenue": 2.0,
    }


def _capacities() -> dict:
    return {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}


def _year1_kpis_with_balancing() -> dict:
    """Year-1 KPI dict with non-trivial DAM + retail + balancing splits.

    The numbers roughly track the case-study workbook (balancing
    carrying ~20 % of total revenue) so the test exercises the regime
    where the old bug visibly broke monotonicity.
    """
    return {
        "profit_load_from_pv_eur": 110_000.0,
        "profit_load_from_bess_eur": 70_000.0,
        "profit_export_from_pv_eur": 60_000.0,
        "profit_export_from_bess_eur": 55_000.0,
        "expense_charge_bess_grid_eur": 12_000.0,
        "profit_total_eur": 110_000.0 + 70_000.0 + 60_000.0 + 55_000.0 - 12_000.0,
        "bm_total_capacity_revenue_eur": 38_000.0,
        "bm_total_activation_revenue_eur": 12_000.0,
    }


def _base_kpis(econ: dict, caps: dict, year1_kpis: dict) -> dict:
    cf = build_yearly_cashflow(year1_kpis, econ, caps)
    return compute_financial_kpis(cf, econ)


def _scenario(sens, variable: str, scenario: str, metric: str) -> float:
    row = sens.loc[
        (sens["variable"] == variable) & (sens["scenario"] == scenario)
    ]
    return float(row[metric].iloc[0])


def test_revenue_npv_monotonic_under_balancing():
    econ, caps, kpis = _econ(), _capacities(), _year1_kpis_with_balancing()
    sens = run_sensitivity_analysis(kpis, econ, caps, _base_kpis(econ, caps, kpis))
    low = _scenario(sens, "Revenue", "low", "npv_eur")
    base = _scenario(sens, "Revenue", "base", "npv_eur")
    high = _scenario(sens, "Revenue", "high", "npv_eur")
    assert low < base < high


def test_revenue_irr_monotonic_under_balancing():
    econ, caps, kpis = _econ(), _capacities(), _year1_kpis_with_balancing()
    sens = run_sensitivity_analysis(kpis, econ, caps, _base_kpis(econ, caps, kpis))
    low = _scenario(sens, "Revenue", "low", "irr_pct")
    base = _scenario(sens, "Revenue", "base", "irr_pct")
    high = _scenario(sens, "Revenue", "high", "irr_pct")
    assert low < base < high


def test_capex_npv_monotonic_under_balancing():
    econ, caps, kpis = _econ(), _capacities(), _year1_kpis_with_balancing()
    sens = run_sensitivity_analysis(kpis, econ, caps, _base_kpis(econ, caps, kpis))
    low = _scenario(sens, "CAPEX", "low", "npv_eur")
    base = _scenario(sens, "CAPEX", "base", "npv_eur")
    high = _scenario(sens, "CAPEX", "high", "npv_eur")
    assert high < base < low


def test_capex_irr_monotonic_under_balancing():
    econ, caps, kpis = _econ(), _capacities(), _year1_kpis_with_balancing()
    sens = run_sensitivity_analysis(kpis, econ, caps, _base_kpis(econ, caps, kpis))
    low = _scenario(sens, "CAPEX", "low", "irr_pct")
    base = _scenario(sens, "CAPEX", "base", "irr_pct")
    high = _scenario(sens, "CAPEX", "high", "irr_pct")
    assert high < base < low


def test_opex_npv_monotonic_under_balancing():
    econ, caps, kpis = _econ(), _capacities(), _year1_kpis_with_balancing()
    sens = run_sensitivity_analysis(kpis, econ, caps, _base_kpis(econ, caps, kpis))
    low = _scenario(sens, "OPEX", "low", "npv_eur")
    base = _scenario(sens, "OPEX", "base", "npv_eur")
    high = _scenario(sens, "OPEX", "high", "npv_eur")
    assert high < base < low


def test_opex_irr_monotonic_under_balancing():
    econ, caps, kpis = _econ(), _capacities(), _year1_kpis_with_balancing()
    sens = run_sensitivity_analysis(kpis, econ, caps, _base_kpis(econ, caps, kpis))
    low = _scenario(sens, "OPEX", "low", "irr_pct")
    base = _scenario(sens, "OPEX", "base", "irr_pct")
    high = _scenario(sens, "OPEX", "high", "irr_pct")
    assert high < base < low


def test_revenue_driver_value_includes_balancing():
    """The Revenue driver's reported base value must include balancing.

    The ``value`` column on the ``base`` row of the Revenue driver is
    the Year-1 revenue base (the +/- scenarios scale every subsequent
    year by the same factor, so the label's EUR values stay Year-1);
    it must be the sum of DAM + retail + balancing rather than DAM +
    retail alone.  Otherwise the dumbbell tornado would annotate the
    wrong EUR base.
    """
    econ, caps, kpis = _econ(), _capacities(), _year1_kpis_with_balancing()
    sens = run_sensitivity_analysis(kpis, econ, caps, _base_kpis(econ, caps, kpis))
    base_value = _scenario(sens, "Revenue", "base", "value")
    # Build the yearly cashflow ourselves and sum revenue + balancing.
    cf = build_yearly_cashflow(kpis, econ, caps)
    year1 = cf["project_year"] == 1
    expected = float(
        cf.loc[year1, "revenue_eur"].sum()
        + cf.loc[year1, "balancing_revenue_eur"].sum()
    )
    assert abs(base_value - expected) < 1e-6
