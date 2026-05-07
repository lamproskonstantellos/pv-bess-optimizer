"""Monthly aggregate plots.

Three vnb-mode figures per month, written directly into ``out_dir``:

* ``monthly_supply_<MM>.pdf``
* ``monthly_surplus_<MM>.pdf``
* ``monthly_combined_<MM>.pdf``

Three merchant-mode figures per month (added in v0.6):

* ``monthly_dispatch_<MM>.pdf`` — exports + curtailment vs charging.
* ``monthly_soc_<MM>.pdf`` — daily min/mean/max SOC envelope.
* ``monthly_revenue_<MM>.pdf`` — daily DAM revenue minus charging cost.

Filenames intentionally do **not** carry a scenario tag — the scenario
is encoded in the parent run-output directory.
"""

from __future__ import annotations

from calendar import month_name
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
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


# ---------------------------------------------------------------------------
# Merchant-mode plots (no load) — added in v0.6
# ---------------------------------------------------------------------------


def plot_monthly_dispatch(
    res: pd.DataFrame, month: int, out_dir: Path,
) -> None:
    """Stacked daily merchant dispatch: exports + curtailment vs charging."""
    g = month_aggregate(res, month)
    if g.empty:
        return
    left, width_days = edges_and_widths_monthly(g["date"])

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    bar_stacked_bins(
        ax, left, width_days,
        [
            g["pv_to_grid_kwh"] / 1000.0,
            g["bess_dis_grid_kwh"] / 1000.0,
            g["pv_curtail_kwh"] / 1000.0,
        ],
        ["PV→Grid (export)", "BESS→Grid (export)", "PV→Curtailment"],
    )
    bar_stacked_bins(
        ax, left, width_days,
        [
            -(g["pv_to_bess_kwh"] / 1000.0),
            -(g["bess_charge_grid_kwh"] / 1000.0),
        ],
        ["PV→BESS (charge)", "Import→BESS (charge)"],
    )
    ax.axhline(0.0, color="black", linewidth=0.6, alpha=0.6)
    _set_mwh_yaxis(ax, "Energy (MWh/day)")
    if show_titles():
        ax.set_title(
            f"Merchant — Monthly Dispatch{title_prefix(get_scenario_label())} "
            f"— {month_name[month]}"
        )
    ax.set_xlabel("Day")
    _setup_day_axis(ax, left, width_days)
    apply_legend(ax, max_rows=2, custom_order=False, plot_type="monthly")
    save_figure(out_dir / f"monthly_dispatch_{month:02d}.pdf")


def plot_monthly_soc(
    res: pd.DataFrame, month: int, out_dir: Path,
) -> None:
    """Daily min / mean / max SOC envelope across the calendar month."""
    df = res[res["timestamp"].dt.month == month]
    if df.empty or "soc_kwh" not in df.columns:
        return
    if float(df["soc_kwh"].max()) <= 1e-9:
        return  # No BESS in the project.
    daily = df.groupby(df["timestamp"].dt.date)["soc_kwh"].agg(
        ["min", "mean", "max"],
    ).reset_index().rename(columns={"timestamp": "date"})
    daily["date"] = pd.to_datetime(daily["date"])
    if daily.empty:
        return

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    ax.fill_between(
        daily["date"], daily["min"], daily["max"],
        color="#1565C0", alpha=0.25, label="Daily min-max",
    )
    ax.plot(
        daily["date"], daily["mean"],
        color="#1565C0", linewidth=1.5, marker="o", markersize=3,
        label="Daily mean",
    )
    if show_titles():
        ax.set_title(
            f"Merchant — Monthly SOC{title_prefix(get_scenario_label())} "
            f"— {month_name[month]}"
        )
    ax.set_xlabel("Day")
    ax.set_ylabel("SOC (kWh)")
    ax.legend(loc="best", framealpha=0.9, fontsize=7)
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    save_figure(out_dir / f"monthly_soc_{month:02d}.pdf")


def plot_monthly_revenue(
    res: pd.DataFrame, month: int, out_dir: Path,
) -> None:
    """Daily DAM revenue minus grid-charging cost."""
    df = res[res["timestamp"].dt.month == month]
    if df.empty:
        return
    cols = {
        "rev_pv": "profit_export_from_pv_eur",
        "rev_bess": "profit_export_from_bess_eur",
        "cost_grid": "expense_charge_bess_grid_eur",
    }
    daily = df.groupby(df["timestamp"].dt.date).agg(
        {c: "sum" for c in cols.values() if c in df.columns}
    ).reset_index().rename(columns={"timestamp": "date"})
    daily["date"] = pd.to_datetime(daily["date"])
    if daily.empty:
        return
    left, width_days = edges_and_widths_monthly(daily["date"])

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    pos = []
    pos_labels = []
    if cols["rev_pv"] in daily.columns:
        pos.append(daily[cols["rev_pv"]].to_numpy(dtype=float))
        pos_labels.append("PV→Grid (revenue)")
    if cols["rev_bess"] in daily.columns:
        pos.append(daily[cols["rev_bess"]].to_numpy(dtype=float))
        pos_labels.append("BESS→Grid (revenue)")
    if pos:
        bar_stacked_bins(ax, left, width_days, pos, pos_labels)
    if cols["cost_grid"] in daily.columns:
        bar_stacked_bins(
            ax, left, width_days,
            [-daily[cols["cost_grid"]].to_numpy(dtype=float)],
            ["Import→BESS (cost)"],
        )
    ax.axhline(0.0, color="black", linewidth=0.6, alpha=0.6)
    if show_titles():
        ax.set_title(
            f"Merchant — Monthly Revenue{title_prefix(get_scenario_label())} "
            f"— {month_name[month]}"
        )
    ax.set_xlabel("Day")
    ax.set_ylabel("EUR/day")
    _setup_day_axis(ax, left, width_days)
    apply_legend(ax, max_rows=2, custom_order=False, plot_type="monthly")
    save_figure(out_dir / f"monthly_revenue_{month:02d}.pdf")
