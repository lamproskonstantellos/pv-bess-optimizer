"""By-hand financial reference case — every KPI to the cent.

A 3-year project small enough to solve with pencil and paper.  Inputs:

* PV 1000 kWp @ 500 EUR/kW CAPEX + 50 EUR/kW DEVEX,
  BESS 500 kW / 2000 kWh @ 50 EUR/kWh CAPEX + 20 EUR/kW DEVEX,
  site lump sums 10 000 (CAPEX) + 5 000 (DEVEX)
  -> Year 0 = -(610 000 + 65 000) = -675 000.
* Year-1 revenue 300 000 split: retail 200 000 (PV-origin),
  DAM 50 000 (PV) + 60 000 (BESS) - 10 000 grid-charge expense.
* Degradation: PV LID 2 % + 1 %/yr  -> pv_factor 1, 0.98, 0.9702;
  BESS 5 %/yr calendar             -> bess_factor 1, 0.95, 0.9025.
* Indexation: retail 2 %, DAM 1 %, OPEX 10 %; aggregator fee 10 %;
  discount rate 10 %.

Hand-derived rows (gross = retail + dam, net = 0.9*gross + opex):

    y1: gross 300 000.0000, opex -20 000, net 250 000.0000
    y2: gross 297 385.0000, opex -22 000, net 245 646.5000
    y3: gross 297 396.2795, opex -24 200, net 243 456.6516

    NPV  = -675 000 + 250 000/1.1 + 245 646.5/1.21 + 243 456.6516/1.331
         = -61 801.05
    IRR  = 4.6988 %   (unique sign change)
    ROI  = (250 000 + 245 646.5 + 243 456.6516)/675 000 = 109.4968 %
           (denominator = |Year-0 CAPEX + DEVEX| = initial investment)
    BCR  = 613 198.949/675 000 = 0.9084
    simple payback    = 2 + 179 353.5/243 456.6516 = 2.7367
    discounted payback: never crosses (NPV < 0) -> NaN
    LCOE = (550 000 + 27 272.7273*... ) -> 117.9102 EUR/MWh
    LCOS = 72.3317 EUR/MWh, lifetime cycles = 1141.0
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import (
    build_yearly_cashflow,
    calculate_irr,
    compute_financial_kpis,
)
from pvbess_opt.lifetime import aggregate_lifetime_to_yearly, build_lifetime_dispatch


def _econ(**overrides) -> dict:
    base = {
        "project_lifecycle_years": 3,
        "project_start_year": 2030,
        "discount_rate_pct": 10.0,
        "opex_inflation_pct": 10.0,
        "retail_inflation_pct": 2.0,
        "dam_inflation_pct": 1.0,
        "capex_pv_eur_per_kw": 500.0,
        # 50 EUR/kWh x 2000 kWh = 100 000 (was 200 EUR/kW x 500 kW).
        "capex_bess_eur_per_kwh": 50.0,
        "devex_pv_eur_per_kw": 50.0,
        "devex_bess_eur_per_kw": 20.0,
        "site_capex_eur": 10_000.0,
        "site_devex_eur": 5_000.0,
        "opex_pv_eur_per_kwp": 10.0,
        "opex_bess_eur_per_kw": 20.0,
        "pv_degradation_year1_pct": 2.0,
        "pv_degradation_annual_pct": 1.0,
        "bess_degradation_annual_pct": 5.0,
        "bess_degradation_pct_per_cycle": 0.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 50.0,
        "aggregator_fee_pct_revenue": 10.0,
        "unavailability_pct": 0.0,
    }
    base.update(overrides)
    return base


_CAPS = {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 2000.0}

_KPIS = {
    "profit_total_eur": 300_000.0,
    "profit_load_from_pv_eur": 200_000.0,
    "profit_load_from_bess_eur": 0.0,
    "profit_export_from_pv_eur": 50_000.0,
    "profit_export_from_bess_eur": 60_000.0,
    "expense_charge_bess_grid_eur": 10_000.0,
    "bess_total_discharge_mwh": 800.0,
    "pv_generation_mwh": 2000.0,
}


def _reference_res(n: int = 96) -> pd.DataFrame:
    """Flat hourly frame whose annual sums equal the KPI dict."""
    return pd.DataFrame({
        "timestamp": pd.date_range("2030-01-01", periods=n, freq="h"),
        "pv_kwh": np.full(n, 2_000_000.0 / n),
        "bess_dis_load_kwh": np.zeros(n),
        "bess_dis_grid_kwh": np.full(n, 800_000.0 / n),
        "bess_charge_grid_kwh": np.zeros(n),
        "pv_to_bess_kwh": np.zeros(n),
        "soc_kwh": np.zeros(n),
        "profit_load_from_pv_eur": np.full(n, 200_000.0 / n),
        "profit_load_from_bess_eur": np.zeros(n),
        "profit_export_from_pv_eur": np.full(n, 50_000.0 / n),
        "profit_export_from_bess_eur": np.full(n, 60_000.0 / n),
        "expense_charge_bess_grid_eur": np.full(n, 10_000.0 / n),
    })


def _fin_kpis(econ: dict) -> dict:
    ycf = build_yearly_cashflow(_KPIS, econ, _CAPS)
    lt = build_lifetime_dispatch(
        _reference_res(), econ, _CAPS, year1_discharge_mwh=800.0,
    )
    lty = aggregate_lifetime_to_yearly(lt)
    return compute_financial_kpis(
        ycf, econ, capacities=_CAPS, lifetime_yearly=lty, year1_kpis=_KPIS,
    )


def test_reference_cashflow_rows_to_the_cent():
    ycf = build_yearly_cashflow(_KPIS, _econ(), _CAPS).set_index("project_year")
    assert float(ycf.loc[0, "capex_eur"]) == pytest.approx(-610_000.0)
    assert float(ycf.loc[0, "devex_eur"]) == pytest.approx(-65_000.0)
    assert float(ycf.loc[0, "net_cashflow_eur"]) == pytest.approx(-675_000.0)
    # calendar mapping: Year 0 = start - 1.
    assert int(ycf.loc[0, "calendar_year"]) == 2029
    assert int(ycf.loc[1, "calendar_year"]) == 2030

    expected = {
        1: (300_000.0000, -30_000.0000, -20_000.0, 250_000.0000),
        2: (297_385.0000, -29_738.5000, -22_000.0, 245_646.5000),
        3: (297_396.2795, -29_739.6280, -24_200.0, 243_456.6516),
    }
    for y, (gross, fee, opex, net) in expected.items():
        assert float(ycf.loc[y, "revenue_eur"]) == pytest.approx(
            gross + fee, abs=0.01,
        )
        assert float(ycf.loc[y, "aggregator_fee_eur"]) == pytest.approx(fee, abs=0.01)
        assert float(ycf.loc[y, "opex_eur"]) == pytest.approx(opex, abs=0.01)
        assert float(ycf.loc[y, "net_cashflow_eur"]) == pytest.approx(net, abs=0.01)


def test_reference_headline_kpis_to_the_cent():
    fin = _fin_kpis(_econ())
    assert fin["npv_eur"] == pytest.approx(-61_801.05, abs=0.01)
    assert fin["irr_pct"] == pytest.approx(4.6988, abs=5e-4)
    assert fin["roi_pct"] == pytest.approx(109.4968, abs=5e-4)
    assert fin["initial_investment_eur"] == pytest.approx(-675_000.0, abs=0.01)
    assert fin["bcr"] == pytest.approx(0.9084, abs=5e-4)
    assert fin["simple_payback_years"] == pytest.approx(2.7367, abs=5e-4)
    assert np.isnan(fin["discounted_payback_years"])  # NPV < 0: no crossing
    assert fin["lcoe_eur_per_mwh"] == pytest.approx(117.9102, abs=5e-4)
    assert fin["lcos_eur_per_mwh"] == pytest.approx(72.3317, abs=5e-4)
    assert fin["bess_lifetime_cycles"] == pytest.approx(1141.0, abs=1e-3)
    assert fin["capex_year"] == 2029
    assert fin["project_start_year"] == 2030
    assert fin["project_end_year"] == 2032


def test_reference_with_replacement_capex_and_lcos():
    """Replacement in year 2 at 50 % of BESS CAPEX:

    * cashflow: capex_eur[y2] = -50 000, bess_factor resets to 1.0,
    * LCOS numerator gains 50 000/1.1^2 = 41 322.3140,
    * bess_factor becomes 1, 1, 0.95 -> discounted discharge
      800/1.1 + 800/1.21 + 760/1.331 = 1 959.5377 MWh,
    * LCOS = (110 000 + 41 322.3140 + 24 793.3884*) / 1 959.5377
      where * = disc. BESS opex 10 000*(1/1.1 + 1.1/1.21 + 1.21/1.331)
      = 27 272.7273 -> LCOS = 91.1417 EUR/MWh.
    """
    econ = _econ(bess_replacement_year=2)
    ycf = build_yearly_cashflow(_KPIS, econ, _CAPS).set_index("project_year")
    assert float(ycf.loc[2, "capex_eur"]) == pytest.approx(-50_000.0)
    assert float(ycf.loc[2, "bess_capacity_factor"]) == pytest.approx(1.0)
    assert float(ycf.loc[3, "bess_capacity_factor"]) == pytest.approx(0.95)

    fin = _fin_kpis(econ)
    disc_repl = 50_000.0 / 1.1**2
    disc_capex = 110_000.0 + disc_repl
    disc_opex = 10_000.0 * (1.0 / 1.1 + 1.1 / 1.21 + 1.21 / 1.331)
    disc_mwh = 800.0 / 1.1 + 800.0 / 1.21 + 760.0 / 1.331
    assert fin["lcos_eur_per_mwh"] == pytest.approx(
        (disc_capex + disc_opex) / disc_mwh, abs=5e-4,
    )


def test_initial_investment_vs_lifecycle_capex_totals():
    """initial_investment_eur is the Year-0 row only; the lifecycle
    total_capex_devex_eur exceeds it by exactly the replacement CAPEX
    (replacement_pct x BESS CAPEX) when a replacement is scheduled, and
    coincides with it when none is."""
    # No replacement: the two coincide.
    fin0 = _fin_kpis(_econ(bess_replacement_year=0))
    assert fin0["initial_investment_eur"] == pytest.approx(-675_000.0, abs=0.01)
    assert fin0["total_capex_devex_eur"] == pytest.approx(
        fin0["initial_investment_eur"], abs=0.01,
    )

    # Replacement in year 2 at 50 % of the 100 000 BESS CAPEX: the
    # lifecycle total carries the extra -50 000; Year 0 is unchanged.
    fin_r = _fin_kpis(_econ(bess_replacement_year=2))
    assert fin_r["initial_investment_eur"] == pytest.approx(-675_000.0, abs=0.01)
    repl = -(50.0 * 2000.0) * 0.50
    assert fin_r["total_capex_devex_eur"] == pytest.approx(
        fin_r["initial_investment_eur"] + repl, abs=0.01,
    )
    # ROI's denominator is the Year-0 outlay in BOTH runs (never the
    # lifecycle total): recompute each numerator from its cashflow and
    # divide by the same 675 000.  Note the replacement run's numerator
    # differs by more than the bare -50 000 CAPEX because the factor
    # reset also lifts years 2..3 BESS revenue.
    for repl_year, fin in ((0, fin0), (2, fin_r)):
        cf = build_yearly_cashflow(
            _KPIS, _econ(bess_replacement_year=repl_year), _CAPS,
        )
        op_net = float(
            cf.loc[cf["project_year"] >= 1, "net_cashflow_eur"].sum()
        )
        assert fin["roi_pct"] == pytest.approx(
            op_net / 675_000.0 * 100.0, abs=5e-4,
        )


def test_lcoe_lcos_exclude_site_lump_sums():
    """Site-wide lump sums move NPV but never LCOE/LCOS (Lazard)."""
    base = _fin_kpis(_econ(site_capex_eur=0.0, site_devex_eur=0.0))
    lump = _fin_kpis(_econ(site_capex_eur=500_000.0, site_devex_eur=100_000.0))
    assert lump["lcoe_eur_per_mwh"] == pytest.approx(base["lcoe_eur_per_mwh"])
    assert lump["lcos_eur_per_mwh"] == pytest.approx(base["lcos_eur_per_mwh"])
    assert lump["npv_eur"] < base["npv_eur"] - 590_000.0


# ---------------------------------------------------------------------------
# IRR edge cases
# ---------------------------------------------------------------------------


def test_irr_multiple_roots_returns_a_valid_root():
    # -100 + 230/(1+r) - 132/(1+r)^2 has roots at exactly 10 % and 20 %.
    cf = np.array([-100.0, 230.0, -132.0])
    irr = calculate_irr(cf)
    assert not np.isnan(irr)
    npv_at_irr = float(sum(c / (1 + irr) ** t for t, c in enumerate(cf)))
    assert abs(npv_at_irr) < 1e-6
    assert irr == pytest.approx(0.10, abs=1e-6) or irr == pytest.approx(
        0.20, abs=1e-6,
    )


def test_irr_never_recovers_gives_deep_negative_root():
    # -100 + 10/(1+r) + 10/(1+r)^2 = 0 -> r = -62.9844 % (project loses
    # most of its capital; the IRR root is real but deeply negative).
    cf = np.array([-100.0, 10.0, 10.0])
    irr = calculate_irr(cf)
    assert irr == pytest.approx(-0.629844, abs=1e-4)


def test_irr_no_sign_change_is_nan():
    assert np.isnan(calculate_irr(np.array([100.0, 50.0, 10.0])))
    assert np.isnan(calculate_irr(np.array([-100.0, -50.0, -10.0])))
    assert np.isnan(calculate_irr(np.array([])))
