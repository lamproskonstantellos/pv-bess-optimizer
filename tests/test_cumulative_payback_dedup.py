"""Dedup plot_cumulative_cashflow vs plot_payback.

plot_cumulative_cashflow now draws ONLY the cumulative + discounted
lines.  Payback markers belong to plot_payback alone, which renders
into cumulative_cashflow_with_payback_<start>-<end>.pdf.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd

from pvbess_opt.plotting.financial import (
    plot_cumulative_cashflow,
    plot_payback,
)


def _yearly_cf() -> pd.DataFrame:
    rows = [{
        "project_year": 0, "calendar_year": 2025,
        "revenue_eur": 0.0, "opex_eur": 0.0,
        "capex_eur": -600_000.0, "devex_eur": -75_000.0,
        "net_cashflow_eur": -675_000.0,
        "discount_factor": 1.0, "discounted_cf_eur": -675_000.0,
        "cumulative_cf_eur": -675_000.0,
        "cumulative_dcf_eur": -675_000.0,
    }]
    r = 0.07
    cum = -675_000.0
    cum_d = -675_000.0
    for y in range(1, 11):
        df_y = 1.0 / (1.0 + r) ** y
        net = 150_000.0
        cum += net
        cum_d += net * df_y
        rows.append({
            "project_year": y, "calendar_year": 2025 + y,
            "revenue_eur": 150_000.0, "opex_eur": 0.0,
            "capex_eur": 0.0, "devex_eur": 0.0,
            "net_cashflow_eur": net,
            "discount_factor": df_y, "discounted_cf_eur": net * df_y,
            "cumulative_cf_eur": cum, "cumulative_dcf_eur": cum_d,
        })
    return pd.DataFrame(rows)


def test_cumulative_cashflow_has_no_payback_lines(tmp_path: Path):
    plt.close("all")
    df = _yearly_cf()

    # Count axvline calls during plot_cumulative_cashflow execution.
    import matplotlib.axes
    original = matplotlib.axes.Axes.axvline
    calls: list[tuple] = []

    def probe(self, *args, **kwargs):
        calls.append((args, kwargs))
        return original(self, *args, **kwargs)

    matplotlib.axes.Axes.axvline = probe
    try:
        out = plot_cumulative_cashflow(df, tmp_path / "cumulative.pdf")
    finally:
        matplotlib.axes.Axes.axvline = original

    assert out.exists()
    # No axvline calls allowed — those would be payback markers.
    assert calls == [], (
        f"plot_cumulative_cashflow drew axvline markers: {calls}"
    )


def test_payback_plot_renders_with_markers(tmp_path: Path):
    """plot_payback still has its payback vertical lines."""
    plt.close("all")
    df = _yearly_cf()
    import matplotlib.axes
    original = matplotlib.axes.Axes.axvline
    calls: list[tuple] = []

    def probe(self, *args, **kwargs):
        calls.append((args, kwargs))
        return original(self, *args, **kwargs)

    matplotlib.axes.Axes.axvline = probe
    try:
        out = plot_payback(
            df, tmp_path / "cumulative_cashflow_with_payback.pdf",
            simple_payback_years=5.0, discounted_payback_years=7.0,
        )
    finally:
        matplotlib.axes.Axes.axvline = original
    assert out.exists()
    # At least the simple + discounted payback verticals.
    assert len(calls) >= 2, (
        f"plot_payback should still draw payback markers, got {calls}"
    )


def test_pipeline_uses_renamed_payback_filename():
    """The pipeline wires plot_payback into the
    cumulative_cashflow_with_payback_<start>-<end>.pdf filename."""
    src = Path("pvbess_opt/pipeline.py").read_text()
    assert "cumulative_cashflow_with_payback_" in src
    assert "payback_visualization.pdf" not in src
