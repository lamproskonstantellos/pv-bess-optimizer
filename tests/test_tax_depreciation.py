"""Depreciation schedules + corporate tax engine (Eqs. E34-E38).

``economics.apply_tax_layer`` is a pure post-processing layer over the
pre-tax yearly cashflow: per-asset straight-line depreciation (PV,
BESS incl. the replacement tranche in service the year AFTER its
month-12 booking, site lump sums), taxable income = EBITDA -
depreciation - debt interest (the E20 schedule), FIFO loss
carry-forward (unlimited by default, optional expiry window) and
corporate tax at the configured rate — TAX_y <= 0 always (losses only
carry forward, never rebate).  Pre-tax columns are NEVER touched.
Locked: zero-default bit-identity (rate 0 => exact-zero tax columns
and a value-identical post-tax family), the per-asset schedule with
horizon truncation and N=0 no-claim, the replacement tranche start,
the carry-forward worked example with FIFO expiry, the interest
deduction, an independent levered reference case, month-12 monthly
booking + reconciliation, the sensitivity stale-column drop and
LCOE/LCOS invariance.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import (
    TAX_LAYER_COLUMNS,
    build_yearly_cashflow,
    compute_financial_kpis,
    derive_monthly_cashflow,
)

N_YEARS = 8


def _econ(**o) -> dict:
    econ = {
        "project_lifecycle_years": N_YEARS,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 500.0,
        "capex_bess_eur_per_kwh": 100.0,
        "devex_pv_eur_per_kw": 20.0,
        "devex_bess_eur_per_kw": 10.0,
        "site_capex_eur": 40_000.0,
        "site_devex_eur": 8_000.0,
        "opex_pv_eur_per_kwp": 5.0,
        "opex_bess_eur_per_kw": 5.0,
        "pv_degradation_year1_pct": 2.0,
        "pv_degradation_annual_pct": 1.0,
        "bess_degradation_annual_pct": 1.5,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
    }
    econ.update(o)
    return econ


def _tax_econ(**o) -> dict:
    kw = {
        "corporate_tax_rate_pct": 22.0,
        "depreciation_years_pv": 4,
        "depreciation_years_bess": 2,
        "depreciation_years_site": 8,
        "tax_loss_carryforward_years": 0,
    }
    kw.update(o)
    return _econ(**kw)


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}


def _kpis(**o) -> dict:
    base = {
        "profit_load_from_pv_eur": 50_000.0,
        "profit_load_from_bess_eur": 10_000.0,
        "profit_export_from_pv_eur": 30_000.0,
        "profit_export_from_bess_eur": 40_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 125_000.0,
    }
    base.update(o)
    return base


# Per-asset bases for the fixture above.
PV_BASE = (500.0 + 20.0) * 1000.0          # 520,000
BESS_BASE = 100.0 * 1000.0 + 10.0 * 500.0  # 105,000
SITE_BASE = 40_000.0 + 8_000.0             # 48,000


# ---------------------------------------------------------------------------
# Zero-default bit-identity
# ---------------------------------------------------------------------------


def test_zero_rate_bit_identity_and_pass_through():
    no_keys = build_yearly_cashflow(_kpis(), _econ(), _caps())
    at_defaults = build_yearly_cashflow(
        _kpis(),
        _econ(
            corporate_tax_rate_pct=0.0,
            depreciation_years_pv=20,
            depreciation_years_bess=10,
            depreciation_years_site=20,
            tax_loss_carryforward_years=0,
        ),
        _caps(),
    )
    pd.testing.assert_frame_equal(no_keys, at_defaults)
    for col in ("depreciation_eur", "debt_interest_eur",
                "taxable_income_eur", "tax_loss_carryforward_eur",
                "corporate_tax_eur"):
        assert (no_keys[col] == 0.0).all(), col
    # The post-tax family passes through value-identical to pre-tax.
    for post, pre in (
        ("net_cashflow_post_tax_eur", "net_cashflow_eur"),
        ("discounted_cf_post_tax_eur", "discounted_cf_eur"),
        ("cumulative_cf_post_tax_eur", "cumulative_cf_eur"),
        ("cumulative_dcf_post_tax_eur", "cumulative_dcf_eur"),
    ):
        pd.testing.assert_series_equal(
            no_keys[post], no_keys[pre], check_names=False,
        )


# ---------------------------------------------------------------------------
# E34 — straight-line schedule
# ---------------------------------------------------------------------------


def test_straight_line_schedule_per_asset():
    cf = build_yearly_cashflow(_kpis(), _tax_econ(), _caps()).set_index(
        "project_year",
    )
    assert float(cf.loc[0, "depreciation_eur"]) == 0.0
    for y in range(1, N_YEARS + 1):
        expected = SITE_BASE / 8.0  # site: full horizon
        if y <= 4:
            expected += PV_BASE / 4.0
        if y <= 2:
            expected += BESS_BASE / 2.0
        assert float(cf.loc[y, "depreciation_eur"]) == pytest.approx(
            expected, abs=0.01,
        ), y
    # Total claimed equals the sum of the bases (no truncation here).
    assert float(cf["depreciation_eur"].sum()) == pytest.approx(
        PV_BASE + BESS_BASE + SITE_BASE, abs=0.01,
    )


def test_horizon_truncation_and_zero_life_no_claim():
    truncated = build_yearly_cashflow(
        _kpis(), _tax_econ(depreciation_years_pv=20), _caps(),
    ).set_index("project_year")
    # 20-year PV life over an 8-year horizon: 8/20 of the base claimed,
    # the rest lost (no terminal write-off).
    pv_claimed = sum(
        PV_BASE / 20.0 for _ in range(1, N_YEARS + 1)
    )
    assert float(truncated["depreciation_eur"].sum()) == pytest.approx(
        pv_claimed + BESS_BASE + SITE_BASE, abs=0.01,
    )
    no_claim = build_yearly_cashflow(
        _kpis(), _tax_econ(depreciation_years_pv=0), _caps(),
    ).set_index("project_year")
    assert float(no_claim["depreciation_eur"].sum()) == pytest.approx(
        BESS_BASE + SITE_BASE, abs=0.01,
    )


def test_replacement_tranche_starts_year_after_booking():
    cf = build_yearly_cashflow(
        _kpis(),
        _tax_econ(bess_replacement_year=3, bess_replacement_cost_pct=50.0),
        _caps(),
    ).set_index("project_year")
    repl_base = 100.0 * 1000.0 * 0.50  # 50,000 over 2 years from year 4
    for y in (4, 5):
        expected = SITE_BASE / 8.0 + repl_base / 2.0
        if y <= 4:
            expected += PV_BASE / 4.0
        assert float(cf.loc[y, "depreciation_eur"]) == pytest.approx(
            expected, abs=0.01,
        ), y
    assert float(cf.loc[3, "depreciation_eur"]) == pytest.approx(
        SITE_BASE / 8.0 + PV_BASE / 4.0, abs=0.01,
    )


# ---------------------------------------------------------------------------
# E35-E37 — EBITDA, carry-forward and the tax clamp
# ---------------------------------------------------------------------------


def test_ebitda_excludes_investment_events_and_tax_never_positive():
    econ = _tax_econ(
        bess_replacement_year=3, bess_replacement_cost_pct=50.0,
        trajectories={
            "revenue_dam": {
                "mode": "replace",
                "values": [1.0, -2.0, 1.5, 1.0, 1.0, 1.0, 1.0, 1.0],
            },
        },
    )
    cf = build_yearly_cashflow(_kpis(), econ, _caps()).set_index(
        "project_year",
    )
    # EBITDA = net - capex - devex: the replacement CAPEX in year 3
    # does not depress taxable income (only its depreciation does).
    y = 3
    ebitda_y = (
        float(cf.loc[y, "net_cashflow_eur"])
        - float(cf.loc[y, "capex_eur"])
        - float(cf.loc[y, "devex_eur"])
    )
    assert float(cf.loc[y, "taxable_income_eur"]) == pytest.approx(
        ebitda_y - float(cf.loc[y, "depreciation_eur"]), abs=0.01,
    )
    assert (cf["corporate_tax_eur"] <= 0.0).all()


def test_carry_forward_fifo_and_expiry_window():
    """Hand-computed vintages: losses accumulate, profits absorb FIFO,
    and a positive window expires aged vintages."""
    tau = 0.22
    # Unlimited window first.
    econ = _tax_econ(
        depreciation_years_pv=2, depreciation_years_bess=2,
        depreciation_years_site=2,
    )
    cf = build_yearly_cashflow(_kpis(), econ, _caps()).set_index(
        "project_year",
    )
    # Years 1-2 carry (PV+BESS+site)/2 depreciation each =
    # 336,500 EUR/yr against ~125k EBITDA => losses; from year 3 no
    # depreciation, profits absorb the carried losses FIFO.
    l_prev = 0.0
    for y in range(1, N_YEARS + 1):
        ti = float(cf.loc[y, "taxable_income_eur"])
        tb_expected = max(0.0, ti - l_prev)
        l_prev = max(0.0, l_prev - max(ti, 0.0)) + max(0.0, -ti)
        assert float(cf.loc[y, "corporate_tax_eur"]) == pytest.approx(
            -tau * tb_expected, abs=0.01,
        ), y
        assert float(
            cf.loc[y, "tax_loss_carryforward_eur"]
        ) == pytest.approx(l_prev, abs=0.01), y
    # The loss pool is eventually consumed and tax turns on.
    assert float(cf.loc[N_YEARS, "corporate_tax_eur"]) < 0.0
    # With a 1-year expiry window the year-1/2 losses die before the
    # profit years can absorb them: strictly more tax is due.
    expiring = build_yearly_cashflow(
        _kpis(),
        _tax_econ(
            depreciation_years_pv=2, depreciation_years_bess=2,
            depreciation_years_site=2, tax_loss_carryforward_years=1,
        ),
        _caps(),
    ).set_index("project_year")
    assert float(expiring["corporate_tax_eur"].sum()) < float(
        cf["corporate_tax_eur"].sum()
    )
    assert float(expiring.loc[N_YEARS, "tax_loss_carryforward_eur"]) == 0.0


# ---------------------------------------------------------------------------
# E36 — interest deduction
# ---------------------------------------------------------------------------


def test_interest_deduction_matches_amortization_schedule():
    from pvbess_opt.economics import _amortization_schedule

    econ = _tax_econ(
        gearing_pct=60.0, debt_interest_rate_pct=5.0,
        debt_tenor_years=4, debt_repayment="annuity",
    )
    cf = build_yearly_cashflow(_kpis(), econ, _caps()).set_index(
        "project_year",
    )
    debt = 0.60 * (-float(cf.loc[0, "net_cashflow_eur"]))
    schedule = {
        int(r["year"]): float(r["interest_eur"])
        for r in _amortization_schedule(debt, 0.05, 4, "annuity")
    }
    for y in range(1, N_YEARS + 1):
        assert float(cf.loc[y, "debt_interest_eur"]) == pytest.approx(
            schedule.get(y, 0.0), abs=0.01,
        ), y
    # All-equity: zero interest and TI = EBITDA - DEP.
    unlevered = build_yearly_cashflow(
        _kpis(), _tax_econ(), _caps(),
    ).set_index("project_year")
    assert (unlevered["debt_interest_eur"] == 0.0).all()
    y = 5
    ebitda_y = (
        float(unlevered.loc[y, "net_cashflow_eur"])
        - float(unlevered.loc[y, "capex_eur"])
        - float(unlevered.loc[y, "devex_eur"])
    )
    assert float(
        unlevered.loc[y, "taxable_income_eur"]
    ) == pytest.approx(
        ebitda_y - float(unlevered.loc[y, "depreciation_eur"]), abs=0.01,
    )


# ---------------------------------------------------------------------------
# Independent levered reference case
# ---------------------------------------------------------------------------


def test_levered_reference_case_to_the_cent():
    """3-year fixture computed independently below (own amortization
    arithmetic, not the module helpers)."""
    econ = _econ(
        project_lifecycle_years=3,
        corporate_tax_rate_pct=22.0,
        depreciation_years_pv=3,
        depreciation_years_bess=3,
        depreciation_years_site=3,
        tax_loss_carryforward_years=0,
        gearing_pct=60.0, debt_interest_rate_pct=5.0,
        debt_tenor_years=3, debt_repayment="linear",
        bess_degradation_annual_pct=0.0,
        pv_degradation_year1_pct=0.0, pv_degradation_annual_pct=0.0,
        opex_pv_eur_per_kwp=0.0, opex_bess_eur_per_kw=0.0,
    )
    cf = build_yearly_cashflow(_kpis(), econ, _caps()).set_index(
        "project_year",
    )
    # Independent arithmetic: Year-0 outlay and linear debt.
    outlay = (
        500.0 * 1000.0 + 100.0 * 1000.0 + 40_000.0        # capex
        + 20.0 * 1000.0 + 10.0 * 500.0 + 8_000.0          # devex
    )
    debt = 0.60 * outlay
    dep = (PV_BASE + BESS_BASE + SITE_BASE) / 3.0
    ebitda = 125_000.0  # flat: no degradation, no OPEX, no inflation
    balance = debt
    for y in (1, 2, 3):
        interest = balance * 0.05
        balance -= debt / 3.0
        ti = ebitda - dep - interest
        expected_tax = -0.22 * max(ti, 0.0)
        assert float(cf.loc[y, "corporate_tax_eur"]) == pytest.approx(
            expected_tax, abs=0.01,
        ), y
        assert float(
            cf.loc[y, "net_cashflow_post_tax_eur"]
        ) == pytest.approx(
            float(cf.loc[y, "net_cashflow_eur"]) + expected_tax, abs=0.01,
        ), y


# ---------------------------------------------------------------------------
# Monthly — month-12 booking and reconciliation
# ---------------------------------------------------------------------------


def test_monthly_month12_booking_and_post_tax_reconciliation():
    from tests.test_monthly_cashflow_reconciliation import _make_res_frame

    res = _make_res_frame()
    econ = _tax_econ(
        depreciation_years_pv=2, depreciation_years_bess=2,
        depreciation_years_site=2,
        bess_replacement_year=3, bess_replacement_cost_pct=50.0,
    )
    yearly = build_yearly_cashflow(_kpis(), econ, _caps())
    monthly, _q = derive_monthly_cashflow(res, yearly, econ)
    yearly_indexed = yearly.set_index("project_year")
    disc = 1.0 + 7.0 / 100.0
    for y in range(1, N_YEARS + 1):
        sub = monthly.loc[monthly["project_year"] == y]
        tax_y = float(yearly_indexed.loc[y, "corporate_tax_eur"])
        by_month = sub.set_index("period")["corporate_tax_eur"]
        assert float(by_month.loc[12]) == pytest.approx(tax_y, abs=1e-9), y
        assert (by_month.loc[1:11] == 0.0).all(), y
        assert float(
            sub["net_cashflow_post_tax_eur"].sum()
        ) == pytest.approx(
            float(yearly_indexed.loc[y, "net_cashflow_post_tax_eur"]),
            abs=1e-6,
        ), y
        # December's post-tax discount factor equals the yearly one.
        dec = sub.loc[sub["period"] == 12].iloc[0]
        if abs(dec["net_cashflow_post_tax_eur"]) > 1e-9:
            implied = (
                float(dec["discounted_cf_post_tax_eur"])
                / float(dec["net_cashflow_post_tax_eur"])
            )
            assert implied == pytest.approx(disc ** (-y), rel=1e-12), y


# ---------------------------------------------------------------------------
# Sensitivity hygiene and LCOE/LCOS invariance
# ---------------------------------------------------------------------------


def test_sensitivity_drops_tax_columns_and_tornado_is_pre_tax():
    from pvbess_opt.sensitivity import (
        _scale_capex,
        _scale_opex,
        _scale_revenue,
        run_sensitivity_analysis,
    )

    econ = _tax_econ()
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    for scaled in (
        _scale_capex(cf, 1.1), _scale_opex(cf, 1.1),
        _scale_revenue(cf, 1.1, econ),
    ):
        assert not set(TAX_LAYER_COLUMNS) & set(scaled.columns)
    # The pre-tax tornado is unchanged with the tax layer on vs off.
    base_kpis = {"npv_eur": 1.0, "irr_pct": 1.0,
                 "simple_payback_years": 5.0}
    with_tax = run_sensitivity_analysis(
        _kpis(), econ, _caps(), base_kpis,
    )
    without_tax = run_sensitivity_analysis(
        _kpis(), _econ(), _caps(), base_kpis,
    )
    pd.testing.assert_frame_equal(with_tax, without_tax)


def _lifetime_yearly() -> pd.DataFrame:
    return pd.DataFrame({
        "project_year": range(1, N_YEARS + 1),
        "pv_generation_mwh": [1500.0] * N_YEARS,
        "bess_discharge_mwh": [300.0] * N_YEARS,
    })


def test_lcoe_lcos_invariance_and_pre_tax_kpis_untouched():
    base_fin = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), _econ(), _caps()), _econ(),
        capacities=_caps(), lifetime_yearly=_lifetime_yearly(),
        year1_kpis=_kpis(),
    )
    fin = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), _tax_econ(), _caps()),
        _tax_econ(), capacities=_caps(),
        lifetime_yearly=_lifetime_yearly(), year1_kpis=_kpis(),
    )
    assert fin["lcoe_eur_per_mwh"] == base_fin["lcoe_eur_per_mwh"]
    assert fin["lcos_eur_per_mwh"] == base_fin["lcos_eur_per_mwh"]
    # The published pre-tax baseline is untouched by any tax setting.
    for key in ("npv_eur", "irr_pct", "roi_pct",
                "simple_payback_years", "total_revenue_eur_lifecycle"):
        assert fin[key] == base_fin[key] or (
            np.isnan(fin[key]) and np.isnan(base_fin[key])
        ), key


def test_apply_tax_layer_is_deterministic():
    """Pure function: same frame in, same columns out."""
    from pvbess_opt.economics import apply_tax_layer

    econ = _tax_econ()
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    again = apply_tax_layer(
        cf.drop(columns=list(TAX_LAYER_COLUMNS)), econ, _caps(),
    )
    pd.testing.assert_frame_equal(again, cf)
