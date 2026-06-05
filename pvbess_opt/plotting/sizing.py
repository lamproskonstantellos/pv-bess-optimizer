"""IEEE-styled sizing plots: efficient frontier + NPV-vs-capacity curve."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ..theme import FINANCIAL_COLORS
from .style import apply_universal_margins, save_figure

__all__ = ["plot_efficient_frontier", "plot_npv_vs_capacity"]


def plot_efficient_frontier(frontier: pd.DataFrame, out_path: Path) -> Path:
    """Scatter NPV vs IRR across the swept sizes; marker area scales with
    BESS energy (MWh)."""
    _fig, ax = plt.subplots()
    irr = frontier["irr_pct"].to_numpy(dtype=float)
    npv = frontier["npv_eur"].to_numpy(dtype=float)
    cap = frontier["bess_capacity_mwh"].to_numpy(dtype=float)
    span = float(np.nanmax(cap) - np.nanmin(cap)) if cap.size else 0.0
    sizes = (
        30.0 + 170.0 * (cap - np.nanmin(cap)) / span
        if span > 0
        else np.full(cap.shape, 60.0)
    )
    ax.scatter(irr, npv, s=sizes, color=FINANCIAL_COLORS["net"], alpha=0.85)
    ax.axhline(0.0, color=FINANCIAL_COLORS["capex"], linewidth=0.8, linestyle="--")
    ax.set_xlabel("IRR (%)")
    ax.set_ylabel("NPV (EUR)")
    apply_universal_margins(ax)
    return save_figure(out_path)


def plot_npv_vs_capacity(
    frontier: pd.DataFrame, breakeven_mwh: float, out_path: Path,
) -> Path:
    """NPV vs BESS energy (MWh); marks the oversizing break-even."""
    _fig, ax = plt.subplots()
    ordered = frontier.sort_values("bess_capacity_mwh")
    mwh = ordered["bess_capacity_mwh"].to_numpy(dtype=float)
    npv = ordered["npv_eur"].to_numpy(dtype=float)
    ax.plot(mwh, npv, color=FINANCIAL_COLORS["net"], marker="o")
    if breakeven_mwh is not None and np.isfinite(breakeven_mwh):
        ax.axvline(
            float(breakeven_mwh),
            color=FINANCIAL_COLORS["tornado_neg"],
            linewidth=1.0,
            linestyle="--",
            label="oversizing break-even",
        )
        ax.legend(loc="best", fontsize=7)
    ax.set_xlabel("BESS energy (MWh)")
    ax.set_ylabel("NPV (EUR)")
    apply_universal_margins(ax)
    return save_figure(out_path)
