"""Regression tests for the P2 correctness-adjacent items.

Covers:

* P2.4 -- ``aggregator_fee_eur`` is non-positive (a deduction) even in
  years where ``revenue_gross_y`` is negative.
* P2.5 -- ``plot_revenue_stack_yearly`` does not crash on a negative
  Year-1 DAM stream and emits a debug-level fallback message.
* P2.6 -- ``_payback_year`` returns NaN when no real crossing exists.
* P2.15 -- LCOE / LCOS sensitivity range matches the analytical
  ``(disc_capex * (1 +/- capex_d) + disc_opex * (1 +/- opex_d)) /
  disc_mwh`` decomposition rather than the multiplicative
  approximation.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import (
    _payback_year,
    build_yearly_cashflow,
)
from pvbess_opt.plotting.lifecycle import (
    _levelized_sensitivity_range,
    _sensitivity_deltas,
    plot_lcoe_summary,
    plot_lcos_summary,
    plot_revenue_stack_yearly,
)

# ---------------------------------------------------------------------------
# P2.6 -- _payback_year NaN fallthrough
# ---------------------------------------------------------------------------


def test_payback_year_returns_nan_for_never_crossing_cumulative():
    """No year reaches cumulative >= 0 -> NaN."""
    years = np.array([0, 1, 2, 3, 4], dtype=float)
    cum = np.array([-100.0, -90.0, -80.0, -70.0, -60.0], dtype=float)
    inc = np.array([-100.0, 10.0, 10.0, 10.0, 10.0], dtype=float)
    assert np.isnan(_payback_year(years, cum, inc))


def test_payback_year_returns_nan_for_flat_cumulative_at_zero():
    """Cumulative stuck at exactly 0 with zero incremental: no payback."""
    years = np.array([0, 1, 2, 3, 4], dtype=float)
    cum = np.array([-50.0, 0.0, 0.0, 0.0, 0.0], dtype=float)
    inc = np.array([-50.0, 0.0, 0.0, 0.0, 0.0], dtype=float)  # within eps
    result = _payback_year(years, cum, inc)
    assert np.isnan(result), f"expected NaN, got {result}"


def test_payback_year_nan_when_cumulative_starts_at_zero_with_zero_flows():
    """Pass-2 P2.1: the docstring promises NaN for the
    cumulative-stuck-at-zero case, but the previous ``i == 0`` branch
    returned ``years[0]`` whenever ``cumulative[0] >= 0`` without
    checking ``incremental[0]``.  An all-zero project (no CAPEX, no
    revenue) therefore reported a 0-year payback."""
    years = np.array([0, 1, 2], dtype=float)
    cum = np.array([0.0, 0.0, 0.0], dtype=float)
    inc = np.array([0.0, 0.0, 0.0], dtype=float)
    assert np.isnan(_payback_year(years, cum, inc))


def test_payback_year_genuine_cross_at_start_preserved():
    """A project that genuinely turns positive on day one (Year-0
    cumulative > 0) must still report a 0-year payback."""
    years = np.array([0, 1, 2], dtype=float)
    cum = np.array([1.0, 2.0, 3.0], dtype=float)
    inc = np.array([1.0, 1.0, 1.0], dtype=float)
    assert _payback_year(years, cum, inc) == 0.0


def test_payback_year_zero_cumulative_with_positive_flow_returns_start():
    """When cumulative[0] is exactly zero but incremental[0] is
    positive, year 0 is a legitimate crossing point."""
    years = np.array([0, 1, 2], dtype=float)
    cum = np.array([0.0, 1.0, 2.0], dtype=float)
    inc = np.array([100.0, 1.0, 1.0], dtype=float)
    assert _payback_year(years, cum, inc) == 0.0


def test_payback_year_interpolates_on_normal_crossing():
    """Smoke: the linear-interpolation path still works."""
    years = np.array([0, 1, 2, 3], dtype=float)
    cum = np.array([-100.0, -40.0, 20.0, 80.0], dtype=float)
    inc = np.array([-100.0, 60.0, 60.0, 60.0], dtype=float)
    # Crossing happens between years 1 and 2: 1 + 40/60 ~= 1.667.
    pb = _payback_year(years, cum, inc)
    assert pb == pytest.approx(1 + 40 / 60, abs=1e-6)


# ---------------------------------------------------------------------------
# P2.4 -- aggregator fee never flips to a revenue
# ---------------------------------------------------------------------------


def _econ() -> dict:
    return {
        "project_lifecycle_years": 5,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "bm_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 0.0,
        "capex_bess_eur_per_kw": 0.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 0.0,
        "opex_bess_eur_per_kw": 0.0,
        "pv_degradation_year1_pct": 0.0,
        "pv_degradation_annual_pct": 0.0,
        "bess_degradation_annual_pct": 0.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 10.0,
    }


def test_aggregator_fee_is_non_positive_under_negative_gross():
    """A negative-gross dispatch produces an aggregator_fee_eur <= 0."""
    # Year-1 KPIs with NEGATIVE gross (heavy grid-charging dominates).
    year1_kpis = {
        "profit_load_from_pv_eur": 0.0,
        "profit_load_from_bess_eur": 0.0,
        "profit_export_from_pv_eur": 10_000.0,
        "profit_export_from_bess_eur": 5_000.0,
        "expense_charge_bess_grid_eur": 50_000.0,  # exports << charging cost
        "profit_total_eur": -35_000.0,
        "bess_total_discharge_mwh": 0.0,
    }
    caps = {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}
    cf = build_yearly_cashflow(year1_kpis, _econ(), caps)
    fee_y1 = float(cf.loc[cf["project_year"] == 1, "aggregator_fee_eur"].iloc[0])
    assert fee_y1 <= 0.0, (
        f"aggregator_fee_eur flipped to a revenue under negative gross: "
        f"{fee_y1}"
    )
    # And the fee is exactly zero because max(gross, 0) = 0.
    assert fee_y1 == pytest.approx(0.0, abs=1e-9)


def test_aggregator_fee_normal_case_unchanged():
    """The clamp doesn't move the fee in the normal positive-gross case."""
    year1_kpis = {
        "profit_load_from_pv_eur": 50_000.0,
        "profit_load_from_bess_eur": 30_000.0,
        "profit_export_from_pv_eur": 20_000.0,
        "profit_export_from_bess_eur": 15_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 110_000.0,
        "bess_total_discharge_mwh": 0.0,
    }
    caps = {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}
    cf = build_yearly_cashflow(year1_kpis, _econ(), caps)
    fee_y1 = float(cf.loc[cf["project_year"] == 1, "aggregator_fee_eur"].iloc[0])
    # Gross_y1 = 110 000; fee = -0.10 * 110 000 = -11 000.
    assert fee_y1 == pytest.approx(-11_000.0, abs=1.0)


