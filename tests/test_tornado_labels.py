"""Regression test: tornado endpoint labels must match their x-axis position.

Issue: when the "low" scenario yields a HIGHER metric than the "high"
scenario (e.g. low CAPEX → higher IRR), the old implementation labelled
the leftmost endpoint with the "low_value" text and the rightmost with
the "high_value" text, which then disagreed with the x-axis coordinates
the labels were drawn at.

The fix re-formats each endpoint label from its own numeric position
(``left`` / ``right`` after ``sorted((low, high))``), guaranteeing
agreement between text and x-coordinate.
"""

from __future__ import annotations

import matplotlib
import pandas as pd

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402

from pvbess_opt.plotting import financial as fin_mod  # noqa: E402
from pvbess_opt.plotting.financial import plot_irr_tornado  # noqa: E402


def _econ() -> dict:
    return {
        "project_lifecycle_years": 10,
        "project_start_year": 2026,
    }


def _inverted_sens_df() -> pd.DataFrame:
    """Sensitivity frame where 'low' yields HIGHER IRR than 'high' —
    the swap territory that broke the previous implementation."""
    rows = []
    for label, low_irr, high_irr in (
        ("Total CAPEX", 13.9, 8.7),       # low CAPEX → higher IRR
        ("Total annual OPEX", 12.5, 9.4),  # low OPEX → higher IRR
    ):
        rows.append({"label": label, "scenario": "low", "irr_pct": low_irr})
        rows.append({"label": label, "scenario": "high", "irr_pct": high_irr})
    return pd.DataFrame(rows)


def test_endpoint_labels_match_axis_position(tmp_path, monkeypatch):
    """Render the tornado and inspect each text artist on its row.

    To inspect the figure we monkey-patch ``save_figure`` so it skips
    ``plt.close()`` — that leaves the current Axes alive for assertion.
    """
    captured: dict[str, plt.Figure] = {}

    def _save_no_close(path):
        path = (path).with_suffix(".pdf") if hasattr(path, "with_suffix") else path
        captured["fig"] = plt.gcf()
        return path

    monkeypatch.setattr(fin_mod, "save_figure", _save_no_close)
    plt.close("all")

    out = tmp_path / "irr_inverted.pdf"
    plot_irr_tornado(_inverted_sens_df(), {"irr_pct": 11.0}, _econ(), out)

    fig = captured["fig"]
    ax = fig.axes[0]

    by_row: dict[int, list[tuple[float, float]]] = {}
    for txt in ax.texts:
        if not txt.get_bbox_patch():
            continue
        x_data, y_data = txt.get_position()
        if abs(y_data - round(y_data)) > 1e-6:
            continue
        raw = txt.get_text().strip().rstrip("%")
        try:
            value = float(raw)
        except ValueError:
            continue
        by_row.setdefault(int(round(y_data)), []).append((float(x_data), value))

    plt.close("all")

    assert by_row, "no bbox-wrapped endpoint labels found"
    for row, items in by_row.items():
        assert len(items) >= 2, f"row {row} has fewer than 2 endpoint labels"
        items.sort(key=lambda t: t[0])
        leftmost_x, leftmost_val = items[0]
        rightmost_x, rightmost_val = items[-1]
        # Printed value must match its x-coordinate (within rounding).
        assert abs(leftmost_val - leftmost_x) < 0.05, (
            f"row {row}: leftmost label {leftmost_val} != x {leftmost_x}"
        )
        assert abs(rightmost_val - rightmost_x) < 0.05, (
            f"row {row}: rightmost label {rightmost_val} != x {rightmost_x}"
        )
        # Leftmost numeric value must not exceed rightmost.
        assert leftmost_val <= rightmost_val + 1e-9, (
            f"row {row}: leftmost {leftmost_val} > rightmost {rightmost_val}"
        )


# ---------------------------------------------------------------------------
# Round-4: endpoint labels live OUTSIDE the dots
# ---------------------------------------------------------------------------


from pvbess_opt.plotting.financial import (  # noqa: E402
    plot_npv_tornado,
)


def _capture_fig(monkeypatch):
    """Patch ``save_figure`` so the most recently drawn figure stays
    alive after the plotting function returns, and return a dict the
    caller can inspect."""
    captured: dict[str, plt.Figure] = {}

    def _save_no_close(path):
        path = path.with_suffix(".pdf") if hasattr(path, "with_suffix") else path
        captured["fig"] = plt.gcf()
        return path

    monkeypatch.setattr(fin_mod, "save_figure", _save_no_close)
    plt.close("all")
    return captured


def _bbox_labels_by_row(ax) -> dict[int, list]:
    """Return ``{row: [text_artist, ...]}`` for every bbox-wrapped
    artist whose anchor sits exactly on a row."""
    out: dict[int, list] = {}
    for txt in ax.texts:
        if not txt.get_bbox_patch():
            continue
        x_data, y_data = txt.get_position()
        if abs(y_data - round(y_data)) > 1e-6:
            continue
        out.setdefault(int(round(y_data)), []).append(txt)
    return out


