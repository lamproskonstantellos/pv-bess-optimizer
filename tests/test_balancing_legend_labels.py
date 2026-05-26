"""Regression test: balancing-product labels are canonical.

Previously the five balancing-product bar labels (FCR, aFRR-up,
aFRR-dn, mFRR-up, mFRR-dn) were absent from
:data:`pvbess_opt.config.FINANCIAL_LABELS` and
:data:`pvbess_opt.config.FINANCIAL_LEGEND_ORDER`, so every balancing-on
revenue-stack plot logged a warning per label inside
:func:`pvbess_opt.config.apply_financial_legend`.

This test renders a balancing-on revenue stack with the WARNING
logger captured and asserts no "Non-canonical financial legend
label" warnings fire.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest

from pvbess_opt.config import (
    BM_COLOURS,
    FINANCIAL_LABEL_TO_COLOR_KEY,
    FINANCIAL_LABELS,
    FINANCIAL_LEGEND_ORDER,
    financial_color,
)


BALANCING_LABELS = ("FCR", "aFRR-up", "aFRR-dn", "mFRR-up", "mFRR-dn")


def test_balancing_labels_in_canonical_tables():
    """All five labels are registered in the canonical tables."""
    for label in BALANCING_LABELS:
        assert label in FINANCIAL_LABELS, f"{label!r} missing from FINANCIAL_LABELS"
        assert label in FINANCIAL_LEGEND_ORDER, (
            f"{label!r} missing from FINANCIAL_LEGEND_ORDER"
        )
        assert label in FINANCIAL_LABEL_TO_COLOR_KEY, (
            f"{label!r} missing from FINANCIAL_LABEL_TO_COLOR_KEY"
        )


def test_balancing_labels_colour_matches_per_product_palette():
    """``financial_color`` returns the same hex as :data:`BM_COLOURS`.

    Keeps the per-product colour consistent between the revenue stack
    (driven by ``financial_color``) and the BESS revenue waterfall /
    capacity-vs-activation split plots (driven by ``BM_COLOURS``).
    """
    pairs = [
        ("FCR", "fcr"),
        ("aFRR-up", "afrr_up"),
        ("aFRR-dn", "afrr_dn"),
        ("mFRR-up", "mfrr_up"),
        ("mFRR-dn", "mfrr_dn"),
    ]
    for label, product_key in pairs:
        assert financial_color(label).lower() == BM_COLOURS[product_key].lower()


def test_balancing_on_revenue_stack_logs_no_legend_warning(caplog, tmp_path: Path):
    """The revenue-stack plot for a balancing-on Year-1 logs no warnings."""
    pytest.importorskip("matplotlib")
    from pvbess_opt.plotting.lifecycle import plot_revenue_stack_yearly

    yearly_cf = pd.DataFrame(
        {
            "project_year": [0, 1, 2, 3, 4, 5],
            "calendar_year": [2025, 2026, 2027, 2028, 2029, 2030],
            "revenue_eur": [0.0, 100_000.0, 95_000.0, 90_000.0, 85_000.0, 80_000.0],
            "revenue_retail_eur": [0.0, 60_000.0, 57_000.0, 54_000.0, 51_000.0, 48_000.0],
            "revenue_dam_eur": [0.0, 40_000.0, 38_000.0, 36_000.0, 34_000.0, 32_000.0],
            "aggregator_fee_eur": [
                0.0, -2_000.0, -1_900.0, -1_800.0, -1_700.0, -1_600.0,
            ],
            "balancing_revenue_eur": [
                0.0, 40_000.0, 39_000.0, 38_000.0, 37_000.0, 36_000.0,
            ],
            "opex_eur": [0.0] * 6,
            "capex_eur": [-1_000_000.0] + [0.0] * 5,
            "devex_eur": [0.0] * 6,
            "discount_factor": [1.0] * 6,
            "net_cashflow_eur": [0.0] * 6,
            "discounted_cf_eur": [0.0] * 6,
            "cumulative_cf_eur": [0.0] * 6,
            "cumulative_dcf_eur": [0.0] * 6,
        }
    )
    year1_kpis = {
        "profit_load_from_pv_eur": 30_000.0,
        "profit_load_from_bess_eur": 30_000.0,
        "profit_export_from_pv_eur": 20_000.0,
        "profit_export_from_bess_eur": 30_000.0,
        "expense_charge_bess_grid_eur": 10_000.0,
        "revenue_bess_fcr_eur": 10_000.0,
        "revenue_bess_afrr_up_eur": 8_000.0,
        "revenue_bess_afrr_dn_eur": 7_000.0,
        "revenue_bess_mfrr_up_eur": 9_000.0,
        "revenue_bess_mfrr_dn_eur": 6_000.0,
    }
    out_path = tmp_path / "rev_stack.png"
    caplog.set_level(logging.WARNING, logger="pvbess_opt.config")
    plot_revenue_stack_yearly(
        yearly_cf, year1_kpis, out_path,
        econ={"aggregator_fee_pct_revenue": 2.0},
    )
    offenders = [
        rec.getMessage() for rec in caplog.records
        if "Non-canonical financial legend label" in rec.getMessage()
    ]
    assert not offenders, "\n".join(offenders)


def test_balancing_off_revenue_stack_still_logs_no_warning(caplog, tmp_path: Path):
    """A balancing-OFF run renders without registering the BM bars; warning-free."""
    pytest.importorskip("matplotlib")
    from pvbess_opt.plotting.lifecycle import plot_revenue_stack_yearly

    yearly_cf = pd.DataFrame(
        {
            "project_year": [0, 1, 2, 3],
            "calendar_year": [2025, 2026, 2027, 2028],
            "revenue_eur": [0.0, 100_000.0, 95_000.0, 90_000.0],
            "revenue_retail_eur": [0.0, 60_000.0, 57_000.0, 54_000.0],
            "revenue_dam_eur": [0.0, 40_000.0, 38_000.0, 36_000.0],
            "aggregator_fee_eur": [0.0, -2_000.0, -1_900.0, -1_800.0],
            "balancing_revenue_eur": [0.0, 0.0, 0.0, 0.0],
            "opex_eur": [0.0] * 4,
            "capex_eur": [-500_000.0, 0.0, 0.0, 0.0],
            "devex_eur": [0.0] * 4,
            "discount_factor": [1.0] * 4,
            "net_cashflow_eur": [0.0] * 4,
            "discounted_cf_eur": [0.0] * 4,
            "cumulative_cf_eur": [0.0] * 4,
            "cumulative_dcf_eur": [0.0] * 4,
        }
    )
    year1_kpis = {
        "profit_load_from_pv_eur": 30_000.0,
        "profit_load_from_bess_eur": 30_000.0,
        "profit_export_from_pv_eur": 20_000.0,
        "profit_export_from_bess_eur": 30_000.0,
        "expense_charge_bess_grid_eur": 10_000.0,
        # No balancing keys set => zero bars => no balancing labels emitted.
    }
    out_path = tmp_path / "rev_stack_off.png"
    caplog.set_level(logging.WARNING, logger="pvbess_opt.config")
    plot_revenue_stack_yearly(
        yearly_cf, year1_kpis, out_path,
        econ={"aggregator_fee_pct_revenue": 2.0},
    )
    offenders = [
        rec.getMessage() for rec in caplog.records
        if "Non-canonical financial legend label" in rec.getMessage()
    ]
    assert not offenders, "\n".join(offenders)


def test_bess_revenue_waterfall_does_not_emit_legend_warnings(caplog, tmp_path: Path):
    """The waterfall doesn't go through apply_financial_legend; no warnings either way."""
    pytest.importorskip("matplotlib")
    from pvbess_opt.plotting.bess_revenue import plot_bess_revenue_waterfall

    year1_kpis = {
        "revenue_bess_dam_eur": 50_000.0,
        "revenue_bess_fcr_eur": 10_000.0,
        "revenue_bess_afrr_up_eur": 8_000.0,
        "revenue_bess_afrr_dn_eur": 7_000.0,
        "revenue_bess_mfrr_up_eur": 9_000.0,
        "revenue_bess_mfrr_dn_eur": 6_000.0,
    }
    out_path = tmp_path / "waterfall.png"
    caplog.set_level(logging.WARNING, logger="pvbess_opt.config")
    plot_bess_revenue_waterfall(year1_kpis, out_path)
    offenders = [
        rec.getMessage() for rec in caplog.records
        if "Non-canonical financial legend label" in rec.getMessage()
    ]
    assert not offenders, "\n".join(offenders)