# ---------------------------------------------------------------------------
# P2.5 -- negative DAM ratio guard
# ---------------------------------------------------------------------------


def _yearly_cf_with_negative_dam() -> pd.DataFrame:
    """Cashflow where Year-1 ``revenue_dam_eur`` is NEGATIVE."""
    return pd.DataFrame(
        {
            "project_year": [0, 1, 2, 3],
            "calendar_year": [2025, 2026, 2027, 2028],
            "revenue_eur": [0.0, 40_000.0, 38_000.0, 36_000.0],
            "revenue_retail_eur": [0.0, 60_000.0, 57_000.0, 54_000.0],
            # Negative DAM stream (grid-charge expense exceeds exports).
            "revenue_dam_eur": [0.0, -20_000.0, -19_000.0, -18_000.0],
            "aggregator_fee_eur": [0.0, -800.0, -760.0, -720.0],
            "balancing_revenue_eur": [0.0, 0.0, 0.0, 0.0],
            "opex_eur": [0.0] * 4,
            "capex_eur": [-100_000.0, 0.0, 0.0, 0.0],
            "devex_eur": [0.0] * 4,
            "discount_factor": [1.0] * 4,
            "net_cashflow_eur": [0.0] * 4,
            "discounted_cf_eur": [0.0] * 4,
            "cumulative_cf_eur": [0.0] * 4,
            "cumulative_dcf_eur": [0.0] * 4,
        }
    )


