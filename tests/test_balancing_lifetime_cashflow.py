"""Lifetime cashflow scaling tests for the balancing revenue lines.

Asserts that the per-year ``balancing_capacity_revenue_eur`` and
``balancing_activation_revenue_eur`` columns from
:func:`pvbess_opt.economics.build_yearly_cashflow` satisfy
``year_y = year_1 * bess_factor(y) * (1 + bm_inflation)^(y-1)``.

Catches future drift between the lifetime degradation logic
(``pvbess_opt.lifetime._bess_factor``) and the cashflow projection
(``pvbess_opt.economics.build_yearly_cashflow``).
"""

from __future__ import annotations

from pvbess_opt.economics import build_yearly_cashflow
from pvbess_opt.lifetime import _bess_factor


def _econ(**overrides) -> dict:
    base: dict = {
        "project_lifecycle_years": 10,
        "project_start_year": 2026,
        "capex_pv_eur_per_kw": 0.0,
        "capex_bess_eur_per_kw": 0.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "site_capex_eur": 0.0,
        "site_devex_eur": 0.0,
        "opex_pv_eur_per_kwp": 0.0,
        "opex_bess_eur_per_kw": 0.0,
        "opex_inflation_pct": 0.0,
        "discount_rate_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "pv_degradation_year1_pct": 0.0,
        "pv_degradation_annual_pct": 0.0,
        "bess_degradation_annual_pct": 2.0,
        "bess_degradation_pct_per_cycle": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
        "bm_inflation_pct": 3.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
    }
    base.update(overrides)
    return base


def test_year5_balancing_revenue_matches_bess_factor_and_inflation():
    year1_kpis = {
        "bm_total_capacity_revenue_eur": 12_000.0,
        "bm_total_activation_revenue_eur": 4_500.0,
        # No DAM/retail breakdown: keep the rest of the cashflow zero so
        # the only revenue lines are the balancing ones.
        "profit_total_eur": 0.0,
        "bess_total_discharge_mwh": 0.0,
    }
    capacities = {"pv_kwp": 0.0, "bess_kw": 1000.0, "bess_kwh": 4000.0}
    econ = _econ()
    df = build_yearly_cashflow(year1_kpis, econ, capacities)

    bm_infl = 0.03
    d_annual = 0.02
    for y in (1, 3, 5, 7, 10):
        bess_f = _bess_factor(y, d_annual)
        expected_cap = (
            year1_kpis["bm_total_capacity_revenue_eur"]
            * bess_f * (1.0 + bm_infl) ** (y - 1)
        )
        expected_act = (
            year1_kpis["bm_total_activation_revenue_eur"]
            * bess_f * (1.0 + bm_infl) ** (y - 1)
        )
        row = df.loc[df["project_year"] == y].iloc[0]
        assert abs(row["balancing_capacity_revenue_eur"] - expected_cap) < 1e-4
        assert abs(row["balancing_activation_revenue_eur"] - expected_act) < 1e-4
        # And the aggregate line equals the sum of the two streams.
        assert abs(
            row["balancing_revenue_eur"] - (expected_cap + expected_act),
        ) < 1e-4


def test_year0_balancing_revenue_is_zero():
    """Year 0 is the CAPEX year; no operational revenue should accrue."""
    year1_kpis = {
        "bm_total_capacity_revenue_eur": 12_000.0,
        "bm_total_activation_revenue_eur": 4_500.0,
        "profit_total_eur": 0.0,
        "bess_total_discharge_mwh": 0.0,
    }
    capacities = {"pv_kwp": 0.0, "bess_kw": 1000.0, "bess_kwh": 4000.0}
    df = build_yearly_cashflow(year1_kpis, _econ(), capacities)
    row = df.loc[df["project_year"] == 0].iloc[0]
    assert row["balancing_capacity_revenue_eur"] == 0.0
    assert row["balancing_activation_revenue_eur"] == 0.0
    assert row["balancing_revenue_eur"] == 0.0


def test_zero_inflation_keeps_pure_bess_factor_decay():
    year1_kpis = {
        "bm_total_capacity_revenue_eur": 10_000.0,
        "bm_total_activation_revenue_eur": 0.0,
        "profit_total_eur": 0.0,
        "bess_total_discharge_mwh": 0.0,
    }
    capacities = {"pv_kwp": 0.0, "bess_kw": 1000.0, "bess_kwh": 4000.0}
    econ = _econ(bm_inflation_pct=0.0, bess_degradation_annual_pct=5.0)
    df = build_yearly_cashflow(year1_kpis, econ, capacities)
    for y in (1, 2, 3, 5, 10):
        bess_f = _bess_factor(y, 0.05)
        row = df.loc[df["project_year"] == y].iloc[0]
        assert abs(
            row["balancing_capacity_revenue_eur"] - 10_000.0 * bess_f,
        ) < 1e-4
