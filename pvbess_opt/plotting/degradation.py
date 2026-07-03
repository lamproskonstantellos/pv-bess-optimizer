"""IEEE-styled battery state-of-health / capacity-fade plot."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.ticker import MultipleLocator

from ..theme import FINANCIAL_COLORS
from .style import apply_universal_margins, save_figure

__all__ = ["plot_soh_trajectory"]

# Fixed presentation axis: SOH is a percentage, so the axis always spans
# the full 0..100 scale with a little headroom so the marker at 100 %
# is not clipped by the top frame.
_SOH_YLIM: tuple[float, float] = (0.0, 105.0)
_SOH_YTICKS: tuple[float, ...] = tuple(float(v) for v in range(0, 101, 10))


def plot_soh_trajectory(degradation: pd.DataFrame, out_path: Path) -> Path:
    """State-of-health (%) over the project life, marking replacement years."""
    _fig, ax = plt.subplots(figsize=(7, 4))
    years = degradation["calendar_year"].to_numpy(dtype=float)
    soh = degradation["soh_pct"].to_numpy(dtype=float)
    ax.plot(years, soh, color=FINANCIAL_COLORS["net"], marker="o")
    replacement_years: list[float] = []
    if "replacement" in degradation.columns:
        replacement_years = degradation.loc[
            degradation["replacement"], "calendar_year"
        ].tolist()
        for i, year in enumerate(replacement_years):
            ax.axvline(
                float(year), color=FINANCIAL_COLORS["capex"],
                linewidth=0.8, linestyle="--",
                label="BESS replacement" if i == 0 else None,
            )
    if replacement_years:
        ax.legend(loc="best")
    ax.set_xlabel("Year")
    ax.set_ylabel("State of health (%)")
    # Publication-style year axis: major ticks every 5 years (works for
    # calendar years, 2025/2030/..., and for project years, 0/5/10/...)
    # with unlabelled yearly minor ticks in between.
    ax.xaxis.set_major_locator(MultipleLocator(5))
    ax.xaxis.set_minor_locator(MultipleLocator(1))
    # Pad the x-axis only: the y-axis is a fixed 0..100 percentage scale
    # (plus headroom), set explicitly AFTER the margin helper so the
    # padding cannot re-scale it.
    apply_universal_margins(ax, skip_y=True)
    ax.set_ylim(*_SOH_YLIM)
    ax.set_yticks(_SOH_YTICKS)
    return save_figure(out_path)
