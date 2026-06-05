"""IEEE-styled battery state-of-health / capacity-fade plot."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

from ..theme import FINANCIAL_COLORS
from .style import apply_universal_margins, save_figure

__all__ = ["plot_soh_trajectory"]


def plot_soh_trajectory(degradation: pd.DataFrame, out_path: Path) -> Path:
    """State-of-health (%) over the project life, marking replacement years."""
    _fig, ax = plt.subplots()
    years = degradation["calendar_year"].to_numpy(dtype=float)
    soh = degradation["soh_pct"].to_numpy(dtype=float)
    ax.plot(years, soh, color=FINANCIAL_COLORS["net"], marker="o")
    if "replacement" in degradation.columns:
        for year in degradation.loc[
            degradation["replacement"], "calendar_year"
        ].tolist():
            ax.axvline(
                float(year), color=FINANCIAL_COLORS["capex"],
                linewidth=0.8, linestyle="--",
            )
    ax.set_xlabel("Year")
    ax.set_ylabel("State of health (%)")
    apply_universal_margins(ax)
    return save_figure(out_path)
