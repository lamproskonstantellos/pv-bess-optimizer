"""IEEE-styled financial plots.

Seven plots total:

* :func:`plot_cumulative_cashflow`  — cumulative undiscounted + discounted lines
* :func:`plot_yearly_cashflow_bars` — stacked yearly bars (revenue / opex / capex)
* :func:`plot_npv_waterfall`        — yearly contribution to total NPV
* :func:`plot_payback`              — cumulative cash-flow with simple + discounted markers
* :func:`plot_monthly_cashflow_year1` — Year-1 monthly bars
* :func:`plot_npv_tornado`          — sorted NPV tornado
* :func:`plot_irr_tornado`          — sorted IRR tornado (omits the discount-rate row)

EUR axes use the compact ``EUR 12.3M`` / ``EUR 45k`` formatter via
:func:`pvbess_opt.plotting._currency.euro_axis_formatter`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import MaxNLocator
from matplotlib.transforms import offset_copy

from ..config import FINANCIAL_COLORS
from ._currency import euro_axis_formatter, format_eur
from .style import save_figure, show_titles


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _calendar_axis(yearly_cf: pd.DataFrame) -> np.ndarray:
    if "calendar_year" in yearly_cf.columns:
        return yearly_cf["calendar_year"].to_numpy(dtype=int)
    return yearly_cf["project_year"].to_numpy(dtype=int)


def _start_end_years(yearly_cf: pd.DataFrame) -> tuple[int, int]:
    if "calendar_year" in yearly_cf.columns and len(yearly_cf) > 0:
        return (
            int(yearly_cf["calendar_year"].iloc[0]),
            int(yearly_cf["calendar_year"].iloc[-1]),
        )
    return (
        int(yearly_cf["project_year"].iloc[0]),
        int(yearly_cf["project_year"].iloc[-1]),
    )


def _operating_window_with_capex(
    yearly_cf: pd.DataFrame,
) -> tuple[int, int, int | None]:
    """Return ``(op_start, op_end, capex_year)`` for v0.6 title strings.

    ``op_start`` is the calendar year of Year 1 (first operating year).
    ``op_end`` is the calendar year of the last row.  ``capex_year`` is
    the calendar year of Year 0 — None when the frame contains only
    operating years (e.g. a sensitivity slice).
    """
    if "calendar_year" in yearly_cf.columns and len(yearly_cf) > 0:
        if "project_year" in yearly_cf.columns and (yearly_cf["project_year"] == 1).any():
            op_start = int(
                yearly_cf.loc[yearly_cf["project_year"] == 1, "calendar_year"].iloc[0]
            )
        else:
            op_start = int(yearly_cf["calendar_year"].iloc[0])
        op_end = int(yearly_cf["calendar_year"].iloc[-1])
        if "project_year" in yearly_cf.columns and (yearly_cf["project_year"] == 0).any():
            capex_year: int | None = int(
                yearly_cf.loc[yearly_cf["project_year"] == 0, "calendar_year"].iloc[0]
            )
        else:
            capex_year = None
        return op_start, op_end, capex_year
    s, e = _start_end_years(yearly_cf)
    return s, e, None


def _title_window(yearly_cf: pd.DataFrame) -> str:
    """Return the ``2026-2045 (CAPEX in 2025)`` title fragment."""
    op_start, op_end, capex_year = _operating_window_with_capex(yearly_cf)
    base = f"{op_start}-{op_end}"
    if capex_year is not None and capex_year != op_start:
        return f"{base} (CAPEX in {capex_year})"
    return base


def _maybe_set_title(ax, text: str) -> None:
    if show_titles():
        ax.set_title(text)


def _resolve_currency_format(econ: dict[str, Any] | None) -> str:
    if econ is None:
        return "auto"
    raw = str(econ.get("currency_format", "auto") or "auto").strip().lower()
    if raw not in ("auto", "millions", "raw"):
        return "auto"
    return raw


def _apply_eur_yaxis(ax, econ: dict[str, Any] | None) -> None:
    ax.yaxis.set_major_formatter(euro_axis_formatter(_resolve_currency_format(econ)))


def _apply_eur_xaxis(ax, econ: dict[str, Any] | None) -> None:
    ax.xaxis.set_major_formatter(euro_axis_formatter(_resolve_currency_format(econ)))


def _integer_year_axis(ax) -> None:
    """Force integer year ticks; subsample sensibly on long horizons."""
    ax.xaxis.set_major_locator(MaxNLocator(integer=True, prune=None, nbins=12))


# ---------------------------------------------------------------------------
# Cumulative cashflow
# ---------------------------------------------------------------------------


def plot_cumulative_cashflow(
    yearly_cf: pd.DataFrame, out_path: Path,
    *, econ: dict[str, Any] | None = None,
) -> Path:
    """Cumulative undiscounted (solid) + discounted (dashed) cash-flow."""
    out_path = Path(out_path)
    years = _calendar_axis(yearly_cf)
    cum = yearly_cf["cumulative_cf_eur"].to_numpy(dtype=float)
    cum_disc = yearly_cf["cumulative_dcf_eur"].to_numpy(dtype=float)

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    ax.plot(
        years, cum,
        color=FINANCIAL_COLORS["net"], linewidth=1.5, label="Cumulative cash-flow",
    )
    ax.plot(
        years, cum_disc,
        color=FINANCIAL_COLORS["discounted"], linewidth=1.5, linestyle="--",
        label="Cumulative discounted cash-flow",
    )
    ax.axhline(0.0, color="grey", linewidth=0.8, alpha=0.6)

    for series, colour, label in (
        (cum, FINANCIAL_COLORS["net"], "Simple payback"),
        (cum_disc, FINANCIAL_COLORS["discounted"], "Discounted payback"),
    ):
        crossing = np.where(series >= 0.0)[0]
        if crossing.size > 0 and crossing[0] >= 1:
            x = float(years[crossing[0]])
            ax.axvline(
                x, color=colour, linewidth=0.8, linestyle=":",
                alpha=0.7, label=label,
            )

    ax.set_xlabel("Calendar year" if "calendar_year" in yearly_cf.columns
                  else "Project year")
    _integer_year_axis(ax)
    ax.set_ylabel("EUR")
    _apply_eur_yaxis(ax, econ)
    _maybe_set_title(ax, f"Cumulative Cash-flow — {_title_window(yearly_cf)}")
    ax.legend(loc="best", framealpha=0.9)
    ax.grid(True, linestyle="--", alpha=0.5)
    return save_figure(out_path)


# ---------------------------------------------------------------------------
# Yearly cashflow bars
# ---------------------------------------------------------------------------


def plot_yearly_cashflow_bars(
    yearly_cf: pd.DataFrame, out_path: Path,
    *, econ: dict[str, Any] | None = None,
) -> Path:
    """Stacked yearly bars for revenue (+), opex (-), capex (-), net line."""
    out_path = Path(out_path)
    years = _calendar_axis(yearly_cf)
    revenue = yearly_cf["revenue_eur"].to_numpy(dtype=float)
    opex = yearly_cf["opex_eur"].to_numpy(dtype=float)  # negative
    if "devex_eur" in yearly_cf.columns:
        devex = yearly_cf["devex_eur"].to_numpy(dtype=float)  # negative
    else:
        devex = np.zeros_like(revenue)
    capex = yearly_cf["capex_eur"].to_numpy(dtype=float)  # negative
    net = yearly_cf["net_cashflow_eur"].to_numpy(dtype=float)

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    width = 0.8
    ax.bar(years, revenue, width=width, color=FINANCIAL_COLORS["revenue"],
           edgecolor="black", linewidth=0.4, label="Revenue")
    ax.bar(years, opex, width=width, color=FINANCIAL_COLORS["opex"],
           edgecolor="black", linewidth=0.4, label="OPEX")
    # Stack DEVEX at the bottom of the negative Year-0 stack and put CAPEX
    # on top of it so both segments remain visually identifiable.  Without
    # the ``bottom=devex`` arg matplotlib overlays the CAPEX bar on the
    # DEVEX bar at the same x and the smaller DEVEX segment disappears
    # inside the CAPEX block.
    ax.bar(years, devex, width=width, color=FINANCIAL_COLORS["devex"],
           edgecolor="black", linewidth=0.4, label="DEVEX")
    ax.bar(years, capex, width=width, bottom=devex,
           color=FINANCIAL_COLORS["capex"],
           edgecolor="black", linewidth=0.4, label="CAPEX")
    ax.plot(years, net, color=FINANCIAL_COLORS["net"], linewidth=1.5,
            marker="o", markersize=3, label="Net cash-flow")
    ax.axhline(0.0, color="black", linewidth=0.8)

    ax.set_xlabel(
        "Calendar year" if "calendar_year" in yearly_cf.columns
        else "Project year"
    )
    _integer_year_axis(ax)
    ax.set_ylabel("EUR")
    _apply_eur_yaxis(ax, econ)
    _maybe_set_title(ax, f"Yearly Cash-flow Stack — {_title_window(yearly_cf)}")
    # Pin to the lower right — the post-payback region is roughly
    # horizontal there, so the legend stays clear of the bars and the
    # Year-0 CAPEX stack on the left.
    ax.legend(loc="lower right", framealpha=0.9, ncol=2, fontsize=7)
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    return save_figure(out_path)


# ---------------------------------------------------------------------------
# NPV waterfall
# ---------------------------------------------------------------------------


def plot_npv_waterfall(
    yearly_cf: pd.DataFrame, out_path: Path,
    *, econ: dict[str, Any] | None = None,
) -> Path:
    """Year-by-year contribution to total NPV (waterfall stacked bar).

    Year 0 is rendered as two stacked bars at the same x coordinate —
    DEVEX (purple) at the bottom of the negative stack, CAPEX (red)
    above it — both labelled in-place.  Years 1..N keep their green /
    red sign-coded incremental bars unchanged.
    """
    out_path = Path(out_path)
    years = _calendar_axis(yearly_cf)
    disc = yearly_cf["discounted_cf_eur"].to_numpy(dtype=float)
    cum = np.cumsum(disc)
    fmt_mode = _resolve_currency_format(econ)

    has_pyear = "project_year" in yearly_cf.columns
    y0_mask = (
        (yearly_cf["project_year"] == 0).to_numpy()
        if has_pyear else np.zeros(len(disc), dtype=bool)
    )

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    bottoms = np.concatenate([[0.0], cum[:-1]])

    # Operating-year bars (Years 1..N) — sign-coded green / red.
    op_mask = ~y0_mask
    op_colours = [
        FINANCIAL_COLORS["revenue"] if d >= 0 else FINANCIAL_COLORS["capex"]
        for d in disc[op_mask]
    ]
    ax.bar(
        years[op_mask], disc[op_mask], bottom=bottoms[op_mask],
        color=op_colours, edgecolor="black", linewidth=0.4,
    )

    # Year-0: split DEVEX / CAPEX into two stacked bars at the same x.
    disc_capex_y0 = 0.0
    disc_devex_y0 = 0.0
    if y0_mask.any():
        y0_idx = int(np.argmax(y0_mask))
        df = yearly_cf
        disc_factor = (
            float(df.loc[df["project_year"] == 0, "discount_factor"].iloc[0])
            if "discount_factor" in df.columns else 1.0
        )
        capex_y0_raw = float(
            df.loc[df["project_year"] == 0, "capex_eur"].iloc[0]
        )
        devex_y0_raw = float(
            df.loc[df["project_year"] == 0, "devex_eur"].iloc[0]
            if "devex_eur" in df.columns else 0.0
        )
        disc_capex_y0 = capex_y0_raw * disc_factor
        disc_devex_y0 = devex_y0_raw * disc_factor
        x0 = float(years[y0_idx])
        bot0 = float(bottoms[y0_idx])
        if abs(disc_devex_y0) > 1e-9:
            ax.bar(
                [x0], [disc_devex_y0], bottom=[bot0],
                color=FINANCIAL_COLORS["devex"],
                edgecolor="black", linewidth=0.4, label="DEVEX",
            )
        if abs(disc_capex_y0) > 1e-9:
            ax.bar(
                [x0], [disc_capex_y0], bottom=[bot0 + disc_devex_y0],
                color=FINANCIAL_COLORS["capex"],
                edgecolor="black", linewidth=0.4, label="CAPEX",
            )

    ax.plot(
        years, cum,
        color=FINANCIAL_COLORS["discounted"], linewidth=1.5,
        marker="o", markersize=3, label="Cumulative NPV",
    )
    ax.axhline(0.0, color="black", linewidth=0.6)

    # Two right-aligned text annotations at the midpoints of the Year-0
    # DEVEX and CAPEX segments — keeps each label glued to its own bar
    # instead of merging into a single "CAPEX + DEVEX" caption.
    if y0_mask.any():
        y0_idx = int(np.argmax(y0_mask))
        bot0 = float(bottoms[y0_idx])
        trans_left = offset_copy(
            ax.transData, fig=ax.figure, x=-5, y=0, units="points",
        )
        if abs(disc_devex_y0) > 1e-9:
            devex_mid = bot0 + 0.5 * disc_devex_y0
            ax.text(
                float(years[y0_idx]), devex_mid, "DEVEX",
                ha="right", va="center",
                fontsize=7, color=FINANCIAL_COLORS["devex"],
                transform=trans_left, clip_on=False,
            )
        if abs(disc_capex_y0) > 1e-9:
            capex_mid = bot0 + disc_devex_y0 + 0.5 * disc_capex_y0
            ax.text(
                float(years[y0_idx]), capex_mid, "CAPEX",
                ha="right", va="center",
                fontsize=7, color=FINANCIAL_COLORS["capex"],
                transform=trans_left, clip_on=False,
            )

    final_npv = float(cum[-1]) if len(cum) > 0 else 0.0
    trans_right = offset_copy(
        ax.transData, fig=ax.figure, x=+5, y=0, units="points",
    )
    ax.text(
        float(years[-1]), final_npv,
        f"NPV = {format_eur(final_npv, fmt_mode)}",
        ha="left", va="center", fontsize=7,
        transform=trans_right, clip_on=False,
    )

    # Extend the x-axis a touch so the right-edge NPV label is fully visible.
    xmin, xmax = ax.get_xlim()
    ax.set_xlim(xmin, xmax + 1.5)

    ax.set_xlabel(
        "Calendar year" if "calendar_year" in yearly_cf.columns
        else "Project year"
    )
    _integer_year_axis(ax)
    ax.set_ylabel("Discounted EUR")
    _apply_eur_yaxis(ax, econ)
    _maybe_set_title(ax, f"NPV Waterfall — {_title_window(yearly_cf)}")
    ax.legend(loc="best", framealpha=0.9)
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    return save_figure(out_path)


# ---------------------------------------------------------------------------
# Payback visualisation
# ---------------------------------------------------------------------------


def plot_payback(
    yearly_cf: pd.DataFrame,
    out_path: Path,
    *,
    simple_payback_years: float | None = None,
    discounted_payback_years: float | None = None,
    econ: dict[str, Any] | None = None,
) -> Path:
    """Cumulative cash-flow with simple + discounted payback markers."""
    out_path = Path(out_path)
    years = _calendar_axis(yearly_cf)
    cum = yearly_cf["cumulative_cf_eur"].to_numpy(dtype=float)
    cum_disc = yearly_cf["cumulative_dcf_eur"].to_numpy(dtype=float)

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    ax.plot(years, cum, color=FINANCIAL_COLORS["net"], linewidth=1.5,
            label="Cumulative cash-flow")
    ax.plot(years, cum_disc, color=FINANCIAL_COLORS["discounted"], linewidth=1.5,
            linestyle="--", label="Cumulative discounted cash-flow")
    ax.axhline(0.0, color="black", linewidth=0.6)

    using_calendar = "calendar_year" in yearly_cf.columns
    # Year-0 row's calendar value is the new "base year" anchor: a payback
    # of N years lands at calendar (capex_year + N) = (project_start_year - 1
    # + N), one step earlier than the v0.5 mapping.
    base_year = float(years[0]) if using_calendar else 0.0

    def _to_axis(payback: float) -> float:
        if using_calendar:
            return base_year + payback
        return payback

    if simple_payback_years is not None and not np.isnan(simple_payback_years):
        x = _to_axis(float(simple_payback_years))
        ax.axvline(
            x, color=FINANCIAL_COLORS["net"], linewidth=0.8, linestyle=":",
            alpha=0.8,
            label=f"Simple payback: {simple_payback_years:.1f} yr",
        )
        ax.scatter([x], [0.0], color=FINANCIAL_COLORS["net"], s=20, zorder=5)
    if (
        discounted_payback_years is not None
        and not np.isnan(discounted_payback_years)
    ):
        x = _to_axis(float(discounted_payback_years))
        ax.axvline(
            x, color=FINANCIAL_COLORS["discounted"], linewidth=0.8, linestyle=":",
            alpha=0.8,
            label=f"Discounted payback: {discounted_payback_years:.1f} yr",
        )
        ax.scatter([x], [0.0], color=FINANCIAL_COLORS["discounted"], s=20, zorder=5)

    ax.set_xlabel("Calendar year" if using_calendar else "Project year")
    _integer_year_axis(ax)
    ax.set_ylabel("EUR")
    _apply_eur_yaxis(ax, econ)
    _maybe_set_title(ax, f"Payback Visualisation — {_title_window(yearly_cf)}")
    ax.legend(loc="best", framealpha=0.9)
    ax.grid(True, linestyle="--", alpha=0.5)
    return save_figure(out_path)


# ---------------------------------------------------------------------------
# Year-1 monthly cashflow
# ---------------------------------------------------------------------------


def plot_monthly_cashflow_year1(
    monthly_cf: pd.DataFrame, out_path: Path,
    *, econ: dict[str, Any] | None = None,
) -> Path:
    """Year-1 monthly stacked bars showing the seasonality of cash-flows."""
    out_path = Path(out_path)
    yr_col = (
        "project_year" if "project_year" in monthly_cf.columns else "year"
    )
    sub = monthly_cf.loc[monthly_cf[yr_col] == 1].sort_values("period")
    months = sub["period"].astype(int).to_numpy()
    revenue = sub["revenue_eur"].astype(float).to_numpy()
    opex = sub["opex_eur"].astype(float).to_numpy()
    net = sub["net_cashflow_eur"].astype(float).to_numpy()

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    ax.bar(months, revenue, color=FINANCIAL_COLORS["revenue"],
           edgecolor="black", linewidth=0.4, label="Revenue")
    ax.bar(months, opex, color=FINANCIAL_COLORS["opex"],
           edgecolor="black", linewidth=0.4, label="OPEX")
    ax.plot(months, net, color=FINANCIAL_COLORS["net"], linewidth=1.5,
            marker="o", markersize=4, label="Net")
    ax.axhline(0.0, color="black", linewidth=0.6)
    ax.set_xticks(np.arange(1, 13))
    ax.set_xlabel("Month")
    ax.set_ylabel("EUR")
    _apply_eur_yaxis(ax, econ)

    if "calendar_year" in monthly_cf.columns and not sub.empty:
        cy = int(sub["calendar_year"].iloc[0])
        _maybe_set_title(ax, f"Year-1 Monthly Cash-flow — {cy}")
    else:
        _maybe_set_title(ax, "Year-1 Monthly Cash-flow")

    # Reorder the legend so Net comes last (Revenue / OPEX / Net) and
    # drop to two columns to avoid the cramped three-column layout.
    handles, labels = ax.get_legend_handles_labels()
    ordered = [(h, lbl)
               for target in ("Revenue", "OPEX", "Net")
               for h, lbl in zip(handles, labels) if lbl == target]
    if ordered:
        h_ord, l_ord = zip(*ordered)
        ax.legend(h_ord, l_ord, loc="best",
                  framealpha=0.9, ncol=2, fontsize=7)
    else:
        ax.legend(loc="best", framealpha=0.9, ncol=2, fontsize=7)
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    return save_figure(out_path)


# ---------------------------------------------------------------------------
# Tornado plots
# ---------------------------------------------------------------------------


def _build_tornado_pivot(
    sens_df: pd.DataFrame,
    metric: str,
    base_value: float,
) -> pd.DataFrame:
    """Pivot sens_df on (label, scenario) and add an ``impact`` column."""
    pivot = sens_df.pivot_table(
        index="label", columns="scenario", values=metric, aggfunc="first",
    )
    if "low" not in pivot.columns:
        pivot["low"] = base_value
    if "high" not in pivot.columns:
        pivot["high"] = base_value
    pivot["impact"] = (pivot["high"] - pivot["low"]).abs()
    return pivot


def _dumbbell_plot(
    pivot: pd.DataFrame,
    base_value: float,
    out_path: Path,
    *,
    title: str,
    xlabel: str,
    value_formatter: Callable[[float], str],
    drop_labels: tuple[str, ...] = (),
    apply_eur_xaxis: bool = False,
    econ: dict[str, Any] | None = None,
) -> Path:
    """Shared dumbbell renderer for NPV and IRR sensitivity tornadoes.

    Each driver becomes a horizontal segment running from ``low`` to
    ``high``, red on the side below ``base_value`` and green above, with
    filled circle markers at each endpoint and bbox-wrapped numeric
    labels offset above the line.  Bars are sorted by absolute impact
    (largest at the top).
    """
    out_path = Path(out_path)

    if pivot.empty:
        plt.figure(figsize=(7, 4))
        ax = plt.gca()
        ax.text(0.5, 0.5, "Sensitivity disabled or empty.",
                ha="center", va="center", fontsize=10,
                transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])
        return save_figure(out_path)

    drop_set = {label.strip().lower() for label in drop_labels}
    if drop_set:
        keep_mask = ~pivot.index.str.strip().str.lower().isin(drop_set)
        pivot = pivot.loc[keep_mask]
    pivot = pivot.loc[pivot["impact"] > 1.0e-9]
    pivot = pivot.sort_values("impact", ascending=True)

    if pivot.empty:
        plt.figure(figsize=(7, 4))
        ax = plt.gca()
        ax.text(0.5, 0.5, "No drivers with non-zero impact.",
                ha="center", va="center", fontsize=10,
                transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])
        return save_figure(out_path)

    labels = pivot.index.tolist()
    y_pos = np.arange(len(labels))
    lows = pivot["low"].astype(float).to_numpy()
    highs = pivot["high"].astype(float).to_numpy()

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    ax.axvline(
        base_value, color="black", linewidth=0.8, linestyle="--",
        alpha=0.6, label=f"Base = {value_formatter(base_value)}",
    )

    red = FINANCIAL_COLORS["tornado_neg"]
    green = FINANCIAL_COLORS["tornado_pos"]

    for i, (low, high) in enumerate(zip(lows, highs)):
        left, right = sorted((low, high))
        if right <= base_value:
            colour_left = colour_right = red
        elif left >= base_value:
            colour_left = colour_right = green
        else:
            colour_left, colour_right = red, green
            ax.plot(
                [left, base_value], [i, i],
                color=red, linewidth=2.0, solid_capstyle="round",
            )
            ax.plot(
                [base_value, right], [i, i],
                color=green, linewidth=2.0, solid_capstyle="round",
            )
            ax.scatter([left], [i], s=64, color=colour_left,
                       edgecolor="black", linewidth=0.4, zorder=5)
            ax.scatter([right], [i], s=64, color=colour_right,
                       edgecolor="black", linewidth=0.4, zorder=5)
            _annotate_dumbbell_endpoints(
                ax, left, right, i, value_formatter,
            )
            continue
        # Same-side branch.
        ax.plot(
            [left, right], [i, i],
            color=colour_left, linewidth=2.0, solid_capstyle="round",
        )
        ax.scatter([left, right], [i, i], s=64, color=colour_left,
                   edgecolor="black", linewidth=0.4, zorder=5)
        _annotate_dumbbell_endpoints(
            ax, left, right, i, value_formatter,
        )

    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels)
    ax.set_xlabel(xlabel)
    if apply_eur_xaxis:
        _apply_eur_xaxis(ax, econ)
    xmin, xmax = ax.get_xlim()
    pad = 0.08 * (xmax - xmin) if xmax > xmin else 1.0
    ax.set_xlim(xmin - pad, xmax + pad)
    ax.set_ylim(-0.6, len(labels) - 0.4)
    _maybe_set_title(ax, title)
    ax.legend(loc="lower right", framealpha=0.9, fontsize=7)
    ax.grid(True, axis="x", linestyle="--", alpha=0.5)

    return save_figure(out_path)


def _annotate_dumbbell_endpoints(
    ax,
    left: float,
    right: float,
    row: int,
    value_formatter: Callable[[float], str],
) -> None:
    """Print each endpoint label at its actual x-position so the text
    value always matches the x-axis coordinate.

    ``left`` and ``right`` are the *sorted* endpoint coordinates (left
    <= right); we label them with their own numeric values rather than
    re-using ``low`` / ``high`` from the scenario direction, which can
    swap when a "low" scenario actually produces the larger metric
    (e.g. low CAPEX → higher IRR).
    """
    above = offset_copy(ax.transData, fig=ax.figure, x=0, y=10, units="points")
    bbox_kwargs = {
        "facecolor": "white", "edgecolor": "grey", "alpha": 0.8,
        "linewidth": 0.5, "boxstyle": "round,pad=0.15",
    }
    ax.text(
        left, row, value_formatter(left),
        ha="left", va="bottom", fontsize=7, transform=above,
        bbox=bbox_kwargs,
    )
    ax.text(
        right, row, value_formatter(right),
        ha="right", va="bottom", fontsize=7, transform=above,
        bbox=bbox_kwargs,
    )


def _econ_title_window(econ: dict[str, Any]) -> str:
    """Build the ``2026-2045 (CAPEX in 2025)`` fragment from the econ dict."""
    start = int(econ.get("project_start_year", 0) or 0)
    n = int(econ.get("project_lifecycle_years", 0) or 0)
    if not start or not n:
        return ""
    end = start + n - 1
    capex_year = start - 1
    return f"{start}-{end} (CAPEX in {capex_year})"


def plot_npv_tornado(
    sens_df: pd.DataFrame,
    base_kpis: dict[str, Any],
    econ: dict[str, Any],
    out_path: Path,
) -> Path:
    """Sorted NPV tornado, dumbbell layout matching the IRR plot."""
    base_npv = float(base_kpis.get("npv_eur", 0.0))
    window = _econ_title_window(econ)
    title = f"NPV Sensitivity — {window}" if window else "NPV Sensitivity"
    fmt_mode = _resolve_currency_format(econ)
    if sens_df.empty:
        return _dumbbell_plot(
            pd.DataFrame(), base_npv, out_path,
            title=title, xlabel="NPV (EUR)",
            value_formatter=lambda v: format_eur(float(v), fmt_mode),
            apply_eur_xaxis=True, econ=econ,
        )
    pivot = _build_tornado_pivot(sens_df, "npv_eur", base_npv)
    return _dumbbell_plot(
        pivot, base_npv, out_path,
        title=title,
        xlabel="NPV (EUR)",
        value_formatter=lambda v: format_eur(float(v), fmt_mode),
        apply_eur_xaxis=True,
        econ=econ,
    )


def plot_irr_tornado(
    sens_df: pd.DataFrame,
    base_kpis: dict[str, Any],
    econ: dict[str, Any],
    out_path: Path,
) -> Path:
    """Sorted IRR tornado, dumbbell layout.

    The IRR is by definition the discount rate that zeroes the NPV, so
    varying the discount rate does not move the IRR — that row is
    filtered out silently before the plot is drawn.
    """
    base_irr = float(base_kpis.get("irr_pct", 0.0) or 0.0)
    window = _econ_title_window(econ)
    title = f"IRR Sensitivity — {window}" if window else "IRR Sensitivity"
    if sens_df.empty:
        return _dumbbell_plot(
            pd.DataFrame(), base_irr, out_path,
            title=title, xlabel="IRR (%)",
            value_formatter=lambda v: f"{v:.1f}%",
            drop_labels=("Discount rate",),
        )
    pivot = _build_tornado_pivot(sens_df, "irr_pct", base_irr)
    return _dumbbell_plot(
        pivot, base_irr, out_path,
        title=title,
        xlabel="IRR (%)",
        value_formatter=lambda v: f"{v:.1f}%",
        drop_labels=("Discount rate",),
    )
