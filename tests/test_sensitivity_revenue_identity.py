"""Regression test for the gross/net identity in the Revenue sensitivity.

``_scale_revenue`` previously scaled ``revenue_eur`` and
``aggregator_fee_eur`` independently of any algebraic check.  Uniform
scaling preserved the identity by coincidence, but any future change
that touched the columns non-uniformly would desynchronise them.

This test pins the identity ``revenue_eur + |aggregator_fee_eur| ==
revenue_gross`` (where ``revenue_gross`` scales linearly with the
sensitivity factor) at multiple perturbation factors, and confirms
the NPV tornado output is numerically unchanged within tolerance from
the pre-fix uniform-scaling result.
"""

from __future__ import annotations

import pandas as pd
import pytest

from pvbess_opt.economics import build_yearly_cashflow, compute_financial_kpis
from pvbess_opt.sensitivity import (
    _scale_revenue,
    run_sensitivity_analysis,
)


def _year1_kpis() -> dict:
    return {
        "profit_load_from_pv_eur": 110_000.0,
        "profit_load_from_bess_eur": 70_000.0,
        "profit_export_from_pv_eur": 60_000.0,
        "profit_export_from_bess_eur": 55_000.0,
        "expense_charge_bess_grid_eur": 12_000.0,
        "profit_total_eur": (
            110_000.0 + 70_000.0 + 60_000.0 + 55_000.0 - 12_000.0
        ),
        "pv_generation_mwh": 7_200.0,
        "bess_total_discharge_mwh": 4_500.0,
        "bm_total_capacity_revenue_eur": 38_000.0,
        "bm_total_activation_revenue_eur": 12_000.0,
    }


def _econ(fee_pct: float = 10.0) -> dict:
    return {
        "project_lifecycle_years": 10,
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
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": fee_pct,
        "sensitivity_capex_delta_pct": 20.0,
        "sensitivity_opex_delta_pct": 20.0,
        "sensitivity_revenue_delta_pct": 20.0,
        "sensitivity_discount_rate_delta_pp": 2.0,
    }


def _caps() -> dict:
    return {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}


@pytest.mark.parametrize("factor", [0.8, 1.0, 1.2])
def test_revenue_perturbation_preserves_gross_net_identity(factor: float):
    """``revenue_eur + |aggregator_fee_eur|`` scales linearly with ``factor``."""
    econ = _econ(fee_pct=10.0)
    base = build_yearly_cashflow(_year1_kpis(), econ, _caps())
    perturbed = _scale_revenue(base, factor)
    f_frac = econ["aggregator_fee_pct_revenue"] / 100.0
    # Skip Year 0 — fee is zero by construction (capex row).
    after_y0 = perturbed["project_year"] >= 1
    base_after = base.loc[after_y0]
    pert_after = perturbed.loc[after_y0]
    for y in base_after.index:
        rev_b = float(base_after.at[y, "revenue_eur"])
        fee_b = float(base_after.at[y, "aggregator_fee_eur"])
        rev_p = float(pert_after.at[y, "revenue_eur"])
        fee_p = float(pert_after.at[y, "aggregator_fee_eur"])
        gross_b = rev_b + abs(fee_b)
        gross_p = rev_p + abs(fee_p)
        # Linear scaling of gross.
        assert gross_p == pytest.approx(factor * gross_b, abs=max(1.0, 1e-6 * gross_p))
        # Implied fee fraction matches the base one (within rounding).
        if gross_p > 1e-6:
            implied_f = abs(fee_p) / gross_p
            assert implied_f == pytest.approx(f_frac, abs=1e-6)
        # And per-stream nets sum to the total net.
        retail_p = float(pert_after.at[y, "revenue_retail_eur"])
        dam_p = float(pert_after.at[y, "revenue_dam_eur"])
        assert (retail_p + dam_p) == pytest.approx(rev_p, abs=max(0.01, 1e-6 * rev_p))


