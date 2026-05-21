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

import logging
from pathlib import Path
from typing import Any, Callable

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import MaxNLocator

from ..config import FINANCIAL_COLORS, apply_financial_legend, financial_color
from ..sensitivity import DriverSensitivity, build_driver_sensitivities
from ._currency import (
    euro_axis_formatter,
    format_eur,
    resolve_currency_format as _resolve_currency_format,
)
from .style import (
    annotate_value_safe,
    apply_fine_ticks,
    apply_universal_margins,
    empty_placeholder,
    save_figure,
    show_titles,
)

logger = logging.getLogger(__name__)


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
    """Return ``(op_start, op_end, capex_year)`` for title strings.

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
        color=financial_color("Cumulative cash-flow"),
        linewidth=1.5, label="Cumulative cash-flow",
    )
    ax.plot(
        years, cum_disc,
        color=financial_color("Cumulative discounted cash-flow"),
        linewidth=1.5, linestyle="--",
        label="Cumulative discounted cash-flow",
    )
    ax.axhline(0.0, color="grey", linewidth=0.8, alpha=0.6)

    ax.set_xlabel("Calendar year" if "calendar_year" in yearly_cf.columns
                  else "Project year")
    _integer_year_axis(ax)
    ax.set_ylabel("EUR")
    _apply_eur_yaxis(ax, econ)
    _maybe_set_title(ax, f"Cumulative Cash-flow — {_title_window(yearly_cf)}")
    apply_financial_legend(ax)
    ax.grid(True, linestyle="--", alpha=0.5)
    apply_universal_margins(ax)
    apply_fine_ticks(ax)
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
    ax.bar(years, revenue, width=width, color=financial_color("Revenue"),
           edgecolor="black", linewidth=0.4, label="Revenue")
    ax.bar(years, opex, width=width, color=financial_color("OPEX"),
           edgecolor="black", linewidth=0.4, label="OPEX")
    # Stack DEVEX at the bottom of the negative Year-0 stack and put CAPEX
    # on top of it so both segments remain visually identifiable.  Without
    # the ``bottom=devex`` arg matplotlib overlays the CAPEX bar on the
    # DEVEX bar at the same x and the smaller DEVEX segment disappears
    # inside the CAPEX block.
    ax.bar(years, devex, width=width, color=financial_color("DEVEX"),
           edgecolor="black", linewidth=0.4, label="DEVEX")
    ax.bar(years, capex, width=width, bottom=devex,
           color=financial_color("CAPEX"),
           edgecolor="black", linewidth=0.4, label="CAPEX")
    ax.plot(years, net, color=financial_color("Net cash-flow"), linewidth=1.5,
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
    apply_financial_legend(ax, loc="lower right")
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    apply_universal_margins(ax)
    apply_fine_ticks(ax)
    return save_figure(out_path)


# ---------------------------------------------------------------------------
# NPV waterfall
# ---------------------------------------------------------------------------


def plot_npv_waterfall(
    yearly_cf: pd.DataFrame, out_path: Path,
    *, econ: dict[str, Any] | None = None,
) -> Path:
    """Year-by-year contribution to NPV — discounted analogue of
    ``plot_yearly_cashflow_bars``.

    The morphology mirrors :func:`plot_yearly_cashflow_bars` exactly so
    the two plots can be read side by side: stacked Revenue (+) / OPEX
    (-) / DEVEX (-) / CAPEX (-) bars per year, a ``Net cash-flow``
    marker line, and one extra ``Cumulative NPV`` line overlaid.  All
    values are discounted to Year 0.
    """
    out_path = Path(out_path)
    years = _calendar_axis(yearly_cf)
    disc_factor = yearly_cf["discount_factor"].astype(float).to_numpy()
    revenue_disc = yearly_cf["revenue_eur"].astype(float).to_numpy() * disc_factor
    opex_disc = yearly_cf["opex_eur"].astype(float).to_numpy() * disc_factor
    if "devex_eur" in yearly_cf.columns:
        devex_disc = (
            yearly_cf["devex_eur"].astype(float).to_numpy() * disc_factor
        )
    else:
        devex_disc = np.zeros_like(revenue_disc)
    capex_disc = yearly_cf["capex_eur"].astype(float).to_numpy() * disc_factor
    net_disc = yearly_cf["discounted_cf_eur"].astype(float).to_numpy()
    cum_disc = np.cumsum(net_disc)

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    width = 0.8
    ax.bar(
        years, revenue_disc, width=width,
        color=financial_color("Revenue"), edgecolor="black",
        linewidth=0.4, label="Revenue",
    )
    ax.bar(
        years, opex_disc, width=width,
        color=financial_color("OPEX"), edgecolor="black",
        linewidth=0.4, label="OPEX",
    )
    # DEVEX at the bottom of the Year-0 negative stack, CAPEX on top of
    # it — mirrors the placement in plot_yearly_cashflow_bars so the
    # smaller DEVEX segment stays visible.
    ax.bar(
        years, devex_disc, width=width,
        color=financial_color("DEVEX"), edgecolor="black",
        linewidth=0.4, label="DEVEX",
    )
    ax.bar(
        years, capex_disc, width=width, bottom=devex_disc,
        color=financial_color("CAPEX"), edgecolor="black",
        linewidth=0.4, label="CAPEX",
    )

    ax.plot(
        years, net_disc, color=financial_color("Net cash-flow"), linewidth=1.5,
        marker="o", markersize=3, label="Net cash-flow",
    )
    ax.plot(
        years, cum_disc,
        color=financial_color("Cumulative NPV"), linewidth=1.5,
        marker="o", markersize=3, label="Cumulative NPV",
    )
    ax.axhline(0.0, color="black", linewidth=0.8)

    ax.set_xlabel(
        "Calendar year" if "calendar_year" in yearly_cf.columns
        else "Project year"
    )
    _integer_year_axis(ax)
    ax.set_ylabel("Discounted EUR")
    _apply_eur_yaxis(ax, econ)
    _maybe_set_title(ax, f"NPV Waterfall — {_title_window(yearly_cf)}")
    apply_financial_legend(ax, loc="lower right")
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    apply_universal_margins(ax)
    apply_fine_ticks(ax)
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
    ax.plot(years, cum, color=financial_color("Cumulative cash-flow"),
            linewidth=1.5, label="Cumulative cash-flow")
    ax.plot(years, cum_disc, color=financial_color("Cumulative discounted cash-flow"),
            linewidth=1.5, linestyle="--",
            label="Cumulative discounted cash-flow")
    ax.axhline(0.0, color="black", linewidth=0.6)

    using_calendar = "calendar_year" in yearly_cf.columns
    # Year-0 row's calendar value is the new "base year" anchor: a payback
    # of N years lands at calendar (capex_year + N) = (project_start_year - 1
    # + N).
    base_year = float(years[0]) if using_calendar else 0.0

    def _to_axis(payback: float) -> float:
        if using_calendar:
            return base_year + payback
        return payback

    if simple_payback_years is not None and not np.isnan(simple_payback_years):
        x = _to_axis(float(simple_payback_years))
        # Label is year-annotated for legend readability; canonical
        # ordering is recovered by apply_financial_legend's prefix match.
        ax.axvline(
            x, color=financial_color("Simple payback"),
            linewidth=0.8, linestyle=":", alpha=0.8,
            label=f"Simple payback: {simple_payback_years:.1f} yr",
        )
        ax.scatter(
            [x], [0.0], color=financial_color("Simple payback"),
            s=20, zorder=5,
        )
    if (
        discounted_payback_years is not None
        and not np.isnan(discounted_payback_years)
    ):
        x = _to_axis(float(discounted_payback_years))
        ax.axvline(
            x, color=financial_color("Discounted payback"),
            linewidth=0.8, linestyle=":", alpha=0.8,
            label=f"Discounted payback: {discounted_payback_years:.1f} yr",
        )
        ax.scatter(
            [x], [0.0], color=financial_color("Discounted payback"),
            s=20, zorder=5,
        )

    ax.set_xlabel("Calendar year" if using_calendar else "Project year")
    _integer_year_axis(ax)
    ax.set_ylabel("EUR")
    _apply_eur_yaxis(ax, econ)
    _maybe_set_title(ax, f"Payback Visualisation — {_title_window(yearly_cf)}")
    apply_financial_legend(ax)
    ax.grid(True, linestyle="--", alpha=0.5)
    apply_universal_margins(ax)
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
    ax.bar(months, revenue, color=financial_color("Revenue"),
           edgecolor="black", linewidth=0.4, label="Revenue")
    ax.bar(months, opex, color=financial_color("OPEX"),
           edgecolor="black", linewidth=0.4, label="OPEX")
    ax.plot(months, net, color=financial_color("Net cash-flow"),
            linewidth=1.5, marker="o", markersize=4, label="Net cash-flow")
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

    apply_financial_legend(ax, max_rows=2)
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    apply_universal_margins(ax)
    return save_figure(out_path)


# ---------------------------------------------------------------------------
# Tornado plots
# ---------------------------------------------------------------------------


def _format_driver_value(
    value: float, driver_type: str, units: str = "",
) -> str:
    """Format a tornado driver's absolute value for a bar-end label.

    ``driver_type`` keys the formatting rule (``capex``, ``opex``,
    ``revenue``, ``discount_rate``).  An unknown type falls back to a
    plain thousands-separated EUR string and logs a warning rather
    than raising.
    """
    dt = str(driver_type).strip().lower()
    # CAPEX / OPEX / revenue are reported as magnitudes: the cashflow
    # carries CAPEX and OPEX with an outflow (negative) sign that should
    # not leak into a label meant to read as the EUR figure itself.
    mag = abs(value)
    if dt == "capex":
        return f"€{mag / 1e6:.1f}M"
    if dt == "opex":
        if mag < 1e6:
            return f"€{mag / 1e3:.0f}k"
        return f"€{mag / 1e6:.1f}M"
    if dt == "revenue":
        if mag < 1e7:
            return f"€{mag / 1e6:.2f}M"
        return f"€{mag / 1e6:.1f}M"
    if dt in ("discount_rate", "discount rate"):
        return f"{value:.1f}%"
    logger.warning(
        "tornado: unknown driver_type %r; using fallback EUR format",
        driver_type,
    )
    return f"€{value:,.0f}{units}"


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
    drivers: dict[str, DriverSensitivity] | None = None,
) -> Path:
    """Shared dumbbell renderer for NPV and IRR sensitivity tornadoes.

    Each driver becomes a horizontal segment running from ``low`` to
    ``high``, red on the side below ``base_value`` and green above, with
    filled circle markers at each endpoint.  Bars are sorted by
    absolute impact (largest at the top).  The metric outcome is read
    directly off the x-axis; the base value is marked once by a dashed
    vertical line whose legend entry carries its numeric value.

    When ``drivers`` is populated each bar end carries the absolute
    driver value that produced it (e.g. ``€17.6M`` for CAPEX,
    ``5.0%`` for the discount rate) and the y-axis tick labels gain
    the ``+/-`` range.  An empty / ``None`` ``drivers`` reproduces the
    metadata-free layout: dots and x-axis position only, no endpoint
    labels.
    """
    out_path = Path(out_path)

    if pivot.empty:
        return empty_placeholder(out_path, "Sensitivity disabled or empty.")

    drop_set = {label.strip().lower() for label in drop_labels}
    if drop_set:
        keep_mask = ~pivot.index.str.strip().str.lower().isin(drop_set)
        pivot = pivot.loc[keep_mask]
    pivot = pivot.loc[pivot["impact"] > 1.0e-9]
    pivot = pivot.sort_values("impact", ascending=True)

    if pivot.empty:
        return empty_placeholder(out_path, "No drivers with non-zero impact.")

    labels = pivot.index.tolist()
    y_pos = np.arange(len(labels))
    lows = pivot["low"].astype(float).to_numpy()
    highs = pivot["high"].astype(float).to_numpy()
    drivers = drivers or {}

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
        # Map each segment end to the absolute driver value that
        # produced it: ``low``/``high`` are the metric outcomes, so the
        # smaller outcome pairs with its scenario's driver value.
        left_driver_text = right_driver_text = None
        ds = drivers.get(labels[i])
        if ds is not None:
            if low <= high:
                lo_dv, hi_dv = ds.low_value, ds.high_value
            else:
                lo_dv, hi_dv = ds.high_value, ds.low_value
            left_driver_text = _format_driver_value(lo_dv, ds.driver_type)
            right_driver_text = _format_driver_value(hi_dv, ds.driver_type)
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
                left_driver_text=left_driver_text,
                right_driver_text=right_driver_text,
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
            left_driver_text=left_driver_text,
            right_driver_text=right_driver_text,
        )

    ax.set_yticks(y_pos)
    if drivers:
        ytick_labels = []
        for lbl in labels:
            ds = drivers.get(lbl)
            if ds is None:
                ytick_labels.append(lbl)
                continue
            unit = "pp" if ds.driver_type == "discount_rate" else "%"
            ytick_labels.append(
                f"{lbl} / ±{ds.sensitivity_pct:g}{unit}"
            )
        ax.set_yticklabels(ytick_labels)
    else:
        ax.set_yticklabels(labels)
    ax.set_xlabel(xlabel)
    if apply_eur_xaxis:
        _apply_eur_xaxis(ax, econ)
    xmin, xmax = ax.get_xlim()
    pad = 0.18 * (xmax - xmin) if xmax > xmin else 1.0
    ax.set_xlim(xmin - pad, xmax + pad)
    ax.set_ylim(-0.6, len(labels) - 0.4)
    _maybe_set_title(ax, title)
    ax.legend(loc="lower right", framealpha=0.9, fontsize=7)
    ax.grid(True, axis="x", linestyle="--", alpha=0.5)
    # Tornado owns its 18% x-padding (above) and its fixed y-row
    # extent; the universal helper only adds defensive padding to
    # neither axis.
    apply_universal_margins(ax, skip_x=True, skip_y=True)
    apply_fine_ticks(ax, axis="x")

    return save_figure(out_path)


def _annotate_dumbbell_endpoints(
    ax,
    left: float,
    right: float,
    row: int,
    value_formatter: Callable[[float], str],
    *,
    left_driver_text: str | None = None,
    right_driver_text: str | None = None,
) -> None:
    """Place each endpoint's driver-value label OUTSIDE the corresponding dot.

    The left label is right-aligned and offset 8 points to the LEFT of
    the leftmost dot; the right label is left-aligned and offset 8
    points to the RIGHT of the rightmost dot.  Both sit on the row
    centerline.  The metric outcome itself is read off the x-axis, so
    only the absolute driver value is printed at each endpoint.

    When ``*_driver_text`` is ``None`` (legacy frames without driver
    metadata) the function is a no-op for that side — the dot plus
    x-axis position carry all the information.  ``left`` / ``right``
    remain in the signature so callers can keep their existing call
    sites unchanged.
    """
    if left_driver_text is not None:
        annotate_value_safe(
            ax, left, row, left_driver_text,
            ha="right", va="center", fontsize=7,
            offset_points=(-8.0, 0.0),
            bbox_alpha=0.85, bbox_pad=0.18,
        )
    if right_driver_text is not None:
        annotate_value_safe(
            ax, right, row, right_driver_text,
            ha="left", va="center", fontsize=7,
            offset_points=(8.0, 0.0),
            bbox_alpha=0.85, bbox_pad=0.18,
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
    """Sorted NPV tornado, dumbbell layout matching the IRR plot.

    margins: delegated to ``_dumbbell_plot``.
    """
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
        drivers=build_driver_sensitivities(sens_df, "npv_eur"),
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

    margins: delegated to ``_dumbbell_plot``.
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
        drivers=build_driver_sensitivities(sens_df, "irr_pct"),
    )
