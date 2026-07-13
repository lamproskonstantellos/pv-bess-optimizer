"""IEEE-styled intraday-venue figures (Eqs. I1-I5, E58/E59).

* :func:`plot_da_ida_price_duration` — day-ahead vs intraday price
  duration curves (each series sorted descending), the venue-spread
  view that motivates the two-stage re-dispatch.
* :func:`plot_intraday_position` — the per-step intraday net position
  (sells positive, buys negative) as a step line over the year.

Both figures are emitted by the pipeline only when the Stage-2
re-dispatch ran (the dispatch frame carries the intraday columns), so
the default figure set stays bit-identical.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ..theme import apply_financial_legend, financial_color
from .style import (
    apply_universal_margins,
    empty_placeholder,
    save_figure,
)

__all__ = [
    "plot_da_ida_price_duration",
    "plot_intraday_position",
]


def plot_da_ida_price_duration(res: pd.DataFrame, out_path: Path) -> Path:
    """Day-ahead vs intraday price duration curves (sorted descending)."""
    out_path = Path(out_path)
    if (
        "dam_price_eur_per_mwh" not in res.columns
        or "ida_price_eur_per_mwh" not in res.columns
    ):
        return empty_placeholder(out_path, "Intraday venue disabled.")
    dam = np.sort(
        res["dam_price_eur_per_mwh"].to_numpy(dtype=float),
    )[::-1]
    ida = np.sort(
        res["ida_price_eur_per_mwh"].to_numpy(dtype=float),
    )[::-1]
    if dam.size == 0:
        return empty_placeholder(out_path, "Intraday venue disabled.")
    _fig, ax = plt.subplots(figsize=(7, 4))
    x = np.arange(1, dam.size + 1) / dam.size * 100.0
    ax.plot(
        x, dam, color=financial_color("Day-ahead price"),
        linewidth=1.2, label="Day-ahead price",
    )
    ax.plot(
        x, ida, color=financial_color("Intraday price"),
        linewidth=1.2, label="Intraday price",
    )
    ax.axhline(0.0, color="black", linewidth=0.6)
    ax.set_xlabel("Share of time (%)")
    ax.set_ylabel("Price (EUR/MWh)")
    # The share-of-time axis is a bounded 0-100 % scale: keep it edge
    # to edge (skip_x) and pad only the y headroom around the curves.
    ax.set_xlim(0.0, 100.0)
    apply_universal_margins(ax, skip_x=True)
    apply_financial_legend(ax)
    return save_figure(out_path)


def plot_intraday_position(res: pd.DataFrame, out_path: Path) -> Path:
    """Per-step intraday net position (sells positive, buys negative)."""
    out_path = Path(out_path)
    needed = ("id_sell_pv_kwh", "id_sell_bess_kwh", "id_buy_kwh")
    if any(col not in res.columns for col in needed):
        return empty_placeholder(out_path, "Intraday venue disabled.")
    net = (
        res["id_sell_pv_kwh"].to_numpy(dtype=float)
        + res["id_sell_bess_kwh"].to_numpy(dtype=float)
        - res["id_buy_kwh"].to_numpy(dtype=float)
    )
    if net.size == 0:
        return empty_placeholder(out_path, "Intraday venue disabled.")
    _fig, ax = plt.subplots(figsize=(7, 4))
    if "timestamp" in res.columns and pd.api.types.is_datetime64_any_dtype(
        res["timestamp"],
    ):
        x = res["timestamp"]
        ax.set_xlabel("Time")
    else:
        x = np.arange(net.size)
        ax.set_xlabel("Timestep")
    ax.plot(
        x, net, drawstyle="steps-post",
        color=financial_color("Intraday net position"),
        linewidth=0.6, label="Intraday net position",
    )
    ax.axhline(0.0, color="black", linewidth=0.6)
    ax.set_ylabel("Net intraday trade (kWh per step)")
    apply_universal_margins(ax)
    apply_financial_legend(ax)
    return save_figure(out_path)