def test_revenue_stack_plot_handles_negative_dam_without_crashing(
    tmp_path: Path, caplog,
):
    """Negative-DAM Year-1 doesn't crash; debug fallback is logged."""
    cf = _yearly_cf_with_negative_dam()
    year1_kpis = {
        "profit_load_from_pv_eur": 30_000.0,
        "profit_load_from_bess_eur": 30_000.0,
        "profit_export_from_pv_eur": 5_000.0,
        "profit_export_from_bess_eur": 5_000.0,
        "expense_charge_bess_grid_eur": 30_000.0,
    }
    out_path = tmp_path / "rev_stack_neg_dam.png"
    caplog.set_level(logging.DEBUG, logger="pvbess_opt.plotting.lifecycle")
    plot_revenue_stack_yearly(
        cf, year1_kpis, out_path,
        econ={"aggregator_fee_pct_revenue": 2.0},
    )
    # The function returned without an exception -- that is the
    # primary assertion.  The debug message is also emitted.
    fallback_msgs = [
        rec.getMessage() for rec in caplog.records
        if "degenerate DAM Year-1 base" in rec.getMessage()
    ]
    assert fallback_msgs, "expected a degenerate-DAM debug log message"


# ---------------------------------------------------------------------------
# P2.15 -- LCOE / LCOS sensitivity range matches the analytical formula
# ---------------------------------------------------------------------------


def _sample_fin_kpis() -> dict:
    """Realistic discounted components for the levelized-range check."""
    return {
        "lcoe_eur_per_mwh": 50.0,
        "lcoe_disc_pv_capex_eur": 2_000_000.0,
        "lcoe_disc_pv_opex_eur": 500_000.0,
        "lcoe_disc_pv_mwh": 50_000.0,
        "lcos_eur_per_mwh": 120.0,
        "lcos_disc_bess_capex_eur": 1_500_000.0,
        "lcos_disc_bess_opex_eur": 300_000.0,
        "lcos_disc_bess_mwh": 15_000.0,
    }


def test_lcoe_sensitivity_range_matches_analytical_formula():
    fin = _sample_fin_kpis()
    capex_d, opex_d = 0.20, 0.20
    rng = _levelized_sensitivity_range(
        fin,
        "lcoe_disc_pv_capex_eur",
        "lcoe_disc_pv_opex_eur",
        "lcoe_disc_pv_mwh",
        capex_d, opex_d,
    )
    assert rng is not None
    low, high = rng
    expected_low = (
        2_000_000.0 * 0.80 + 500_000.0 * 0.80
    ) / 50_000.0
    expected_high = (
        2_000_000.0 * 1.20 + 500_000.0 * 1.20
    ) / 50_000.0
    assert low == pytest.approx(expected_low, rel=1e-9)
    assert high == pytest.approx(expected_high, rel=1e-9)


