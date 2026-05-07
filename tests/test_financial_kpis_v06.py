"""v0.6 financial KPIs (LCOE / LCOS / capacity factor / lifetime cycles).

Hand-checked tiny scenario:

* PV: 1000 kWp at 500 EUR/kWp -> 500 000 EUR Year-0 CAPEX
* BESS: 500 kW / 2000 kWh at 200 EUR/kW -> 100 000 EUR Year-0 CAPEX
* Licensing: 90 EUR/kW * (1000 + 500) = 135 000 EUR Year-0 CAPEX
* OPEX: PV 7 EUR/kWp = 7 000; BESS 14 EUR/kW = 7 000 -> 14 000/yr (no inflation)
* Discount rate: 7%
* Lifetime: 5 years
* Year-1 PV: 1 800 MWh; BESS discharge: 700 MWh
* Annual degradation off (LID 0%, annual 0%, BESS 0%, opex_infl 0%, rev_infl 0%)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import build_yearly_cashflow, compute_financial_kpis


def _hand_econ() -> dict:
    return {
        "project_lifecycle_years": 5,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "revenue_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 500.0,
        "capex_bess_eur_per_kw": 200.0,
        "capex_licenses_eur_per_kw": 90.0,
        "opex_pv_eur_per_kwp": 7.0,
        "opex_bess_eur_per_kw": 14.0,
        "pv_degradation_year1_pct": 0.0,
        "pv_degradation_annual_pct": 0.0,
        "bess_degradation_annual_pct": 0.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "sensitivity_capex_delta_pct": 10.0,
        "sensitivity_opex_delta_pct": 10.0,
    }


def _hand_caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 2000.0}


def _hand_year1_kpis() -> dict:
    return {
        "profit_total_eur": 100_000.0,
        "pv_generation_mwh": 1800.0,
        "profit_load_from_pv_eur": 30_000.0,
        "profit_load_from_bess_eur": 5_000.0,
        "profit_export_from_pv_eur": 50_000.0,
        "profit_export_from_bess_eur": 17_000.0,
        "expense_charge_bess_grid_eur": 2_000.0,
    }


def _hand_lifetime_yearly() -> pd.DataFrame:
    rows = []
    for y in range(1, 6):
        rows.append({
            "project_year": y,
            "calendar_year": 2025 + y,
            "pv_generation_mwh": 1800.0,
            "bess_discharge_mwh": 700.0,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# LCOE / LCOS
# ---------------------------------------------------------------------------


def test_lcoe_formula_hand_checked():
    """LCOE = (Disc CAPEX + Disc OPEX) / Disc PV gen."""
    yearly = build_yearly_cashflow(_hand_year1_kpis(), _hand_econ(), _hand_caps())
    fin = compute_financial_kpis(
        yearly, _hand_econ(),
        capacities=_hand_caps(),
        lifetime_yearly=_hand_lifetime_yearly(),
        year1_kpis=_hand_year1_kpis(),
    )
    # Disc factors
    r = 0.07
    disc = [(1 / (1 + r) ** y) for y in range(0, 6)]
    capex_y0 = 500_000 + 100_000 + 135_000  # 735 000
    opex_per_year = 7_000 + 7_000  # 14 000
    disc_capex = capex_y0 * disc[0]
    disc_opex = sum(opex_per_year * disc[y] for y in range(1, 6))
    disc_pv = sum(1800.0 * disc[y] for y in range(1, 6))
    expected_lcoe = (disc_capex + disc_opex) / disc_pv
    assert fin["lcoe_eur_per_mwh"] == pytest.approx(expected_lcoe, rel=1e-3)


def test_lcos_formula_hand_checked():
    """LCOS = (Disc BESS CAPEX share + Disc BESS OPEX) / Disc BESS discharge."""
    yearly = build_yearly_cashflow(_hand_year1_kpis(), _hand_econ(), _hand_caps())
    fin = compute_financial_kpis(
        yearly, _hand_econ(),
        capacities=_hand_caps(),
        lifetime_yearly=_hand_lifetime_yearly(),
        year1_kpis=_hand_year1_kpis(),
    )
    r = 0.07
    disc = [(1 / (1 + r) ** y) for y in range(0, 6)]
    bess_capex_y0 = 200.0 * 500.0  # 100 000
    bess_share = 500.0 / (1000.0 + 500.0)
    bess_lic_share = 90.0 * 500.0 * bess_share  # 90 * 500 * 0.333... = 15 000
    disc_bess_capex = (bess_capex_y0 + bess_lic_share) * disc[0]
    disc_bess_opex = sum(14.0 * 500.0 * disc[y] for y in range(1, 6))
    disc_bess = sum(700.0 * disc[y] for y in range(1, 6))
    expected_lcos = (disc_bess_capex + disc_bess_opex) / disc_bess
    assert fin["lcos_eur_per_mwh"] == pytest.approx(expected_lcos, rel=1e-3)


def test_lcoe_nan_when_no_pv():
    caps = {"pv_kwp": 0.0, "bess_kw": 500.0, "bess_kwh": 2000.0}
    econ = _hand_econ()
    yearly = build_yearly_cashflow(
        {"profit_total_eur": 50_000.0}, econ, caps,
    )
    fin = compute_financial_kpis(
        yearly, econ, capacities=caps,
        lifetime_yearly=_hand_lifetime_yearly().assign(pv_generation_mwh=0.0),
        year1_kpis={"pv_generation_mwh": 0.0},
    )
    assert np.isnan(fin["lcoe_eur_per_mwh"])
    assert not np.isnan(fin["lcos_eur_per_mwh"])


def test_lcos_nan_when_no_bess():
    caps = {"pv_kwp": 1000.0, "bess_kw": 0.0, "bess_kwh": 0.0}
    econ = _hand_econ()
    yearly = build_yearly_cashflow(
        {"profit_total_eur": 50_000.0}, econ, caps,
    )
    fin = compute_financial_kpis(
        yearly, econ, capacities=caps,
        lifetime_yearly=_hand_lifetime_yearly().assign(bess_discharge_mwh=0.0),
        year1_kpis={"pv_generation_mwh": 1800.0},
    )
    assert np.isnan(fin["lcos_eur_per_mwh"])
    assert not np.isnan(fin["lcoe_eur_per_mwh"])


# ---------------------------------------------------------------------------
# pv_capacity_factor and bess_lifetime_cycles
# ---------------------------------------------------------------------------


def test_pv_capacity_factor():
    yearly = build_yearly_cashflow(_hand_year1_kpis(), _hand_econ(), _hand_caps())
    fin = compute_financial_kpis(
        yearly, _hand_econ(),
        capacities=_hand_caps(),
        lifetime_yearly=_hand_lifetime_yearly(),
        year1_kpis=_hand_year1_kpis(),
    )
    expected = 1800.0 / (1000.0 * 8.76)  # ~0.2055
    assert fin["pv_capacity_factor"] == pytest.approx(expected, rel=1e-3)


def test_bess_lifetime_cycles():
    yearly = build_yearly_cashflow(_hand_year1_kpis(), _hand_econ(), _hand_caps())
    fin = compute_financial_kpis(
        yearly, _hand_econ(),
        capacities=_hand_caps(),
        lifetime_yearly=_hand_lifetime_yearly(),
        year1_kpis=_hand_year1_kpis(),
    )
    # 5 years × 700 MWh / 2 MWh = 1750 cycles
    expected = 5 * 700.0 * 1000.0 / 2000.0
    assert fin["bess_lifetime_cycles"] == pytest.approx(expected, rel=1e-3)


# ---------------------------------------------------------------------------
# Year-1 revenue breakdown
# ---------------------------------------------------------------------------


def test_revenue_breakdown_y1_keys_pass_through():
    yearly = build_yearly_cashflow(_hand_year1_kpis(), _hand_econ(), _hand_caps())
    fin = compute_financial_kpis(
        yearly, _hand_econ(),
        capacities=_hand_caps(),
        lifetime_yearly=_hand_lifetime_yearly(),
        year1_kpis=_hand_year1_kpis(),
    )
    assert fin["revenue_breakdown_y1_load_pv_eur"] == 30_000.0
    assert fin["revenue_breakdown_y1_load_bess_eur"] == 5_000.0
    assert fin["revenue_breakdown_y1_export_pv_eur"] == 50_000.0
    assert fin["revenue_breakdown_y1_export_bess_eur"] == 17_000.0
    assert fin["revenue_breakdown_y1_grid_charge_cost_eur"] == 2_000.0


# ---------------------------------------------------------------------------
# Plot helpers
# ---------------------------------------------------------------------------


def test_plot_revenue_stack_yearly(tmp_path):
    from pvbess_opt.plotting.lifecycle import plot_revenue_stack_yearly
    yearly = build_yearly_cashflow(_hand_year1_kpis(), _hand_econ(), _hand_caps())
    out = plot_revenue_stack_yearly(
        yearly, _hand_year1_kpis(), tmp_path / "rev.pdf",
    )
    assert out.exists()


def test_plot_lifetime_cycles(tmp_path):
    from pvbess_opt.plotting.lifecycle import plot_lifetime_cycles
    out = plot_lifetime_cycles(
        _hand_lifetime_yearly(), 2000.0, tmp_path / "cycles.pdf",
        bess_present=True,
    )
    assert out.exists()


def test_plot_lifetime_cycles_no_bess_returns_placeholder(tmp_path):
    from pvbess_opt.plotting.lifecycle import plot_lifetime_cycles
    out = plot_lifetime_cycles(
        _hand_lifetime_yearly(), 0.0, tmp_path / "cycles.pdf",
        bess_present=False,
    )
    assert out.exists()


def test_plot_lcoe_lcos_summary_hybrid(tmp_path):
    from pvbess_opt.plotting.lifecycle import plot_lcoe_lcos_summary
    yearly = build_yearly_cashflow(_hand_year1_kpis(), _hand_econ(), _hand_caps())
    fin = compute_financial_kpis(
        yearly, _hand_econ(),
        capacities=_hand_caps(),
        lifetime_yearly=_hand_lifetime_yearly(),
        year1_kpis=_hand_year1_kpis(),
    )
    out = plot_lcoe_lcos_summary(
        fin, None, _hand_caps(), _hand_econ(), tmp_path / "summary.pdf",
    )
    assert out.exists()


def test_plot_lcoe_lcos_summary_pv_only(tmp_path):
    from pvbess_opt.plotting.lifecycle import plot_lcoe_lcos_summary
    caps = {"pv_kwp": 1000.0, "bess_kw": 0.0, "bess_kwh": 0.0}
    fin = {
        "lcoe_eur_per_mwh": 60.0,
        "lcos_eur_per_mwh": float("nan"),
    }
    out = plot_lcoe_lcos_summary(
        fin, None, caps, _hand_econ(), tmp_path / "summary_pv.pdf",
    )
    assert out.exists()
