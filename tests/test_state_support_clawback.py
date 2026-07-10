"""State support with two-way clawback (Eqs. E31/E31a).

A fixed EUR/MW/yr support (availability-scaled, no fade) over a phase
window, netted TWO-WAY against realised market revenue (the E25a base)
relative to an indexed threshold: clawback above it, compensation
below it, both at the same share — the settlement form of RRF-style
storage-support schemes (Tameio Anakampsis / TAA reference).  No floor
is applied: a net-repayment year is flagged in the run log.  Locked:
zero-default bit-identity, E31/E31a cent-level algebra across all
three regimes (clawback / compensation / repayment), the netting-base
scope, monthly booking (support flat 1/12, netting month 12), the
econ-threaded sensitivity recompute against the un-scaled threshold,
validation + stacking warnings, registries and LCOE/LCOS invariance.
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

N_YEARS = 5
BESS_KW = 500.0
UNAVAIL_PCT = 2.0
SIGMA = 40_000.0  # EUR/MW/yr support
THETA = 60_000.0  # EUR/MW/yr threshold
REV1_DAM_BESS = 35_000.0
DAM_TRAJ = [2.0, 0.5, -0.2, 1.5, 1.5]


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
        "bess_power_kw": BESS_KW,
    }
    econ.update(o)
    return econ


def _ss_econ(**o) -> dict:
    kw = {
        "state_support_eur_per_mw_year": SIGMA,
        "state_support_year_from": 1,
        "state_support_year_to": 0,
        "state_support_clawback_threshold_eur_per_mw_year": THETA,
        "state_support_clawback_share_pct": 100.0,
        "state_support_indexation_pct": 0.0,
        "trajectories": {
            "revenue_dam": {"mode": "replace", "values": list(DAM_TRAJ)},
        },
    }
    kw.update(o)
    return _econ(**kw)


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": BESS_KW, "bess_kwh": 1000.0}


def _kpis(**o) -> dict:
    base = {
        "profit_load_from_pv_eur": 50_000.0,
        "profit_load_from_bess_eur": 0.0,
        "profit_export_from_pv_eur": 30_000.0,
        "profit_export_from_bess_eur": 40_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 115_000.0,
    }
    base.update(o)
    return base


def _support_level(y: int = 1, infl: float = 0.0) -> float:
    return (
        SIGMA * (BESS_KW / 1000.0) * availability_factor(UNAVAIL_PCT)
        * (1.0 + infl) ** (y - 1)
    )


def _theta_level(y: int = 1, infl: float = 0.0) -> float:
    return THETA * (BESS_KW / 1000.0) * (1.0 + infl) ** (y - 1)


# ---------------------------------------------------------------------------
# Zero-default bit-identity
# ---------------------------------------------------------------------------


def test_zero_default_bit_identity():
    base = build_yearly_cashflow(_kpis(), _econ(), _caps())
    with_keys = build_yearly_cashflow(
        _kpis(),
        _econ(
            state_support_eur_per_mw_year=0.0,
            state_support_year_from=1,
            state_support_year_to=0,
            state_support_clawback_threshold_eur_per_mw_year=0.0,
            state_support_clawback_share_pct=0.0,
            state_support_indexation_pct=0.0,
        ),
        _caps(),
    )
    pd.testing.assert_frame_equal(base, with_keys)
    assert (base["state_support_eur"] == 0.0).all()
    assert (base["state_support_clawback_eur"] == 0.0).all()


# ---------------------------------------------------------------------------
# E31/E31a — three regimes, cent level, repayment flag
# ---------------------------------------------------------------------------


def test_e31_e31a_cent_lock_and_repayment_flag(caplog):
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.economics"):
        cf = build_yearly_cashflow(
            _kpis(), _ss_econ(), _caps(),
        ).set_index("project_year")
    assert float(cf.loc[0, "state_support_eur"]) == 0.0
    for y in range(1, N_YEARS + 1):
        bess_factor = float(cf.loc[y, "bess_capacity_factor"])
        m = REV1_DAM_BESS * bess_factor * DAM_TRAJ[y - 1]
        assert float(cf.loc[y, "state_support_eur"]) == pytest.approx(
            _support_level(), abs=0.01,
        ), y
        assert float(
            cf.loc[y, "state_support_clawback_eur"]
        ) == pytest.approx(-(m - _theta_level()), abs=0.01), y
    # Regime coverage: y1 clawback + net repayment; y2/y3 compensation;
    # y4 clawback with positive combined support.
    assert float(cf.loc[1, "state_support_clawback_eur"]) < 0.0
    assert (
        float(cf.loc[1, "state_support_eur"])
        + float(cf.loc[1, "state_support_clawback_eur"])
    ) < 0.0
    assert float(cf.loc[2, "state_support_clawback_eur"]) > 0.0
    assert float(cf.loc[3, "state_support_clawback_eur"]) > _theta_level()
    assert "net repayment" in caplog.text
    assert "[1]" in caplog.text or "1," in caplog.text


def test_indexation_escalates_support_and_threshold():
    infl = 0.02
    cf = build_yearly_cashflow(
        _kpis(),
        _ss_econ(state_support_indexation_pct=infl * 100.0),
        _caps(),
    ).set_index("project_year")
    y = 3
    bess_factor = float(cf.loc[y, "bess_capacity_factor"])
    m = REV1_DAM_BESS * bess_factor * DAM_TRAJ[y - 1]
    assert float(cf.loc[y, "state_support_eur"]) == pytest.approx(
        _support_level(y, infl), abs=0.01,
    )
    assert float(cf.loc[y, "state_support_clawback_eur"]) == pytest.approx(
        -(m - _theta_level(y, infl)), abs=0.01,
    )


def test_window_boundaries_and_share_zero_degenerate():
    cf = build_yearly_cashflow(
        _kpis(),
        _ss_econ(state_support_year_from=2, state_support_year_to=4),
        _caps(),
    ).set_index("project_year")
    for y in (1, 5):
        assert float(cf.loc[y, "state_support_eur"]) == 0.0, y
        assert float(cf.loc[y, "state_support_clawback_eur"]) == 0.0, y
    for y in (2, 3, 4):
        assert float(cf.loc[y, "state_support_eur"]) > 0.0, y

    pure = build_yearly_cashflow(
        _kpis(), _ss_econ(state_support_clawback_share_pct=0.0), _caps(),
    ).set_index("project_year")
    assert (pure["state_support_clawback_eur"] == 0.0).all()
    assert float(pure.loc[1, "state_support_eur"]) == pytest.approx(
        _support_level(), abs=0.01,
    )


# ---------------------------------------------------------------------------
# Netting-base scope
# ---------------------------------------------------------------------------


def test_netting_base_scope():
    base_cb = build_yearly_cashflow(
        _kpis(), _ss_econ(), _caps(),
    ).set_index("project_year")["state_support_clawback_eur"]
    # Retail / PPA inputs never enter the netting base.
    retail_heavy = build_yearly_cashflow(
        _kpis(profit_load_from_pv_eur=90_000.0,
              profit_total_eur=155_000.0),
        _ss_econ(), _caps(),
    ).set_index("project_year")["state_support_clawback_eur"]
    pd.testing.assert_series_equal(base_cb, retail_heavy)
    # Balancing enters exactly per E25a (net of the BSP fee).
    with_bal = build_yearly_cashflow(
        _kpis(bm_total_capacity_revenue_eur=12_000.0,
              bm_total_activation_revenue_eur=3_000.0),
        _ss_econ(balancing_aggregator_fee_pct_revenue=10.0),
        _caps(),
    ).set_index("project_year")
    for y in range(1, N_YEARS + 1):
        bess_factor = float(with_bal.loc[y, "bess_capacity_factor"])
        bal_net = 15_000.0 * bess_factor * 0.90
        assert float(
            with_bal.loc[y, "state_support_clawback_eur"]
        ) == pytest.approx(float(base_cb.loc[y]) + bal_net * -1.0, abs=0.01), y
    # A 'zeroed' toll zeroes the base: the netting tops up to theta.
    with_toll = build_yearly_cashflow(
        _kpis(),
        _ss_econ(bess_toll_eur_per_mw_year=50_000.0,
                 bess_toll_year_from=1, bess_toll_year_to=2),
        _caps(),
    ).set_index("project_year")
    for y in (1, 2):
        assert float(
            with_toll.loc[y, "state_support_clawback_eur"]
        ) == pytest.approx(_theta_level(), abs=0.01), y


# ---------------------------------------------------------------------------
# Monthly booking
# ---------------------------------------------------------------------------


def test_monthly_support_flat_netting_month12():
    from tests.test_monthly_cashflow_reconciliation import _make_res_frame

    res = _make_res_frame()
    econ = _ss_econ()
    yearly = build_yearly_cashflow(_kpis(), econ, _caps())
    monthly, _q = derive_monthly_cashflow(res, yearly, econ)
    yearly_indexed = yearly.set_index("project_year")
    for y in range(1, N_YEARS + 1):
        sub = monthly.loc[monthly["project_year"] == y]
        ss_y = float(yearly_indexed.loc[y, "state_support_eur"])
        cb_y = float(yearly_indexed.loc[y, "state_support_clawback_eur"])
        assert np.allclose(
            sub["state_support_eur"].to_numpy(), ss_y / 12.0,
        ), y
        by_month = sub.set_index("period")["state_support_clawback_eur"]
        assert float(by_month.loc[12]) == pytest.approx(cb_y, abs=1e-9), y
        assert (by_month.loc[1:11] == 0.0).all(), y
        assert float(sub["net_cashflow_eur"].sum()) == pytest.approx(
            float(yearly_indexed.loc[y, "net_cashflow_eur"]), abs=1e-6,
        ), y


# ---------------------------------------------------------------------------
# Sensitivity — scaled base vs un-scaled threshold
# ---------------------------------------------------------------------------


def test_sensitivity_recompute_scaled_base_unscaled_threshold():
    from pvbess_opt.sensitivity import _recompute_net, _scale_revenue

    econ = _ss_econ()
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    recomputed = _recompute_net(cf.copy())
    pd.testing.assert_series_equal(
        recomputed["net_cashflow_eur"], cf["net_cashflow_eur"],
    )
    factor = 1.1
    scaled = _scale_revenue(cf, factor, econ).set_index("project_year")
    base = cf.set_index("project_year")
    for y in range(1, N_YEARS + 1):
        # Gross support does NOT scale (fixed EUR/MW).
        assert float(scaled.loc[y, "state_support_eur"]) == float(
            base.loc[y, "state_support_eur"],
        ), y
        # The netting is recomputed from the SCALED market base against
        # the UN-scaled threshold.
        m_scaled = factor * float(base.loc[y, "bess_market_revenue_eur"])
        assert float(
            scaled.loc[y, "state_support_clawback_eur"]
        ) == pytest.approx(-(m_scaled - _theta_level()), abs=0.01), y
    # Factor 1.0 is a no-op on the pair.
    noop = _scale_revenue(cf, 1.0, econ)
    for col in ("state_support_eur", "state_support_clawback_eur"):
        assert np.allclose(
            noop[col].to_numpy(), cf[col].to_numpy(), atol=1e-9,
        ), col
    # Revenue-stabilising: at share = 100 % the netting fully absorbs
    # the market-revenue move — the (market base + netting) sum is
    # invariant under the driver, year for year.
    for y in range(1, N_YEARS + 1):
        stabilised_scaled = (
            float(scaled.loc[y, "bess_market_revenue_eur"])
            + float(scaled.loc[y, "state_support_clawback_eur"])
        )
        stabilised_base = (
            float(base.loc[y, "bess_market_revenue_eur"])
            + float(base.loc[y, "state_support_clawback_eur"])
        )
        assert stabilised_scaled == pytest.approx(
            stabilised_base, abs=0.01,
        ), y


# ---------------------------------------------------------------------------
# Validation and warnings
# ---------------------------------------------------------------------------


def _typed(**econ_overrides) -> dict:
    typed = {
        sheet: dict(defaults) for sheet, defaults in _SHEET_DEFAULTS.items()
    }
    typed["bess"]["bess_power_kw"] = BESS_KW
    typed["economics"].update(econ_overrides)
    return typed


def test_validation_and_toll_overlap_warning(caplog):
    with pytest.raises(ValueError, match="state_support_eur_per_mw_year"):
        validate_workbook_params(
            _typed(state_support_eur_per_mw_year=-1.0), dt_minutes=15,
        )
    with pytest.raises(
        ValueError, match="state_support_clawback_share_pct",
    ):
        validate_workbook_params(
            _typed(state_support_clawback_share_pct=150.0), dt_minutes=15,
        )
    with pytest.raises(ValueError, match="state_support_year_from"):
        validate_workbook_params(
            _typed(state_support_year_from=0), dt_minutes=15,
        )
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io"):
        validate_workbook_params(
            _typed(
                state_support_eur_per_mw_year=SIGMA,
                state_support_year_from=1,
                state_support_year_to=10,
                bess_toll_eur_per_mw_year=50_000.0,
                bess_toll_year_from=5,
                bess_toll_year_to=0,
            ),
            dt_minutes=15,
        )
    assert "cumulating two capacity payments" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io"):
        validate_workbook_params(
            _typed(
                state_support_eur_per_mw_year=SIGMA,
                state_support_year_from=1,
                state_support_year_to=4,
                bess_toll_eur_per_mw_year=50_000.0,
                bess_toll_year_from=5,
                bess_toll_year_to=0,
            ),
            dt_minutes=15,
        )
    assert "cumulating two capacity payments" not in caplog.text


# ---------------------------------------------------------------------------
# KPI, SUMMARY, theme, LCOE/LCOS invariance and IRR robustness
# ---------------------------------------------------------------------------


def _lifetime_yearly() -> pd.DataFrame:
    return pd.DataFrame({
        "project_year": range(1, N_YEARS + 1),
        "pv_generation_mwh": [1500.0 * 0.99 ** (y - 1)
                              for y in range(1, N_YEARS + 1)],
        "bess_discharge_mwh": [300.0 * 0.985 ** (y - 1)
                               for y in range(1, N_YEARS + 1)],
    })


def test_kpi_summary_theme_lcoe_and_irr_robustness():
    from pvbess_opt.io import _SUMMARY_OPTIONAL_FINANCIAL_KEYS
    from pvbess_opt.theme import (
        FINANCIAL_COLORS,
        FINANCIAL_LABELS,
        FINANCIAL_LEGEND_ORDER,
    )

    base_fin = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), _econ(), _caps()), _econ(),
        capacities=_caps(), lifetime_yearly=_lifetime_yearly(),
        year1_kpis=_kpis(),
    )
    cf = build_yearly_cashflow(_kpis(), _ss_econ(), _caps())
    # The sign-flipping repayment profile must not crash the IRR /
    # payback machinery (multiple sign changes are legitimate here).
    fin = compute_financial_kpis(
        cf, _ss_econ(), capacities=_caps(),
        lifetime_yearly=_lifetime_yearly(), year1_kpis=_kpis(),
    )
    op = cf.loc[cf["project_year"] >= 1]
    assert fin["total_state_support_eur_lifecycle"] == pytest.approx(
        float(op["state_support_eur"].sum()), abs=0.01,
    )
    assert fin[
        "total_state_support_clawback_eur_lifecycle"
    ] == pytest.approx(
        float(op["state_support_clawback_eur"].sum()), abs=0.01,
    )
    assert np.isfinite(fin["npv_eur"])
    assert fin["lcoe_eur_per_mwh"] == base_fin["lcoe_eur_per_mwh"]
    assert fin["lcos_eur_per_mwh"] == base_fin["lcos_eur_per_mwh"]
    assert ("total_state_support_eur_lifecycle",
            "Lifetime state support [EUR]") in (
        _SUMMARY_OPTIONAL_FINANCIAL_KEYS
    )
    assert ("total_state_support_clawback_eur_lifecycle",
            "Lifetime state-support netting [EUR]") in (
        _SUMMARY_OPTIONAL_FINANCIAL_KEYS
    )
    for label in ("State support", "State-support netting"):
        assert label in FINANCIAL_LABELS
        assert label in FINANCIAL_LEGEND_ORDER
    assert FINANCIAL_COLORS["state_support"] == "#FF8F00"
    assert FINANCIAL_COLORS["state_support_clawback"] == "#6A1B9A"
