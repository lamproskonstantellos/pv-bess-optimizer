"""Annotation-safety tests for the universal axes margin rule.

Verifies that the ``apply_universal_margins`` helper actually pads
the limits in the documented baseline-aware way.
"""

from __future__ import annotations

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402

from pvbess_opt.plotting import lifecycle as life_mod  # noqa: E402
from pvbess_opt.plotting.lifecycle import (  # noqa: E402
    plot_revenue_stack_yearly,
)
from pvbess_opt.plotting.style import (  # noqa: E402
    apply_universal_margins,
)


def test_apply_universal_margins_pads_top_for_non_negative_data():
    """Baseline-aware: ymin >= 0 keeps the floor; only the top pads."""
    fig, ax = plt.subplots()
    ax.plot([0, 1, 2], [0, 10, 20])
    ax.set_xlim(0.0, 2.0)
    ax.set_ylim(0.0, 20.0)
    apply_universal_margins(ax)
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    # No bar artists → x pads both sides symmetrically.
    assert xmin < 0.0 and xmax > 2.0
    # Floor preserved at 0; top padded by 5 %.
    assert ymin == 0.0
    assert ymax > 20.0
    plt.close(fig)


def test_apply_universal_margins_pads_both_for_signed_data():
    """Line plot crossing zero pads top and bottom symmetrically."""
    fig, ax = plt.subplots()
    ax.plot([0, 1, 2], [-10, 0, 10])
    ax.set_xlim(0.0, 2.0)
    ax.set_ylim(-10.0, 10.0)
    apply_universal_margins(ax)
    ymin, ymax = ax.get_ylim()
    assert ymin < -10.0 and ymax > 10.0
    plt.close(fig)


def test_apply_universal_margins_skip_x_leaves_x_alone():
    fig, ax = plt.subplots()
    ax.set_xlim(0.0, 1.0)
    ax.set_ylim(0.0, 10.0)
    apply_universal_margins(ax, skip_x=True)
    xmin, xmax = ax.get_xlim()
    assert xmin == 0.0 and xmax == 1.0
    plt.close(fig)


def test_apply_universal_margins_bar_plot_x_tight_left():
    """Bar plots: leftmost bar sits at the left frame edge."""
    fig, ax = plt.subplots()
    ax.bar([0, 1, 2], [3, 4, 5])
    xmin_before, xmax_before = ax.get_xlim()
    apply_universal_margins(ax)
    xmin_after, xmax_after = ax.get_xlim()
    # No left padding; only the right side extends.
    assert xmin_after == xmin_before
    assert xmax_after > xmax_before
    plt.close(fig)


def test_bar_plot_revenue_stack_floors_at_zero(tmp_path, monkeypatch):
    """plot_revenue_stack_yearly has non-negative bars only when no
    grid-charging cost is present — the y-axis floor must stay at 0
    so the bars sit flush against the €0 axis line."""
    captured: dict[str, plt.Figure] = {}

    def _save_no_close(path):
        path = path.with_suffix(".pdf") if hasattr(path, "with_suffix") else path
        captured["fig"] = plt.gcf()
        return path

    monkeypatch.setattr(life_mod, "save_figure", _save_no_close)
    plt.close("all")

    yearly = _yearly_cf_fixture()
    year1_kpis = {
        "profit_load_from_pv_eur": 600_000.0,
        "profit_load_from_bess_eur": 200_000.0,
        "profit_export_from_pv_eur": 150_000.0,
        "profit_export_from_bess_eur": 50_000.0,
        # No grid-charging cost — all stack values non-negative.
        "expense_charge_bess_grid_eur": 0.0,
    }
    plot_revenue_stack_yearly(yearly, year1_kpis, tmp_path / "rev.pdf")
    fig = captured["fig"]
    ax = fig.axes[0]
    ymin, _ = ax.get_ylim()
    assert ymin == 0.0, f"Bar plot floor drifted from 0 to {ymin}"
    plt.close("all")


def _yearly_cf_fixture() -> pd.DataFrame:
    years = np.arange(0, 11)
    rev = np.array([0.0] + [1_000_000.0] * 10)
    opex = np.array([0.0] + [-100_000.0] * 10)
    capex = np.array([-5_000_000.0] + [0.0] * 10)
    devex = np.zeros_like(rev)
    net = rev + opex + capex + devex
    discount = (1.0 / np.power(1.08, years)).astype(float)
    return pd.DataFrame({
        "project_year": years,
        "calendar_year": 2025 + years,
        "revenue_eur": rev,
        "opex_eur": opex,
        "capex_eur": capex,
        "devex_eur": devex,
        "net_cashflow_eur": net,
        "discount_factor": discount,
        "cumulative_cf_eur": np.cumsum(net),
        "discounted_cf_eur": net * discount,
        "cumulative_dcf_eur": np.cumsum(net * discount),
    })
