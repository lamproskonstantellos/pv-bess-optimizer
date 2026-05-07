"""Monthly aggregate plots.

Three figures per month, written directly into ``out_dir``:

* ``monthly_supply_<MM>.pdf``
* ``monthly_surplus_<MM>.pdf``
* ``monthly_combined_<MM>.pdf``

Filenames intentionally do **not** carry a scenario tag — the scenario
is encoded in the parent run-output directory.
"""

from __future__ import annotations

from calendar import month_name
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.ticker import ScalarFormatter

from ..config import XTICK_ROT
from .helpers import (
    bar_stacked_bins,
    edges_and_widths_monthly,
    line_if_nonzero,
    month_aggregate,
    pad_line_to_bins_end,
    title_prefix,
)
from .style import apply_legend, get_scenario_label, save_figure, show_titles


def _setup_day_axis(ax, left: pd.Series, width_days) -> None:
    ax.set_xlim(left.iloc[0],
                left.iloc[-1] + pd.to_timedelta(width_days[-1], unit="D"))
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=1))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d-%m-%Y"))
    plt.setp(ax.get_xticklabels(), rotation=XTICK_ROT, ha="right")
    ax.margins(x=0)


def _set_mwh_yaxis(ax, ylabel: str) -> None:
    ax.set_ylabel(ylabel)
    ax.yaxis.set_major_formatter(ScalarFormatter(useMathText=False))
    ax.yaxis.get_major_formatter().set_scientific(False)


def plot_monthly_supply(
    res: pd.DataFrame, month: int, out_dir: Path,
) -> None:
    """Stacked PV / BESS / Import → Load per day-of-month."""
    g = month_aggregate(res, month)
    if g.empty:
        return
    left, width_days = edges_and_widths_monthly(g["date"])

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    bar_stacked_bins(
        ax, left, width_days,
        [
            g["pv_to_load_kwh"] / 1000.0,
            g["bess_dis_load_kwh"] / 1000.0,
            g["grid_to_load_kwh"] / 1000.0,
        ],
        ["PV→Load", "BESS→Load", "Import→Load"],
    )
    t_pad, y_pad = pad_line_to_bins_end(
        left, width_days, (g["load_kwh"] / 1000.0).to_numpy(),
    )
    line_if_nonzero(ax, t_pad, y_pad, "Load (demand)", linewidth=1.5,
                    step_post=True)

    _set_mwh_yaxis(ax, "Energy (MWh/day)")
    if show_titles():
        ax.set_title(
            f"Monthly Load Supply{title_prefix(get_scenario_label())} "
            f"— {month_name[month]}"
        )
    ax.set_xlabel("Day")
    _setup_day_axis(ax, left, width_days)
    apply_legend(ax, max_rows=2, custom_order=True, plot_type="monthly")
    save_figure(out_dir / f"monthly_supply_{month:02d}.pdf")


def plot_monthly_surplus(
    res: pd.DataFrame, month: int, out_dir: Path,
) -> None:
    """Stacked surplus / curtailment / charge / grid-discharge."""
    g = month_aggregate(res, month)
    if g.empty:
        return
    left, width_days = edges_and_widths_monthly(g["date"])

    stacks = [
        g["pv_to_bess_kwh"] / 1000.0,
        g["pv_to_grid_kwh"] / 1000.0,
        g["pv_curtail_kwh"] / 1000.0,
        g["bess_dis_grid_kwh"] / 1000.0,
        g["bess_charge_grid_kwh"] / 1000.0,
    ]
    labels = [
        "PV→BESS (charge)", "PV→Grid (export)", "PV→Curtailment",
        "BESS→Grid (export)", "Import→BESS (charge)",
    ]

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    bar_stacked_bins(ax, left, width_days, stacks, labels)
    _set_mwh_yaxis(ax, "Energy (MWh/day)")
    if show_titles():
        ax.set_title(
            f"Monthly Surplus Energy Flows{title_prefix(get_scenario_label())} "
            f"— {month_name[month]}"
        )
    ax.set_xlabel("Day")
    _setup_day_axis(ax, left, width_days)
    apply_legend(ax, max_rows=2, custom_order=True, plot_type="monthly")
    save_figure(out_dir / f"monthly_surplus_{month:02d}.pdf")


def plot_monthly_combined(
    res: pd.DataFrame, month: int, out_dir: Path,
) -> None:
    """Supply at base + surplus on top of the load line."""
    g = month_aggregate(res, month)
    if g.empty:
        return
    left, width_days = edges_and_widths_monthly(g["date"])

    plt.figure(figsize=(7, 4))
    ax = plt.gca()

    bar_stacked_bins(
        ax, left, width_days,
        [
            g["pv_to_load_kwh"] / 1000.0,
            g["bess_dis_load_kwh"] / 1000.0,
            g["grid_to_load_kwh"] / 1000.0,
        ],
        ["PV→Load", "BESS→Load", "Import→Load"],
    )
    bar_stacked_bins(
        ax, left, width_days,
        [
            g["pv_to_bess_kwh"] / 1000.0,
            g["pv_to_grid_kwh"] / 1000.0,
            g["pv_curtail_kwh"] / 1000.0,
            g["bess_dis_grid_kwh"] / 1000.0,
            g["bess_charge_grid_kwh"] / 1000.0,
        ],
        [
            "PV→BESS (charge)", "PV→Grid (export)", "PV→Curtailment",
            "BESS→Grid (export)", "Import→BESS (charge)",
        ],
        bottom=(g["load_kwh"] / 1000.0).to_numpy(),
    )
    t_pad, y_pad = pad_line_to_bins_end(
        left, width_days, (g["load_kwh"] / 1000.0).to_numpy(),
    )
    line_if_nonzero(ax, t_pad, y_pad, "Load (demand)", linewidth=1.8,
                    step_post=True)

    _set_mwh_yaxis(ax, "Energy (MWh/day)")
    if show_titles():
        ax.set_title(
            f"Monthly Energy Flows{title_prefix(get_scenario_label())} "
            f"— {month_name[month]}"
        )
    ax.set_xlabel("Day")
    _setup_day_axis(ax, left, width_days)
    apply_legend(ax, max_rows=2, custom_order=True, plot_type="monthly")
    save_figure(out_dir / f"monthly_combined_{month:02d}.pdf")
