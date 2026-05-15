"""Daily dispatch plots.

Three vnb-mode figures per calendar day, all written into the
``out_dir/<YYYY>-<MM>/`` subdirectory of the daily plot folder:

* ``daily_supply_<YYYY-MM-DD>.pdf`` — stacked load supply
* ``daily_surplus_<YYYY-MM-DD>.pdf`` — surplus / charges / curtailment
* ``daily_combined_<YYYY-MM-DD>.pdf`` — supply + surplus on top of the
  load line

Three merchant-mode figures per calendar day (no
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
import numpy as np
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
# Merchant-mode plots (no load)
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


def plot_daily_combined_merchant(
    res: pd.DataFrame, date_str: str, out_dir: Path,
) -> None:
    """Combined merchant-mode dispatch view for one calendar day.

    Stacks every PV-origin flow (charge / export / curtail) plus the
    BESS-discharged export and any grid-charging draw.  The PV
    generation line is overlaid as the natural ceiling of the
    PV-origin stacks.

    Filename: ``daily_combined_<YYYY-MM-DD>.pdf``.
    """
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
    t_pad, ypads = pad_right_to_end(t, series, end)
    plot_stack_filtered(ax, t_pad, ypads, labels, step_post=True)

    t_pad, [pv_pad] = pad_right_to_end(t, [df["pv_kwh"].to_numpy()], end)
    line_if_nonzero(
        ax, t_pad, pv_pad, "PV generation",
        linewidth=1.8, step_post=True,
    )

    if show_titles():
        plt.title(
            f"Merchant — Daily Combined Flows"
            f"{title_prefix(get_scenario_label())} "
            f"— {pretty_date(date_str)}"
        )
    plt.xlabel("Time (HH:mm)")
    plt.ylabel("Energy (kWh)")
    _setup_day_axes(ax, start, end)
    apply_legend(ax, max_rows=2, custom_order=True, plot_type="daily")
    apply_universal_margins(ax, skip_x=True)
    save_figure_daily(out_dir / f"daily_combined_{date_str}.pdf", date_str)


def plot_daily_soc(
    res: pd.DataFrame, date_str: str, out_dir: Path, *,
    e_cap_kwh: float | None = None,
) -> None:
    """SOC trajectory for one day — SOC (%) on left, SOC (kWh) on right."""
    day = pd.to_datetime(date_str).date()
    df = res[res["timestamp"].dt.date == day]
    if df.empty:
        return
    soc_kwh = df["soc_kwh"].to_numpy(dtype=float)
    max_kwh = float(soc_kwh.max())
    if max_kwh <= 1e-9:
        # No BESS in the project — skip the plot.
        return
    start = pd.Timestamp(day)
    end = start + pd.Timedelta(days=1)

    if "soc_pct" in df.columns:
        soc_pct = df["soc_pct"].to_numpy(dtype=float)
        max_pct = float(df["soc_pct"].max())
    else:
        soc_pct = np.zeros_like(soc_kwh)
        max_pct = 0.0
    if max_pct > 1e-9:
        capacity_kwh = max_kwh / max_pct * 100.0
    elif e_cap_kwh is not None and e_cap_kwh > 0.0:
        capacity_kwh = float(e_cap_kwh)
        soc_pct = soc_kwh / capacity_kwh * 100.0
    else:
        capacity_kwh = max_kwh
        soc_pct = soc_kwh / capacity_kwh * 100.0

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    soc_colour = FINANCIAL_COLORS["net"]
    t_pad, [soc_pct_pad] = pad_right_to_end(df["timestamp"], [soc_pct], end)
    ax.plot(
        t_pad, soc_pct_pad, drawstyle="steps-post",
        color=soc_colour, linewidth=1.5, label="SOC",
    )

    ax.set_ylim(0.0, 100.0)
    ax.set_yticks(np.arange(0, 101, 10))
    ax.set_ylabel("SOC (%)")
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)

    ax2 = ax.twinx()
    ax2.set_ylim(0.0, capacity_kwh)
    ax2.set_yticks(np.linspace(0.0, capacity_kwh, 11))
    ax2.set_ylabel("SOC (kWh)")
    ax2.grid(False)

    if show_titles():
        plt.title(
            f"Merchant — Daily SOC{title_prefix(get_scenario_label())} "
            f"— {pretty_date(date_str)}"
        )
    ax.set_xlabel("Time (HH:mm)")
    _setup_day_axes(ax, start, end)
    apply_universal_margins(ax, skip_x=True, skip_y=True)
    apply_legend(ax, max_rows=2, custom_order=False, plot_type="daily")
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
