"""Imbalance cost cashflow line (Eqs. E28/E28a).

The Year-1 MC MEAN projects as its own signed column: PV-error-driven
volume on the PV degradation curve, settlement prices riding the DAM
escalation series.  Locked: cent-level projection, zero-default
bit-identity, PV-shape monthly reconciliation, sensitivity handling
(net recompute folds it; the Revenue driver SCALES it — a price-spread
times volume), lifetime total + SUMMARY registry + theme registration.
"""

from __future__ import annotations

import pandas as pd
import pytest

from pvbess_opt.economics import (
    build_yearly_cashflow,
    compute_financial_kpis,
    derive_monthly_cashflow,
)

N_YEARS = 3
IMB_1 = 40_000.0
DAM_INFL = 0.02


def _econ(**o) -> dict:
    econ = {
        "project_lifecycle_years": N_YEARS,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": DAM_INFL * 100.0,
        "capex_pv_eur_per_kw": 500.0,
        "capex_bess_eur_per_kwh": 100.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 5.0,
        "opex_bess_eur_per_kw": 5.0,
        "pv_degradation_year1_pct": 2.0,
        "pv_degradation_annual_pct": 1.0,
        "bess_degradation_annual_pct": 0.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
    }
    econ.update(o)
    return econ


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}


def _kpis(imb: float | None = IMB_1) -> dict:
    base = {
        "profit_load_from_pv_eur": 50_000.0,
        "profit_load_from_bess_eur": 10_000.0,
        "profit_export_from_pv_eur": 30_000.0,
        "profit_export_from_bess_eur": 40_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 125_000.0,
    }
    if imb is not None:
        base["imbalance_cost_year1_eur"] = imb
    return base


def test_projection_on_pv_curve_and_dam_series():
    cf = build_yearly_cashflow(_kpis(), _econ(), _caps()).set_index(
        "project_year",
    )
    assert float(cf.loc[0, "imbalance_cost_eur"]) == 0.0
    for y in range(1, N_YEARS + 1):
        pv_f = float(cf.loc[y, "pv_production_factor"])
        expected = -IMB_1 * pv_f * (1.0 + DAM_INFL) ** (y - 1)
        assert float(cf.loc[y, "imbalance_cost_eur"]) == pytest.approx(
            expected, abs=0.01,
        ), y
    # And it is part of the net.
    base = build_yearly_cashflow(_kpis(imb=0.0), _econ(), _caps())
    assert float(cf.loc[1, "net_cashflow_eur"]) == pytest.approx(
        float(base.set_index("project_year").loc[1, "net_cashflow_eur"])
        - IMB_1,
        abs=0.01,
    )


def test_missing_kpi_is_bit_identical_to_zero():
    pd.testing.assert_frame_equal(
        build_yearly_cashflow(_kpis(imb=None), _econ(), _caps()),
        build_yearly_cashflow(_kpis(imb=0.0), _econ(), _caps()),
    )


def test_monthly_rides_pv_shape_and_reconciles():
    from tests.test_monthly_cashflow_reconciliation import _make_res_frame

    res = _make_res_frame()
    yearly = build_yearly_cashflow(_kpis(), _econ(), _caps())
    monthly, _q = derive_monthly_cashflow(res, yearly, _econ())
    yearly_indexed = yearly.set_index("project_year")
    for y in range(1, N_YEARS + 1):
        month_sum = float(
            monthly.loc[
                monthly["project_year"] == y, "imbalance_cost_eur"
            ].sum()
        )
        assert month_sum == pytest.approx(
            float(yearly_indexed.loc[y, "imbalance_cost_eur"]), abs=0.01,
        ), y


def test_sensitivity_folds_and_scales():
    from pvbess_opt.sensitivity import _recompute_net, _scale_revenue

    cf = build_yearly_cashflow(
        _kpis(), _econ(aggregator_fee_pct_revenue=5.0), _caps(),
    )
    recomputed = _recompute_net(cf.copy())
    pd.testing.assert_series_equal(
        recomputed["net_cashflow_eur"], cf["net_cashflow_eur"],
    )
    # Perturbed frames deliberately drop the tax-layer columns
    # (Eqs. E34-E38 stale-value guard), so the no-op compares
    # against the pre-tax view.
    from pvbess_opt.economics import TAX_LAYER_COLUMNS

    pd.testing.assert_frame_equal(
        _scale_revenue(cf, 1.0),
        cf.drop(columns=list(TAX_LAYER_COLUMNS)),
    )
    scaled = _scale_revenue(cf, 1.1)
    # Price-proportional: the imbalance line scales WITH the driver.
    pd.testing.assert_series_equal(
        scaled["imbalance_cost_eur"], cf["imbalance_cost_eur"] * 1.1,
    )


def test_lifetime_total_summary_and_theme():
    from pvbess_opt.io import _SUMMARY_OPTIONAL_FINANCIAL_KEYS
    from pvbess_opt.theme import (
        FINANCIAL_COLORS,
        FINANCIAL_LABELS,
        FINANCIAL_LEGEND_ORDER,
    )

    cf = build_yearly_cashflow(_kpis(), _econ(), _caps())
    fin = compute_financial_kpis(cf, _econ())
    expected = float(
        cf.loc[cf["project_year"] >= 1, "imbalance_cost_eur"].sum()
    )
    assert fin["total_imbalance_cost_eur_lifecycle"] == pytest.approx(
        expected, abs=0.01,
    )
    assert ("total_imbalance_cost_eur_lifecycle",
            "Lifetime imbalance cost [EUR]") in (
        _SUMMARY_OPTIONAL_FINANCIAL_KEYS
    )
    assert "Imbalance cost" in FINANCIAL_LABELS
    assert "Imbalance cost" in FINANCIAL_LEGEND_ORDER
    assert FINANCIAL_COLORS["imbalance_cost"] == "#4E342E"
