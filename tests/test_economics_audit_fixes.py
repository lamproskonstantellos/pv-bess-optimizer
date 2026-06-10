"""Regression locks for the pre-publication audit fixes in economics.py.

Three fixes are locked here:

1. ``derive_monthly_cashflow`` discounts month ``m`` of year ``y`` at
   ``t = (y - 1) + m/12`` (end-of-month convention).  Previously it used
   ``t = y + (m - 1)/12`` — every month was discounted 11/12 of a year
   too late, so December of Year 1 carried a 1.92-year discount and the
   monthly DCF summed BELOW the yearly DCF even though intra-year cash
   arrives earlier than the end-of-year lump.
2. ``derive_monthly_cashflow`` derates ``pv_production_mwh`` by the
   availability factor so the monthly sheet reconciles with
   ``kpis_year1['pv_generation_mwh']`` and ``lifetime_dispatch_yearly``
   (both derated upstream).  Previously the monthly column was raw.
3. ``read_economic_params`` merges the ``balancing`` sheet, so
   ``bm_inflation_pct`` (workbook default 2 %/yr) reaches
   ``build_yearly_cashflow``.  Previously the sheet was dropped and the
   balancing revenue lines were silently held nominal (0 % indexation).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.availability import availability_factor
from pvbess_opt.economics import (
    build_yearly_cashflow,
    derive_monthly_cashflow,
    read_economic_params,
)


def _econ(**overrides) -> dict:
    base = {
        "project_lifecycle_years": 3,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 500.0,
        "capex_bess_eur_per_kw": 200.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 7.0,
        "opex_bess_eur_per_kw": 14.0,
        "pv_degradation_year1_pct": 0.0,
        "pv_degradation_annual_pct": 0.0,
        "bess_degradation_annual_pct": 0.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 50.0,
        "aggregator_fee_pct_revenue": 0.0,
        "unavailability_pct": 0.0,
    }
    base.update(overrides)
    return base


_CAPS = {"pv_kwp": 100.0, "bess_kw": 100.0, "bess_kwh": 1000.0}


def _flat_res(n: int = 35040, revenue_eur: float = 100_000.0) -> pd.DataFrame:
    ts = pd.date_range("2026-01-01", periods=n, freq="15min")
    return pd.DataFrame({
        "timestamp": ts,
        "pv_kwh": np.full(n, 10.0),
        "profit_load_from_pv_eur": np.full(n, revenue_eur / n),
        "profit_load_from_bess_eur": np.zeros(n),
        "profit_export_from_pv_eur": np.zeros(n),
        "profit_export_from_bess_eur": np.zeros(n),
        "expense_charge_bess_grid_eur": np.zeros(n),
    })


def _kpis(avail: float = 1.0, revenue_eur: float = 100_000.0) -> dict:
    return {
        "profit_total_eur": revenue_eur * avail,
        "profit_load_from_pv_eur": revenue_eur * avail,
        "profit_load_from_bess_eur": 0.0,
        "profit_export_from_pv_eur": 0.0,
        "profit_export_from_bess_eur": 0.0,
        "expense_charge_bess_grid_eur": 0.0,
        "bess_total_discharge_mwh": 0.0,
    }


# ---------------------------------------------------------------------------
# Fix 1 — end-of-month discounting
# ---------------------------------------------------------------------------


def test_monthly_discounting_uses_end_of_month_convention():
    econ = _econ()
    r = econ["discount_rate_pct"] / 100.0
    ycf = build_yearly_cashflow(_kpis(), econ, _CAPS)
    mcf, _ = derive_monthly_cashflow(_flat_res(), ycf, econ)

    for y in (1, 2, 3):
        sub = mcf[mcf["project_year"] == y].set_index("period")
        for m in (1, 6, 12):
            net = float(sub.loc[m, "net_cashflow_eur"])
            dcf = float(sub.loc[m, "discounted_cf_eur"])
            t_implied = np.log(net / dcf) / np.log(1.0 + r)
            assert t_implied == pytest.approx((y - 1) + m / 12.0, abs=1e-9)


def test_december_discount_factor_matches_yearly_row():
    econ = _econ()
    r = econ["discount_rate_pct"] / 100.0
    ycf = build_yearly_cashflow(_kpis(), econ, _CAPS)
    mcf, _ = derive_monthly_cashflow(_flat_res(), ycf, econ)
    dec = mcf[(mcf["project_year"] == 2) & (mcf["period"] == 12)].iloc[0]
    implied_factor = float(dec["discounted_cf_eur"]) / float(dec["net_cashflow_eur"])
    yearly_factor = 1.0 / (1.0 + r) ** 2
    assert implied_factor == pytest.approx(yearly_factor, rel=1e-12)


def test_monthly_dcf_sum_refines_yearly_dcf_upward():
    """Intra-year cash arrives earlier than the end-of-year lump, so the
    monthly DCF sum must be >= the yearly DCF for positive flows."""
    econ = _econ()
    ycf = build_yearly_cashflow(_kpis(), econ, _CAPS)
    mcf, _ = derive_monthly_cashflow(_flat_res(), ycf, econ)
    for y in (1, 2, 3):
        yearly_dcf = float(
            ycf.loc[ycf["project_year"] == y, "discounted_cf_eur"].iloc[0]
        )
        monthly_dcf = float(
            mcf.loc[mcf["project_year"] == y, "discounted_cf_eur"].sum()
        )
        if yearly_dcf > 0:
            assert monthly_dcf >= yearly_dcf
            # ...but only by the intra-year timing wedge (< (1+r)^1 - 1).
            assert monthly_dcf / yearly_dcf < 1.07


# ---------------------------------------------------------------------------
# Fix 2 — pv_production_mwh derated by availability
# ---------------------------------------------------------------------------


def test_monthly_pv_production_is_availability_derated():
    unav = 4.0
    avail = availability_factor(unav)
    econ = _econ(unavailability_pct=unav)
    res = _flat_res()
    raw_pv_y1_mwh = float(res["pv_kwh"].sum()) / 1000.0

    ycf = build_yearly_cashflow(_kpis(avail), econ, _CAPS)
    mcf, qcf = derive_monthly_cashflow(res, ycf, econ)

    monthly_y1 = float(mcf.loc[mcf["project_year"] == 1, "pv_production_mwh"].sum())
    assert monthly_y1 == pytest.approx(raw_pv_y1_mwh * avail, rel=1e-9)
    quarterly_y1 = float(qcf.loc[qcf["project_year"] == 1, "pv_production_mwh"].sum())
    assert quarterly_y1 == pytest.approx(raw_pv_y1_mwh * avail, rel=1e-9)


def test_monthly_pv_production_unchanged_at_full_availability():
    econ = _econ(unavailability_pct=0.0)
    res = _flat_res()
    raw_pv_y1_mwh = float(res["pv_kwh"].sum()) / 1000.0
    ycf = build_yearly_cashflow(_kpis(), econ, _CAPS)
    mcf, _ = derive_monthly_cashflow(res, ycf, econ)
    monthly_y1 = float(mcf.loc[mcf["project_year"] == 1, "pv_production_mwh"].sum())
    assert monthly_y1 == pytest.approx(raw_pv_y1_mwh, rel=1e-9)


# ---------------------------------------------------------------------------
# Fix 3 — read_economic_params merges the balancing sheet
# ---------------------------------------------------------------------------


def test_read_economic_params_carries_balancing_keys(repo_input_xlsx):
    econ = read_economic_params(repo_input_xlsx)
    assert "bm_inflation_pct" in econ
    assert econ["bm_inflation_pct"] == pytest.approx(2.0)
    assert "balancing_enabled" in econ


def test_bm_inflation_from_workbook_indexes_balancing_revenue(repo_input_xlsx):
    """End-to-end: the workbook's bm_inflation_pct must index the
    balancing revenue lines in the multi-year cashflow."""
    econ = read_economic_params(repo_input_xlsx)
    bm_infl = float(econ["bm_inflation_pct"]) / 100.0
    assert bm_infl > 0.0  # the shipped workbook carries 2 %

    kpis = _kpis()
    kpis["bm_total_capacity_revenue_eur"] = 10_000.0
    kpis["bm_total_activation_revenue_eur"] = 0.0
    # Disable degradation so the indexation is isolated.
    econ_flat = {**econ, "bess_degradation_annual_pct": 0.0,
                 "bess_degradation_pct_per_cycle": 0.0,
                 "bess_replacement_year": 0,
                 "project_lifecycle_years": 3}
    ycf = build_yearly_cashflow(kpis, econ_flat, _CAPS)
    y1 = float(ycf.loc[ycf["project_year"] == 1, "balancing_revenue_eur"].iloc[0])
    y3 = float(ycf.loc[ycf["project_year"] == 3, "balancing_revenue_eur"].iloc[0])
    assert y1 == pytest.approx(10_000.0)
    assert y3 == pytest.approx(10_000.0 * (1.0 + bm_infl) ** 2)