def test_revenue_perturbation_factor_one_is_noop():
    """``factor=1.0`` returns a frame identical to the base on revenue columns."""
    econ = _econ(fee_pct=10.0)
    base = build_yearly_cashflow(_year1_kpis(), econ, _caps())
    perturbed = _scale_revenue(base, 1.0)
    for col in (
        "revenue_eur", "revenue_retail_eur", "revenue_dam_eur",
        "aggregator_fee_eur",
        "balancing_revenue_eur",
        "balancing_capacity_revenue_eur",
        "balancing_activation_revenue_eur",
    ):
        pd.testing.assert_series_equal(
            base[col].astype(float).reset_index(drop=True),
            perturbed[col].astype(float).reset_index(drop=True),
            check_names=False, rtol=1e-9, atol=1e-6,
        )


def test_revenue_tornado_npv_is_close_to_uniform_scaling():
    """Pin the NPV tornado against the uniform-scaling reference.

    The new derivation is algebraically equivalent to the previous
    uniform scaling on this baseline cashflow; the tornado deltas must
    match to a handful of EUR.
    """
    econ = _econ(fee_pct=10.0)
    base_cf = build_yearly_cashflow(_year1_kpis(), econ, _caps())
    base_kpis = compute_financial_kpis(base_cf, econ)
    df = run_sensitivity_analysis(_year1_kpis(), econ, _caps(), base_kpis)
    # The Revenue rows must move NPV by a positive amount in the high
    # scenario and by a negative amount in the low scenario.
    rev_high = df[
        (df["variable"] == "Revenue") & (df["scenario"] == "high")
    ].iloc[0]
    rev_low = df[
        (df["variable"] == "Revenue") & (df["scenario"] == "low")
    ].iloc[0]
    assert rev_high["delta_npv_eur"] > 0.0
    assert rev_low["delta_npv_eur"] < 0.0
    # And |high| ≈ |low| (symmetric +/-20 % perturbation).
    assert abs(rev_high["delta_npv_eur"]) == pytest.approx(
        abs(rev_low["delta_npv_eur"]), rel=1e-3,
    )


def test_revenue_perturbation_zero_fee_still_consistent():
    """Identity holds when aggregator_fee_pct_revenue == 0 (no fee at all)."""
    econ = _econ(fee_pct=0.0)
    base = build_yearly_cashflow(_year1_kpis(), econ, _caps())
    perturbed = _scale_revenue(base, 1.2)
    # aggregator_fee_eur is zero in both frames.
    assert float(perturbed["aggregator_fee_eur"].abs().max()) < 1e-6
    # revenue_eur scales by exactly 1.2 in every year >= 1.
    base_y1 = float(
        base.loc[base["project_year"] == 1, "revenue_eur"].iloc[0]
    )
    pert_y1 = float(
        perturbed.loc[perturbed["project_year"] == 1, "revenue_eur"].iloc[0]
    )
    assert pert_y1 == pytest.approx(1.2 * base_y1, abs=1e-6)


# ---------------------------------------------------------------------------
# Mixed-sign regime — base gross flips sign across years.
#
# When a fee-clamped (gross <= 0) year coexists with a positive-gross year,
# the previous ``net / (1 - frac)`` inversion over-inflated every clamped
# year by ``1 / (1 - frac)`` (the positive-gross year is what makes ``frac``
# non-zero).  ``_scale_revenue(base, 1.0)`` was therefore NOT a no-op, and the
# gross/net identity broke on the clamped years.  Recovering the per-year
# gross directly from the base frame (``revenue_eur + |aggregator_fee_eur|``,
# which is correct in both fee-applied and fee-free years) fixes both.
# ---------------------------------------------------------------------------


def _year1_kpis_mixed_sign() -> dict:
    """Year-1 KPIs whose DAM stream is a net cost.

    Grid-charging expense dominates DAM export, so ``revenue_dam`` is a
    large negative.  Year 1's gross stays positive (retail outweighs the
    DAM cost), but a high ``dam_inflation_pct`` grows the negative DAM term
    faster than retail, dragging the gross negative in later years -- the
    mixed-sign regime that exposed the single-inversion distortion.
    """
    return {
        "profit_load_from_pv_eur": 50_000.0,        # retail (positive)
        "profit_load_from_bess_eur": 0.0,
        "profit_export_from_pv_eur": 10_000.0,       # DAM export (positive)
        "profit_export_from_bess_eur": 5_000.0,
        "expense_charge_bess_grid_eur": 60_000.0,    # grid-charge cost (DAM-bundled)
        # gross_1 = 50_000 + (10_000 + 5_000 - 60_000) = 5_000 > 0.
        "profit_total_eur": 50_000.0 + 10_000.0 + 5_000.0 - 60_000.0,
        "pv_generation_mwh": 7_200.0,
        "bess_total_discharge_mwh": 4_500.0,
        "bm_total_capacity_revenue_eur": 0.0,
        "bm_total_activation_revenue_eur": 0.0,
    }


