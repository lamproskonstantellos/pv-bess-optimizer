"""Capacity-market payment with derating factor (Eq. E32).

The simplest contracted structure: a payment on the DERATED power
block over a contract window — availability-scaled, contractually
indexed, no capacity-fade scaling (the derating factor is the user's
duration-eligibility lever, held constant over the contract; the
payment is on derated MW by stated convention).  The revenue counts as
realised market revenue for the state-support netting (Eq. E31a),
computed BEFORE the clawback in the year loop (order locked here).
Locked: zero-default bit-identity, the worked-example cent lock, the
E31a coupling, flat-1/12 monthly reconciliation, the no-scale Revenue
sensitivity rule, validation + stacking warnings and registries.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import (
    build_yearly_cashflow,
    compute_financial_kpis,
    derive_monthly_cashflow,
)
from pvbess_opt.io import _SHEET_DEFAULTS, validate_workbook_params

N_YEARS = 5


def _econ(**o) -> dict:
    econ = {
        "project_lifecycle_years": N_YEARS,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "unavailability_pct": 1.0,
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
        "bess_power_kw": 1000.0,
    }
    econ.update(o)
    return econ


def _cm_econ(**o) -> dict:
    kw = {
        "capacity_market_eur_per_mw_year": 50_000.0,
        "capacity_market_derating_pct": 40.0,
        "capacity_market_year_from": 1,
        "capacity_market_year_to": 0,
        "capacity_market_indexation_pct": 0.0,
    }
    kw.update(o)
    return _econ(**kw)


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 1000.0, "bess_kwh": 2000.0}


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


def test_zero_default_bit_identity():
    base = build_yearly_cashflow(_kpis(), _econ(), _caps())
    with_keys = build_yearly_cashflow(
        _kpis(),
        _econ(
            capacity_market_eur_per_mw_year=0.0,
            capacity_market_derating_pct=100.0,
            capacity_market_year_from=1,
            capacity_market_year_to=0,
            capacity_market_indexation_pct=0.0,
        ),
        _caps(),
    )
    pd.testing.assert_frame_equal(base, with_keys)
    assert (base["capacity_market_revenue_eur"] == 0.0).all()


def test_worked_example_cent_lock():
    """kappa=50k EUR/MW/yr, delta=40 %, 1 MW, A=0.99 => 19,800 EUR."""
    cf = build_yearly_cashflow(_kpis(), _cm_econ(), _caps()).set_index(
        "project_year",
    )
    assert float(cf.loc[0, "capacity_market_revenue_eur"]) == 0.0
    assert float(
        cf.loc[1, "capacity_market_revenue_eur"]
    ) == pytest.approx(19_800.0, abs=0.01)
    # No capacity-fade scaling: every in-window year pays the same
    # (flat indexation here), and indexation rides E2.
    assert float(cf.loc[N_YEARS, "capacity_market_revenue_eur"]) == (
        pytest.approx(19_800.0, abs=0.01)
    )
    indexed = build_yearly_cashflow(
        _kpis(), _cm_econ(capacity_market_indexation_pct=2.0), _caps(),
    ).set_index("project_year")
    assert float(
        indexed.loc[3, "capacity_market_revenue_eur"]
    ) == pytest.approx(19_800.0 * 1.02 ** 2, abs=0.01)
    windowed = build_yearly_cashflow(
        _kpis(),
        _cm_econ(capacity_market_year_from=2, capacity_market_year_to=3),
        _caps(),
    ).set_index("project_year")
    active = {
        y: abs(float(windowed.loc[y, "capacity_market_revenue_eur"])) > 1e-9
        for y in range(1, N_YEARS + 1)
    }
    assert active == {y: 2 <= y <= 3 for y in range(1, N_YEARS + 1)}


def test_e31a_coupling_capacity_joins_netting_base():
    """With the support active, the clawback moves by exactly c x R_cm."""
    ss = {
        "state_support_eur_per_mw_year": 40_000.0,
        "state_support_clawback_threshold_eur_per_mw_year": 60_000.0,
        "state_support_clawback_share_pct": 50.0,
    }
    without_cm = build_yearly_cashflow(
        _kpis(), _econ(**ss), _caps(),
    ).set_index("project_year")
    with_cm = build_yearly_cashflow(
        _kpis(), _cm_econ(**ss), _caps(),
    ).set_index("project_year")
    for y in range(1, N_YEARS + 1):
        r_cm = float(with_cm.loc[y, "capacity_market_revenue_eur"])
        assert r_cm > 0.0, y
        assert float(
            with_cm.loc[y, "state_support_clawback_eur"]
        ) == pytest.approx(
            float(without_cm.loc[y, "state_support_clawback_eur"])
            - 0.50 * r_cm,
            abs=0.01,
        ), y
    # ...while the E25a base itself stays capacity-free (the capacity
    # payment is not wholesale trading margin).
    pd.testing.assert_series_equal(
        with_cm["bess_market_revenue_eur"],
        without_cm["bess_market_revenue_eur"],
    )


def test_monthly_flat_twelfth_and_reconciliation():
    from tests.test_monthly_cashflow_reconciliation import _make_res_frame

    res = _make_res_frame()
    econ = _cm_econ()
    yearly = build_yearly_cashflow(_kpis(), econ, _caps())
    monthly, _q = derive_monthly_cashflow(res, yearly, econ)
    yearly_indexed = yearly.set_index("project_year")
    for y in range(1, N_YEARS + 1):
        sub = monthly.loc[monthly["project_year"] == y]
        cm_y = float(
            yearly_indexed.loc[y, "capacity_market_revenue_eur"]
        )
        assert np.allclose(
            sub["capacity_market_revenue_eur"].to_numpy(), cm_y / 12.0,
        ), y
        assert float(sub["net_cashflow_eur"].sum()) == pytest.approx(
            float(yearly_indexed.loc[y, "net_cashflow_eur"]), abs=1e-6,
        ), y


def test_sensitivity_no_revenue_scaling():
    from pvbess_opt.sensitivity import _recompute_net, _scale_revenue

    econ = _cm_econ(
        state_support_eur_per_mw_year=40_000.0,
        state_support_clawback_threshold_eur_per_mw_year=60_000.0,
        state_support_clawback_share_pct=50.0,
    )
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    recomputed = _recompute_net(cf.copy())
    pd.testing.assert_series_equal(
        recomputed["net_cashflow_eur"], cf["net_cashflow_eur"],
    )
    factor = 1.2
    scaled = _scale_revenue(cf, factor, econ).set_index("project_year")
    base = cf.set_index("project_year")
    for y in range(1, N_YEARS + 1):
        # Administered capacity price: NOT scaled by the driver...
        assert float(
            scaled.loc[y, "capacity_market_revenue_eur"]
        ) == float(base.loc[y, "capacity_market_revenue_eur"]), y
        # ...but it joins the E31a netting base at its UN-scaled value:
        # the clawback delta is driven by the scaled E25a base only.
        m_scaled = factor * float(base.loc[y, "bess_market_revenue_eur"])
        r_cm = float(base.loc[y, "capacity_market_revenue_eur"])
        theta = 60_000.0 * 1.0  # 1 MW, threshold not availability-scaled
        assert float(
            scaled.loc[y, "state_support_clawback_eur"]
        ) == pytest.approx(
            -0.50 * (m_scaled + r_cm - theta), abs=0.01,
        ), y


def _typed(**econ_overrides) -> dict:
    typed = {
        sheet: dict(defaults) for sheet, defaults in _SHEET_DEFAULTS.items()
    }
    typed["bess"]["bess_power_kw"] = 1000.0
    typed["economics"].update(econ_overrides)
    return typed


def test_validation_and_stacking_warnings(caplog):
    with pytest.raises(ValueError, match="capacity_market_derating_pct"):
        validate_workbook_params(
            _typed(capacity_market_derating_pct=120.0), dt_minutes=15,
        )
    with pytest.raises(
        ValueError, match="capacity_market_eur_per_mw_year",
    ):
        validate_workbook_params(
            _typed(capacity_market_eur_per_mw_year=-1.0), dt_minutes=15,
        )
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io"):
        validate_workbook_params(
            _typed(
                capacity_market_eur_per_mw_year=50_000.0,
                state_support_eur_per_mw_year=40_000.0,
                bess_toll_eur_per_mw_year=50_000.0,
            ),
            dt_minutes=15,
        )
    assert "support-cumulation" in caplog.text
    assert "capacity obligation" in caplog.text


def _lifetime_yearly() -> pd.DataFrame:
    return pd.DataFrame({
        "project_year": range(1, N_YEARS + 1),
        "pv_generation_mwh": [1500.0] * N_YEARS,
        "bess_discharge_mwh": [300.0] * N_YEARS,
    })


def test_kpi_summary_theme_and_lcoe_invariance():
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
    cf = build_yearly_cashflow(_kpis(), _cm_econ(), _caps())
    fin = compute_financial_kpis(
        cf, _cm_econ(), capacities=_caps(),
        lifetime_yearly=_lifetime_yearly(), year1_kpis=_kpis(),
    )
    assert fin[
        "total_capacity_market_revenue_eur_lifecycle"
    ] == pytest.approx(19_800.0 * N_YEARS, abs=0.01)
    assert base_fin["total_capacity_market_revenue_eur_lifecycle"] == 0.0
    assert fin["lcos_eur_per_mwh"] == base_fin["lcos_eur_per_mwh"]
    assert ("total_capacity_market_revenue_eur_lifecycle",
            "Lifetime capacity-market revenue [EUR]") in (
        _SUMMARY_OPTIONAL_FINANCIAL_KEYS
    )
    assert "Capacity-market revenue" in FINANCIAL_LABELS
    assert "Capacity-market revenue" in FINANCIAL_LEGEND_ORDER
    assert FINANCIAL_COLORS["capacity_market_revenue"] == "#D84315"
