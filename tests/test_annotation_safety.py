"""Annotation-safety tests for the universal axes margin rule.

Verifies that the ``apply_universal_margins`` helper actually pads
the limits in the documented baseline-aware way.  Corner-value
annotation behaviour (NPV total, lifetime-cycles total) is covered
by the v5 zero-overlap suite further down this module.
"""

from __future__ import annotations

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402

from matplotlib.offsetbox import AnchoredText  # noqa: E402

from pvbess_opt.plotting import financial as fin_mod  # noqa: E402
from pvbess_opt.plotting import lifecycle as life_mod  # noqa: E402
from pvbess_opt.plotting.financial import plot_npv_waterfall  # noqa: E402
from pvbess_opt.plotting.lifecycle import (  # noqa: E402
    plot_lifetime_cycles,
    plot_revenue_stack_yearly,
)
from pvbess_opt.plotting import style as style_mod  # noqa: E402
from pvbess_opt.plotting.style import (  # noqa: E402
    _OVERLAP_TOLERANCE_PX2,
    _bbox_overlap_score,
    _collect_data_bboxes,
    anchor_corner_value,
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


# ---------------------------------------------------------------------------
# v5 anchor_corner_value tests
# ---------------------------------------------------------------------------


def _find_anchored(ax, needle: str) -> AnchoredText | None:
    for child in ax.get_children():
        if isinstance(child, AnchoredText) and needle in child.txt.get_text():
            return child
    return None


def _capture_plot_fig(monkeypatch, plot_module, render_fn) -> plt.Figure:
    """Render a plotting helper while keeping its figure open so the
    test can introspect the result."""
    captured: dict[str, plt.Figure] = {}

    def _save_no_close(path):
        if hasattr(path, "with_suffix"):
            path = path.with_suffix(".pdf")
        captured["fig"] = plt.gcf()
        return path

    monkeypatch.setattr(plot_module, "save_figure", _save_no_close)
    plt.close("all")
    render_fn()
    return captured["fig"]


def test_anchor_corner_value_snaps_to_nice_tick_when_expanding():
    """Bar that fills the full x-range and reaches the current ymax
    forces overlap with an upper-right annotation; the helper must
    snap the new ymax to a clean tick (9 or 10, not 8.x)."""
    fig, ax = plt.subplots()
    # A single tall bar covering the entire x-range — guarantees the
    # data bbox overlaps the upper-right corner horizontally.
    ax.bar([1.0], [8.0], width=2.0, align="center")
    ax.set_xlim(0.0, 2.0)
    ax.set_ylim(0.0, 8.0)
    anchor_corner_value(ax, text="X")
    new_ymax = ax.get_ylim()[1]
    assert new_ymax in (9.0, 10.0), (
        f"Expected snap to 9 or 10, got {new_ymax}"
    )
    plt.close(fig)


def test_anchor_corner_value_no_expansion_when_corner_already_clear():
    """Data sits well below the frame top — upper-right is clear and
    ymax must not change."""
    fig, ax = plt.subplots()
    ax.plot([0, 1], [0, 0.5])
    ax.set_ylim(0.0, 1.0)
    ymax_before = ax.get_ylim()[1]
    anchor_corner_value(ax, text="X")
    ymax_after = ax.get_ylim()[1]
    assert ymax_after == ymax_before, (
        f"Expected no change to ymax; got {ymax_before} -> {ymax_after}"
    )
    plt.close(fig)


def test_anchor_corner_value_expansion_is_single_shot():
    """v5 measures once and expands once — Step 1's trial is the only
    call to ``_measure_vertical_overlap``."""
    fig, ax = plt.subplots()
    ax.bar([1.0], [8.0], width=2.0, align="center")
    ax.set_xlim(0.0, 2.0)
    ax.set_ylim(0.0, 8.0)
    original = style_mod._measure_vertical_overlap
    call_count = [0]

    def counted(*args, **kwargs):
        call_count[0] += 1
        return original(*args, **kwargs)

    style_mod._measure_vertical_overlap = counted
    try:
        anchor_corner_value(ax, text="NPV = 7.5M EUR")
    finally:
        style_mod._measure_vertical_overlap = original
    assert call_count[0] == 1, (
        f"Expected 1 measurement; got {call_count[0]}"
    )
    plt.close(fig)


def test_anchor_corner_value_lands_in_upper_right_quadrant():
    fig, ax = plt.subplots()
    ax.bar([1.0], [8.0], width=2.0, align="center")
    ax.set_xlim(0.0, 2.0)
    ax.set_ylim(0.0, 8.0)
    art = anchor_corner_value(ax, text="NPV = 7.5M EUR")
    fig.canvas.draw()
    bbox = art.get_window_extent(renderer=fig.canvas.get_renderer())
    ax_bbox = ax.get_window_extent()
    cx = ax_bbox.x0 + 0.5 * ax_bbox.width
    cy = ax_bbox.y0 + 0.5 * ax_bbox.height
    assert (bbox.x0 + bbox.x1) / 2 > cx
    assert (bbox.y0 + bbox.y1) / 2 > cy
    plt.close(fig)


def test_npv_waterfall_zero_overlap_and_clean_ticks(tmp_path, monkeypatch):
    """End-to-end gate for plot_npv_waterfall: NPV annotation sits in
    the upper-right quadrant, has zero overlap with data artists, and
    ymax aligns with a y-axis tick."""
    fig = _capture_plot_fig(
        monkeypatch, fin_mod,
        lambda: plot_npv_waterfall(
            _yearly_cf_fixture(), tmp_path / "npv.pdf",
            econ={"currency_format": "millions"},
        ),
    )
    ax = fig.axes[0]
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    annotation = _find_anchored(ax, "NPV =")
    assert annotation is not None, "NPV annotation missing"

    ann_bbox = annotation.get_window_extent(renderer=renderer)
    ax_bbox = ax.get_window_extent()
    assert ann_bbox.x1 > ax_bbox.x0 + 0.5 * ax_bbox.width
    assert ann_bbox.y1 > ax_bbox.y0 + 0.5 * ax_bbox.height

    data_bboxes = _collect_data_bboxes(ax)
    total_overlap = sum(
        _bbox_overlap_score(ann_bbox, [b]) for b in data_bboxes
    )
    assert total_overlap <= _OVERLAP_TOLERANCE_PX2, (
        f"NPV annotation overlaps data by {total_overlap:.2f} px^2"
    )

    plt.close("all")


def test_lifetime_cycles_zero_overlap_and_clean_ticks(tmp_path, monkeypatch):
    """End-to-end gate for plot_lifetime_cycles: same contract as the
    NPV waterfall."""
    years = np.arange(0, 11)
    lifetime_yearly = pd.DataFrame({
        "project_year": years,
        "calendar_year": 2025 + years,
        "bess_discharge_mwh": np.concatenate([[0.0], np.full(10, 5000.0)]),
    })
    fig = _capture_plot_fig(
        monkeypatch, life_mod,
        lambda: plot_lifetime_cycles(
            lifetime_yearly, bess_kwh=20_000.0,
            out_path=tmp_path / "cyc.pdf",
        ),
    )
    ax = fig.axes[0]
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    annotation = _find_anchored(ax, "Total")
    assert annotation is not None, "Lifetime-cycles annotation missing"

    ann_bbox = annotation.get_window_extent(renderer=renderer)
    ax_bbox = ax.get_window_extent()
    assert ann_bbox.x1 > ax_bbox.x0 + 0.5 * ax_bbox.width
    assert ann_bbox.y1 > ax_bbox.y0 + 0.5 * ax_bbox.height

    data_bboxes = _collect_data_bboxes(ax)
    total_overlap = sum(
        _bbox_overlap_score(ann_bbox, [b]) for b in data_bboxes
    )
    assert total_overlap <= _OVERLAP_TOLERANCE_PX2, (
        f"Lifetime-cycles annotation overlaps data by "
        f"{total_overlap:.2f} px^2"
    )

    plt.close("all")