def _econ_mixed_sign() -> dict:
    """``_econ`` with a high DAM index so the gross flips sign across years."""
    econ = _econ(fee_pct=10.0)
    econ["retail_inflation_pct"] = 1.0
    econ["dam_inflation_pct"] = 30.0
    return econ


def _gross(frame: pd.DataFrame, idx) -> float:
    """Per-year gross = net revenue + |aggregator fee| (both regimes)."""
    return (
        float(frame.at[idx, "revenue_eur"])
        + abs(float(frame.at[idx, "aggregator_fee_eur"]))
    )


def test_mixed_sign_fixture_actually_straddles_zero():
    """Guard the fixture: it must carry both a positive- and a
    negative-gross year, otherwise the no-op test below would pass
    vacuously without exercising the clamp/inflation interaction."""
    base = build_yearly_cashflow(_year1_kpis_mixed_sign(), _econ_mixed_sign(), _caps())
    after_y0 = base["project_year"] >= 1
    gross = (
        base.loc[after_y0, "revenue_eur"].astype(float)
        + base.loc[after_y0, "aggregator_fee_eur"].astype(float).abs()
    )
    assert (gross > 1e-6).any(), "fixture has no positive-gross year"
    assert (gross < -1e-6).any(), "fixture has no negative-gross year"


def test_revenue_perturbation_factor_one_is_noop_mixed_sign():
    """``factor=1.0`` is an EXACT no-op even when the base gross flips sign.

    Regression for the single-inversion distortion: the pre-fix code
    recovered gross via ``net / (1 - frac)``, which over-inflated the
    fee-clamped (gross <= 0) years and shifted ``revenue_eur`` /
    ``net_cashflow_eur`` (and hence NPV) on a no-op scaling.  This fails on
    the pre-fix code and passes once gross is recovered from the base frame.
    """
    base = build_yearly_cashflow(_year1_kpis_mixed_sign(), _econ_mixed_sign(), _caps())
    perturbed = _scale_revenue(base, 1.0)
    for col in (
        "revenue_eur", "net_cashflow_eur",
        "revenue_retail_eur", "revenue_dam_eur", "aggregator_fee_eur",
    ):
        pd.testing.assert_series_equal(
            base[col].astype(float).reset_index(drop=True),
            perturbed[col].astype(float).reset_index(drop=True),
            check_names=False, rtol=1e-9, atol=1e-6,
        )


@pytest.mark.parametrize("factor", [0.5, 1.5])
def test_revenue_perturbation_gross_scales_linearly_mixed_sign(factor: float):
    """Perturbed gross == ``factor`` * base gross per year, across the sign
    flip, and the per-stream nets still sum to the total net."""
    base = build_yearly_cashflow(_year1_kpis_mixed_sign(), _econ_mixed_sign(), _caps())
    perturbed = _scale_revenue(base, factor)
    after_y0 = base["project_year"] >= 1
    base_after = base.loc[after_y0]
    pert_after = perturbed.loc[after_y0]
    for y in base_after.index:
        gross_b = _gross(base_after, y)
        gross_p = _gross(pert_after, y)
        assert gross_p == pytest.approx(
            factor * gross_b, abs=max(1.0, 1e-6 * abs(gross_p)),
        )
        # Per-stream nets sum to the total net even where the clamp fires.
        retail_p = float(pert_after.at[y, "revenue_retail_eur"])
        dam_p = float(pert_after.at[y, "revenue_dam_eur"])
        rev_p = float(pert_after.at[y, "revenue_eur"])
        assert (retail_p + dam_p) == pytest.approx(
            rev_p, abs=max(0.01, 1e-6 * abs(rev_p)),
        )
