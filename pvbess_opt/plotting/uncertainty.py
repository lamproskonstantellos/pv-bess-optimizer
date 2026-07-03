"""Rolling-horizon Monte Carlo distribution + 4-source comparison plots."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.axes import Axes

from ..theme import COLORS, FINANCIAL_COLORS, UNCERTAINTY_SOURCE_COLORS
from ._currency import euro_axis_formatter
from .style import (
    apply_universal_margins,
    attach_legend_clear_of_data,
    save_figure,
    show_titles,
)
from .style import (
    empty_placeholder as _empty_placeholder,
)

__all__ = [
    "plot_foresight_gap_comparison",
    "plot_rolling_horizon_distribution",
]


def _degenerate_spread_threshold(p50: float) -> float:
    """Spread below which an MC ensemble renders as degenerate."""
    return max(1.0, 1e-6 * abs(float(p50)))


def _render_degenerate_ensemble(
    ax: Axes,
    profits: np.ndarray,
    *,
    pf_profit_eur: float | None,
) -> None:
    """Dedicated layout when every seed lands on the same profit.

    A histogram of a zero-width distribution renders as one full-height
    bar with sub-euro tick labels — misleading noise.  Instead: a single
    narrow bar at the common value, a readable x-window around it,
    whole-euro tick labels, one collapsed legend entry and the
    perfect-foresight marker.  Legend entries carry series names only
    (no computed values) so the figure drops into a paper unchanged;
    the common value is readable off the x-axis and quoted in
    SUMMARY.md.
    """
    value = float(np.median(profits))
    # Readable window: +/- 2 % of the value with a 1 000 EUR minimum span.
    half_span = max(0.02 * abs(value), 500.0)
    lo, hi = value - half_span, value + half_span
    bar_width = 2.0 * half_span * 0.02
    ax.bar(
        [value], [len(profits)], width=bar_width,
        color=COLORS["BESS to grid"], edgecolor="black", linewidth=0.4,
        alpha=0.85,
        label="MC seeds (all equal)",
    )
    if pf_profit_eur is not None and not np.isnan(pf_profit_eur):
        ax.axvline(
            float(pf_profit_eur),
            color=FINANCIAL_COLORS["perfect_foresight"], linewidth=1.0,
            linestyle="--",
            label="Perfect foresight",
        )
        lo = min(lo, float(pf_profit_eur) - 0.1 * half_span)
        hi = max(hi, float(pf_profit_eur) + 0.1 * half_span)
    ax.set_xlim(lo, hi)


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
        all_profits = mc_df["profit_total_eur"].astype(float).to_numpy()
        all_spread = float(all_profits.max() - all_profits.min())
        if all_spread < _degenerate_spread_threshold(
            float(np.median(all_profits)),
        ):
            # Every seed of every source set lands on the same profit
            # (e.g. a PV-only plant): four overlapping identical
            # histograms carry no information — collapse to the shared
            # degenerate layout.
            _render_degenerate_ensemble(
                ax, all_profits, pf_profit_eur=pf_profit_eur,
            )
            ax.set_xlabel("Profit (EUR)")
            ax.set_ylabel("Frequency (seeds)")
            ax.xaxis.set_major_formatter(
                euro_axis_formatter(currency_format, min_resolution_eur=1.0),
            )
            if show_titles():
                ax.set_title(
                    "Rolling-horizon MC profit distribution by source set",
                )
            ax.grid(True, axis="y", linestyle="--", alpha=0.5)
            apply_universal_margins(ax, skip_x=True, skip_y=True)
            attach_legend_clear_of_data(
                ax, loc="best", framealpha=0.9, fontsize=7,
            )
            return save_figure(out_path)
        fallback_colour = COLORS["BESS to grid"]
        for source_set, group in mc_df.groupby("source_set"):
            profits = group["profit_total_eur"].astype(float).to_numpy()
            colour = UNCERTAINTY_SOURCE_COLORS.get(str(source_set), fallback_colour)
            ax.hist(
                profits,
                bins=max(10, len(profits) // 3),
                color=colour, edgecolor="black", linewidth=0.4,
                alpha=0.45,
                label=str(source_set),
            )
        if pf_profit_eur is not None and not np.isnan(pf_profit_eur):
            ax.axvline(
                float(pf_profit_eur), color="black", linewidth=1.0,
                linestyle="--",
                label="Perfect foresight",
            )
        ax.set_xlabel("Profit (EUR)")
        ax.set_ylabel("Frequency (seeds)")
        ax.xaxis.set_major_formatter(
            euro_axis_formatter(currency_format, min_resolution_eur=1.0),
        )
        if show_titles():
            ax.set_title("Rolling-horizon MC profit distribution by source set")
        ax.grid(True, axis="y", linestyle="--", alpha=0.5)
        apply_universal_margins(ax, skip_y=True)
        attach_legend_clear_of_data(ax, loc="best", framealpha=0.9, fontsize=7)
        return save_figure(out_path)

    profits = mc_df["profit_total_eur"].astype(float).to_numpy()
    p10, p50, p90 = np.percentile(profits, [10, 50, 90])
    spread = float(profits.max() - profits.min())

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    if spread < _degenerate_spread_threshold(float(p50)):
        # Near-degenerate ensemble: every seed on (numerically) the same
        # profit.  The P10/P50/P90 legend collapses to a single entry.
        _render_degenerate_ensemble(
            ax, profits, pf_profit_eur=pf_profit_eur,
        )
        ax.set_xlabel("Profit (EUR)")
        ax.set_ylabel("Frequency (seeds)")
        ax.xaxis.set_major_formatter(
            euro_axis_formatter(currency_format, min_resolution_eur=1.0),
        )
        if show_titles():
            ax.set_title("Rolling-horizon Monte Carlo profit distribution")
        ax.grid(True, axis="y", linestyle="--", alpha=0.5)
        apply_universal_margins(ax, skip_x=True, skip_y=True)
        attach_legend_clear_of_data(ax, loc="best", framealpha=0.9, fontsize=7)
        return save_figure(out_path)

    ax.hist(profits, bins=max(10, len(profits) // 3),
            color=COLORS["BESS to grid"],
            edgecolor="black", linewidth=0.4, alpha=0.85)
    ax.axvline(p10, color=FINANCIAL_COLORS["percentile_p10"],
               linewidth=1.0, linestyle="--", label="P10")
    ax.axvline(p50, color=FINANCIAL_COLORS["percentile_p50"],
               linewidth=1.0, linestyle="--", label="P50")
    ax.axvline(p90, color=FINANCIAL_COLORS["percentile_p90"],
               linewidth=1.0, linestyle="--", label="P90")
    if pf_profit_eur is not None and not np.isnan(pf_profit_eur):
        ax.axvline(
            float(pf_profit_eur),
            color=FINANCIAL_COLORS["perfect_foresight"], linewidth=1.0,
            linestyle="--",
            label="Perfect foresight",
        )

    ax.set_xlabel("Profit (EUR)")
    ax.set_ylabel("Frequency (seeds)")
    ax.xaxis.set_major_formatter(
        euro_axis_formatter(currency_format, min_resolution_eur=1.0),
    )
    if show_titles():
        ax.set_title("Rolling-horizon Monte Carlo profit distribution")
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    apply_universal_margins(ax, skip_y=True)
    attach_legend_clear_of_data(ax, loc="best", framealpha=0.9, fontsize=7)
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
    fallback_colour = COLORS["BESS to grid"]
    colours = [UNCERTAINTY_SOURCE_COLORS.get(s, fallback_colour) for s in sources]

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    # ``orientation=`` / ``tick_labels=`` require matplotlib >= 3.9; the
    # project pins matplotlib >= 3.10 in requirements/base.txt.
    bplot = ax.boxplot(
        data, orientation="horizontal", patch_artist=True, widths=0.6,
        tick_labels=sources,
    )
    for patch, colour in zip(bplot["boxes"], colours, strict=False):
        patch.set_facecolor(colour)
        patch.set_alpha(0.45)
        patch.set_edgecolor("black")
    for whisker in bplot["whiskers"]:
        whisker.set_color("black")
    for cap in bplot["caps"]:
        cap.set_color("black")
    for median in bplot["medians"]:
        median.set_color("black")

    ax.axvline(0.0, color="black", linewidth=0.8, alpha=0.6)
    ax.set_xlabel("Foresight gap (%)")
    ax.set_ylabel("Source set")
    if show_titles():
        ax.set_title("Foresight-gap comparison by uncertainty source")
    ax.grid(True, axis="x", linestyle="--", alpha=0.5)
    apply_universal_margins(ax)
    return save_figure(out_path)
