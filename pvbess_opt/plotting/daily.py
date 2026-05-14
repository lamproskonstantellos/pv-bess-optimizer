"""Daily dispatch plots.

Three vnb-mode figures per calendar day, all written into the
``out_dir/<YYYY>-<MM>/`` subdirectory of the daily plot folder:

* ``daily_supply_<YYYY-MM-DD>.pdf`` — stacked load supply
* ``daily_surplus_<YYYY-MM-DD>.pdf`` — surplus / charges / curtailment
* ``daily_combined_<YYYY-MM-DD>.pdf`` — supply + surplus on top of the
  load line

Three merchant-mode figures per calendar day (added in v0.6 — no
load, so the supply / combined views collapse to a single stack):

* ``daily_dispatch_<YYYY-MM-DD>.pdf`` — stacked PV/BESS exports +
  curtailment plus negative charging stacks.
* ``daily_soc_<YYYY-MM-DD>.pdf`` — SOC trajectory (kWh + %).
* ``daily_revenue_<YYYY-MM-DD>.pdf`` — DAM revenue per step minus
  grid-charging cost.

Filenames intentionally do **not** carry a scenario tag — the scenario
is encoded in the parent run-output directory (``results/<...>``) so
downstream archival code can rely on a stable naming convention.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

from ..config import FINANCIAL_COLORS, XTICK_ROT
from .helpers import (
    fill_stacked_above,
    line_if_nonzero,
    pad_right_to_end,
    plot_stack_filtered,
    pretty_date,
    title_prefix,
)
from .style import (
    apply_legend,
    apply_universal_margins,
    get_scenario_label,
    save_figure_daily,
    show_titles,
)


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
    apply_universal_margins(ax, skip_x=True)
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
    apply_universal_margins(ax, skip_x=True)
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
    apply_universal_margins(ax, skip_x=True)
    save_figure_daily(out_dir / f"daily_combined_{date_str}.pdf", date_str)


# ---------------------------------------------------------------------------
# Merchant-mode plots (no load) — added in v0.6
# ---------------------------------------------------------------------------


def plot_daily_dispatch(
    res: pd.DataFrame, date_str: str, out_dir: Path,
) -> None:
    """Stacked merchant dispatch: exports + curtailment vs charging."""
    day = pd.to_datetime(date_str).date()
    df = res[res["timestamp"].dt.date == day]
    if df.empty:
        return
    start = pd.Timestamp(day)
    end = start + pd.Timedelta(days=1)
    t = df["timestamp"]

    plt.figure(figsize=(7, 4))
    ax = plt.gca()

    # Positive stacks: exports + curtailment.
    pos_series = [
        df["pv_to_grid_kwh"].to_numpy(),
        df["bess_dis_grid_kwh"].to_numpy(),
        df["pv_curtail_kwh"].to_numpy(),
    ]
    pos_labels = [
        "PV→Grid (export)",
        "BESS→Grid (export)",
        "PV→Curtailment",
    ]
    t_pad, pos_pads = pad_right_to_end(t, pos_series, end)
    plot_stack_filtered(ax, t_pad, pos_pads, pos_labels, step_post=True)

    # Negative stacks: charging is consumption from the system's POV.
    neg_series = [
        -df["pv_to_bess_kwh"].to_numpy(),
        -df["bess_charge_grid_kwh"].to_numpy(),
    ]
    neg_labels = ["PV→BESS (charge)", "Import→BESS (charge)"]
    t_pad, neg_pads = pad_right_to_end(t, neg_series, end)
    plot_stack_filtered(ax, t_pad, neg_pads, neg_labels, step_post=True)

    ax.axhline(0.0, color="black", linewidth=0.6, alpha=0.6)

    if show_titles():
        plt.title(
            f"Merchant — Daily Dispatch{title_prefix(get_scenario_label())} "
            f"— {pretty_date(date_str)}"
        )
    plt.xlabel("Time (HH:mm)")
    plt.ylabel("Energy (kWh)")
    _setup_day_axes(ax, start, end)
    apply_legend(ax, max_rows=2, custom_order=True, plot_type="daily")
    apply_universal_margins(ax, skip_x=True)
    save_figure_daily(out_dir / f"daily_dispatch_{date_str}.pdf", date_str)


def plot_daily_soc(
    res: pd.DataFrame, date_str: str, out_dir: Path, *,
    e_cap_kwh: float | None = None,
) -> None:
    """SOC trajectory for one day — kWh on left axis, % on right."""
    day = pd.to_datetime(date_str).date()
    df = res[res["timestamp"].dt.date == day]
    if df.empty:
        return
    soc_kwh = df["soc_kwh"].to_numpy(dtype=float)
    if soc_kwh.max() <= 1e-9:
        # No BESS in the project — skip the plot.
        return
    start = pd.Timestamp(day)
    end = start + pd.Timedelta(days=1)

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    soc_colour = FINANCIAL_COLORS["net"]
    t_pad, [soc_pad] = pad_right_to_end(df["timestamp"], [soc_kwh], end)
    ax.plot(
        t_pad, soc_pad, drawstyle="steps-post",
        color=soc_colour, linewidth=1.5, label="SOC (kWh / %)",
    )

    if "soc_pct" in df.columns:
        soc_pct = df["soc_pct"].to_numpy(dtype=float)
        _t_pct, [soc_pct_pad] = pad_right_to_end(df["timestamp"], [soc_pct], end)
        ax2 = ax.twinx()
        ax2.plot(
            _t_pct, soc_pct_pad, drawstyle="steps-post",
            color=soc_colour, linewidth=1.5, label="_nolegend_",
        )
        ax2.set_ylabel("SOC (%)")

    if show_titles():
        plt.title(
            f"Merchant — Daily SOC{title_prefix(get_scenario_label())} "
            f"— {pretty_date(date_str)}"
        )
    ax.set_xlabel("Time (HH:mm)")
    ax.set_ylabel("SOC (kWh)")
    _setup_day_axes(ax, start, end)
    ax.legend(loc="best", framealpha=0.9, fontsize=7)
    apply_universal_margins(ax, skip_x=True)
    save_figure_daily(out_dir / f"daily_soc_{date_str}.pdf", date_str)


def plot_daily_revenue(
    res: pd.DataFrame, date_str: str, out_dir: Path,
) -> None:
    """DAM revenue per step (positive) minus grid-charging cost (negative)."""
    day = pd.to_datetime(date_str).date()
    df = res[res["timestamp"].dt.date == day]
    if df.empty:
        return
    start = pd.Timestamp(day)
    end = start + pd.Timedelta(days=1)
    t = df["timestamp"]

    rev_pv = df.get("profit_export_from_pv_eur", pd.Series(0.0, index=df.index))
    rev_bess = df.get("profit_export_from_bess_eur", pd.Series(0.0, index=df.index))
    cost_grid = df.get("expense_charge_bess_grid_eur", pd.Series(0.0, index=df.index))

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    t_pad, pos = pad_right_to_end(
        t, [rev_pv.to_numpy(), rev_bess.to_numpy()], end,
    )
    plot_stack_filtered(
        ax, t_pad, pos, ["PV→Grid (revenue)", "BESS→Grid (revenue)"],
        step_post=True,
    )
    t_pad_n, neg = pad_right_to_end(t, [(-cost_grid).to_numpy()], end)
    plot_stack_filtered(
        ax, t_pad_n, neg, ["Import→BESS (cost)"], step_post=True,
    )
    ax.axhline(0.0, color="black", linewidth=0.6, alpha=0.6)

    if show_titles():
        plt.title(
            f"Merchant — Daily Revenue{title_prefix(get_scenario_label())} "
            f"— {pretty_date(date_str)}"
        )
    ax.set_xlabel("Time (HH:mm)")
    ax.set_ylabel("EUR")
    _setup_day_axes(ax, start, end)
    apply_legend(ax, max_rows=2, custom_order=False, plot_type="daily")
    apply_universal_margins(ax, skip_x=True)
    save_figure_daily(out_dir / f"daily_revenue_{date_str}.pdf", date_str)