def test_endpoint_labels_outside_dots(tmp_path, monkeypatch):
    """Both labels lie horizontally OUTSIDE the dot positions.

    The left label's bbox must end at or before the left dot's
    x-coordinate, and the right label's bbox must start at or after
    the right dot's x-coordinate.  The 8-pt outward offset is applied
    via ``offset_copy``, so the bbox is checked in display coordinates
    converted back to data.
    """
    captured = _capture_fig(monkeypatch)
    plot_irr_tornado(_inverted_sens_df(), {"irr_pct": 11.0}, _econ(),
                     tmp_path / "irr.pdf")
    fig = captured["fig"]
    ax = fig.axes[0]
    fig.canvas.draw()
    inv = ax.transData.inverted()

    by_row = _bbox_labels_by_row(ax)
    assert by_row, "no bbox-wrapped endpoint labels found"
    for row, texts in by_row.items():
        assert len(texts) >= 2, f"row {row} expected 2 endpoint labels"
        # Sort by anchor x (== dot x) so element 0 == left endpoint.
        texts.sort(key=lambda t: t.get_position()[0])
        left_txt, right_txt = texts[0], texts[-1]
        left_anchor_x = left_txt.get_position()[0]
        right_anchor_x = right_txt.get_position()[0]
        # Get the rendered text bbox in display coords, convert to data.
        left_bbox_disp = left_txt.get_window_extent(fig.canvas.get_renderer())
        right_bbox_disp = right_txt.get_window_extent(fig.canvas.get_renderer())
        left_x1_data = inv.transform((left_bbox_disp.x1, 0))[0]
        right_x0_data = inv.transform((right_bbox_disp.x0, 0))[0]
        assert left_x1_data <= left_anchor_x + 1e-6, (
            f"row {row}: left label bbox.x1={left_x1_data} "
            f"crosses into the dot at x={left_anchor_x}"
        )
        assert right_x0_data >= right_anchor_x - 1e-6, (
            f"row {row}: right label bbox.x0={right_x0_data} "
            f"crosses into the dot at x={right_anchor_x}"
        )
    plt.close("all")


def test_endpoint_labels_do_not_overlap_y_axis_spine(tmp_path, monkeypatch):
    """The leftmost endpoint label must not be clipped by the y-axis
    spine — i.e. its bbox.x0 in axes coordinates must be > 0."""
    captured = _capture_fig(monkeypatch)
    # Use an NPV sensitivity so the metric is in EUR.
    sens_rows = []
    for label, low, high in (
        ("Total CAPEX", 5_000_000.0, 15_000_000.0),
        ("Total annual OPEX", 8_000_000.0, 12_000_000.0),
    ):
        sens_rows.append({"label": label, "scenario": "low", "npv_eur": low})
        sens_rows.append({"label": label, "scenario": "high", "npv_eur": high})
    sens_df = pd.DataFrame(sens_rows)
    plot_npv_tornado(
        sens_df, {"npv_eur": 10_000_000.0},
        {"project_lifecycle_years": 20, "project_start_year": 2026,
         "currency_format": "millions"},
        tmp_path / "npv.pdf",
    )
    fig = captured["fig"]
    ax = fig.axes[0]
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    ax_disp = ax.get_window_extent(renderer)

    by_row = _bbox_labels_by_row(ax)
    for row, texts in by_row.items():
        # The leftmost label per row anchors at the smaller x.
        leftmost = min(texts, key=lambda t: t.get_position()[0])
        bbox = leftmost.get_window_extent(renderer)
        assert bbox.x0 > ax_disp.x0, (
            f"row {row}: leftmost label bbox.x0 ({bbox.x0}) is at or "
            f"left of the y-axis spine ({ax_disp.x0})"
        )
    plt.close("all")


def test_short_range_row_labels_dont_collide(tmp_path, monkeypatch):
    """Pathological case: a row whose low/high values are very close
    (≈ the discount-rate row before it gets dropped).  The two
    endpoint label bboxes must not overlap horizontally."""
    captured = _capture_fig(monkeypatch)
    # A near-zero spread for one driver alongside a wide one so the
    # x-axis range itself is large.
    sens_rows = []
    for label, low, high in (
        ("Total CAPEX", 5_000_000.0, 15_000_000.0),
        ("Tight driver", 9_900_000.0, 10_100_000.0),
    ):
        sens_rows.append({"label": label, "scenario": "low", "npv_eur": low})
        sens_rows.append({"label": label, "scenario": "high", "npv_eur": high})
    sens_df = pd.DataFrame(sens_rows)
    plot_npv_tornado(
        sens_df, {"npv_eur": 10_000_000.0},
        {"project_lifecycle_years": 20, "project_start_year": 2026,
         "currency_format": "millions"},
        tmp_path / "npv.pdf",
    )
    fig = captured["fig"]
    ax = fig.axes[0]
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()

    by_row = _bbox_labels_by_row(ax)
    for row, texts in by_row.items():
        if len(texts) < 2:
            continue
        texts.sort(key=lambda t: t.get_position()[0])
        left_bbox = texts[0].get_window_extent(renderer)
        right_bbox = texts[-1].get_window_extent(renderer)
        assert left_bbox.x1 < right_bbox.x0, (
            f"row {row}: endpoint labels overlap "
            f"(left.x1={left_bbox.x1} >= right.x0={right_bbox.x0})"
        )
    plt.close("all")
