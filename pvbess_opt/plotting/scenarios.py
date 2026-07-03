"""IEEE-styled scenario-comparison plots."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ..theme import FINANCIAL_COLORS
from .style import apply_universal_margins, save_figure

__all__ = ["plot_scenario_comparison_bars", "plot_scenario_revenue_bridge"]


def plot_scenario_comparison_bars(comparison: pd.DataFrame, out_path: Path) -> Path:
    """NPV bars per scenario with IRR markers on a secondary axis."""
    _fig, ax = plt.subplots(figsize=(7, 4))
    names = [str(n) for n in comparison["name"].tolist()]
    x = np.arange(len(names))
    ax.bar(
        x, comparison["npv_eur"].to_numpy(dtype=float),
        color=FINANCIAL_COLORS["net"], width=0.6,
    )
    ax.axhline(0.0, color=FINANCIAL_COLORS["capex"], linewidth=0.8, linestyle="--")
    ax.set_xticks(x)
    ax.set_xticklabels(names, rotation=30, ha="right")
    ax.set_ylabel("NPV (EUR)")
    if "irr_pct" in comparison.columns:
        ax2 = ax.twinx()
        ax2.plot(
            x, comparison["irr_pct"].to_numpy(dtype=float),
            color=FINANCIAL_COLORS["net_revenue_line"],
            marker="o", linestyle="none",
        )
        ax2.set_ylabel("IRR (%)")
    apply_universal_margins(ax)
    return save_figure(out_path)


def plot_scenario_revenue_bridge(comparison: pd.DataFrame, out_path: Path) -> Path:
    """Per-revenue-stream delta between the first two scenarios."""
    _fig, ax = plt.subplots(figsize=(7, 4))
    streams = [
        c for c in comparison.columns
        if c.startswith("revenue_") and c.endswith("_eur")
    ]
    first = comparison.iloc[0]
    second = comparison.iloc[1]
    deltas = [float(second[s]) - float(first[s]) for s in streams]
    labels = [s.replace("revenue_", "").replace("_eur", "") for s in streams]
    x = np.arange(len(streams))
    colors = [
        FINANCIAL_COLORS["tornado_pos"] if d >= 0
        else FINANCIAL_COLORS["tornado_neg"]
        for d in deltas
    ]
    ax.bar(x, deltas, color=colors, width=0.6)
    ax.axhline(0.0, color=FINANCIAL_COLORS["net_revenue_line"], linewidth=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.set_ylabel("Revenue delta (EUR)")
    apply_universal_margins(ax)
    return save_figure(out_path)
