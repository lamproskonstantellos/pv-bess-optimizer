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
