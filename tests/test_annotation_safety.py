"""Annotation-safety tests for the universal axes margin rule.

Verifies that:

* the ``apply_universal_margins`` helper actually pads the limits;
* the NPV-waterfall total annotation has clear vertical breathing
  room above the topmost data point.
"""

from __future__ import annotations

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402

from pvbess_opt.plotting import financial as fin_mod  # noqa: E402
from pvbess_opt.plotting.financial import plot_npv_waterfall  # noqa: E402
from pvbess_opt.plotting.style import apply_universal_margins  # noqa: E402


def test_apply_universal_margins_pads_both_axes():
    fig, ax = plt.subplots()
    ax.plot([0, 1, 2], [0, 10, 20])
    ax.set_xlim(0.0, 2.0)
    ax.set_ylim(0.0, 20.0)
    apply_universal_margins(ax)
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    # 2% on x, 5% on y, applied to a span-2 / span-20 axis.
    assert xmin < 0.0 and xmax > 2.0
    assert ymin < 0.0 and ymax > 20.0
    plt.close(fig)


def test_apply_universal_margins_skip_x_leaves_x_alone():
    fig, ax = plt.subplots()
    ax.set_xlim(0.0, 1.0)
    ax.set_ylim(0.0, 10.0)
    apply_universal_margins(ax, skip_x=True)
    xmin, xmax = ax.get_xlim()
    assert xmin == 0.0 and xmax == 1.0
    plt.close(fig)


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


def test_npv_total_annotation_has_breathing_room(tmp_path, monkeypatch):
    """The NPV = €X.XM annotation must sit above the topmost data
    point with at least 2% axes-fraction vertical whitespace."""
    captured: dict[str, plt.Figure] = {}

    def _save_no_close(path):
        path = path.with_suffix(".pdf") if hasattr(path, "with_suffix") else path
        captured["fig"] = plt.gcf()
        return path

    monkeypatch.setattr(fin_mod, "save_figure", _save_no_close)
    plt.close("all")

    plot_npv_waterfall(_yearly_cf_fixture(), tmp_path / "npv.pdf",
                       econ={"currency_format": "millions"})

    fig = captured["fig"]
    ax = fig.axes[0]
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()

    # Find the NPV bbox annotation: the only bbox-wrapped text
    # anchored in axes coordinates.
    npv_text = None
    for txt in ax.texts:
        if txt.get_bbox_patch() and "NPV" in txt.get_text():
            npv_text = txt
            break
    assert npv_text is not None, "NPV total annotation not found"

    bbox_disp = npv_text.get_window_extent(renderer)
    inv = ax.transAxes.inverted()
    bbox_y0_frac = inv.transform((0.0, bbox_disp.y0))[1]
    # Top edge of the topmost data marker: walk every line + every
    # bar height the axes carries.
    line_max = -np.inf
    for line in ax.lines:
        ys = np.asarray(line.get_ydata(), dtype=float)
        if ys.size > 0:
            line_max = max(line_max, float(np.nanmax(ys)))
    bar_max = -np.inf
    for patch in ax.patches:
        try:
            bar_max = max(bar_max, float(patch.get_y()) + float(patch.get_height()))
        except AttributeError:
            continue
    top_data_y = max(line_max, bar_max)
    ymin, ymax = ax.get_ylim()
    top_data_frac = (top_data_y - ymin) / (ymax - ymin)

    assert bbox_y0_frac - top_data_frac > 0.02, (
        f"NPV annotation bbox y0_frac={bbox_y0_frac:.3f} sits too "
        f"close to the topmost data point at y_frac={top_data_frac:.3f}"
    )
    # And the bbox should live in the top 10% of axes.
    assert bbox_y0_frac > 0.85, (
        f"NPV annotation bbox y0_frac={bbox_y0_frac:.3f} is too far "
        "from the top of the frame; expected y0 > 0.85."
    )
    plt.close("all")
