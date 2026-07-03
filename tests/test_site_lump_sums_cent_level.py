"""Cent-level lock: site lump sums through every project-level output.

A by-hand case with site sums (1.5 MEUR) three orders of magnitude above
the per-asset sums (2,150 EUR) so any output that drops them is
unmissable.  Every expectation below is hand-computed:

    Year-0 CAPEX  = -(10 kWp x 100 + 10 kWh x 100 + 1,000,000) = -1,002,000
    Year-0 DEVEX  = -(10 x 10 + 5 x 10 + 500,000)            =   -500,150
    initial_investment_eur                                    = -1,502,150
    revenue 700,000 EUR/yr flat, opex 0, 3 years, r = 10 %.

Complements test_site_lump_sum_costs.py (directional checks) with exact
values across NPV / IRR / ROI / BCR / paybacks, the Year-0 bar geometry
(heights AND stacked bottoms), the NPV-waterfall CAPEX bar, the tornado
CAPEX driver value, the debt-schedule basis, and the SUMMARY.md row.
"""

from __future__ import annotations

import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pytest

import pvbess_opt.plotting.financial as financial_mod
from pvbess_opt.economics import (
    build_debt_schedule,
    build_yearly_cashflow,
    compute_financial_kpis,
)
from pvbess_opt.io import write_summary_md
from pvbess_opt.sensitivity import run_sensitivity_analysis

Y0_CAPEX = -(10 * 100.0 + 10 * 100.0 + 1_000_000.0)  # -1,002,000
Y0_DEVEX = -(10 * 10.0 + 5 * 10.0 + 500_000.0)       # -500,150
Y0 = Y0_CAPEX + Y0_DEVEX                              # -1,502,150
REV = 700_000.0
R = 0.10


def _econ() -> dict:
    return {
        "project_lifecycle_years": 3,
        "project_start_year": 2026,
        "discount_rate_pct": 10.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 100.0,
        # 100 EUR/kWh x 10 kWh == the original 200 EUR/kW x 5 kW = 1,000 EUR,
        # so every hand-computed total below is unchanged.
        "capex_bess_eur_per_kwh": 100.0,
        "devex_pv_eur_per_kw": 10.0,
        "devex_bess_eur_per_kw": 10.0,
        "opex_pv_eur_per_kwp": 0.0,
        "opex_bess_eur_per_kw": 0.0,
        "pv_degradation_year1_pct": 0.0,
        "pv_degradation_annual_pct": 0.0,
        "bess_degradation_annual_pct": 0.0,
        "bess_degradation_pct_per_cycle": 0.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
        "site_capex_eur": 1_000_000.0,
        "site_devex_eur": 500_000.0,
        "gearing_pct": 50.0,
        "debt_interest_rate_pct": 5.0,
        "debt_tenor_years": 3,
        "debt_repayment": "linear",
        "sensitivity_capex_delta_pct": 10.0,
        "sensitivity_opex_delta_pct": 10.0,
        "sensitivity_revenue_delta_pct": 10.0,
        "sensitivity_discount_rate_delta_pp": 2.0,
    }


def _caps() -> dict:
    return {"pv_kwp": 10.0, "bess_kw": 5.0, "bess_kwh": 10.0}


def _kpis() -> dict:
    return {
        "profit_total_eur": REV,
        "profit_load_from_pv_eur": 0.6 * REV,
        "profit_load_from_bess_eur": 0.15 * REV,
        "profit_export_from_pv_eur": 0.2 * REV,
        "profit_export_from_bess_eur": 0.05 * REV,
        "expense_charge_bess_grid_eur": 0.0,
        "pv_generation_mwh": 15.0,
        "bess_total_discharge_mwh": 3.0,
    }


@pytest.fixture(scope="module")
def cashflow_and_kpis():
    econ, caps, kpis = _econ(), _caps(), _kpis()
    cf = build_yearly_cashflow(kpis, econ, caps)
    fin = compute_financial_kpis(cf, econ, capacities=caps, year1_kpis=kpis)
    return cf, fin, econ, caps, kpis


def test_year0_columns_carry_site_sums(cashflow_and_kpis):
    cf, _fin, _e, _c, _k = cashflow_and_kpis
    y0 = cf.loc[cf["project_year"] == 0]
    assert float(y0["capex_eur"].iloc[0]) == pytest.approx(Y0_CAPEX, abs=0.005)
    assert float(y0["devex_eur"].iloc[0]) == pytest.approx(Y0_DEVEX, abs=0.005)


def test_initial_investment_and_roi_denominator(cashflow_and_kpis):
    _cf, fin, _e, _c, _k = cashflow_and_kpis
    assert fin["initial_investment_eur"] == pytest.approx(Y0, abs=0.005)
    assert fin["roi_pct"] == pytest.approx(
        3 * REV / abs(Y0) * 100.0, abs=1e-4,
    )


