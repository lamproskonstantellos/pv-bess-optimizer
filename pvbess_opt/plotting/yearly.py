"""Yearly aggregate plots.

Three vnb-mode figures per calendar year, written directly into
``out_dir``:

* ``yearly_supply.pdf``
* ``yearly_surplus.pdf``
* ``yearly_combined.pdf``

Three merchant-mode figures per calendar year:

* ``yearly_dispatch.pdf`` — monthly exports + curtailment vs charging.
* ``yearly_soc.pdf`` — monthly min/mean/max SOC envelope.
* ``yearly_revenue.pdf`` — monthly DAM revenue minus charging cost.

Filenames intentionally do **not** carry a year suffix — the year is
already encoded in the ``out_dir`` parent (``05_energy_plots/<YYYY>/``).
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import ScalarFormatter

from ..config import COLORS, FINANCIAL_COLORS, XTICK_ROT
from .financial import _integer_year_axis
from .helpers import (
    bar_stacked_bins,
    edges_and_widths_yearly,
    line_if_nonzero,
    pad_line_to_bins_end,
    title_prefix,
    year_aggregate,
)
from .style import (
    apply_legend,
    apply_universal_margins,
    get_scenario_label,
    save_figure,
    show_titles,
)


def _setup_month_axis(ax, left: pd.Series, width_days) -> None:
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%Y"))
    ax.set_xlim(left.iloc[0],
                left.iloc[-1] + pd.to_timedelta(width_days[-1], unit="D"))
    plt.setp(ax.get_xticklabels(), rotation=XTICK_ROT, ha="right")
    ax.margins(x=0)


def _set_mwh_yaxis(ax, ylabel: str) -> None:
    ax.set_ylabel(ylabel)
    ax.yaxis.set_major_formatter(ScalarFormatter(useMathText=False))
    ax.yaxis.get_major_formatter().set_scientific(False)


def plot_yearly_supply(res: pd.DataFrame, year: int, out_dir: Path) -> None:
    """Stacked PV / BESS / Import → Load per month-of-year."""
    mth = year_aggregate(res, year)
    if mth.empty:
        return
    left, width_days = edges_and_widths_yearly(mth["month_start"])

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    bar_stacked_bins(
        ax, left, width_days,
        [
            mth["pv_to_load_kwh"] / 1000.0,
            mth["bess_dis_load_kwh"] / 1000.0,
            mth["grid_to_load_kwh"] / 1000.0,
        ],
        ["PV→Load", "BESS→Load", "Import→Load"],
    )
    t_pad, y_pad = pad_line_to_bins_end(
        left, width_days, (mth["load_kwh"] / 1000.0).to_numpy(),
    )
    line_if_nonzero(ax, t_pad, y_pad, "Load (demand)", linewidth=1.5,
                    step_post=True)

    _set_mwh_yaxis(ax, "Energy (MWh/month)")
    if show_titles():
        ax.set_title(
            f"Yearly Load Supply{title_prefix(get_scenario_label())} — {year}"
        )
    ax.set_xlabel("Month")
    _setup_month_axis(ax, left, width_days)
    apply_legend(ax, max_rows=2, custom_order=True, plot_type="yearly")
    apply_universal_margins(ax, skip_x=True)
    save_figure(out_dir / "yearly_supply.pdf")


def plot_yearly_surplus(res: pd.DataFrame, year: int, out_dir: Path) -> None:
    """Stacked surplus categories per month."""
    mth = year_aggregate(res, year)
    if mth.empty:
        return
    left, width_days = edges_and_widths_yearly(mth["month_start"])

    stacks = [
        mth["pv_to_bess_kwh"] / 1000.0,
        mth["pv_to_grid_kwh"] / 1000.0,
        mth["pv_curtail_kwh"] / 1000.0,
        mth["bess_dis_grid_kwh"] / 1000.0,
        mth["bess_charge_grid_kwh"] / 1000.0,
    ]
    labels = [
        "PV→BESS (charge)", "PV→Grid (export)", "PV→Curtailment",
        "BESS→Grid (export)", "Import→BESS (charge)",
    ]

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    bar_stacked_bins(ax, left, width_days, stacks, labels)
    _set_mwh_yaxis(ax, "Energy (MWh/month)")
    if show_titles():
        ax.set_title(
            f"Yearly Surplus Energy Flows{title_prefix(get_scenario_label())} "
            f"— {year}"
        )
    ax.set_xlabel("Month")
    _setup_month_axis(ax, left, width_days)
    apply_legend(ax, max_rows=2, custom_order=True, plot_type="yearly")
    apply_universal_margins(ax, skip_x=True)
    save_figure(out_dir / "yearly_surplus.pdf")


def plot_yearly_combined(res: pd.DataFrame, year: int, out_dir: Path) -> None:
    """Supply at base + surplus on top of the load line."""
    mth = year_aggregate(res, year)
    if mth.empty:
        return
    left, width_days = edges_and_widths_yearly(mth["month_start"])

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    bar_stacked_bins(
        ax, left, width_days,
        [
            mth["pv_to_load_kwh"] / 1000.0,
            mth["bess_dis_load_kwh"] / 1000.0,
            mth["grid_to_load_kwh"] / 1000.0,
        ],
        ["PV→Load", "BESS→Load", "Import→Load"],
    )
    bar_stacked_bins(
        ax, left, width_days,
        [
            mth["pv_to_bess_kwh"] / 1000.0,
            mth["pv_to_grid_kwh"] / 1000.0,
            mth["pv_curtail_kwh"] / 1000.0,
            mth["bess_dis_grid_kwh"] / 1000.0,
            mth["bess_charge_grid_kwh"] / 1000.0,
        ],
        [
            "PV→BESS (charge)", "PV→Grid (export)", "PV→Curtailment",
            "BESS→Grid (export)", "Import→BESS (charge)",
        ],
        bottom=(mth["load_kwh"] / 1000.0).to_numpy(),
    )
    t_pad, y_pad = pad_line_to_bins_end(
        left, width_days, (mth["load_kwh"] / 1000.0).to_numpy(),
    )
    line_if_nonzero(ax, t_pad, y_pad, "Load (demand)", linewidth=1.8,
                    step_post=True)

    _set_mwh_yaxis(ax, "Energy (MWh/month)")
    if show_titles():
        ax.set_title(
            f"Yearly Energy Flows{title_prefix(get_scenario_label())} — {year}"
        )
    ax.set_xlabel("Month")
    _setup_month_axis(ax, left, width_days)
    apply_legend(ax, max_rows=2, custom_order=True, plot_type="yearly")
    apply_universal_margins(ax, skip_x=True)
    save_figure(out_dir / "yearly_combined.pdf")


# ---------------------------------------------------------------------------
# Merchant-mode plots (no load)
# ---------------------------------------------------------------------------


def plot_yearly_dispatch(res: pd.DataFrame, year: int, out_dir: Path) -> None:
    """Monthly merchant dispatch: exports + curtailment vs charging."""
    mth = year_aggregate(res, year)
    if mth.empty:
        return
    left, width_days = edges_and_widths_yearly(mth["month_start"])

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    bar_stacked_bins(
        ax, left, width_days,
        [
            mth["pv_to_grid_kwh"] / 1000.0,
            mth["bess_dis_grid_kwh"] / 1000.0,
            mth["pv_curtail_kwh"] / 1000.0,
        ],
        ["PV→Grid (export)", "BESS→Grid (export)", "PV→Curtailment"],
    )
    bar_stacked_bins(
        ax, left, width_days,
        [
            -(mth["pv_to_bess_kwh"] / 1000.0),
            -(mth["bess_charge_grid_kwh"] / 1000.0),
        ],
        ["PV→BESS (charge)", "Import→BESS (charge)"],
    )
    ax.axhline(0.0, color="black", linewidth=0.6, alpha=0.6)
    _set_mwh_yaxis(ax, "Energy (MWh/month)")
    if show_titles():
        ax.set_title(
            f"Merchant — Yearly Dispatch{title_prefix(get_scenario_label())} "
            f"— {year}"
        )
    ax.set_xlabel("Month")
    _setup_month_axis(ax, left, width_days)
    apply_legend(ax, max_rows=2, custom_order=False, plot_type="yearly")
    apply_universal_margins(ax, skip_x=True)
    save_figure(out_dir / "yearly_dispatch.pdf")


def plot_yearly_combined_merchant(
    res: pd.DataFrame, year: int, out_dir: Path,
) -> None:
    """Yearly merchant-mode combined view.

    Monthly bars stacked as in :func:`plot_daily_combined_merchant`, with
    the PV generation line overlaid on top.

    Filename: ``yearly_combined.pdf``.
    """
    mth = year_aggregate(res, year)
    if mth.empty:
        return
    left, width_days = edges_and_widths_yearly(mth["month_start"])

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    bar_stacked_bins(
        ax, left, width_days,
        [
            mth["pv_to_bess_kwh"] / 1000.0,
            mth["pv_to_grid_kwh"] / 1000.0,
            mth["pv_curtail_kwh"] / 1000.0,
            mth["bess_dis_grid_kwh"] / 1000.0,
            mth["bess_charge_grid_kwh"] / 1000.0,
        ],
        [
            "PV→BESS (charge)", "PV→Grid (export)", "PV→Curtailment",
            "BESS→Grid (export)", "Import→BESS (charge)",
        ],
    )
    t_pad, y_pad = pad_line_to_bins_end(
        left, width_days, (mth["pv_kwh"] / 1000.0).to_numpy(),
    )
    line_if_nonzero(ax, t_pad, y_pad, "PV generation",
                    linewidth=1.8, step_post=True)

    _set_mwh_yaxis(ax, "Energy (MWh/month)")
    if show_titles():
        ax.set_title(
            f"Merchant — Yearly Combined Flows"
            f"{title_prefix(get_scenario_label())} — {year}"
        )
    ax.set_xlabel("Month")
    _setup_month_axis(ax, left, width_days)
    apply_legend(ax, max_rows=2, custom_order=True, plot_type="yearly")
    apply_universal_margins(ax, skip_x=True)
    save_figure(out_dir / "yearly_combined.pdf")


def plot_yearly_soc(res: pd.DataFrame, year: int, out_dir: Path) -> None:
    """Monthly min / mean / max SOC envelope for the calendar year.

    SOC (%) is drawn on the left axis (fixed 0–100), SOC (kWh) appears
    on the right axis with ticks proportional to the BESS capacity so
    the two scales line up on every grid line.
    """
    df = res[pd.to_datetime(res["timestamp"]).dt.year == year]
    if df.empty or "soc_kwh" not in df.columns:
        return
    max_kwh = float(df["soc_kwh"].max())
    if max_kwh <= 1e-9:
        return  # No BESS in the project.
    if "soc_pct" in df.columns:
        max_pct = float(df["soc_pct"].max())
    else:
        max_pct = 0.0
    if max_pct > 1e-9:
        capacity_kwh = max_kwh / max_pct * 100.0
    else:
        capacity_kwh = max_kwh

    monthly = (
        df.groupby(pd.to_datetime(df["timestamp"]).dt.to_period("M"))["soc_kwh"]
        .agg(["min", "mean", "max"]).reset_index()
    )
    monthly["month_start"] = monthly["timestamp"].dt.to_timestamp()
    monthly = monthly.sort_values("month_start").reset_index(drop=True)
    if monthly.empty:
        return

    monthly_min_pct = monthly["min"] / capacity_kwh * 100.0
    monthly_mean_pct = monthly["mean"] / capacity_kwh * 100.0
    monthly_max_pct = monthly["max"] / capacity_kwh * 100.0

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    soc_colour = FINANCIAL_COLORS["net"]
    ax.fill_between(
        monthly["month_start"], monthly_min_pct, monthly_max_pct,
        color=soc_colour, alpha=0.20, edgecolor=soc_colour,
        label="Monthly min-max",
    )
    ax.plot(
        monthly["month_start"], monthly_mean_pct,
        color=soc_colour, linewidth=1.5, linestyle="-",
        marker="o", markersize=3,
        label="Monthly mean",
    )
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%Y"))
    plt.setp(ax.get_xticklabels(), rotation=XTICK_ROT, ha="right")

    ax.set_ylim(0.0, 100.0)
    ax.set_yticks(np.arange(0, 101, 10))
    ax.set_ylabel("SOC (%)")
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)

    if show_titles():
        ax.set_title(
            f"Merchant — Yearly SOC{title_prefix(get_scenario_label())} "
            f"— {year}"
        )
    ax.set_xlabel("Month")

    ax2 = ax.twinx()
    ax2.set_ylim(0.0, capacity_kwh)
    ax2.set_yticks(np.linspace(0.0, capacity_kwh, 11))
    ax2.set_ylabel("SOC (kWh)")
    ax2.grid(False)

    apply_universal_margins(ax, skip_x=True, skip_y=True)
    apply_legend(ax, max_rows=2, custom_order=False, plot_type="yearly")
    save_figure(out_dir / "yearly_soc.pdf")


def plot_yearly_revenue(res: pd.DataFrame, year: int, out_dir: Path) -> None:
    """Monthly DAM revenue minus grid-charging cost."""
    df = res[pd.to_datetime(res["timestamp"]).dt.year == year].copy()
    if df.empty:
        return
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    cols = {
        "rev_pv": "profit_export_from_pv_eur",
        "rev_bess": "profit_export_from_bess_eur",
        "cost_grid": "expense_charge_bess_grid_eur",
    }
    monthly = df.groupby(df["timestamp"].dt.to_period("M")).agg(
        {c: "sum" for c in cols.values() if c in df.columns}
    ).reset_index()
    monthly["month_start"] = monthly["timestamp"].dt.to_timestamp()
    monthly = monthly.sort_values("month_start").reset_index(drop=True)
    if monthly.empty:
        return
    left, width_days = edges_and_widths_yearly(monthly["month_start"])

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    pos = []
    pos_labels = []
    if cols["rev_pv"] in monthly.columns:
        pos.append(monthly[cols["rev_pv"]].to_numpy(dtype=float))
        pos_labels.append("PV→Grid (revenue)")
    if cols["rev_bess"] in monthly.columns:
        pos.append(monthly[cols["rev_bess"]].to_numpy(dtype=float))
        pos_labels.append("BESS→Grid (revenue)")
    if pos:
        bar_stacked_bins(ax, left, width_days, pos, pos_labels)
    if cols["cost_grid"] in monthly.columns:
        bar_stacked_bins(
            ax, left, width_days,
            [-monthly[cols["cost_grid"]].to_numpy(dtype=float)],
            ["Import→BESS (cost)"],
        )
    ax.axhline(0.0, color="black", linewidth=0.6, alpha=0.6)
    if show_titles():
        ax.set_title(
            f"Merchant — Yearly Revenue{title_prefix(get_scenario_label())} "
            f"— {year}"
        )
    ax.set_xlabel("Month")
    ax.set_ylabel("EUR/month")
    _setup_month_axis(ax, left, width_days)
    apply_legend(ax, max_rows=2, custom_order=False, plot_type="yearly")
    apply_universal_margins(ax, skip_x=True)
    save_figure(out_dir / "yearly_revenue.pdf")


def plot_lifetime_summary(
    yearly_aggregate: pd.DataFrame, out_path: Path,
) -> None:
    """One-shot summary plot for ``05_energy_plots/lifetime_summary_*.pdf``.

    Plots the per-year MWh totals (PV generation, exports, imports,
    BESS round-trip) so a reader can scan the entire lifetime in a
    single figure.  Powered by
    :func:`pvbess_opt.lifetime.aggregate_lifetime_to_yearly`.
    """
    if yearly_aggregate.empty:
        return
    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    x = yearly_aggregate["calendar_year"].to_numpy(dtype=int)
    for column, color, label in [
        ("pv_generation_mwh", COLORS["PV→Load"], "PV generation"),
        ("export_total_mwh", COLORS["BESS→Grid (export)"], "Grid exports"),
        ("import_to_load_mwh", COLORS["Import→Load"], "Grid imports → load"),
        ("bess_discharge_mwh", COLORS["BESS→Load"], "BESS discharge"),
    ]:
        if column in yearly_aggregate.columns:
            ax.plot(
                x,
                yearly_aggregate[column].to_numpy(dtype=float),
                color=color, linewidth=1.5, marker="o", markersize=3,
                label=label,
            )
    ax.set_xlabel("Calendar year")
    _integer_year_axis(ax)
    ax.set_ylabel("Energy (MWh/year)")
    if show_titles():
        ax.set_title(
            f"Lifetime Energy Summary — {int(x[0])}-{int(x[-1])}"
        )
    ax.legend(loc="best", framealpha=0.9, fontsize=7)
    ax.grid(True, linestyle="--", alpha=0.5)
    apply_universal_margins(ax)
    save_figure(out_path)
