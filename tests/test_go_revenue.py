"""Guarantees-of-origin revenue line (Eq. E54).

`go_price_eur_per_mwh` pays a flat contracted price on the eligible
renewable injection — the availability- and curtailment-derated PV
grid export.  Locked here: zero-default bit-identity, the E54
arithmetic to the cent including degradation, monthly reconciliation
on the PV production shape, sensitivity-driver membership, theme
registration and the loader validation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import (
    build_yearly_cashflow,
    compute_financial_kpis,
    derive_monthly_cashflow,
)

GO_PRICE = 1.5


def _econ(**overrides) -> dict:
    econ = {
        "project_lifecycle_years": 6,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 400.0,
        "capex_bess_eur_per_kwh": 100.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 5.0,
        "opex_bess_eur_per_kw": 5.0,
        "pv_degradation_year1_pct": 2.0,
        "pv_degradation_annual_pct": 1.0,
        "bess_degradation_annual_pct": 1.5,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
    }
    econ.update(overrides)
    return econ


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}


def _kpis() -> dict:
    return {
        "profit_load_from_pv_eur": 80_000.0,
        "profit_load_from_bess_eur": 20_000.0,
        "profit_export_from_pv_eur": 10_000.0,
        "profit_export_from_bess_eur": 30_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 135_000.0,
        "pv_export_mwh": 800.0,
        "bess_export_mwh": 300.0,
        "bess_total_discharge_mwh": 500.0,
    }


def test_zero_default_is_bit_identical():
    cf_absent = build_yearly_cashflow(_kpis(), _econ(), _caps())
    cf_zero = build_yearly_cashflow(
        _kpis(), _econ(go_price_eur_per_mwh=0.0), _caps(),
    )
    pd.testing.assert_frame_equal(cf_absent, cf_zero)
    assert (cf_absent["go_revenue_eur"] == 0.0).all()
    kpi = compute_financial_kpis(cf_absent, _econ())
    assert kpi["total_go_revenue_eur_lifecycle"] == 0.0


def test_e54_arithmetic_to_the_cent():
    econ = _econ(go_price_eur_per_mwh=GO_PRICE)
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    op = cf[cf["project_year"] >= 1].set_index("project_year")
    for y in op.index:
        pv_f = float(op.loc[y, "pv_production_factor"])
        expected = GO_PRICE * 800.0 * pv_f
        assert float(op.loc[y, "go_revenue_eur"]) == pytest.approx(
            expected, abs=0.01,
        ), y
    # Year 0 carries no operating revenue.
    assert float(
        cf.loc[cf["project_year"] == 0, "go_revenue_eur"].iloc[0]
    ) == 0.0
    # Folded into the net (Eq. E54 is a net_cashflow component).
    base = build_yearly_cashflow(_kpis(), _econ(), _caps())
    delta_net = (
        float(op.loc[1, "net_cashflow_eur"])
        - float(base.loc[base["project_year"] == 1,
                         "net_cashflow_eur"].iloc[0])
    )
    assert delta_net == pytest.approx(GO_PRICE * 800.0, abs=0.01)
    kpi = compute_financial_kpis(cf, econ)
    assert kpi["total_go_revenue_eur_lifecycle"] == pytest.approx(
        float(op["go_revenue_eur"].sum()), abs=0.01,
    )


def test_monthly_reconciliation_rides_pv_shape():
    econ = _econ(go_price_eur_per_mwh=GO_PRICE, project_lifecycle_years=4)
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    n = 96
    res = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "savings_self_consumption_eur": np.full(n, 10.0),
        "profit_export_from_pv_eur": np.full(n, 5.0),
        "profit_export_from_bess_eur": np.full(n, 5.0),
        "expense_charge_bess_grid_eur": np.full(n, 1.0),
        "pv_kwh": np.full(n, 100.0),
    })
    monthly, quarterly = derive_monthly_cashflow(res, cf, econ)
    for y in range(1, 5):
        y_total = float(cf.loc[
            cf["project_year"] == y, "go_revenue_eur",
        ].iloc[0])
        m_total = float(monthly.loc[
            monthly["project_year"] == y, "go_revenue_eur",
        ].sum())
        assert m_total == pytest.approx(y_total, abs=1e-6), y
        y_net = float(cf.loc[
            cf["project_year"] == y, "net_cashflow_eur",
        ].iloc[0])
        m_net = float(monthly.loc[
            monthly["project_year"] == y, "net_cashflow_eur",
        ].sum())
        assert m_net == pytest.approx(y_net, abs=1e-6), y
    assert "go_revenue_eur" in quarterly.columns


def test_sensitivity_membership():
    from pvbess_opt.economics import TAX_LAYER_COLUMNS
    from pvbess_opt.sensitivity import _recompute_net, _scale_revenue

    econ = _econ(go_price_eur_per_mwh=GO_PRICE)
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    scaled = _scale_revenue(cf, 1.1, econ)
    assert float(scaled.loc[scaled["project_year"] == 1,
                            "go_revenue_eur"].iloc[0]) == pytest.approx(
        1.1 * float(cf.loc[cf["project_year"] == 1,
                           "go_revenue_eur"].iloc[0]),
    )
    base = cf.drop(columns=list(TAX_LAYER_COLUMNS))
    recomputed = _recompute_net(base.copy())
    pd.testing.assert_series_equal(
        recomputed["net_cashflow_eur"], base["net_cashflow_eur"],
    )


def test_theme_registration_and_validation(tmp_path):
    from pvbess_opt.theme import (
        FINANCIAL_LABELS,
        FINANCIAL_LEGEND_ORDER,
        financial_color,
    )
    assert "GO revenue" in FINANCIAL_LABELS
    assert "GO revenue" in FINANCIAL_LEGEND_ORDER
    assert financial_color("GO revenue") == "#7CB342"

    from pvbess_opt.io import (
        BALANCING_SHEET_DEFAULTS,
        BESS_SHEET_DEFAULTS,
        ECONOMICS_SHEET_DEFAULTS,
        PROJECT_SHEET_DEFAULTS,
        PV_SHEET_DEFAULTS,
        SIMULATION_SHEET_DEFAULTS,
        read_workbook,
        write_workbook,
    )
    n = 24
    typed = {
        "ts": pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
            "pv_kwh": np.full(n, 100.0),
            "dam_price_eur_per_mwh": np.full(n, 60.0),
        }),
        "project": dict(PROJECT_SHEET_DEFAULTS, mode="merchant"),
        "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=1000.0),
        "bess": dict(BESS_SHEET_DEFAULTS),
        "economics": dict(
            ECONOMICS_SHEET_DEFAULTS, go_price_eur_per_mwh=-1.0,
        ),
        "simulation": dict(SIMULATION_SHEET_DEFAULTS),
        "balancing": dict(BALANCING_SHEET_DEFAULTS),
    }
    path = tmp_path / "neg.xlsx"
    write_workbook(typed, path)
    with pytest.raises(ValueError, match="go_price_eur_per_mwh"):
        read_workbook(path)
