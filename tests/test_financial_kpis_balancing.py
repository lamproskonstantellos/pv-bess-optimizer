"""End-to-end audit of the cashflow-derived financial KPIs with
balancing on.

Locks four properties that must hold once the balancing-revenue
stream is part of ``balancing_revenue_eur`` in the yearly cashflow:

* ``npv_eur`` equals ``sum(discounted_cf_eur)`` over the full
  Year-0..N cashflow (the cashflow already carries balancing).
* The reported ``irr_pct`` is a true root of NPV: re-discounting the
  net cashflow at that rate produces an NPV close to zero.
* ``roi_pct`` equals ``sum(net_cashflow Year 1..N) / |capex_y0| *
  100``, with balancing folded into the numerator via
  ``net_cashflow_eur``.
* ``lcoe_eur_per_mwh`` and ``lcos_eur_per_mwh`` are invariant under a
  balancing-on / balancing-off toggle when capacities and prices are
  held constant.  Lazard convention: balancing revenue does not
  enter either metric.
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
        "capex_bess_eur_per_kw": 300.0,
        "devex_pv_eur_per_kw": 60.0,
        "devex_bess_eur_per_kw": 30.0,
        "opex_pv_eur_per_kwp": 7.0,
        "opex_bess_eur_per_kw": 14.0,
        "pv_degradation_year1_pct": 2.5,
        "pv_degradation_annual_pct": 0.55,
        "bess_degradation_annual_pct": 2.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 50.0,
        "aggregator_fee_pct_revenue": 2.0,
    }


def _capacities() -> dict:
    return {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}


def _year1_kpis_balancing_on() -> dict:
    """Year-1 KPIs with non-trivial balancing revenue.

    Roughly matches the case-study workbook proportions: balancing
    revenue is ~20 % of the DAM + retail base, so the audit checks
    exercise the regime where balancing meaningfully moves NPV / IRR.
    """
    return {
        "profit_load_from_pv_eur": 110_000.0,
        "profit_load_from_bess_eur": 70_000.0,
        "profit_export_from_pv_eur": 60_000.0,
        "profit_export_from_bess_eur": 55_000.0,
        "expense_charge_bess_grid_eur": 12_000.0,
        "profit_total_eur": 110_000.0 + 70_000.0 + 60_000.0 + 55_000.0 - 12_000.0,
        "pv_generation_mwh": 7_200.0,
        "bm_total_capacity_revenue_eur": 38_000.0,
        "bm_total_activation_revenue_eur": 12_000.0,
    }


def _year1_kpis_balancing_off() -> dict:
    """Same per-stream Year-1 figures but no balancing revenue."""
    kpis = _year1_kpis_balancing_on()
    kpis["bm_total_capacity_revenue_eur"] = 0.0
    kpis["bm_total_activation_revenue_eur"] = 0.0
    return kpis


def _lifetime_yearly() -> pd.DataFrame:
    rows = []
    for y in range(1, 21):
        rows.append({
            "project_year": y,
            "calendar_year": 2025 + y,
            "pv_generation_mwh": 7_200.0 * (1.0 - 0.005 * (y - 1)),
            "bess_discharge_mwh": 4_500.0 * (1.0 - 0.02 * (y - 1)),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# NPV / IRR / ROI consistency under balancing
# ---------------------------------------------------------------------------


def test_npv_equals_sum_discounted_cashflow():
    econ, caps = _econ(), _capacities()
    cf = build_yearly_cashflow(_year1_kpis_balancing_on(), econ, caps)
    fin = compute_financial_kpis(cf, econ)
    expected_npv = float(cf["discounted_cf_eur"].sum())
    assert fin["npv_eur"] == pytest.approx(expected_npv, abs=1e-2)
    # And the cashflow row that carries balancing revenue is non-zero,
    # so the assertion above is exercising the balancing pathway.
    assert float(cf["balancing_revenue_eur"].sum()) > 0.0


def test_irr_is_a_root_of_npv():
    """IRR is by definition the discount rate that zeroes NPV.

    The KPI dict carries a rounded IRR (4 decimal places), so the
    test recomputes the unrounded root via ``calculate_irr`` for the
    sub-EUR equality check, and additionally confirms that the
    rounded value reported by ``compute_financial_kpis`` matches the
    unrounded root to four decimal places.
    """
    econ, caps = _econ(), _capacities()
    cf = build_yearly_cashflow(_year1_kpis_balancing_on(), econ, caps)
    fin = compute_financial_kpis(cf, econ)
    net = cf["net_cashflow_eur"].to_numpy(dtype=float)
    years = cf["project_year"].to_numpy(dtype=float)

    irr_unrounded = calculate_irr(net)
    assert not np.isnan(irr_unrounded)
    npv_at_irr = float(np.sum(net / (1.0 + irr_unrounded) ** years))
    assert abs(npv_at_irr) < 1.0

    reported = float(fin["irr_pct"])
    assert reported == pytest.approx(irr_unrounded * 100.0, abs=5e-4)


def test_roi_matches_cashflow_definition():
    econ, caps = _econ(), _capacities()
    cf = build_yearly_cashflow(_year1_kpis_balancing_on(), econ, caps)
    fin = compute_financial_kpis(cf, econ)
    capex_y0 = float(cf.loc[cf["project_year"] == 0, "capex_eur"].iloc[0])
    devex_y0 = float(cf.loc[cf["project_year"] == 0, "devex_eur"].iloc[0])
    op_net = float(cf.loc[cf["project_year"] >= 1, "net_cashflow_eur"].sum())
    expected_roi = op_net / abs(capex_y0 + devex_y0) * 100.0
    # The implementation uses |capex_y0| only (DEVEX lives in its own
    # row by build convention).  Recompute against the implementation's
    # exact denominator.
    expected_roi_impl = op_net / abs(capex_y0) * 100.0
    assert fin["roi_pct"] == pytest.approx(expected_roi_impl, rel=1e-6)
    # Sanity: both conventions are within an order of magnitude — the
    # capex_y0 row already aggregates per-asset CAPEX + the site lump
    # sum (DEVEX flows through devex_eur).
    assert abs(expected_roi - expected_roi_impl) < abs(expected_roi_impl)


# ---------------------------------------------------------------------------
# LCOE / LCOS invariance under the balancing toggle
# ---------------------------------------------------------------------------


def test_lcoe_invariant_under_balancing_toggle():
    """LCOE must be unchanged by toggling balancing on/off."""
    econ, caps = _econ(), _capacities()
    cf_on = build_yearly_cashflow(_year1_kpis_balancing_on(), econ, caps)
    cf_off = build_yearly_cashflow(_year1_kpis_balancing_off(), econ, caps)
    ly = _lifetime_yearly()
    fin_on = compute_financial_kpis(
        cf_on, econ, capacities=caps, lifetime_yearly=ly,
        year1_kpis=_year1_kpis_balancing_on(),
    )
    fin_off = compute_financial_kpis(
        cf_off, econ, capacities=caps, lifetime_yearly=ly,
        year1_kpis=_year1_kpis_balancing_off(),
    )
    assert fin_on["lcoe_eur_per_mwh"] == pytest.approx(
        fin_off["lcoe_eur_per_mwh"], rel=1e-9,
    )
    # And LCOE must be a finite number (the fixture has PV present).
    assert not np.isnan(fin_on["lcoe_eur_per_mwh"])


def test_lcos_invariant_under_balancing_toggle():
    """LCOS must be unchanged by toggling balancing on/off.

    Balancing capacity revenue does not move the LCOS discharge-MWh
    denominator (the capacity reservation is paid for *standby*, not
    for delivered energy), and is excluded from the LCOS numerator by
    Lazard convention.
    """
    econ, caps = _econ(), _capacities()
    cf_on = build_yearly_cashflow(_year1_kpis_balancing_on(), econ, caps)
    cf_off = build_yearly_cashflow(_year1_kpis_balancing_off(), econ, caps)
    ly = _lifetime_yearly()
    fin_on = compute_financial_kpis(
        cf_on, econ, capacities=caps, lifetime_yearly=ly,
        year1_kpis=_year1_kpis_balancing_on(),
    )
    fin_off = compute_financial_kpis(
        cf_off, econ, capacities=caps, lifetime_yearly=ly,
        year1_kpis=_year1_kpis_balancing_off(),
    )
    assert fin_on["lcos_eur_per_mwh"] == pytest.approx(
        fin_off["lcos_eur_per_mwh"], rel=1e-9,
    )
    assert not np.isnan(fin_on["lcos_eur_per_mwh"])


def test_npv_differs_when_balancing_toggled():
    """Sanity guard: balancing must visibly move NPV.

    LCOE / LCOS are designed to be invariant under the toggle; NPV is
    not — without this guard, the LCOE / LCOS tests above could pass
    trivially on a cashflow where balancing is silently zeroed out.
    """
    econ, caps = _econ(), _capacities()
    cf_on = build_yearly_cashflow(_year1_kpis_balancing_on(), econ, caps)
    cf_off = build_yearly_cashflow(_year1_kpis_balancing_off(), econ, caps)
    fin_on = compute_financial_kpis(cf_on, econ)
    fin_off = compute_financial_kpis(cf_off, econ)
    assert fin_on["npv_eur"] > fin_off["npv_eur"]
