"""Rolling-horizon Monte Carlo distribution plot."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ._currency import euro_axis_formatter
from .style import save_figure, show_titles


def plot_rolling_horizon_distribution(
    mc_df: pd.DataFrame,
    out_path: Path,
    *,
    pf_profit_eur: float | None = None,
    currency_format: str = "auto",
) -> Path:
    """Histogram of the Monte Carlo profit values.

    Vertical lines at P10 / P50 / P90 plus an optional dashed marker at
    the perfect-foresight benchmark.
    """
    out_path = Path(out_path)
    if mc_df.empty or "profit_total_eur" not in mc_df.columns:
        plt.figure(figsize=(7, 4))
        ax = plt.gca()
        ax.text(0.5, 0.5, "Rolling-horizon Monte Carlo: no data.",
                ha="center", va="center", fontsize=10,
                transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])
        return save_figure(out_path)

    profits = mc_df["profit_total_eur"].astype(float).to_numpy()
    p10, p50, p90 = np.percentile(profits, [10, 50, 90])

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    ax.hist(profits, bins=max(10, len(profits) // 3), color="#5B9BD5",
            edgecolor="black", linewidth=0.4, alpha=0.85)
    ax.axvline(p10, color="#C62828", linewidth=1.0, linestyle=":",
               label=f"P10 = {p10:,.0f}")
    ax.axvline(p50, color="#1565C0", linewidth=1.2, linestyle="--",
               label=f"P50 = {p50:,.0f}")
    ax.axvline(p90, color="#2E7D32", linewidth=1.0, linestyle=":",
               label=f"P90 = {p90:,.0f}")
    if pf_profit_eur is not None and not np.isnan(pf_profit_eur):
        ax.axvline(
            float(pf_profit_eur), color="black", linewidth=1.0,
            linestyle="-.",
            label=f"Perfect-foresight = {float(pf_profit_eur):,.0f}",
        )

    ax.set_xlabel("Profit (EUR)")
    ax.set_ylabel("Frequency (seeds)")
    ax.xaxis.set_major_formatter(euro_axis_formatter(currency_format))
    if show_titles():
        ax.set_title("Rolling-horizon Monte Carlo profit distribution")
    ax.legend(loc="best", framealpha=0.9, fontsize=7)
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    return save_figure(out_path)
