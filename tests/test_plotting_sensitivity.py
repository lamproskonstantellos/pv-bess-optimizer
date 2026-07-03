"""Sensitivity tornado annotation tests.

The IRR / NPV tornado plots annotate each bar end with the absolute
driver value that produced it and fold the +/- range into the y-axis
tick labels.  The base value is shown once, as a dashed vertical line
whose legend entry carries the formatted base value.  A frame without
the driver-value metadata falls back to the annotation-free layout
(dots + x-axis only).
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
import pytest

from pvbess_opt.plotting import financial as fin_mod
from pvbess_opt.plotting.financial import (
    _format_driver_value,
    plot_irr_tornado,
    plot_npv_tornado,
)


def _econ() -> dict:
    return {"project_lifecycle_years": 20, "project_start_year": 2026,
            "currency_format": "millions"}


# ---------------------------------------------------------------------------
# Fixtures — full-metadata sensitivity frames
# ---------------------------------------------------------------------------


def _rows(variable, label, base_v, low_v, high_v, delta,
          base_o, low_o, high_o, metric):
    """Three (base/low/high) sensitivity rows for one driver."""
    common = {"variable": variable, "label": label}
    return [
        {**common, "scenario": "base", "delta_value": 0.0,
         "value": base_v, metric: base_o},
        {**common, "scenario": "low", "delta_value": -delta,
         "value": low_v, metric: low_o},
        {**common, "scenario": "high", "delta_value": +delta,
         "value": high_v, metric: high_o},
    ]


def _irr_sens_df() -> pd.DataFrame:
    """IRR frame spanning 12-20 %, with a deliberately narrow OPEX bar
    (OPEX +/-5 %)."""
    rows = []
    # CAPEX +/-20 %: cheap CAPEX -> high IRR.
    rows += _rows("CAPEX", "Total CAPEX",
                  22.0e6, 17.6e6, 26.4e6, 0.20,
                  15.9, 18.0, 13.0, "irr_pct")
    # OPEX +/-5 %: narrow bar 15.6-16.2.
    rows += _rows("OPEX", "Total annual OPEX",
                  500.0e3, 475.0e3, 525.0e3, 0.05,
                  15.9, 16.2, 15.6, "irr_pct")
    # Revenue +/-20 %: wide bar 12-20.
    rows += _rows("Revenue", "Year-1 revenue base",
                  1.76e6, 1.408e6, 2.112e6, 0.20,
                  15.9, 12.0, 20.0, "irr_pct")
    return pd.DataFrame(rows)


def _npv_sens_df() -> pd.DataFrame:
    """NPV frame including the discount-rate driver."""
    rows = []
    rows += _rows("CAPEX", "Total CAPEX",
                  22.0e6, 17.6e6, 26.4e6, 0.20,
                  9.0e6, 13.0e6, 5.0e6, "npv_eur")
    rows += _rows("OPEX", "Total annual OPEX",
                  500.0e3, 475.0e3, 525.0e3, 0.05,
                  9.0e6, 9.4e6, 8.6e6, "npv_eur")
    rows += _rows("Revenue", "Year-1 revenue base",
                  1.76e6, 1.408e6, 2.112e6, 0.20,
                  9.0e6, 4.0e6, 14.0e6, "npv_eur")
    rows += _rows("DiscountRate", "Discount rate",
                  7.0, 5.0, 9.0, 2.0,
                  9.0e6, 12.0e6, 6.0e6, "npv_eur")
    return pd.DataFrame(rows)


def _minimal_sens_df() -> pd.DataFrame:
    """Sensitivity frame without the optional driver-value metadata."""
    rows = []
    for label, low, high in (
        ("Total CAPEX", 13.0, 18.0),
        ("Year-1 revenue base", 12.0, 20.0),
    ):
        rows.append({"label": label, "scenario": "low", "irr_pct": low})
        rows.append({"label": label, "scenario": "high", "irr_pct": high})
    return pd.DataFrame(rows)


def _capture(monkeypatch):
    captured: dict[str, plt.Figure] = {}

    def _save_no_close(path):
        captured["fig"] = plt.gcf()
        return path

    monkeypatch.setattr(fin_mod, "save_figure", _save_no_close)
    plt.close("all")
    return captured


@pytest.fixture(autouse=True)
def _close_figures():
    plt.close("all")
    yield
    plt.close("all")


def _bbox_texts(ax):
    """Text artists carrying a bbox patch (the value annotations)."""
    return [t for t in ax.texts if t.get_bbox_patch() is not None]


def _endpoint_texts(ax):
    """Bbox texts anchored exactly on an integer row (the bar-end
    metric / driver labels; excludes the base annotation)."""
    out = []
    for t in ax.texts:
        if t.get_bbox_patch() is None:
            continue
        _, y = t.get_position()
        if abs(y - round(y)) < 1e-6:
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# _format_driver_value
# ---------------------------------------------------------------------------


def test_format_driver_value():
    assert _format_driver_value(26.4e6, "capex") == "€26.4M"
    assert _format_driver_value(17.6e6, "capex") == "€17.6M"
    assert _format_driver_value(600.0e3, "opex") == "€600k"
    assert _format_driver_value(400.0e3, "opex") == "€400k"
    assert _format_driver_value(1.2e6, "opex") == "€1.2M"
    assert _format_driver_value(1.76e6, "revenue") == "€1.76M"
    assert _format_driver_value(12.4e6, "revenue") == "€12.4M"
    assert _format_driver_value(5.0, "discount_rate") == "5.0%"
    assert _format_driver_value(7.0, "discount_rate") == "7.0%"
    # Case / spacing tolerance.
    assert _format_driver_value(22.0e6, "CAPEX") == "€22.0M"


def test_format_driver_value_unknown_type_warns(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        out = _format_driver_value(1_234_567.0, "mystery")
    assert out == "€1,234,567"
    assert any("mystery" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Driver-value annotations
# ---------------------------------------------------------------------------


def test_tornado_renders_driver_values(tmp_path, monkeypatch):
    captured = _capture(monkeypatch)
    plot_irr_tornado(_irr_sens_df(), {"irr_pct": 15.9}, _econ(),
                     tmp_path / "irr.pdf")
    ax = captured["fig"].axes[0]
    texts = {t.get_text() for t in _bbox_texts(ax)}
    for expected in ("€17.6M", "€26.4M", "€475k", "€525k",
                     "€1.41M", "€2.11M"):
        assert expected in texts, f"missing driver-value label {expected!r}"


def test_tornado_npv_renders_discount_rate_value(tmp_path, monkeypatch):
    captured = _capture(monkeypatch)
    plot_npv_tornado(_npv_sens_df(), {"npv_eur": 9.0e6}, _econ(),
                     tmp_path / "npv.pdf")
    ax = captured["fig"].axes[0]
    texts = {t.get_text() for t in _bbox_texts(ax)}
    # Discount-rate driver values render as X.X%.
    assert "5.0%" in texts and "9.0%" in texts


def test_tornado_base_line_drawn(tmp_path, monkeypatch):
    captured = _capture(monkeypatch)
    plot_irr_tornado(_irr_sens_df(), {"irr_pct": 15.9}, _econ(),
                     tmp_path / "irr.pdf")
    ax = captured["fig"].axes[0]
    dashed = [
        ln for ln in ax.get_lines()
        if ln.get_linestyle() == "--"
        and ln.get_xdata()[0] == pytest.approx(15.9)
    ]
    assert dashed, "no base-case dashed vertical line at the base value"


def test_tornado_base_annotation_present(tmp_path, monkeypatch):
    """The Base marker appears in the legend (from the dashed axvline)
    and nowhere else on the chart — no annotation above the top bar.
    The legend entry is the bare name: the base value is readable off
    the x-axis and quoted in SUMMARY.md."""
    captured = _capture(monkeypatch)
    plot_irr_tornado(_irr_sens_df(), {"irr_pct": 15.9}, _econ(),
                     tmp_path / "irr.pdf")
    ax = captured["fig"].axes[0]
    legend_texts = [t.get_text() for t in ax.get_legend().get_texts()]
    assert any(t == "Base" for t in legend_texts), legend_texts
    assert not any("=" in t for t in legend_texts), legend_texts
    bbox_texts = [t.get_text() for t in _bbox_texts(ax)]
    assert not any(t.startswith("Base") for t in bbox_texts), bbox_texts

    captured = _capture(monkeypatch)
    plot_npv_tornado(_npv_sens_df(), {"npv_eur": 9.0e6}, _econ(),
                     tmp_path / "npv.pdf")
    ax = captured["fig"].axes[0]
    legend_texts = [t.get_text() for t in ax.get_legend().get_texts()]
    assert any(t == "Base" for t in legend_texts), legend_texts
    assert not any("=" in t for t in legend_texts), legend_texts
    bbox_texts = [t.get_text() for t in _bbox_texts(ax)]
    assert not any(t.startswith("Base") for t in bbox_texts), bbox_texts


def test_tornado_y_axis_labels_include_range(tmp_path, monkeypatch):
    captured = _capture(monkeypatch)
    plot_irr_tornado(_irr_sens_df(), {"irr_pct": 15.9}, _econ(),
                     tmp_path / "irr.pdf")
    ax = captured["fig"].axes[0]
    labels = [t.get_text() for t in ax.get_yticklabels()]
    assert any("±20%" in lbl for lbl in labels), labels
    assert any("±5%" in lbl for lbl in labels), labels


# ---------------------------------------------------------------------------
# Layout geometry — no overlaps, labels strictly outside the bars
# ---------------------------------------------------------------------------


def _overlap(b1, b2) -> bool:
    return (b1.x0 < b2.x1 and b2.x0 < b1.x1
            and b1.y0 < b2.y1 and b2.y0 < b1.y1)


def test_tornado_no_label_overlap(tmp_path, monkeypatch):
    """The IRR frame includes a very narrow bar (OPEX +/-5 %); no two
    text bounding boxes may overlap anywhere on the chart."""
    captured = _capture(monkeypatch)
    plot_irr_tornado(_irr_sens_df(), {"irr_pct": 15.9}, _econ(),
                     tmp_path / "irr.pdf")
    fig = captured["fig"]
    ax = fig.axes[0]
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    boxes = [t.get_window_extent(renderer) for t in _bbox_texts(ax)]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            assert not _overlap(boxes[i], boxes[j]), (
                f"text bboxes {i} and {j} overlap"
            )


def test_tornado_labels_outside_bar(tmp_path, monkeypatch):
    """Every bar-end label's bbox is entirely left of the bar's left
    edge OR entirely right of its right edge — never inside."""
    sens_df = _irr_sens_df()
    captured = _capture(monkeypatch)
    plot_irr_tornado(sens_df, {"irr_pct": 15.9}, _econ(),
                     tmp_path / "irr.pdf")
    fig = captured["fig"]
    ax = fig.axes[0]
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    inv = ax.transData.inverted()

    # Reconstruct each row's bar x-range from the dumbbell segments.
    seg_lines = [
        ln for ln in ax.get_lines()
        if ln.get_linestyle() == "-" and len(ln.get_xdata()) == 2
    ]
    row_range: dict[int, list[float]] = {}
    for ln in seg_lines:
        xs = ln.get_xdata()
        ys = ln.get_ydata()
        if abs(ys[0] - ys[1]) > 1e-6:
            continue
        row = round(ys[0])
        lo, hi = sorted(map(float, xs))
        cur = row_range.setdefault(row, [lo, hi])
        cur[0] = min(cur[0], lo)
        cur[1] = max(cur[1], hi)
    assert row_range, "no dumbbell bar segments found"

    for t in _endpoint_texts(ax):
        _, y = t.get_position()
        row = round(y)
        if row not in row_range:
            continue
        x_left, x_right = row_range[row]
        bbox = t.get_window_extent(renderer)
        bx0 = inv.transform((bbox.x0, bbox.y0))[0]
        bx1 = inv.transform((bbox.x1, bbox.y0))[0]
        outside = (bx1 <= x_left + 1e-6) or (bx0 >= x_right - 1e-6)
        assert outside, (
            f"row {row}: label {t.get_text()!r} bbox "
            f"[{bx0:.3f}, {bx1:.3f}] intrudes into bar "
            f"[{x_left:.3f}, {x_right:.3f}]"
        )


# ---------------------------------------------------------------------------
# Minimal frame (no optional driver-value metadata) still renders
# ---------------------------------------------------------------------------


def test_tornado_minimal_frame(tmp_path, monkeypatch):
    """A frame without driver-value metadata renders zero endpoint
    labels: no driver-value labels (no metadata to draw from) and no
    metric labels either (the metric is read from the x-axis).  The
    bare ``Base`` legend entry from the dashed axvline is still
    present; the y-axis ticks omit the ``±range`` suffix."""
    captured = _capture(monkeypatch)
    out = plot_irr_tornado(_minimal_sens_df(), {"irr_pct": 15.0}, _econ(),
                           tmp_path / "irr_minimal.pdf")
    assert out.exists() or out is not None
    ax = captured["fig"].axes[0]
    ylabels = [t.get_text() for t in ax.get_yticklabels()]
    assert not any("±" in lbl for lbl in ylabels), ylabels
    annotations = [t.get_text() for t in _bbox_texts(ax)]
    assert not any(a.startswith("Base") for a in annotations), annotations
    # No endpoint labels at all — dots + x-axis carry the information.
    assert _endpoint_texts(ax) == [], (
        f"unexpected endpoint labels: "
        f"{[t.get_text() for t in _endpoint_texts(ax)]}"
    )
    legend_texts = [t.get_text() for t in ax.get_legend().get_texts()]
    assert any(t == "Base" for t in legend_texts), legend_texts
