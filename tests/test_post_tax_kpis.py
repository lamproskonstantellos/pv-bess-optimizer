"""Post-tax financial KPIs (Eq. E39).

The tax layer surfaces as headline metrics ALONGSIDE (never replacing)
the pre-tax baseline: `npv_post_tax_eur`, `irr_post_tax_pct`,
`equity_irr_post_tax_pct` (post-tax equity flows via the E20
schedule), the post-tax payback pair, lifetime tax/depreciation totals
and a rate echo.  All NaN while the rate is 0 (the all-equity
equity_irr precedent) so the SUMMARY renderer self-skips the rows.
Locked: NaN gating + SUMMARY registry, hand-checked value identities,
the irr ordering under positive lifetime tax, the rate->0 continuity
of the levered equity IRR, and the pre-tax-baseline guarantee.
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

N_YEARS = 8

POST_TAX_KPI_KEYS = (
    "npv_post_tax_eur", "irr_post_tax_pct", "equity_irr_post_tax_pct",
    "simple_payback_post_tax_years", "discounted_payback_post_tax_years",
    "total_corporate_tax_eur_lifecycle",
    "total_depreciation_eur_lifecycle",
)


def _econ(**o) -> dict:
    econ = {
        "project_lifecycle_years": N_YEARS,
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


def _kpis() -> dict:
    return {
        "profit_load_from_pv_eur": 60_000.0,
        "profit_load_from_bess_eur": 10_000.0,
        "profit_export_from_pv_eur": 40_000.0,
        "profit_export_from_bess_eur": 40_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 145_000.0,
    }


def test_nan_gating_at_zero_rate_and_summary_self_skip():
    from pvbess_opt.io import _SUMMARY_OPTIONAL_FINANCIAL_KEYS

    fin = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), _econ(), _caps()), _econ(),
    )
    for key in POST_TAX_KPI_KEYS:
        assert np.isnan(fin[key]), key
    assert fin["corporate_tax_rate_pct"] == 0.0
    registry = dict(_SUMMARY_OPTIONAL_FINANCIAL_KEYS)
    for key, label in (
        ("npv_post_tax_eur", "NPV post-tax [EUR]"),
        ("irr_post_tax_pct", "IRR post-tax [%]"),
        ("equity_irr_post_tax_pct", "Equity IRR post-tax [%]"),
        ("total_corporate_tax_eur_lifecycle",
         "Lifetime corporate tax [EUR]"),
    ):
        assert registry.get(key) == label, key
    # The renderer gate (abs(value) > 1e-9) is False for NaN, so the
    # rows self-skip at rate 0.
    assert not (abs(fin["npv_post_tax_eur"]) > 1e-9)


def test_post_tax_values_and_irr_ordering():
    econ = _tax_econ()
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    fin = compute_financial_kpis(cf, econ)
    assert fin["npv_post_tax_eur"] == pytest.approx(
        float(cf["discounted_cf_post_tax_eur"].sum()), abs=0.01,
    )
    expected_irr = calculate_irr(
        cf["net_cashflow_post_tax_eur"].to_numpy(dtype=float)
    ) * 100.0
    assert fin["irr_post_tax_pct"] == pytest.approx(
        expected_irr, abs=1e-3,
    )
    op = cf.loc[cf["project_year"] >= 1]
    total_tax = float(op["corporate_tax_eur"].sum())
    assert total_tax < 0.0
    assert fin["total_corporate_tax_eur_lifecycle"] == pytest.approx(
        total_tax, abs=0.01,
    )
    assert fin["total_depreciation_eur_lifecycle"] == pytest.approx(
        float(op["depreciation_eur"].sum()), abs=0.01,
    )
    # Taxes only remove cash: post-tax IRR / NPV sit below pre-tax.
    assert fin["irr_post_tax_pct"] <= fin["irr_pct"]
    assert fin["npv_post_tax_eur"] <= fin["npv_eur"]
    assert fin["simple_payback_post_tax_years"] >= fin[
        "simple_payback_years"
    ]
    assert fin["corporate_tax_rate_pct"] == 22.0
    # All-equity: the post-tax equity IRR stays NaN.
    assert np.isnan(fin["equity_irr_post_tax_pct"])


def test_levered_equity_irr_and_rate_continuity():
    lev = dict(gearing_pct=60.0, debt_interest_rate_pct=5.0,
               debt_tenor_years=5, debt_repayment="annuity")
    fin = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), _tax_econ(**lev), _caps()),
        _tax_econ(**lev),
    )
    assert np.isfinite(fin["equity_irr_post_tax_pct"])
    # min_dscr deliberately stays pre-tax: identical with tax on/off.
    fin_pre = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), _econ(**lev), _caps()),
        _econ(**lev),
    )
    assert fin["min_dscr"] == fin_pre["min_dscr"]
    # Continuity: as the rate -> 0 the post-tax equity IRR collapses to
    # the pre-tax equity IRR.
    tiny = compute_financial_kpis(
        build_yearly_cashflow(
            _kpis(), _tax_econ(corporate_tax_rate_pct=1e-9, **lev),
            _caps(),
        ),
        _tax_econ(corporate_tax_rate_pct=1e-9, **lev),
    )
    assert tiny["equity_irr_post_tax_pct"] == pytest.approx(
        fin_pre["equity_irr_pct"], abs=1e-3,
    )


def test_sculpted_post_tax_equity_irr_services_committed_schedule():
    """Under ``sculpted`` repayment the post-tax equity IRR must net the
    post-tax project cashflow against the SAME committed debt service the
    debt sheet and the tax-shield interest use (sculpted on PRE-tax
    CFADS) — debt service is a fixed contractual obligation, identical in
    both views of one run.  Re-sculpting it on post-tax CFADS silently
    re-times the service and biases the IRR."""
    from pvbess_opt.economics import (
        _amortization_schedule,
        _cfads_for_schedule,
        _financing_params,
        _resolved_debt_eur,
    )

    lev = dict(gearing_pct=60.0, debt_interest_rate_pct=6.0,
               debt_tenor_years=6, debt_repayment="sculpted")
    econ = _tax_econ(**lev)
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    fin = compute_financial_kpis(cf, econ)

    _g, rate, tenor, repayment = _financing_params(econ)
    net_cf = cf["net_cashflow_eur"].to_numpy(dtype=float)
    post_tax = cf["net_cashflow_post_tax_eur"].to_numpy(dtype=float)
    debt = _resolved_debt_eur(econ, -float(net_cf[0]))
    assert debt > 0.0

    def _equity_irr(schedule_cf: np.ndarray) -> float:
        sched = _amortization_schedule(
            debt, rate, tenor, repayment,
            cfads=_cfads_for_schedule(schedule_cf, tenor, repayment),
        )
        eq = post_tax.copy()
        eq[0] += debt
        for row in sched:
            y = int(row["year"])
            if y < eq.size:
                eq[y] -= float(row["debt_service_eur"])
        return calculate_irr(eq) * 100.0

    committed = _equity_irr(net_cf)      # correct: sculpt on pre-tax
    resculpted = _equity_irr(post_tax)   # the old, buggy behaviour
    # The two genuinely differ (depreciation + tax make post-tax CFADS
    # non-proportional to pre-tax), so this test discriminates.
    assert abs(committed - resculpted) > 1e-3
    # (reported KPI is rounded to 4 dp, so the tolerance sits well above
    # rounding but far below the ~0.8 pp bug gap above.)
    assert fin["equity_irr_post_tax_pct"] == pytest.approx(
        committed, abs=1e-3,
    )


def test_pre_tax_baseline_untouched():
    base = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), _econ(), _caps()), _econ(),
    )
    taxed = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), _tax_econ(), _caps()),
        _tax_econ(),
    )
    for key in ("npv_eur", "irr_pct", "roi_pct", "bcr",
                "simple_payback_years", "discounted_payback_years",
                "initial_investment_eur", "total_revenue_eur_lifecycle"):
        assert taxed[key] == base[key] or (
            isinstance(base[key], float) and np.isnan(base[key])
            and np.isnan(taxed[key])
        ), key


def test_perturbed_frames_without_post_tax_columns_stay_nan():
    """compute_financial_kpis on a sensitivity-perturbed frame (which
    drops the tax columns) reports NaN post-tax KPIs instead of
    raising — the pre-tax tornado path stays clean."""
    from pvbess_opt.sensitivity import _scale_revenue

    econ = _tax_econ()
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    perturbed = _scale_revenue(cf, 1.1, econ)
    fin = compute_financial_kpis(perturbed, econ)
    assert np.isnan(fin["npv_post_tax_eur"])
    assert np.isfinite(fin["npv_eur"])


def test_round_trip_types():
    """The new keys are plain floats (NaN included) so the results
    workbook scalar rows and JSON digests serialise them unchanged."""
    fin = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), _tax_econ(), _caps()),
        _tax_econ(),
    )
    for key in (*POST_TAX_KPI_KEYS, "corporate_tax_rate_pct"):
        assert isinstance(fin[key], float), key
    frame = pd.DataFrame([{k: fin[k] for k in POST_TAX_KPI_KEYS}])
    assert frame.shape == (1, len(POST_TAX_KPI_KEYS))