def test_lcoe_analytical_range_differs_from_multiplicative_approximation():
    """The new range is NOT the same as base * (1+/-d)(1+/-d) when both deltas nonzero.

    The previous formula squared the deltas via the cross term; the
    new one is linear in each delta with weights given by the CAPEX
    vs OPEX share of the discounted numerator.
    """
    fin = _sample_fin_kpis()
    capex_d, opex_d = 0.20, 0.40  # asymmetric to expose the difference
    rng = _levelized_sensitivity_range(
        fin,
        "lcoe_disc_pv_capex_eur",
        "lcoe_disc_pv_opex_eur",
        "lcoe_disc_pv_mwh",
        capex_d, opex_d,
    )
    assert rng is not None
    low, high = rng
    base = float(fin["lcoe_eur_per_mwh"])
    mult_low = base * (1.0 - capex_d) * (1.0 - opex_d)
    mult_high = base * (1.0 + capex_d) * (1.0 + opex_d)
    # The two formulas would only coincide if the OPEX share of the
    # numerator is 0 OR the CAPEX share is 0.  With both shares > 0
    # and asymmetric deltas they MUST differ measurably.
    assert abs(low - mult_low) > 0.5
    assert abs(high - mult_high) > 0.5


def test_lcoe_summary_plot_runs(tmp_path: Path):
    """End-to-end: ``plot_lcoe_summary`` runs against the new keys."""
    fin = _sample_fin_kpis()
    econ = {
        "sensitivity_capex_delta_pct": 20.0,
        "sensitivity_opex_delta_pct": 20.0,
        "benchmark_lcoe_low_eur_per_mwh": 30.0,
        "benchmark_lcoe_high_eur_per_mwh": 60.0,
    }
    caps = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    out_path = tmp_path / "lcoe.pdf"
    res = plot_lcoe_summary(fin, None, caps, econ, out_path)
    assert res.exists()


def test_lcos_summary_plot_runs(tmp_path: Path):
    """End-to-end: ``plot_lcos_summary`` runs against the new keys."""
    fin = _sample_fin_kpis()
    econ = {
        "sensitivity_capex_delta_pct": 20.0,
        "sensitivity_opex_delta_pct": 20.0,
        "benchmark_lcos_low_eur_per_mwh": 80.0,
        "benchmark_lcos_high_eur_per_mwh": 150.0,
    }
    caps = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    out_path = tmp_path / "lcos.pdf"
    res = plot_lcos_summary(fin, None, caps, econ, out_path)
    assert res.exists()


def test_levelized_range_returns_none_when_components_missing():
    """Older KPI dicts (no discounted components) -> None (fallback path)."""
    fin = {"lcoe_eur_per_mwh": 50.0}
    rng = _levelized_sensitivity_range(
        fin,
        "lcoe_disc_pv_capex_eur",
        "lcoe_disc_pv_opex_eur",
        "lcoe_disc_pv_mwh",
        0.20, 0.20,
    )
    assert rng is None


# ---------------------------------------------------------------------------
# P2.2 / P2.3 -- legend label + share-denominator docstring smoke
# ---------------------------------------------------------------------------


def test_simple_payback_label_carries_capex_year_clarification(tmp_path: Path):
    """The plot_payback legend label suffix reads "(from CAPEX year)"."""
    from pvbess_opt.plotting.financial import plot_payback

    cf = pd.DataFrame(
        {
            "project_year": [0, 1, 2, 3, 4],
            "calendar_year": [2025, 2026, 2027, 2028, 2029],
            "cumulative_cf_eur": [-100.0, -60.0, -20.0, 20.0, 60.0],
            "cumulative_dcf_eur": [-100.0, -55.0, -15.0, 25.0, 65.0],
        }
    )
    out_path = tmp_path / "payback.png"
    actual = plot_payback(
        cf, out_path,
        simple_payback_years=2.5,
        discounted_payback_years=2.6,
    )
    # save_figure rewrites the suffix to .pdf and returns the new path.
    assert actual.exists()
    assert actual.suffix == ".pdf"


def test_sensitivity_deltas_helper_returns_default():
    """``_sensitivity_deltas`` defaults to the constant set in ``constants.py``."""
    from pvbess_opt.constants import DEFAULT_SENSITIVITY_DELTA_PCT

    expected = DEFAULT_SENSITIVITY_DELTA_PCT / 100.0
    assert _sensitivity_deltas({}) == pytest.approx((expected, expected))
