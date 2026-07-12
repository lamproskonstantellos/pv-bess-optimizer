"""Intraday revenue stream through the financial stack (Eqs. E58/E59, I6).

Covers the yearly cashflow rows, the fee-applicability decisions, the
monthly/quarterly reconciliation, the sensitivity component list and
scaling decisions, the availability/curtailment derates, the LCOE/LCOS
exclusion, the SUMMARY gating, the theme registrations and the lifetime
per-origin recompute.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.availability import apply_operating_derates
from pvbess_opt.economics import (
    build_yearly_cashflow,
    compute_financial_kpis,
    derive_monthly_cashflow,
)
from pvbess_opt.sensitivity import _recompute_net, _scale_revenue
from pvbess_opt.theme import (
    FINANCIAL_COLORS,
    FINANCIAL_LABELS,
    FINANCIAL_LEGEND_ORDER,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _kpis(**overrides) -> dict:
    kpis = {
        "profit_load_from_pv_eur": 0.0,
        "profit_load_from_bess_eur": 0.0,
        "profit_export_from_pv_eur": 500_000.0,
        "profit_export_from_bess_eur": 200_000.0,
        "expense_charge_bess_grid_eur": 50_000.0,
        "profit_total_eur": 650_000.0,
        "pv_export_mwh": 8_000.0,
        "bess_export_mwh": 2_000.0,
        "bess_total_discharge_mwh": 2_100.0,
        "pv_generation_mwh": 9_000.0,
    }
    kpis.update(overrides)
    return kpis


def _id_kpis(**overrides) -> dict:
    """KPI dict of a two-stage run: 40k net margin, 1k venue fee."""
    kpis = _kpis(
        id_net_revenue_eur=40_000.0,
        id_venue_fee_eur=1_000.0,
        id_traded_volume_mwh=5_000.0,
        id_sell_pv_mwh=500.0,
        id_sell_bess_mwh=2_000.0,
        profit_total_eur=690_000.0,
    )
    kpis.update(overrides)
    return kpis


def _econ(**overrides) -> dict:
    econ = {
        "project_lifecycle_years": 20,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 1.5,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 2.0,
        "capex_pv_eur_per_kw": 500.0,
        "capex_bess_eur_per_kwh": 200.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 10.0,
        "opex_bess_eur_per_kw": 5.0,
        "pv_degradation_year1_pct": 2.0,
        "pv_degradation_annual_pct": 0.5,
        "bess_degradation_annual_pct": 2.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
    }
    econ.update(overrides)
    return econ


def _id_econ(**overrides) -> dict:
    econ = _econ(id_fee_eur_per_mwh=0.2, id_inflation_pct=1.0)
    econ.update(overrides)
    return econ


def _caps() -> dict:
    return {"pv_kwp": 5_000.0, "bess_kw": 2_500.0, "bess_kwh": 10_000.0}


def _op(cf: pd.DataFrame) -> pd.DataFrame:
    return cf[cf["project_year"] >= 1].set_index("project_year")


# ---------------------------------------------------------------------------
# Yearly cashflow rows (Eqs. E58/E59)
# ---------------------------------------------------------------------------


def test_zero_default_is_bit_identical():
    """Absent id keys == id keys at zero: identical frames, zero column."""
    cf_absent = build_yearly_cashflow(_kpis(), _econ(), _caps())
    cf_zero = build_yearly_cashflow(
        _kpis(
            id_net_revenue_eur=0.0, id_venue_fee_eur=0.0,
            id_traded_volume_mwh=0.0, id_sell_pv_mwh=0.0,
            id_sell_bess_mwh=0.0,
        ),
        _econ(id_fee_eur_per_mwh=0.0, id_inflation_pct=0.0),
        _caps(),
    )
    pd.testing.assert_frame_equal(cf_absent, cf_zero)
    assert float(cf_absent["intraday_revenue_eur"].abs().max()) == 0.0
    assert float(cf_absent["intraday_fee_eur"].abs().max()) == 0.0


def test_year1_rows_and_per_origin_fade():
    """Year-1 margin = net + fee; later years fade per origin and index
    by id_inflation_pct (Eq. E58); the fee rides the fading volume at a
    flat rate (Eq. E59)."""
    cf = build_yearly_cashflow(_id_kpis(), _id_econ(), _caps())
    op = _op(cf)
    assert op.loc[1, "intraday_revenue_eur"] == pytest.approx(41_000.0)
    assert op.loc[1, "intraday_fee_eur"] == pytest.approx(-0.2 * 5_000.0)
    # Year-2 expectation: margin split 500/2500 PV : 2000/2500 BESS,
    # PV leg fades on the Year-2 PV factor, BESS on the pooled factor,
    # both indexed by (1 + 1%)^1.
    pv_f2 = float(op.loc[2, "pv_production_factor"])
    bess_f2 = float(op.loc[2, "bess_capacity_factor"])
    share_pv = 500.0 / 2_500.0
    expected = (
        41_000.0 * share_pv * pv_f2 + 41_000.0 * (1 - share_pv) * bess_f2
    ) * 1.01
    assert op.loc[2, "intraday_revenue_eur"] == pytest.approx(
        expected, rel=1e-9,
    )
    expected_fee = -0.2 * (
        5_000.0 * share_pv * pv_f2 + 5_000.0 * (1 - share_pv) * bess_f2
    )
    assert op.loc[2, "intraday_fee_eur"] == pytest.approx(
        expected_fee, rel=1e-9,
    )


def test_net_cashflow_identity_with_intraday_on():
    cf = build_yearly_cashflow(_id_kpis(), _id_econ(), _caps())
    op = _op(cf)
    components = (
        "revenue_eur", "balancing_revenue_eur",
        "balancing_aggregator_fee_eur", "route_to_market_fee_eur",
        "optimizer_fee_eur", "optimizer_floor_topup_eur",
        "grid_charging_fee_eur", "imbalance_cost_eur", "toll_revenue_eur",
        "state_support_eur", "state_support_clawback_eur",
        "capacity_market_revenue_eur", "revenue_levy_eur",
        "curtailment_compensation_eur", "go_revenue_eur",
        "support_settlement_eur", "intraday_revenue_eur",
        "intraday_fee_eur", "ppa_revenue_eur", "opex_eur", "capex_eur",
        "devex_eur", "augmentation_capex_eur",
    )
    rebuilt = sum(op[c].astype(float) for c in components)
    assert float(
        (rebuilt - op["net_cashflow_eur"]).abs().max()
    ) < 1e-9


def test_optimizer_share_charges_bess_origin_id_margin():
    """E13d amendment (Eq. I6): the BESS-origin ID margin joins the
    optimizer-share base; the zero clamp still holds against a
    hand-built negative margin."""
    econ = _id_econ(optimizer_revenue_share_pct=10.0)
    cf_on = build_yearly_cashflow(_id_kpis(), econ, _caps())
    cf_off = build_yearly_cashflow(
        _kpis(), _econ(optimizer_revenue_share_pct=10.0), _caps(),
    )
    fee_on = float(_op(cf_on).loc[1, "optimizer_fee_eur"])
    fee_off = float(_op(cf_off).loc[1, "optimizer_fee_eur"])
    # BESS-origin Year-1 margin = 41k x (2000 / 2500) = 32.8k; the fee
    # grows by 10 % of it.
    assert fee_on == pytest.approx(fee_off - 0.10 * 32_800.0, rel=1e-9)

    # Clamp: a (hand-built) negative net margin never flips the fee
    # positive nor reduces it below zero.
    kpis_neg = _id_kpis(
        id_net_revenue_eur=-500_000.0, id_venue_fee_eur=0.0,
        profit_total_eur=150_000.0,
        profit_export_from_bess_eur=60_000.0,
    )
    cf_neg = build_yearly_cashflow(kpis_neg, econ, _caps())
    assert float(_op(cf_neg).loc[1, "optimizer_fee_eur"]) <= 0.0
    assert float(_op(cf_neg).loc[1, "optimizer_fee_eur"]) == pytest.approx(
        0.0, abs=1e-9,
    )


def test_aggregator_fee_excludes_id_margin():
    """E13 decision (Eq. I6): the energy-aggregator ad-valorem fee does
    NOT charge the intraday margin — intermediation is priced by the
    explicit venue fee (the balancing/E13b precedent)."""
    econ = _id_econ(aggregator_fee_pct_revenue=5.0)
    cf_on = build_yearly_cashflow(_id_kpis(), econ, _caps())
    cf_off = build_yearly_cashflow(
        _kpis(), _econ(aggregator_fee_pct_revenue=5.0), _caps(),
    )
    pd.testing.assert_series_equal(
        cf_on["aggregator_fee_eur"], cf_off["aggregator_fee_eur"],
    )


def test_bess_market_revenue_base_includes_id_margin():
    """E25a netting base gains the BESS-origin ID margin (Eq. I6)."""
    cf_on = build_yearly_cashflow(_id_kpis(), _id_econ(), _caps())
    cf_off = build_yearly_cashflow(_kpis(), _econ(), _caps())
    delta = float(
        _op(cf_on).loc[1, "bess_market_revenue_eur"]
        - _op(cf_off).loc[1, "bess_market_revenue_eur"]
    )
    assert delta == pytest.approx(41_000.0 * (2_000.0 / 2_500.0), rel=1e-9)


def test_monthly_and_quarterly_reconcile_to_yearly():
    cf = build_yearly_cashflow(_id_kpis(), _id_econ(), _caps())
    res_stub = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=8760, freq="h"),
        "pv_kwh": 100.0,
        "pv_to_grid_kwh": 90.0,
        "profit_load_from_pv_eur": 0.0,
        "profit_export_from_pv_eur": 5.0,
    })
    monthly, quarterly = derive_monthly_cashflow(res_stub, cf, _id_econ())
    yearly = _op(cf)[["intraday_revenue_eur", "intraday_fee_eur"]]
    rec_m = monthly.groupby("project_year")[
        ["intraday_revenue_eur", "intraday_fee_eur"]
    ].sum()
    assert float((rec_m - yearly).abs().max().max()) < 1e-6
    rec_q = quarterly.groupby("project_year")[
        ["intraday_revenue_eur", "intraday_fee_eur"]
    ].sum()
    assert float((rec_q - yearly).abs().max().max()) < 1e-6
    # Monthly net carries the two columns.
    net_rebuilt = monthly["net_cashflow_eur"] - (
        monthly["intraday_revenue_eur"] + monthly["intraday_fee_eur"]
    )
    assert not np.allclose(
        net_rebuilt, monthly["net_cashflow_eur"],
    ), "intraday columns must be part of the monthly net"


# ---------------------------------------------------------------------------
# Financial KPIs / SUMMARY / LCOE
# ---------------------------------------------------------------------------


def test_lifetime_totals_and_lcoe_invariance():
    cf_on = build_yearly_cashflow(_id_kpis(), _id_econ(), _caps())
    cf_off = build_yearly_cashflow(_kpis(), _econ(), _caps())
    fk_on = compute_financial_kpis(cf_on, _id_econ())
    fk_off = compute_financial_kpis(cf_off, _econ())
    assert fk_on["total_intraday_revenue_eur_lifecycle"] == pytest.approx(
        float(_op(cf_on)["intraday_revenue_eur"].sum()), abs=0.01,
    )
    assert fk_on["total_intraday_fee_eur_lifecycle"] == pytest.approx(
        float(_op(cf_on)["intraday_fee_eur"].sum()), abs=0.01,
    )
    for key in ("lcoe_eur_per_mwh", "lcos_eur_per_mwh"):
        a, b = fk_on[key], fk_off[key]
        assert (a == b) or (math.isnan(a) and math.isnan(b))


def test_summary_rows_render_only_when_set(tmp_path):
    from pvbess_opt.io import write_summary_md

    def _render(fin_kpis):
        out = tmp_path / "SUMMARY.md"
        write_summary_md(
            out,
            kpis_year1={"mode": "merchant", "profit_total_eur": 1.0},
            financial_kpis=fin_kpis,
            params={"mode": "merchant"},
            solver_name="highs",
        )
        return out.read_text()

    fin_on = compute_financial_kpis(
        build_yearly_cashflow(_id_kpis(), _id_econ(), _caps()), _id_econ(),
    )
    text_on = _render(fin_on)
    assert "Lifetime intraday revenue [EUR]" in text_on
    assert "Lifetime intraday venue fee [EUR]" in text_on

    fin_off = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), _econ(), _caps()), _econ(),
    )
    text_off = _render(fin_off)
    assert "intraday" not in text_off.lower()


# ---------------------------------------------------------------------------
# Sensitivity
# ---------------------------------------------------------------------------


def test_recompute_net_matches_cashflow_identity():
    cf = build_yearly_cashflow(_id_kpis(), _id_econ(), _caps())
    rebuilt = _recompute_net(cf.copy())
    pd.testing.assert_series_equal(
        rebuilt["net_cashflow_eur"], cf["net_cashflow_eur"],
    )


def test_revenue_driver_scales_margin_not_fee():
    cf = build_yearly_cashflow(_id_kpis(), _id_econ(), _caps())
    scaled = _scale_revenue(cf.copy(), 1.10, _id_econ())
    op, op_s = _op(cf), _op(scaled)
    assert float(op_s.loc[1, "intraday_revenue_eur"]) == pytest.approx(
        1.10 * float(op.loc[1, "intraday_revenue_eur"]), rel=1e-9,
    )
    assert float(op_s.loc[1, "intraday_fee_eur"]) == pytest.approx(
        float(op.loc[1, "intraday_fee_eur"]), rel=1e-12,
    )


def test_scale_revenue_noop_identity():
    cf = build_yearly_cashflow(_id_kpis(), _id_econ(), _caps())
    noop = _scale_revenue(cf.copy(), 1.0, _id_econ())
    from pvbess_opt.economics import TAX_LAYER_COLUMNS

    pre_tax = [c for c in cf.columns if c not in TAX_LAYER_COLUMNS]
    pd.testing.assert_frame_equal(noop[pre_tax], cf[pre_tax])


# ---------------------------------------------------------------------------
# Derates
# ---------------------------------------------------------------------------


def test_availability_and_curtailment_derate_id_keys():
    kpis = _id_kpis()
    out = apply_operating_derates(
        dict(kpis),
        {"unavailability_pct": 10.0, "curtailment_pct": 5.0},
    )
    factor = 0.9 * 0.95
    for key in (
        "id_net_revenue_eur", "id_venue_fee_eur", "id_sell_mwh",
        "id_buy_mwh", "id_traded_volume_mwh", "id_sell_pv_mwh",
        "id_sell_bess_mwh",
    ):
        if key in kpis:
            assert out[key] == pytest.approx(
                float(kpis[key]) * factor, rel=1e-9,
            ), key
    # profit recomposition folds the id-net delta (availability scales
    # profit_total directly; curtailment recomposes from components).
    assert out["profit_total_eur"] < float(kpis["profit_total_eur"])


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------


def test_theme_registrations():
    assert FINANCIAL_COLORS["intraday_revenue"] == "#26C6DA"
    assert FINANCIAL_COLORS["intraday_fee"] == "#E91E63"
    for label in ("Intraday revenue", "Intraday fee"):
        assert label in FINANCIAL_LABELS
        assert label in FINANCIAL_LEGEND_ORDER


# ---------------------------------------------------------------------------
# Lifetime per-origin recompute
# ---------------------------------------------------------------------------


def test_lifetime_recomputes_id_settlement_per_origin():
    from pvbess_opt.lifetime import build_lifetime_dispatch

    n = 8760
    res = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "pv_kwh": 100.0,
        "pv_to_grid_kwh": 80.0,
        "bess_dis_grid_kwh": 20.0,
        "bess_dis_load_kwh": 0.0,
        "dam_price_eur_per_mwh": 60.0,
        "ida_price_eur_per_mwh": 70.0,
        "id_sell_pv_kwh": 5.0,
        "id_sell_bess_kwh": 3.0,
        "id_buy_pv_kwh": 0.0,
        "id_buy_bess_kwh": 1.0,
        "id_buy_kwh": 1.0,
        "profit_load_from_pv_eur": 0.0,
        "profit_load_from_bess_eur": 0.0,
        "profit_export_from_pv_eur": 4.8,
        "profit_export_from_bess_eur": 1.2,
        "expense_charge_bess_grid_eur": 0.0,
        "id_revenue_eur": (70.0 - 60.0) / 1000.0 * (5.0 + 3.0 - 1.0),
        "id_fee_eur": 0.2 / 1000.0 * (5.0 + 3.0 + 1.0),
    })
    econ = _id_econ(project_lifecycle_years=3)
    lifetime = build_lifetime_dispatch(
        res, econ, _caps(), year1_discharge_mwh=200.0,
    )
    y3 = lifetime[lifetime["project_year"] == 3]
    pv_f = float(
        (1.0 - 0.02) * (1.0 - 0.005) ** 1
    )
    # The kWh columns scale per origin...
    assert float(y3["id_sell_pv_kwh"].iloc[0]) == pytest.approx(
        5.0 * pv_f, rel=1e-9,
    )
    # ...and the settlement column is rebuilt from the scaled trades.
    bess_f = float(y3["id_sell_bess_kwh"].iloc[0]) / 3.0
    expected = (70.0 - 60.0) / 1000.0 * (
        5.0 * pv_f + 3.0 * bess_f - 1.0 * bess_f
    )
    assert float(y3["id_revenue_eur"].iloc[0]) == pytest.approx(
        expected, rel=1e-9,
    )
    expected_fee = 0.2 / 1000.0 * (5.0 * pv_f + 3.0 * bess_f + 1.0 * bess_f)
    assert float(y3["id_fee_eur"].iloc[0]) == pytest.approx(
        expected_fee, rel=1e-9,
    )
