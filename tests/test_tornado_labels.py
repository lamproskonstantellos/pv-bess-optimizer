"""Regression tests: tornado endpoint driver-value labels must match
the scenario that produced the metric at that x position.

Issue: when the "low" scenario yields a HIGHER metric than the "high"
scenario (e.g. low CAPEX → higher IRR), the leftmost dot corresponds
to the HIGH driver value and the rightmost dot to the LOW driver
value.  The endpoint label on each side must therefore carry the
driver value of the scenario whose metric outcome sits at that x.
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
    """Sensitivity frame where the 'low' scenario yields a HIGHER IRR
    than the 'high' scenario — the swap territory that broke the
    previous implementation.  Carries the full driver-value metadata
    so the endpoint labels render with the v0.8.8+ layout."""
    rows = []
    # (variable, label, base_v, low_v, high_v, delta, low_irr, high_irr)
    drivers = (
        ("CAPEX", "Total CAPEX",
         30.0e6, 17.6e6, 42.9e6, 0.20, 13.9, 8.7),
        ("OPEX", "Total annual OPEX",
         500.0e3, 400.0e3, 600.0e3, 0.20, 12.5, 9.4),
    )
    for var, label, base_v, low_v, high_v, delta, low_irr, high_irr in drivers:
        common = {"variable": var, "label": label}
        rows.append({**common, "scenario": "base", "delta_value": 0.0,
                     "value": base_v, "irr_pct": 11.0})
        rows.append({**common, "scenario": "low", "delta_value": -delta,
                     "value": low_v, "irr_pct": low_irr})
        rows.append({**common, "scenario": "high", "delta_value": +delta,
                     "value": high_v, "irr_pct": high_irr})
    return pd.DataFrame(rows)


def test_endpoint_labels_match_axis_position(tmp_path, monkeypatch):
    """Each endpoint's driver-value label must come from the scenario
    that produced the metric outcome at that x position.

    The fixture inverts the usual ordering (low CAPEX → high IRR), so
    the leftmost dot — corresponding to the smallest IRR — must carry
    the HIGH-scenario driver value (€42.9M for CAPEX), and the
    rightmost dot the LOW-scenario value (€17.6M).
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

    # Map each ytick row index → row label.
    yticklabels = [t.get_text() for t in ax.get_yticklabels()]

    # Collect bbox-wrapped texts anchored exactly on a row.
    by_row: dict[int, list[tuple[float, str]]] = {}
    for txt in ax.texts:
        if not txt.get_bbox_patch():
            continue
        x_data, y_data = txt.get_position()
        if abs(y_data - round(y_data)) > 1e-6:
            continue
        by_row.setdefault(int(round(y_data)), []).append(
            (float(x_data), txt.get_text())
        )

    plt.close("all")

    # Expected leftmost / rightmost driver-value labels per row label.
    # Leftmost dot == smallest IRR → labelled with the HIGH-scenario
    # driver value; rightmost dot == largest IRR → labelled with the
    # LOW-scenario driver value.
    expected = {
        "Total CAPEX": ("€42.9M", "€17.6M"),
        "Total annual OPEX": ("€600k", "€400k"),
    }

    assert by_row, "no bbox-wrapped endpoint labels found"
    matched_labels = set()
    for row, items in by_row.items():
        assert len(items) >= 2, f"row {row} has fewer than 2 endpoint labels"
        items.sort(key=lambda t: t[0])
        leftmost_text = items[0][1]
        rightmost_text = items[-1][1]
        # Strip any ``/ ±...`` suffix the y-tick label may carry.
        row_label = yticklabels[row].split(" / ")[0]
        assert row_label in expected, (
            f"unexpected row label {row_label!r} (yticklabels={yticklabels})"
        )
        want_left, want_right = expected[row_label]
        assert leftmost_text == want_left, (
            f"row {row_label}: leftmost label {leftmost_text!r} != "
            f"expected {want_left!r}"
        )
        assert rightmost_text == want_right, (
            f"row {row_label}: rightmost label {rightmost_text!r} != "
            f"expected {want_right!r}"
        )
        matched_labels.add(row_label)
    assert matched_labels == set(expected), (
        f"missing rows: {set(expected) - matched_labels}"
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
