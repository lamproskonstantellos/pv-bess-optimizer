"""Balancing-market plot family.

Two dedicated figures live here:

* :func:`plot_balancing_reservation_profile` — a 24-hour stacked area
  chart of the average per-product reservation. Reads the
  ``bm_reservation_<product>_kw`` columns from the Year-1 dispatch
  frame.
* :func:`plot_balancing_mc_distribution` — a histogram of the realised
  balancing revenue across the Monte Carlo scenarios, with P10 / P50
  / P90 vertical lines. Reads the raw realisations from the result
  dict produced by :func:`pvbess_opt.rolling_horizon.monte_carlo_balancing`.

Both helpers gracefully no-op when the data is absent so the main
pipeline can call them unconditionally and skip the page write only
when the helper returns ``None``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ..balancing import PRODUCTS_ALL
from ..config import BM_COLOURS
from .style import (
    apply_fine_ticks,
    apply_universal_margins,
    empty_placeholder,
    save_figure,
    show_titles,
)


def _hour_of_day(ts_column: pd.Series) -> np.ndarray:
    """Return an integer hour-of-day array for a timestamp column."""
    timestamps = pd.to_datetime(ts_column, errors="coerce")
    return timestamps.dt.hour.to_numpy(dtype=int)


def plot_balancing_reservation_profile(
    res: pd.DataFrame, out_path: Path,
) -> Path | None:
    """Render the 24-hour average reservation stacked area chart.

    Returns ``None`` when the dispatch frame does not carry the
    balancing-reservation columns (the gate is off or the project has
    no BESS).
    """
    cols = [f"bm_reservation_{p}_kw" for p in PRODUCTS_ALL]
    if not all(c in res.columns for c in cols):
        return None
    if not pd.api.types.is_datetime64_any_dtype(res.get("timestamp")):
        return empty_placeholder(
            Path(out_path),
            "Balancing reservation plot needs a datetime timestamp column.",
        )

    hours = _hour_of_day(res["timestamp"])
    averaged = pd.DataFrame({"hour": hours, **{c: res[c] for c in cols}})
    profile = (
        averaged.groupby("hour")[cols].mean().reindex(range(24), fill_value=0.0)
    )

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    x = np.arange(24)
    stacked = np.zeros(24, dtype=float)
    for product in PRODUCTS_ALL:
        col = f"bm_reservation_{product}_kw"
        values = profile[col].to_numpy(dtype=float)
        ax.fill_between(
            x, stacked, stacked + values,
            color=BM_COLOURS.get(product, BM_COLOURS["quantile_outer"]),
            alpha=0.85, linewidth=0.4, edgecolor="black",
            label=product.upper().replace("_", " "),
        )
        stacked = stacked + values

    ax.set_xlabel("Hour of day")
    ax.set_ylabel("Average reservation (kW)")
    ax.set_xticks(x)
    ax.set_xlim(0, 23)
    if show_titles():
        ax.set_title("Balancing reservation profile (Year-1 average)")
    ax.legend(loc="upper right", frameon=True, framealpha=0.9, fontsize=8)
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    apply_universal_margins(ax)
    apply_fine_ticks(ax)
    return save_figure(Path(out_path))


def plot_balancing_mc_distribution(
    mc_results: dict[str, Any], out_path: Path,
) -> Path | None:
    """Render the realised balancing-revenue histogram with P10/P50/P90.

    Returns ``None`` when the Monte Carlo dict is empty (balancing gate
    is off) or does not carry the raw realisations.
    """
    realisations = mc_results.get("bm_mc_total_realised_eur")
    if not realisations:
        return None
    values = np.asarray(realisations, dtype=float)
    if values.size == 0:
        return None

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    ax.hist(
        values, bins=30, color=BM_COLOURS["mc_histogram"], edgecolor="black",
        linewidth=0.4, alpha=0.85,
    )
    p10 = float(mc_results.get(
        "bm_total_balancing_revenue_p10_eur", float(np.quantile(values, 0.10)),
    ))
    p50 = float(mc_results.get(
        "bm_total_balancing_revenue_p50_eur", float(np.quantile(values, 0.50)),
    ))
    p90 = float(mc_results.get(
        "bm_total_balancing_revenue_p90_eur", float(np.quantile(values, 0.90)),
    ))
    for value, label, colour in [
        (p10, "P10", BM_COLOURS["quantile_outer"]),
        (p50, "P50", BM_COLOURS["quantile_centre"]),
        (p90, "P90", BM_COLOURS["quantile_outer"]),
    ]:
        ax.axvline(value, color=colour, linestyle="--", linewidth=1.2, label=label)

    ax.set_xlabel("Realised balancing revenue (EUR)")
    ax.set_ylabel("Scenario count")
    if show_titles():
        ax.set_title("Balancing revenue — Monte Carlo distribution")
    ax.legend(loc="upper right", frameon=True, framealpha=0.9, fontsize=8)
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    apply_universal_margins(ax)
    apply_fine_ticks(ax)
    return save_figure(Path(out_path))
