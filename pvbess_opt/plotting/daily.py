"""Daily dispatch plots.

Three figures per calendar day, all written into the
``out_dir/<YYYY>-<MM>/`` subdirectory of the daily plot folder:

* ``daily_supply_<YYYY-MM-DD>.pdf`` — stacked load supply
* ``daily_surplus_<YYYY-MM-DD>.pdf`` — surplus / charges / curtailment
* ``daily_combined_<YYYY-MM-DD>.pdf`` — supply + surplus on top of the
  load line

Filenames intentionally do **not** carry a scenario tag — the scenario
is encoded in the parent run-output directory (``results/<...>``) so
downstream archival code can rely on a stable naming convention.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

from ..config import XTICK_ROT
from .helpers import (
    fill_stacked_above,
    line_if_nonzero,
    pad_right_to_end,
    plot_stack_filtered,
    pretty_date,
    title_prefix,
)
from .style import apply_legend, get_scenario_label, save_figure_daily, show_titles


def _setup_day_axes(ax, start: pd.Timestamp, end: pd.Timestamp) -> None:
    ax.set_xlim(start, end)
    ticks = pd.date_range(start, end, freq="1h")
    ax.set_xticks(ticks)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    plt.setp(ax.get_xticklabels(), rotation=XTICK_ROT, ha="right")


def plot_daily_supply(res: pd.DataFrame, date_str: str, out_dir: Path) -> None:
    """Stacked PV / BESS / Import → Load with the load line overlaid."""
    day = pd.to_datetime(date_str).date()
    df = res[res["timestamp"].dt.date == day]
    if df.empty:
        return
    start = pd.Timestamp(day)
    end = start + pd.Timedelta(days=1)
    t = df["timestamp"]

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    series = [
        df["pv_to_load_kwh"].to_numpy(),
        df["bess_dis_load_kwh"].to_numpy(),
        df["grid_to_load_kwh"].to_numpy(),
    ]
    labels = ["PV→Load", "BESS→Load", "Import→Load"]
    t_pad, ypads = pad_right_to_end(t, series, end)
    plot_stack_filtered(ax, t_pad, ypads, labels, step_post=True)

    t_pad, [load_pad] = pad_right_to_end(t, [df["load_kwh"].to_numpy()], end)
    line_if_nonzero(ax, t_pad, load_pad, "Load (demand)", linewidth=1.5,
                    step_post=True)

    if show_titles():
        plt.title(
            f"Daily Load Supply{title_prefix(get_scenario_label())} "
            f"— {pretty_date(date_str)}"
        )
    plt.xlabel("Time (HH:mm)")
    plt.ylabel("Energy (kWh)")
    _setup_day_axes(ax, start, end)
    apply_legend(ax, max_rows=2, custom_order=True, plot_type="daily")
    save_figure_daily(out_dir / f"daily_supply_{date_str}.pdf", date_str)


def plot_daily_surplus(res: pd.DataFrame, date_str: str, out_dir: Path) -> None:
    """Charges / exports / curtailment stacked together."""
    day = pd.to_datetime(date_str).date()
    df = res[res["timestamp"].dt.date == day]
    if df.empty:
        return
    start = pd.Timestamp(day)
    end = start + pd.Timedelta(days=1)
    t = df["timestamp"]

    stacks = [
        df["pv_to_bess_kwh"].to_numpy(),
        df["pv_to_grid_kwh"].to_numpy(),
        df["pv_curtail_kwh"].to_numpy(),
        df["bess_dis_grid_kwh"].to_numpy(),
        df["bess_charge_grid_kwh"].to_numpy(),
    ]
    labels = [
        "PV→BESS (charge)",
        "PV→Grid (export)",
        "PV→Curtailment",
        "BESS→Grid (export)",
        "Import→BESS (charge)",
    ]
    t_pad, ypads = pad_right_to_end(t, stacks, end)

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    plot_stack_filtered(ax, t_pad, ypads, labels, step_post=True)

    if show_titles():
        plt.title(
            f"Daily Surplus Energy Flows{title_prefix(get_scenario_label())} "
            f"— {pretty_date(date_str)}"
        )
    plt.xlabel("Time (HH:mm)")
    plt.ylabel("Energy (kWh)")
    _setup_day_axes(ax, start, end)
    apply_legend(ax, max_rows=2, custom_order=True, plot_type="daily")
    save_figure_daily(out_dir / f"daily_surplus_{date_str}.pdf", date_str)


def plot_daily_combined(
    res: pd.DataFrame, date_str: str, out_dir: Path,
) -> None:
    """Supply at base + surplus stacked above the load line."""
    day = pd.to_datetime(date_str).date()
    df = res[res["timestamp"].dt.date == day]
    if df.empty:
        return
    start = pd.Timestamp(day)
    end = start + pd.Timedelta(days=1)
    t = df["timestamp"]

    plt.figure(figsize=(7, 4))
    ax = plt.gca()

    supply_series = [
        df["pv_to_load_kwh"].to_numpy(),
        df["bess_dis_load_kwh"].to_numpy(),
        df["grid_to_load_kwh"].to_numpy(),
    ]
    supply_labels = ["PV→Load", "BESS→Load", "Import→Load"]
    t_pad, ypads = pad_right_to_end(t, supply_series, end)
    plot_stack_filtered(ax, t_pad, ypads, supply_labels, step_post=True)

    t_pad, [load_pad] = pad_right_to_end(t, [df["load_kwh"].to_numpy()], end)
    line_if_nonzero(ax, t_pad, load_pad, "Load (demand)", linewidth=1.8,
                    step_post=True)

    surplus_series = [
        df["pv_to_bess_kwh"].to_numpy(),
        df["pv_to_grid_kwh"].to_numpy(),
        df["pv_curtail_kwh"].to_numpy(),
        df["bess_dis_grid_kwh"].to_numpy(),
        df["bess_charge_grid_kwh"].to_numpy(),
    ]
    surplus_labels = [
        "PV→BESS (charge)",
        "PV→Grid (export)",
        "PV→Curtailment",
        "BESS→Grid (export)",
        "Import→BESS (charge)",
    ]
    t_pad, ypads = pad_right_to_end(t, surplus_series, end)
    fill_stacked_above(ax, t_pad, load_pad, ypads, surplus_labels,
                       step_post=True)

    if show_titles():
        plt.title(
            f"Daily Energy Flows{title_prefix(get_scenario_label())} "
            f"— {pretty_date(date_str)}"
        )
    plt.xlabel("Time (HH:mm)")
    plt.ylabel("Energy (kWh)")
    _setup_day_axes(ax, start, end)
    apply_legend(ax, max_rows=2, custom_order=True, plot_type="daily")
    save_figure_daily(out_dir / f"daily_combined_{date_str}.pdf", date_str)
