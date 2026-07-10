"""BESS tolling agreement (Eqs. E29/E29a).

A fixed EUR/MW/yr payment for dispatch rights over a phase window
(Eq. E25): availability-conditioned, contractually indexed, and with NO
capacity-fade scaling (the payment is on the power block).  Under the
default 'zeroed' merchant treatment the toller keeps every BESS-origin
merchant stream in toll years; 'retained' stacks the toll on top.
Locked: zero-default bit-identity, the E29 cent-level rate math with
inclusive phase boundaries, the exact E29a zeroing scope (and what is
deliberately NOT zeroed), monthly flat-1/12 reconciliation incl. a
replacement year inside the window, sensitivity handling (net recompute
folds it; the Revenue driver does NOT scale it), warnings, SUMMARY /
theme registration and LCOE/LCOS invariance.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.availability import availability_factor
from pvbess_opt.economics import (
    build_yearly_cashflow,
    compute_financial_kpis,
    derive_monthly_cashflow,
)
from pvbess_opt.io import _SHEET_DEFAULTS, validate_workbook_params

N_YEARS = 8
TOLL_RATE = 80_000.0
BESS_KW = 500.0
UNAVAIL_PCT = 2.0


def _econ(**o) -> dict:
    econ = {
        "project_lifecycle_years": N_YEARS,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "unavailability_pct": UNAVAIL_PCT,
        "capex_pv_eur_per_kw": 500.0,
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
        # Active merchant fee structures — the zeroing scope must gate
        # every one of them in toll years.
        "balancing_aggregator_fee_pct_revenue": 10.0,
        "optimizer_revenue_share_pct": 15.0,
        "route_to_market_fee_eur_per_mwh": 2.0,
    }
    econ.update(o)
    return econ


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": BESS_KW, "bess_kwh": 1000.0}


def _kpis(**o) -> dict:
    base = {
        "profit_load_from_pv_eur": 50_000.0,
        "profit_load_from_bess_eur": 0.0,
        "profit_export_from_pv_eur": 30_000.0,
        "profit_export_from_bess_eur": 40_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        # retail + dam + ppa - grid charging fee = 115000 - 800
        "profit_total_eur": 114_200.0,
        "bm_total_capacity_revenue_eur": 12_000.0,
        "bm_total_activation_revenue_eur": 3_000.0,
        "pv_export_mwh": 600.0,
        "bess_export_mwh": 200.0,
        "expense_grid_charging_fee_eur": 800.0,
        "imbalance_cost_year1_eur": 1_000.0,
    }
    base.update(o)
    return base


def _toll_econ(**o) -> dict:
    kw = {
        "bess_toll_eur_per_mw_year": TOLL_RATE,
        "bess_toll_year_from": 1,
        "bess_toll_year_to": 5,
        "bess_toll_merchant_treatment": "zeroed",
        "bess_toll_indexation_pct": 0.0,
    }
    kw.update(o)
    return _econ(**kw)


# ---------------------------------------------------------------------------
# Zero-default bit-identity
# ---------------------------------------------------------------------------


def test_zero_default_bit_identity():
    """No toll keys == keys at defaults, apart from the all-zero column."""
    base = build_yearly_cashflow(_kpis(), _econ(), _caps())
    with_keys = build_yearly_cashflow(
        _kpis(),
        _econ(
            bess_toll_eur_per_mw_year=0.0,
            bess_toll_year_from=1,
            bess_toll_year_to=0,
            bess_toll_merchant_treatment="zeroed",
            bess_toll_indexation_pct=0.0,
        ),
        _caps(),
    )
    pd.testing.assert_frame_equal(base, with_keys)
    assert (base["toll_revenue_eur"] == 0.0).all()


# ---------------------------------------------------------------------------
# E29 — rate math and phase boundaries
# ---------------------------------------------------------------------------


def test_e29_cent_level_rate_and_indexation():
    infl = 0.03
    cf = build_yearly_cashflow(
        _kpis(), _toll_econ(bess_toll_indexation_pct=infl * 100.0), _caps(),
    ).set_index("project_year")
    avail = availability_factor(UNAVAIL_PCT)
    assert float(cf.loc[0, "toll_revenue_eur"]) == 0.0
    for y in range(1, N_YEARS + 1):
        if 1 <= y <= 5:
            expected = (
                TOLL_RATE * (BESS_KW / 1000.0) * avail
                * (1.0 + infl) ** (y - 1)
            )
        else:
            expected = 0.0
        assert float(cf.loc[y, "toll_revenue_eur"]) == pytest.approx(
            expected, abs=0.01,
        ), y


def test_e29_phase_boundaries_inclusive_and_open_ended():
    cf = build_yearly_cashflow(
        _kpis(), _toll_econ(bess_toll_year_from=2, bess_toll_year_to=4),
        _caps(),
    ).set_index("project_year")
    active = {
        y: abs(float(cf.loc[y, "toll_revenue_eur"])) > 1e-9
        for y in range(1, N_YEARS + 1)
    }
    assert active == {y: 2 <= y <= 4 for y in range(1, N_YEARS + 1)}

    open_ended = build_yearly_cashflow(
        _kpis(), _toll_econ(bess_toll_year_from=3, bess_toll_year_to=0),
        _caps(),
    ).set_index("project_year")
    assert all(
        abs(float(open_ended.loc[y, "toll_revenue_eur"])) > 1e-9
        for y in range(3, N_YEARS + 1)
    )
    assert all(
        float(open_ended.loc[y, "toll_revenue_eur"]) == 0.0 for y in (1, 2)
    )


# ---------------------------------------------------------------------------
# E29a — merchant zeroing scope
# ---------------------------------------------------------------------------


def test_zeroing_scope_lock():
    base = build_yearly_cashflow(_kpis(), _econ(), _caps()).set_index(
        "project_year",
    )
    cf = build_yearly_cashflow(_kpis(), _toll_econ(), _caps()).set_index(
        "project_year",
    )
    rev1_dam_bess = 40_000.0 - 5_000.0
    for y in range(1, 6):  # toll years
        bess_factor = float(cf.loc[y, "bess_capacity_factor"])
        # The DAM stream drops by exactly the BESS-DAM component.
        assert float(cf.loc[y, "revenue_dam_eur"]) == pytest.approx(
            float(base.loc[y, "revenue_dam_eur"])
            - rev1_dam_bess * bess_factor,
            abs=0.01,
        ), y
        # Every BESS-origin merchant column is gated to zero.
        for col in (
            "balancing_capacity_revenue_eur",
            "balancing_activation_revenue_eur",
            "balancing_revenue_eur",
            "balancing_aggregator_fee_eur",
            "optimizer_fee_eur",
            "grid_charging_fee_eur",
            "bess_market_revenue_eur",
        ):
            assert float(cf.loc[y, col]) == 0.0, (y, col)
        # The RTM fee keeps charging the PV export only.
        pv_factor = float(cf.loc[y, "pv_production_factor"])
        assert float(cf.loc[y, "route_to_market_fee_eur"]) == pytest.approx(
            -2.0 * 600.0 * pv_factor, abs=0.01,
        ), y
        # NOT zeroed: retail stream and the PV-error-driven imbalance.
        assert float(cf.loc[y, "revenue_retail_eur"]) == pytest.approx(
            float(base.loc[y, "revenue_retail_eur"]), abs=1e-9,
        ), y
        assert float(cf.loc[y, "imbalance_cost_eur"]) == pytest.approx(
            float(base.loc[y, "imbalance_cost_eur"]), abs=1e-9,
        ), y
    # Post-toll years are bit-equal to the merchant baseline.
    from pvbess_opt.economics import TAX_LAYER_COLUMNS

    for y in range(6, N_YEARS + 1):
        for col in base.columns:
            if col in ("cumulative_cf_eur", "cumulative_dcf_eur",
                       "toll_revenue_eur", *TAX_LAYER_COLUMNS):
                continue  # cumulatives carry the toll-years history
            assert float(cf.loc[y, col]) == float(base.loc[y, col]), (y, col)


def test_retained_treatment_stacks_on_top():
    base = build_yearly_cashflow(_kpis(), _econ(), _caps())
    cf = build_yearly_cashflow(
        _kpis(), _toll_econ(bess_toll_merchant_treatment="retained"),
        _caps(),
    )
    from pvbess_opt.economics import TAX_LAYER_COLUMNS

    for col in base.columns:
        if col in ("toll_revenue_eur", "net_cashflow_eur",
                   "discounted_cf_eur", "cumulative_cf_eur",
                   "cumulative_dcf_eur", *TAX_LAYER_COLUMNS):
            continue
        pd.testing.assert_series_equal(cf[col], base[col])
    diff = cf["net_cashflow_eur"] - base["net_cashflow_eur"]
    pd.testing.assert_series_equal(
        diff, cf["toll_revenue_eur"], check_names=False,
    )


def test_net_folds_the_toll():
    base = build_yearly_cashflow(_kpis(), _econ(), _caps()).set_index(
        "project_year",
    )
    cf = build_yearly_cashflow(_kpis(), _toll_econ(), _caps()).set_index(
        "project_year",
    )
    y = 2
    expected_net = (
        float(base.loc[y, "net_cashflow_eur"])
        # remove the zeroed merchant streams...
        - float(base.loc[y, "revenue_dam_eur"])
        + float(cf.loc[y, "revenue_dam_eur"])
        - float(base.loc[y, "balancing_revenue_eur"])
        - float(base.loc[y, "balancing_aggregator_fee_eur"])
        - float(base.loc[y, "optimizer_fee_eur"])
        - float(base.loc[y, "grid_charging_fee_eur"])
        - float(base.loc[y, "route_to_market_fee_eur"])
        + float(cf.loc[y, "route_to_market_fee_eur"])
        # ...and add the toll.
        + float(cf.loc[y, "toll_revenue_eur"])
    )
    assert float(cf.loc[y, "net_cashflow_eur"]) == pytest.approx(
        expected_net, abs=0.01,
    )


# ---------------------------------------------------------------------------
# Monthly reconciliation
# ---------------------------------------------------------------------------


def test_monthly_flat_twelfth_and_reconciliation():
    from tests.test_monthly_cashflow_reconciliation import _make_res_frame

    res = _make_res_frame()
    econ = _toll_econ(
        bess_replacement_year=3, bess_replacement_cost_pct=50.0,
    )
    yearly = build_yearly_cashflow(_kpis(), econ, _caps())
    monthly, quarterly = derive_monthly_cashflow(res, yearly, econ)
    yearly_indexed = yearly.set_index("project_year")
    for y in range(1, N_YEARS + 1):
        sub = monthly.loc[monthly["project_year"] == y]
        toll_y = float(yearly_indexed.loc[y, "toll_revenue_eur"])
        # Flat 1/12 allocation, exact reconciliation.
        assert float(sub["toll_revenue_eur"].sum()) == pytest.approx(
            toll_y, abs=1e-6,
        ), y
        assert np.allclose(
            sub["toll_revenue_eur"].to_numpy(), toll_y / 12.0,
        ), y
        # The monthly net still reconciles the yearly net exactly —
        # including the replacement year inside the toll window.
        assert float(sub["net_cashflow_eur"].sum()) == pytest.approx(
            float(yearly_indexed.loc[y, "net_cashflow_eur"]), abs=1e-6,
        ), y
    q1 = quarterly.loc[
        (quarterly["project_year"] == 1) & (quarterly["period"] == 1),
        "toll_revenue_eur",
    ]
    assert float(q1.iloc[0]) == pytest.approx(
        float(yearly_indexed.loc[1, "toll_revenue_eur"]) / 4.0, abs=1e-6,
    )


# ---------------------------------------------------------------------------
# Sensitivity
# ---------------------------------------------------------------------------


def test_sensitivity_recompute_and_no_revenue_scaling():
    from pvbess_opt.sensitivity import _recompute_net, _scale_revenue

    cf = build_yearly_cashflow(_kpis(), _toll_econ(), _caps())
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
    # Fixed contractual EUR/MW: the toll does NOT scale with the driver.
    pd.testing.assert_series_equal(
        scaled["toll_revenue_eur"], cf["toll_revenue_eur"],
    )
    # ...while a price-driven stream does (post-toll merchant years).
    assert float(scaled.set_index("project_year").loc[
        6, "balancing_revenue_eur",
    ]) == pytest.approx(
        1.1 * float(cf.set_index("project_year").loc[
            6, "balancing_revenue_eur",
        ]),
        abs=0.01,
    )


# ---------------------------------------------------------------------------
# Validation and warnings
# ---------------------------------------------------------------------------


def _typed(bess_power_kw: float = BESS_KW, **econ_overrides) -> dict:
    typed = {
        sheet: dict(defaults) for sheet, defaults in _SHEET_DEFAULTS.items()
    }
    typed["bess"]["bess_power_kw"] = bess_power_kw
    typed["economics"].update(econ_overrides)
    return typed


def test_window_and_treatment_validation():
    with pytest.raises(ValueError, match="bess_toll_year_from"):
        validate_workbook_params(
            _typed(bess_toll_year_from=0), dt_minutes=15,
        )
    with pytest.raises(ValueError, match="bess_toll_year_to"):
        validate_workbook_params(
            _typed(bess_toll_year_from=5, bess_toll_year_to=3),
            dt_minutes=15,
        )
    with pytest.raises(ValueError, match="bess_toll_merchant_treatment"):
        validate_workbook_params(
            _typed(bess_toll_merchant_treatment="keep"), dt_minutes=15,
        )
    with pytest.raises(ValueError, match="bess_toll_eur_per_mw_year"):
        validate_workbook_params(
            _typed(bess_toll_eur_per_mw_year=-1.0), dt_minutes=15,
        )
    # Valid configurations pass.
    validate_workbook_params(
        _typed(
            bess_toll_eur_per_mw_year=TOLL_RATE,
            bess_toll_year_from=1,
            bess_toll_year_to=0,
        ),
        dt_minutes=15,
    )


def test_stacking_warnings(caplog):
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io"):
        validate_workbook_params(
            _typed(
                bess_toll_eur_per_mw_year=TOLL_RATE,
                bess_toll_merchant_treatment="retained",
                optimizer_revenue_share_pct=15.0,
            ),
            dt_minutes=15,
        )
    text = caplog.text
    assert "double-monetis" in text
    assert "optimizer revenue" in text

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io"):
        validate_workbook_params(
            _typed(bess_power_kw=0.0, bess_toll_eur_per_mw_year=TOLL_RATE),
            dt_minutes=15,
        )
    assert "no-op" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io"):
        validate_workbook_params(_typed(), dt_minutes=15)
    assert "toll" not in caplog.text.lower()


def test_retail_bess_stream_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.economics"):
        build_yearly_cashflow(
            _kpis(
                profit_load_from_bess_eur=10_000.0,
                profit_total_eur=124_200.0,
            ),
            _toll_econ(),
            _caps(),
        )
    assert "NOT zeroed" in caplog.text


# ---------------------------------------------------------------------------
# KPI, SUMMARY, theme and LCOE/LCOS invariance
# ---------------------------------------------------------------------------


def _lifetime_yearly() -> pd.DataFrame:
    return pd.DataFrame({
        "project_year": range(1, N_YEARS + 1),
        "pv_generation_mwh": [1500.0 * 0.99 ** (y - 1)
                              for y in range(1, N_YEARS + 1)],
        "bess_discharge_mwh": [300.0 * 0.985 ** (y - 1)
                               for y in range(1, N_YEARS + 1)],
    })


def test_lifetime_total_summary_theme_and_lcoe_invariance():
    from pvbess_opt.io import _SUMMARY_OPTIONAL_FINANCIAL_KEYS
    from pvbess_opt.theme import (
        FINANCIAL_COLORS,
        FINANCIAL_LABELS,
        FINANCIAL_LEGEND_ORDER,
    )

    base_cf = build_yearly_cashflow(_kpis(), _econ(), _caps())
    toll_cf = build_yearly_cashflow(_kpis(), _toll_econ(), _caps())
    base_fin = compute_financial_kpis(
        base_cf, _econ(), capacities=_caps(),
        lifetime_yearly=_lifetime_yearly(), year1_kpis=_kpis(),
    )
    toll_fin = compute_financial_kpis(
        toll_cf, _toll_econ(), capacities=_caps(),
        lifetime_yearly=_lifetime_yearly(), year1_kpis=_kpis(),
    )
    expected = float(
        toll_cf.loc[toll_cf["project_year"] >= 1, "toll_revenue_eur"].sum()
    )
    assert toll_fin["total_toll_revenue_eur_lifecycle"] == pytest.approx(
        expected, abs=0.01,
    )
    assert base_fin["total_toll_revenue_eur_lifecycle"] == 0.0
    # Revenue-agnostic metrics are invariant under any toll setting.
    assert toll_fin["lcoe_eur_per_mwh"] == base_fin["lcoe_eur_per_mwh"]
    assert toll_fin["lcos_eur_per_mwh"] == base_fin["lcos_eur_per_mwh"]
    assert ("total_toll_revenue_eur_lifecycle",
            "Lifetime tolling revenue [EUR]") in (
        _SUMMARY_OPTIONAL_FINANCIAL_KEYS
    )
    assert "Tolling revenue" in FINANCIAL_LABELS
    assert "Tolling revenue" in FINANCIAL_LEGEND_ORDER
    assert FINANCIAL_COLORS["toll_revenue"] == "#00695C"