def test_npv_bcr_irr_to_the_cent(cashflow_and_kpis):
    _cf, fin, _e, _c, _k = cashflow_and_kpis
    npv_hand = Y0 + sum(REV / (1 + R) ** y for y in (1, 2, 3))
    assert fin["npv_eur"] == pytest.approx(npv_hand, abs=0.01)
    dcf_pos = sum(REV / (1 + R) ** y for y in (1, 2, 3))
    assert fin["bcr"] == pytest.approx(dcf_pos / abs(Y0), abs=1e-4)

    def f(rate: float) -> float:
        return Y0 + sum(REV / (1 + rate) ** y for y in (1, 2, 3))

    lo, hi = -0.99, 10.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if f(lo) * f(mid) <= 0:
            hi = mid
        else:
            lo = mid
    assert fin["irr_pct"] == pytest.approx(0.5 * (lo + hi) * 100.0, abs=0.01)


def test_paybacks_to_the_cent(cashflow_and_kpis):
    _cf, fin, _e, _c, _k = cashflow_and_kpis
    # cumulative: Y0 + 700k/yr => crossing in year 3 at 2 + 102,150/700,000.
    assert fin["simple_payback_years"] == pytest.approx(
        2 + 102_150.0 / REV, abs=1e-3,
    )
    d1, d2, d3 = (REV / 1.1, REV / 1.21, REV / 1.331)
    cum2 = Y0 + d1 + d2
    assert fin["discounted_payback_years"] == pytest.approx(
        2 + (-cum2) / d3, abs=1e-3,
    )


def test_debt_schedule_basis_includes_site_sums(cashflow_and_kpis):
    cf, _fin, econ, _c, _k = cashflow_and_kpis
    schedule = build_debt_schedule(cf, econ)
    debt = 0.5 * abs(Y0)
    assert float(schedule["principal_eur"].sum()) == pytest.approx(
        debt, abs=0.05,
    )
    assert float(schedule.loc[0, "interest_eur"]) == pytest.approx(
        debt * 0.05, abs=0.01,
    )


def test_tornado_capex_driver_value_is_year0_outlay(cashflow_and_kpis):
    _cf, fin, econ, caps, kpis = cashflow_and_kpis
    sens = run_sensitivity_analysis(kpis, econ, caps, fin)
    base = float(
        sens.loc[
            (sens["variable"] == "CAPEX") & (sens["scenario"] == "base"),
            "value",
        ].iloc[0]
    )
    assert base == pytest.approx(Y0, abs=0.005)


def _capture_bar_figure(render):
    plt.close("all")
    captured: dict = {}
    original = financial_mod.save_figure

    def _keep(out):
        captured["fig"] = plt.gcf()
        return Path(out)

    financial_mod.save_figure = _keep
    try:
        render()
    finally:
        financial_mod.save_figure = original
    return captured["fig"]


def test_year0_bar_geometry_includes_site_sums(cashflow_and_kpis, tmp_path):
    cf, _fin, _e, _c, _k = cashflow_and_kpis
    fig = _capture_bar_figure(
        lambda: financial_mod.plot_yearly_cashflow_bars(cf, tmp_path / "b.pdf"),
    )
    ax = fig.axes[0]
    by_label = {c.get_label(): c.patches[0] for c in ax.containers}
    assert by_label["CAPEX"].get_height() == pytest.approx(Y0_CAPEX, abs=0.01)
    assert by_label["DEVEX"].get_height() == pytest.approx(Y0_DEVEX, abs=0.01)
    # Stacking: the CAPEX bar hangs below the DEVEX segment.
    assert by_label["CAPEX"].get_y() == pytest.approx(Y0_DEVEX, abs=0.01)


def test_npv_waterfall_capex_bar_includes_site_sums(cashflow_and_kpis, tmp_path):
    cf, _fin, _e, _c, _k = cashflow_and_kpis
    fig = _capture_bar_figure(
        lambda: financial_mod.plot_npv_waterfall(cf, tmp_path / "w.pdf"),
    )
    ax = fig.axes[0]
    by_label = {c.get_label(): c.patches[0] for c in ax.containers}
    # Year 0 discounts at 1.0, so the discounted bar equals the outlay.
    assert by_label["CAPEX"].get_height() == pytest.approx(Y0_CAPEX, abs=0.01)


def test_summary_md_initial_investment_row(cashflow_and_kpis, tmp_path):
    _cf, fin, _e, _c, kpis = cashflow_and_kpis
    out = write_summary_md(
        tmp_path / "SUMMARY.md",
        kpis_year1=kpis,
        financial_kpis=fin,
        params={
            "mode": "self_consumption",
            "pv_nameplate_kwp": 10.0,
            "bess_power_kw": 5.0,
            "bess_capacity_kwh": 10.0,
        },
    )
    text = out.read_text()
    assert "| Initial investment, Year 0 [EUR] | -1,502,150 |" in text


def test_simple_payback_nan_when_never_recovered():
    """Site sums big enough that 3 x 200k never recovers Y0 -> NaN."""
    econ, caps = _econ(), _caps()
    kpis = dict(_kpis())
    for key in (
        "profit_total_eur", "profit_load_from_pv_eur",
        "profit_load_from_bess_eur", "profit_export_from_pv_eur",
        "profit_export_from_bess_eur",
    ):
        kpis[key] = kpis[key] * (200_000.0 / REV)
    cf = build_yearly_cashflow(kpis, econ, caps)
    fin = compute_financial_kpis(cf, econ, capacities=caps, year1_kpis=kpis)
    assert math.isnan(fin["simple_payback_years"])
