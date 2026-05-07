"""Rolling-horizon Monte Carlo distribution + 4-source comparison plots."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ._currency import euro_axis_formatter
from .style import save_figure, show_titles


# Distinct hues for the four uncertainty source sets.  Picked to be
# colour-blind-friendly when overlaid as semi-transparent histograms.
_SOURCE_SET_COLORS: dict[str, str] = {
    "dam": "#C62828",   # red — DAM noise only
    "pv": "#EF6C00",    # amber — PV noise only
    "load": "#1565C0",  # blue — load noise only
    "all": "#2E7D32",   # green — all three combined
}


def _empty_placeholder(out_path: Path, message: str) -> Path:
    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    ax.text(0.5, 0.5, message, ha="center", va="center", fontsize=10,
            transform=ax.transAxes)
    ax.set_xticks([])
    ax.set_yticks([])
    return save_figure(out_path)


def plot_rolling_horizon_distribution(
    mc_df: pd.DataFrame,
    out_path: Path,
    *,
    pf_profit_eur: float | None = None,
    currency_format: str = "auto",
) -> Path:
    """Histogram of the Monte Carlo profit values.

    With a ``source_set`` column present (the 4-source comparison
    workflow), draws one semi-transparent histogram per source set
    using distinct colours.  Otherwise produces the single-ensemble
    P10 / P50 / P90 histogram with an optional dashed marker at the
    perfect-foresight benchmark.
    """
    out_path = Path(out_path)
    if mc_df.empty or "profit_total_eur" not in mc_df.columns:
        return _empty_placeholder(
            out_path, "Rolling-horizon Monte Carlo: no data.",
        )

    if "source_set" in mc_df.columns:
        plt.figure(figsize=(7, 4))
        ax = plt.gca()
        for source_set, group in mc_df.groupby("source_set"):
            profits = group["profit_total_eur"].astype(float).to_numpy()
            colour = _SOURCE_SET_COLORS.get(str(source_set), "#5B9BD5")
            ax.hist(
                profits,
                bins=max(10, len(profits) // 3),
                color=colour, edgecolor="black", linewidth=0.4,
                alpha=0.45,
                label=f"{source_set} (P50 = {np.median(profits):,.0f})",
            )
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
            ax.set_title("Rolling-horizon MC profit distribution by source set")
        ax.legend(loc="best", framealpha=0.9, fontsize=7)
        ax.grid(True, axis="y", linestyle="--", alpha=0.5)
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


def plot_foresight_gap_comparison(
    mc_df: pd.DataFrame,
    out_path: Path,
) -> Path:
    """Horizontal box-plot of the foresight gap (%) per source set.

    Sorted by median gap so the most-impactful source ends up at the
    bottom of the panel.  Empty / single-source DataFrames produce a
    placeholder figure.
    """
    out_path = Path(out_path)
    if (
        mc_df.empty
        or "foresight_gap_pct" not in mc_df.columns
        or "source_set" not in mc_df.columns
    ):
        return _empty_placeholder(
            out_path, "Foresight-gap comparison: no compare-sources data.",
        )

    grouped = {
        str(s): g["foresight_gap_pct"].astype(float).to_numpy()
        for s, g in mc_df.groupby("source_set")
    }
    sources = sorted(grouped, key=lambda s: float(np.median(grouped[s])))
    data = [grouped[s] for s in sources]
    colours = [_SOURCE_SET_COLORS.get(s, "#5B9BD5") for s in sources]

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    bplot = ax.boxplot(
        data, orientation="horizontal", patch_artist=True, widths=0.6,
        tick_labels=sources,
    )
    for patch, colour in zip(bplot["boxes"], colours):
        patch.set_facecolor(colour)
        patch.set_alpha(0.45)
        patch.set_edgecolor("black")
    for whisker in bplot["whiskers"]:
        whisker.set_color("black")
    for cap in bplot["caps"]:
        cap.set_color("black")
    for median in bplot["medians"]:
        median.set_color("black")

    ax.axvline(0.0, color="grey", linewidth=0.8, alpha=0.6)
    ax.set_xlabel("Foresight gap (%)")
    ax.set_ylabel("Source set")
    if show_titles():
        ax.set_title("Foresight-gap comparison by uncertainty source")
    ax.grid(True, axis="x", linestyle="--", alpha=0.5)
    return save_figure(out_path)
