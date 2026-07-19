"""IEEE-styled multi-year price-scenario figures (pricedata layer).

* :func:`plot_price_path_fan` — the yearly mean DAM price path of every
  enabled scenario on one axis (the fan the Year-1-only price plots
  cannot show).
* :func:`plot_capture_kpis` — the applied scenario's yearly PV capture
  price against the DAM baseload price, plus the realized BESS spread.

Both figures are emitted by the pipeline only when the price-scenario
engine is armed, so the default figure set stays bit-identical.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

from ..theme import apply_financial_legend, financial_color, scenario_path_color
from .style import (
    apply_universal_margins,
    empty_placeholder,
    legend_below,
    save_figure,
)

__all__ = [
    "plot_capture_kpis",
    "plot_price_path_fan",
]


def plot_price_path_fan(
    paths_by_scenario: dict[str, pd.DataFrame], out_path: Path,
) -> Path:
    """Yearly mean DAM price per scenario (one line per enabled row).

    Scenario names are user-defined, so the series draw from the
    ordered :data:`pvbess_opt.theme.SCENARIO_PATH_COLORS` registry and
    the legend renders the names verbatim through ``legend_below``
    (the canonical-label reorder of ``apply_financial_legend`` cannot
    know them).
    """
    out_path = Path(out_path)
    usable = {
        name: frame for name, frame in (paths_by_scenario or {}).items()
        if frame is not None and not frame.empty
        and "dam_mean_price_eur_per_mwh" in frame.columns
    }
    if not usable:
        return empty_placeholder(out_path, "Price scenarios disabled.")
    _fig, ax = plt.subplots(figsize=(7, 4))
    last_year = 1
    for index, (name, frame) in enumerate(usable.items()):
        years = frame["project_year"].to_numpy(dtype=int)
        ax.plot(
            years,
            frame["dam_mean_price_eur_per_mwh"].to_numpy(dtype=float),
            color=scenario_path_color(index),
            linewidth=1.4,
            marker="o",
            markersize=2.5,
            label=name,
        )
        last_year = max(last_year, int(years.max()))
    ax.set_xlabel("Operating year")
    ax.set_ylabel("Mean DAM price (EUR/MWh)")
    ax.set_xlim(1, last_year)
    apply_universal_margins(ax, skip_x=True)
    legend_below(ax)
    return save_figure(out_path)


def plot_capture_kpis(paths: pd.DataFrame, out_path: Path) -> Path:
    """Capture price vs baseload price plus the realized BESS spread."""
    out_path = Path(out_path)
    needed = (
        "project_year", "dam_mean_price_eur_per_mwh",
        "pv_capture_price_eur_per_mwh",
        "bess_realized_spread_eur_per_mwh",
    )
    if (
        paths is None or paths.empty
        or any(col not in paths.columns for col in needed)
    ):
        return empty_placeholder(out_path, "Price scenarios disabled.")
    years = paths["project_year"].to_numpy(dtype=int)
    _fig, ax = plt.subplots(figsize=(7, 4))
    series = (
        ("DAM baseload price", "dam_mean_price_eur_per_mwh"),
        ("PV capture price", "pv_capture_price_eur_per_mwh"),
        ("Realized BESS spread", "bess_realized_spread_eur_per_mwh"),
    )
    drew_any = False
    for label, column in series:
        values = paths[column].to_numpy(dtype=float)
        if pd.isna(values).all():
            continue  # e.g. no BESS: the spread column is all-NaN
        ax.plot(
            years, values,
            color=financial_color(label),
            linewidth=1.4, marker="o", markersize=2.5, label=label,
        )
        drew_any = True
    if not drew_any:
        return empty_placeholder(out_path, "Price scenarios disabled.")
    ax.axhline(0.0, color="black", linewidth=0.6)
    ax.set_xlabel("Operating year")
    ax.set_ylabel("Price (EUR/MWh)")
    ax.set_xlim(1, int(years.max()))
    apply_universal_margins(ax, skip_x=True)
    apply_financial_legend(ax)
    return save_figure(out_path)
