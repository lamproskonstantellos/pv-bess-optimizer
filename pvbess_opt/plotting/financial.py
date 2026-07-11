"""IEEE-styled financial plots.

Eight plots total:

* :func:`plot_cumulative_cashflow`  — cumulative undiscounted + discounted lines
* :func:`plot_yearly_cashflow_bars` — stacked yearly bars (revenue / opex / capex)
* :func:`plot_npv_waterfall`        — yearly contribution to total NPV
* :func:`plot_payback`              — cumulative cash-flow with simple + discounted markers
* :func:`plot_monthly_cashflow_year1` — Year-1 monthly bars
* :func:`plot_npv_tornado`          — sorted NPV tornado
* :func:`plot_irr_tornado`          — sorted IRR tornado (omits the discount-rate row)
* :func:`plot_dscr_profile`         — per-year debt-service coverage over the tenor

EUR axes use the compact ``EUR 12.3M`` / ``EUR 45k`` formatter via
:func:`pvbess_opt.plotting._currency.euro_axis_formatter`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.axes import Axes

from ..io import PROJECT_SHEET_DEFAULTS
from ..sensitivity import DriverSensitivity, build_driver_sensitivities
from ..theme import (
    FINANCIAL_COLORS,
    XTICK_ROT,
    apply_financial_legend,
    financial_color,
)
from ._currency import (
    euro_axis_formatter,
)
from ._currency import (
    resolve_currency_format as _resolve_currency_format,
)
from .style import (
    annotate_value_safe,
    apply_fine_ticks,
    apply_month_axis,
    apply_universal_margins,
    empty_placeholder,
    legend_below,
    save_figure,
    show_titles,
)

logger = logging.getLogger(__name__)

__all__ = [
    "plot_cumulative_cashflow",
    "plot_dscr_profile",
    "plot_irr_tornado",
    "plot_monthly_cashflow_year1",
    "plot_npv_tornado",
    "plot_npv_waterfall",
    "plot_payback",
    "plot_yearly_cashflow_bars",
]


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


def _integer_year_axis(ax, years: np.ndarray, *, bars: bool = True) -> None:
    """Common calendar-year axis for every per-year figure.

    EVERY project year is labelled — the reader never interpolates
    between sparse ticks — and the labels rotate ``XTICK_ROT``
    right-anchored like every other dense axis in the report (month
    and date axes), which is how 20+ four-digit years fit the standard
    7-inch canvas.  The window hugs the data exactly like the energy
    plots' time axes: line figures span edge to edge (first to last
    year, no empty slot), bar figures keep the half-slot each side
    that the bar geometry needs.  The cashflow views open at Year 0
    (the CAPEX year), the operational frames at Year 1.  Call AFTER
    :func:`apply_universal_margins`: the window must not be re-padded.
    """
    if len(years) == 0:
        return
    first = int(np.min(years))
    last = int(np.max(years))
    ax.set_xticks(np.arange(first, last + 1))
    plt.setp(ax.get_xticklabels(), rotation=XTICK_ROT, ha="right")
    pad = 0.5 if bars else 0.0
    ax.set_xlim(first - pad, last + pad)


# ---------------------------------------------------------------------------
# Cumulative cashflow
# ---------------------------------------------------------------------------


def plot_cumulative_cashflow(
    yearly_cf: pd.DataFrame, out_path: Path,
    *, econ: dict[str, Any] | None = None,
) -> Path:
    """Cumulative undiscounted + discounted cash-flow (both solid,
    colour-distinguished data curves)."""
    out_path = Path(out_path)
    years = _calendar_axis(yearly_cf)
    cum = yearly_cf["cumulative_cf_eur"].to_numpy(dtype=float)
    cum_disc = yearly_cf["cumulative_dcf_eur"].to_numpy(dtype=float)

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    ax.plot(
        years, cum,
        color=financial_color("Cumulative cash-flow"),
        linewidth=1.5, marker="o", markersize=3,
        label="Cumulative cash-flow",
    )
    ax.plot(
        years, cum_disc,
        color=financial_color("Cumulative discounted cash-flow"),
        linewidth=1.5, marker="o", markersize=3,
        label="Cumulative discounted cash-flow",
    )
    ax.axhline(0.0, color="black", linewidth=0.8, alpha=0.6)

    ax.set_xlabel("Year")
    ax.set_ylabel("EUR")
    _apply_eur_yaxis(ax, econ)
    _maybe_set_title(ax, f"Cumulative Cash-flow - {_title_window(yearly_cf)}")
    ax.grid(True, linestyle="--", alpha=0.5)
    apply_universal_margins(ax, skip_y=True)
    _integer_year_axis(ax, years, bars=False)
    apply_fine_ticks(ax)
    apply_financial_legend(ax)
    return save_figure(out_path)


# ---------------------------------------------------------------------------
# DSCR profile
# ---------------------------------------------------------------------------


def plot_dscr_profile(
    debt_schedule: pd.DataFrame | None, out_path: Path,
    *,
    p90_schedule: pd.DataFrame | None = None,
    target_dscr: float | None = None,
    econ: dict[str, Any] | None = None,
) -> Path | None:
    """Per-year debt-service coverage over the tenor (Eqs. E20/E44).

    Base-case DSCR line from :func:`economics.build_debt_schedule`,
    an optional P90 production-case line (Eq. E44 — same committed
    debt, haircut CFADS) and an optional target-DSCR reference drawn
    as a plotted dashed series with a legend entry (house rule: no
    computed values as axes text).  Returns None without touching the
    filesystem for all-equity runs (no schedule), so default output
    directories stay bit-identical.
    """
    if debt_schedule is None or debt_schedule.empty:
        return None
    out_path = Path(out_path)
    op_years = debt_schedule["year"].to_numpy(dtype=float)
    start = int(
        (econ or {}).get(
            "project_start_year",
            PROJECT_SHEET_DEFAULTS["project_start_year"],
        )
        or PROJECT_SHEET_DEFAULTS["project_start_year"]
    )
    years = op_years + (start - 1)
    dscr = debt_schedule["dscr"].to_numpy(dtype=float)
    base_mask = np.isfinite(dscr)
    if not bool(base_mask.any()):
        return None

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    ax.plot(
        years[base_mask], dscr[base_mask],
        color=financial_color("DSCR base case"),
        linewidth=1.5, marker="o", markersize=3,
        label="DSCR base case",
    )
    if p90_schedule is not None and not p90_schedule.empty:
        p90_years = p90_schedule["year"].to_numpy(dtype=float) + (start - 1)
        p90_dscr = p90_schedule["dscr"].to_numpy(dtype=float)
        p90_mask = np.isfinite(p90_dscr)
        if bool(p90_mask.any()):
            ax.plot(
                p90_years[p90_mask], p90_dscr[p90_mask],
                color=financial_color("DSCR P90 case"),
                linewidth=1.5, marker="s", markersize=3,
                label="DSCR P90 case",
            )
    if target_dscr is not None and np.isfinite(float(target_dscr)):
        ax.plot(
            years, np.full(years.shape, float(target_dscr)),
            color=financial_color("Target DSCR"),
            linewidth=1.2, linestyle="--",
            label="Target DSCR",
        )
    # DSCR = 1 is the break-even coverage — the same neutral rule line
    # every cashflow figure draws at zero.
    ax.axhline(1.0, color="black", linewidth=0.8, alpha=0.6)

    ax.set_xlabel("Year")
    ax.set_ylabel("DSCR [-]")
    _maybe_set_title(ax, "Debt Service Coverage - " + (
        f"{int(years.min())}-{int(years.max())}"
    ))
    ax.grid(True, linestyle="--", alpha=0.5)
    apply_universal_margins(ax, skip_y=True)
    _integer_year_axis(ax, years, bars=False)
    apply_fine_ticks(ax)
    apply_financial_legend(ax)
    return save_figure(out_path)


# ---------------------------------------------------------------------------
# Yearly cashflow bars
# ---------------------------------------------------------------------------


def _optional_revenue_streams(
    yearly_cf: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray,
           np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray,
           np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return the (balancing, balancing-fee, ppa, rtm-fee, optimizer-fee,
    grid-charging-fee, imbalance-cost, toll-revenue, floor-top-up,
    state-support, state-support-netting, capacity-market,
    revenue-levy, curtailment-compensation, augmentation-capex) yearly
    columns.

    Missing or all-zero columns come back as zero arrays so callers can
    stack them unconditionally; all-zero streams draw nothing.
    """
    n = len(yearly_cf)
    out = []
    for col in (
        "balancing_revenue_eur", "balancing_aggregator_fee_eur",
        "ppa_revenue_eur", "route_to_market_fee_eur", "optimizer_fee_eur",
        "grid_charging_fee_eur", "imbalance_cost_eur",
        "toll_revenue_eur", "optimizer_floor_topup_eur",
        "state_support_eur", "state_support_clawback_eur",
        "capacity_market_revenue_eur", "revenue_levy_eur",
        "curtailment_compensation_eur", "augmentation_capex_eur",
    ):
        if col in yearly_cf.columns:
            out.append(yearly_cf[col].to_numpy(dtype=float))
        else:
            out.append(np.zeros(n, dtype=float))
    return (
        out[0], out[1], out[2], out[3], out[4], out[5], out[6], out[7],
        out[8], out[9], out[10], out[11], out[12], out[13], out[14],
    )


def _stack_cashflow_bars(
    ax: Axes,
    years: np.ndarray,
    width: float,
    revenue: np.ndarray,
    balancing: np.ndarray,
    ppa: np.ndarray,
    opex: np.ndarray,
    devex: np.ndarray,
    capex: np.ndarray,
    bal_fee: np.ndarray,
    rtm_fee: np.ndarray | None = None,
    opt_fee: np.ndarray | None = None,
    gcf_fee: np.ndarray | None = None,
    imb_cost: np.ndarray | None = None,
    toll: np.ndarray | None = None,
    floor_topup: np.ndarray | None = None,
    support: np.ndarray | None = None,
    support_net: np.ndarray | None = None,
    capacity_market: np.ndarray | None = None,
    levy: np.ndarray | None = None,
    curtailment_comp: np.ndarray | None = None,
    aug_capex: np.ndarray | None = None,
) -> None:
    """Draw the shared cashflow bar stack (yearly bars / NPV waterfall).

    Positive streams stack upward from the Revenue base (Balancing
    revenue, the PPA leg, then the tolling payment); negative streams
    stack downward (OPEX, then the balancing aggregator fee, DEVEX,
    CAPEX).  Every stream is stacked with a cumulative ``bottom`` so no
    segment paints over another, and all-zero streams are omitted
    entirely so non-balancing / non-PPA runs keep their four-bar legend.
    """
    ax.bar(years, revenue, width=width, color=financial_color("Revenue"),
           edgecolor="black", linewidth=0.4, label="Revenue")
    pos_bottom = np.clip(revenue, 0.0, None)
    if np.any(np.abs(balancing) > 1e-9):
        ax.bar(years, balancing, width=width, bottom=pos_bottom,
               color=financial_color("Balancing revenue"),
               edgecolor="black", linewidth=0.4, label="Balancing revenue")
        pos_bottom = pos_bottom + np.clip(balancing, 0.0, None)
    if np.any(np.abs(ppa) > 1e-9):
        ax.bar(years, ppa, width=width, bottom=pos_bottom,
               color=financial_color("PPA revenue"),
               edgecolor="black", linewidth=0.4, label="PPA revenue")
        pos_bottom = pos_bottom + np.clip(ppa, 0.0, None)
    if toll is not None and np.any(np.abs(toll) > 1e-9):
        ax.bar(years, toll, width=width, bottom=pos_bottom,
               color=financial_color("Tolling revenue"),
               edgecolor="black", linewidth=0.4, label="Tolling revenue")
        pos_bottom = pos_bottom + np.clip(toll, 0.0, None)
    if floor_topup is not None and np.any(np.abs(floor_topup) > 1e-9):
        ax.bar(years, floor_topup, width=width, bottom=pos_bottom,
               color=financial_color("Optimizer floor top-up"),
               edgecolor="black", linewidth=0.4,
               label="Optimizer floor top-up")
        pos_bottom = pos_bottom + np.clip(floor_topup, 0.0, None)
    if support is not None and np.any(np.abs(support) > 1e-9):
        ax.bar(years, support, width=width, bottom=pos_bottom,
               color=financial_color("State support"),
               edgecolor="black", linewidth=0.4, label="State support")
        pos_bottom = pos_bottom + np.clip(support, 0.0, None)
    if capacity_market is not None and np.any(
        np.abs(capacity_market) > 1e-9
    ):
        ax.bar(years, capacity_market, width=width, bottom=pos_bottom,
               color=financial_color("Capacity-market revenue"),
               edgecolor="black", linewidth=0.4,
               label="Capacity-market revenue")
        pos_bottom = pos_bottom + np.clip(capacity_market, 0.0, None)
    if curtailment_comp is not None and np.any(
        np.abs(curtailment_comp) > 1e-9
    ):
        ax.bar(years, curtailment_comp, width=width, bottom=pos_bottom,
               color=financial_color("Curtailment compensation"),
               edgecolor="black", linewidth=0.4,
               label="Curtailment compensation")
        pos_bottom = pos_bottom + np.clip(curtailment_comp, 0.0, None)
    ax.bar(years, opex, width=width, color=financial_color("OPEX"),
           edgecolor="black", linewidth=0.4, label="OPEX")
    # Stack EVERY negative segment: the balancing fee below OPEX, DEVEX
    # below both, CAPEX below all.  Without a cumulative ``bottom``
    # matplotlib overlays same-x bars and the smaller segment disappears
    # inside the larger block — Year 0 would hide DEVEX inside CAPEX,
    # and a BESS replacement year (where OPEX and CAPEX are both
    # non-zero while DEVEX is 0) would hide OPEX entirely and
    # understate the year's visible outflow.
    neg_bottom = opex.copy()
    if np.any(np.abs(bal_fee) > 1e-9):
        ax.bar(years, bal_fee, width=width, bottom=neg_bottom,
               color=financial_color("Balancing aggregator fee"),
               edgecolor="black", linewidth=0.4,
               label="Balancing aggregator fee")
        neg_bottom = neg_bottom + bal_fee
    # Structural market-access fees keep their own negative slots, exactly
    # like the BSP fee: all-zero streams draw nothing so a fee-free run's
    # figure is unchanged.
    if rtm_fee is not None and np.any(np.abs(rtm_fee) > 1e-9):
        ax.bar(years, rtm_fee, width=width, bottom=neg_bottom,
               color=financial_color("Route-to-market fee"),
               edgecolor="black", linewidth=0.4,
               label="Route-to-market fee")
        neg_bottom = neg_bottom + rtm_fee
    if opt_fee is not None and np.any(np.abs(opt_fee) > 1e-9):
        ax.bar(years, opt_fee, width=width, bottom=neg_bottom,
               color=financial_color("Optimizer fee"),
               edgecolor="black", linewidth=0.4,
               label="Optimizer fee")
        neg_bottom = neg_bottom + opt_fee
    if gcf_fee is not None and np.any(np.abs(gcf_fee) > 1e-9):
        ax.bar(years, gcf_fee, width=width, bottom=neg_bottom,
               color=financial_color("Grid-charging fee"),
               edgecolor="black", linewidth=0.4,
               label="Grid-charging fee")
        neg_bottom = neg_bottom + gcf_fee
    if imb_cost is not None and np.any(np.abs(imb_cost) > 1e-9):
        ax.bar(years, imb_cost, width=width, bottom=neg_bottom,
               color=financial_color("Imbalance cost"),
               edgecolor="black", linewidth=0.4,
               label="Imbalance cost")
        neg_bottom = neg_bottom + imb_cost
    if levy is not None and np.any(np.abs(levy) > 1e-9):
        ax.bar(years, levy, width=width, bottom=neg_bottom,
               color=financial_color("Revenue levy"),
               edgecolor="black", linewidth=0.4,
               label="Revenue levy")
        neg_bottom = neg_bottom + levy
    if support_net is not None and np.any(np.abs(support_net) > 1e-9):
        # The two-way netting (Eq. E31a) is signed per year: clawback
        # years stack on the deduction side, compensation years on the
        # revenue side — one band, element-wise bottoms.
        ax.bar(years, support_net, width=width,
               bottom=np.where(support_net >= 0.0, pos_bottom, neg_bottom),
               color=financial_color("State-support netting"),
               edgecolor="black", linewidth=0.4,
               label="State-support netting")
        pos_bottom = pos_bottom + np.clip(support_net, 0.0, None)
        neg_bottom = neg_bottom + np.clip(support_net, None, 0.0)
    ax.bar(years, devex, width=width, bottom=neg_bottom,
           color=financial_color("DEVEX"),
           edgecolor="black", linewidth=0.4, label="DEVEX")
    ax.bar(years, capex, width=width, bottom=neg_bottom + devex,
           color=financial_color("CAPEX"),
           edgecolor="black", linewidth=0.4, label="CAPEX")
    # Augmentation events (Eq. E51): a mid-life investment bar in the
    # CAPEX family, stacked below the (usually zero) event-year CAPEX
    # slot; all-zero runs draw nothing so default figures are unchanged.
    if aug_capex is not None and np.any(np.abs(aug_capex) > 1e-9):
        ax.bar(years, aug_capex, width=width,
               bottom=neg_bottom + devex + capex,
               color=financial_color("Augmentation CAPEX"),
               edgecolor="black", linewidth=0.4,
               label="Augmentation CAPEX")


def plot_yearly_cashflow_bars(
    yearly_cf: pd.DataFrame, out_path: Path,
    *, econ: dict[str, Any] | None = None,
) -> Path:
    """Stacked yearly bars for revenue (+), opex (-), capex (-), net line.

    Balancing revenue (gross), the PPA contract leg and the balancing
    aggregator fee join the stack when their cashflow columns carry
    value, so the bars always sum to the overlaid net line.  Zero-value
    streams are omitted (a no-balancing run draws no balancing bar).
    """
    out_path = Path(out_path)
    years = _calendar_axis(yearly_cf)
    revenue = yearly_cf["revenue_eur"].to_numpy(dtype=float)
    (balancing, bal_fee, ppa, rtm_fee, opt_fee, gcf_fee, imb_cost,
     toll, floor_topup, support, support_net, capacity_market,
     levy, curtailment_comp, aug_capex) = _optional_revenue_streams(
        yearly_cf,
    )
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
    _stack_cashflow_bars(
        ax, years, width, revenue, balancing, ppa, opex, devex, capex,
        bal_fee, rtm_fee, opt_fee, gcf_fee, imb_cost, toll, floor_topup,
        support, support_net, capacity_market, levy, curtailment_comp,
        aug_capex,
    )
    ax.plot(years, net, color=financial_color("Net cash-flow"), linewidth=1.5,
            marker="o", markersize=3, label="Net cash-flow")
    ax.axhline(0.0, color="black", linewidth=0.8)

    ax.set_xlabel("Year")
    ax.set_ylabel("EUR")
    _apply_eur_yaxis(ax, econ)
    _maybe_set_title(ax, f"Yearly Cash-flow Stack - {_title_window(yearly_cf)}")
    # Pin to the lower right — the post-payback region is roughly
    # horizontal there, so the legend stays clear of the bars and the
    # Year-0 CAPEX stack on the left.
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    apply_universal_margins(ax, skip_y=True)
    _integer_year_axis(ax, years)
    apply_fine_ticks(ax)
    apply_financial_legend(ax)
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
    the two plots can be read side by side: stacked Revenue (+) /
    Balancing revenue (+) / PPA revenue (+) / OPEX (-) / Balancing
    aggregator fee (-) / DEVEX (-) / CAPEX (-) bars per year (all-zero
    streams omitted), a ``Net cash-flow`` marker line, and one extra
    ``Cumulative discounted cash-flow`` line overlaid — the same series
    (and name) the cumulative-cashflow figure carries.  All values are
    discounted to Year 0, so the bars sum to the discounted net line.
    """
    out_path = Path(out_path)
    years = _calendar_axis(yearly_cf)
    disc_factor = yearly_cf["discount_factor"].astype(float).to_numpy()
    revenue_disc = yearly_cf["revenue_eur"].astype(float).to_numpy() * disc_factor
    (balancing, bal_fee, ppa, rtm_fee, opt_fee, gcf_fee, imb_cost,
     toll, floor_topup, support, support_net, capacity_market,
     levy, curtailment_comp, aug_capex) = _optional_revenue_streams(
        yearly_cf,
    )
    balancing_disc = balancing * disc_factor
    bal_fee_disc = bal_fee * disc_factor
    ppa_disc = ppa * disc_factor
    rtm_fee_disc = rtm_fee * disc_factor
    opt_fee_disc = opt_fee * disc_factor
    gcf_fee_disc = gcf_fee * disc_factor
    imb_cost_disc = imb_cost * disc_factor
    toll_disc = toll * disc_factor
    floor_topup_disc = floor_topup * disc_factor
    support_disc = support * disc_factor
    support_net_disc = support_net * disc_factor
    capacity_market_disc = capacity_market * disc_factor
    levy_disc = levy * disc_factor
    curtailment_comp_disc = curtailment_comp * disc_factor
    aug_capex_disc = aug_capex * disc_factor
    opex_disc = yearly_cf["opex_eur"].astype(float).to_numpy() * disc_factor
    if "devex_eur" in yearly_cf.columns:
        devex_disc = (
            yearly_cf["devex_eur"].astype(float).to_numpy() * disc_factor
        )
    else:
        devex_disc = np.zeros_like(revenue_disc)
    capex_disc = yearly_cf["capex_eur"].astype(float).to_numpy() * disc_factor
    net_disc = yearly_cf["discounted_cf_eur"].astype(float).to_numpy()
    # Identical to the economics frame's ``cumulative_dcf_eur`` column
    # (cumsum of the discounted net), recomputed so the plot's input
    # contract stays minimal.
    cum_disc = np.cumsum(net_disc)

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    width = 0.8
    _stack_cashflow_bars(
        ax, years, width, revenue_disc, balancing_disc, ppa_disc,
        opex_disc, devex_disc, capex_disc, bal_fee_disc,
        rtm_fee_disc, opt_fee_disc, gcf_fee_disc, imb_cost_disc,
        toll_disc, floor_topup_disc, support_disc, support_net_disc,
        capacity_market_disc, levy_disc, curtailment_comp_disc,
        aug_capex_disc,
    )

    ax.plot(
        years, net_disc,
        color=financial_color("Net cash-flow (discounted)"), linewidth=1.5,
        marker="o", markersize=3, label="Net cash-flow (discounted)",
    )
    ax.plot(
        years, cum_disc,
        color=financial_color("Cumulative discounted cash-flow"),
        linewidth=1.5,
        marker="o", markersize=3, label="Cumulative discounted cash-flow",
    )
    ax.axhline(0.0, color="black", linewidth=0.8)

    ax.set_xlabel("Year")
    ax.set_ylabel("Discounted EUR")
    _apply_eur_yaxis(ax, econ)
    _maybe_set_title(ax, f"NPV Waterfall - {_title_window(yearly_cf)}")
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    apply_universal_margins(ax, skip_y=True)
    _integer_year_axis(ax, years)
    apply_fine_ticks(ax)
    apply_financial_legend(ax)
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
            linewidth=1.5, marker="o", markersize=3,
            label="Cumulative cash-flow")
    ax.plot(years, cum_disc, color=financial_color("Cumulative discounted cash-flow"),
            linewidth=1.5, marker="o", markersize=3,
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
        # The payback value itself lives in SUMMARY.md and the KPI
        # sheet; the legend names the marker only so the figure drops
        # into a paper unchanged.  The reference frame is years since
        # project year 0 (CAPEX commitment), not since the Commercial
        # Operation Date.
        ax.axvline(
            x, color=financial_color("Simple payback"),
            linewidth=0.8, linestyle="--", alpha=0.8,
            label="Simple payback",
        )
    if (
        discounted_payback_years is not None
        and not np.isnan(discounted_payback_years)
    ):
        x = _to_axis(float(discounted_payback_years))
        ax.axvline(
            x, color=financial_color("Discounted payback"),
            linewidth=0.8, linestyle="--", alpha=0.8,
            label="Discounted payback",
        )

    ax.set_xlabel("Year")
    ax.set_ylabel("EUR")
    _apply_eur_yaxis(ax, econ)
    _maybe_set_title(ax, f"Payback Visualisation - {_title_window(yearly_cf)}")
    ax.grid(True, linestyle="--", alpha=0.5)
    apply_universal_margins(ax, skip_y=True)
    _integer_year_axis(ax, years, bars=False)
    apply_financial_legend(ax)
    return save_figure(out_path)


# ---------------------------------------------------------------------------
# Year-1 monthly cashflow
# ---------------------------------------------------------------------------


def plot_monthly_cashflow_year1(
    monthly_cf: pd.DataFrame, out_path: Path,
    *, econ: dict[str, Any] | None = None,
) -> Path:
    """Year-1 monthly stacked bars showing the seasonality of cash-flows.

    ``revenue_eur`` is the DAM + retail post-fee stream;
    ``balancing_revenue_eur`` (gross), its BSP fee
    (``balancing_aggregator_fee_eur``, stacked below OPEX) and the PPA
    leg join the stack when they carry value so the per-month bars
    reconcile to the net-cash-flow line.  The energy aggregator fee is
    already netted out of ``revenue_eur`` upstream (see
    :func:`derive_monthly_cashflow`) and is therefore not shown as a
    separate bar here.  Investment events booked in the monthly frame
    (a Year-1 BESS replacement lands in month 12) are drawn as CAPEX /
    DEVEX bars stacked below OPEX, mirroring the yearly stack, so the
    bars still reconcile to the net line.
    """
    out_path = Path(out_path)
    yr_col = (
        "project_year" if "project_year" in monthly_cf.columns else "year"
    )
    sub = monthly_cf.loc[monthly_cf[yr_col] == 1].sort_values("period")
    months = sub["period"].astype(int).to_numpy()
    revenue = sub["revenue_eur"].astype(float).to_numpy()
    opex = sub["opex_eur"].astype(float).to_numpy()
    net = sub["net_cashflow_eur"].astype(float).to_numpy()
    if "balancing_revenue_eur" in sub.columns:
        balancing = sub["balancing_revenue_eur"].astype(float).to_numpy()
    else:
        balancing = np.zeros_like(revenue)
    if "balancing_aggregator_fee_eur" in sub.columns:
        bal_fee = (
            sub["balancing_aggregator_fee_eur"].astype(float).to_numpy()
        )
    else:
        bal_fee = np.zeros_like(revenue)
    if "ppa_revenue_eur" in sub.columns:
        ppa = sub["ppa_revenue_eur"].astype(float).to_numpy()
    else:
        ppa = np.zeros_like(revenue)
    if "route_to_market_fee_eur" in sub.columns:
        rtm_fee = sub["route_to_market_fee_eur"].astype(float).to_numpy()
    else:
        rtm_fee = np.zeros_like(revenue)
    if "optimizer_fee_eur" in sub.columns:
        opt_fee = sub["optimizer_fee_eur"].astype(float).to_numpy()
    else:
        opt_fee = np.zeros_like(revenue)
    if "grid_charging_fee_eur" in sub.columns:
        gcf_fee = sub["grid_charging_fee_eur"].astype(float).to_numpy()
    else:
        gcf_fee = np.zeros_like(revenue)
    if "imbalance_cost_eur" in sub.columns:
        imb_cost = sub["imbalance_cost_eur"].astype(float).to_numpy()
    else:
        imb_cost = np.zeros_like(revenue)
    if "toll_revenue_eur" in sub.columns:
        toll = sub["toll_revenue_eur"].astype(float).to_numpy()
    else:
        toll = np.zeros_like(revenue)
    if "optimizer_floor_topup_eur" in sub.columns:
        floor_topup = (
            sub["optimizer_floor_topup_eur"].astype(float).to_numpy()
        )
    else:
        floor_topup = np.zeros_like(revenue)
    if "state_support_eur" in sub.columns:
        support = sub["state_support_eur"].astype(float).to_numpy()
    else:
        support = np.zeros_like(revenue)
    if "state_support_clawback_eur" in sub.columns:
        support_net = (
            sub["state_support_clawback_eur"].astype(float).to_numpy()
        )
    else:
        support_net = np.zeros_like(revenue)
    if "capacity_market_revenue_eur" in sub.columns:
        capacity_market = (
            sub["capacity_market_revenue_eur"].astype(float).to_numpy()
        )
    else:
        capacity_market = np.zeros_like(revenue)
    if "revenue_levy_eur" in sub.columns:
        levy = sub["revenue_levy_eur"].astype(float).to_numpy()
    else:
        levy = np.zeros_like(revenue)
    if "devex_eur" in sub.columns:
        devex = sub["devex_eur"].astype(float).to_numpy()
    else:
        devex = np.zeros_like(revenue)
    if "capex_eur" in sub.columns:
        capex = sub["capex_eur"].astype(float).to_numpy()
    else:
        capex = np.zeros_like(revenue)

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    ax.bar(months, revenue, color=financial_color("Revenue"),
           edgecolor="black", linewidth=0.4, label="Revenue")
    pos_bottom = np.clip(revenue, 0.0, None)
    if np.any(np.abs(balancing) > 1e-9):
        ax.bar(months, balancing, bottom=pos_bottom,
               color=financial_color("Balancing revenue"),
               edgecolor="black", linewidth=0.4,
               label="Balancing revenue")
        pos_bottom = pos_bottom + np.clip(balancing, 0.0, None)
    if np.any(np.abs(ppa) > 1e-9):
        ax.bar(months, ppa, bottom=pos_bottom,
               color=financial_color("PPA revenue"),
               edgecolor="black", linewidth=0.4, label="PPA revenue")
        pos_bottom = pos_bottom + np.clip(ppa, 0.0, None)
    if np.any(np.abs(toll) > 1e-9):
        ax.bar(months, toll, bottom=pos_bottom,
               color=financial_color("Tolling revenue"),
               edgecolor="black", linewidth=0.4, label="Tolling revenue")
        pos_bottom = pos_bottom + np.clip(toll, 0.0, None)
    if np.any(np.abs(floor_topup) > 1e-9):
        ax.bar(months, floor_topup, bottom=pos_bottom,
               color=financial_color("Optimizer floor top-up"),
               edgecolor="black", linewidth=0.4,
               label="Optimizer floor top-up")
        pos_bottom = pos_bottom + np.clip(floor_topup, 0.0, None)
    if np.any(np.abs(support) > 1e-9):
        ax.bar(months, support, bottom=pos_bottom,
               color=financial_color("State support"),
               edgecolor="black", linewidth=0.4, label="State support")
        pos_bottom = pos_bottom + np.clip(support, 0.0, None)
    if np.any(np.abs(capacity_market) > 1e-9):
        ax.bar(months, capacity_market, bottom=pos_bottom,
               color=financial_color("Capacity-market revenue"),
               edgecolor="black", linewidth=0.4,
               label="Capacity-market revenue")
        pos_bottom = pos_bottom + np.clip(capacity_market, 0.0, None)
    ax.bar(months, opex, color=financial_color("OPEX"),
           edgecolor="black", linewidth=0.4, label="OPEX")
    neg_bottom = opex.copy()
    # The BSP fee is GROSS balancing revenue's deduction, so it joins
    # the negative stack (below OPEX) whenever it carries value — the
    # bars then reconcile to the net line with the fee on.
    if np.any(np.abs(bal_fee) > 1e-9):
        ax.bar(months, bal_fee, bottom=neg_bottom,
               color=financial_color("Balancing aggregator fee"),
               edgecolor="black", linewidth=0.4,
               label="Balancing aggregator fee")
        neg_bottom = neg_bottom + bal_fee
    # Structural market-access fees join the same negative stack (they are
    # part of net_cashflow_eur on the monthly frame), each in its own slot.
    if np.any(np.abs(rtm_fee) > 1e-9):
        ax.bar(months, rtm_fee, bottom=neg_bottom,
               color=financial_color("Route-to-market fee"),
               edgecolor="black", linewidth=0.4,
               label="Route-to-market fee")
        neg_bottom = neg_bottom + rtm_fee
    if np.any(np.abs(opt_fee) > 1e-9):
        ax.bar(months, opt_fee, bottom=neg_bottom,
               color=financial_color("Optimizer fee"),
               edgecolor="black", linewidth=0.4,
               label="Optimizer fee")
        neg_bottom = neg_bottom + opt_fee
    if np.any(np.abs(gcf_fee) > 1e-9):
        ax.bar(months, gcf_fee, bottom=neg_bottom,
               color=financial_color("Grid-charging fee"),
               edgecolor="black", linewidth=0.4,
               label="Grid-charging fee")
        neg_bottom = neg_bottom + gcf_fee
    if np.any(np.abs(imb_cost) > 1e-9):
        ax.bar(months, imb_cost, bottom=neg_bottom,
               color=financial_color("Imbalance cost"),
               edgecolor="black", linewidth=0.4,
               label="Imbalance cost")
        neg_bottom = neg_bottom + imb_cost
    if np.any(np.abs(levy) > 1e-9):
        ax.bar(months, levy, bottom=neg_bottom,
               color=financial_color("Revenue levy"),
               edgecolor="black", linewidth=0.4,
               label="Revenue levy")
        neg_bottom = neg_bottom + levy
    if np.any(np.abs(support_net) > 1e-9):
        # Signed netting (Eq. E31a): element-wise side assignment, one
        # band (the month-12 booking makes it a single-month bar).
        ax.bar(months, support_net,
               bottom=np.where(
                   support_net >= 0.0, pos_bottom, neg_bottom,
               ),
               color=financial_color("State-support netting"),
               edgecolor="black", linewidth=0.4,
               label="State-support netting")
        pos_bottom = pos_bottom + np.clip(support_net, 0.0, None)
        neg_bottom = neg_bottom + np.clip(support_net, None, 0.0)
    if np.any(np.abs(devex) > 1e-9):
        ax.bar(months, devex, bottom=neg_bottom,
               color=financial_color("DEVEX"),
               edgecolor="black", linewidth=0.4, label="DEVEX")
    if np.any(np.abs(capex) > 1e-9):
        ax.bar(months, capex, bottom=neg_bottom + devex,
               color=financial_color("CAPEX"),
               edgecolor="black", linewidth=0.4, label="CAPEX")
    ax.plot(months, net, color=financial_color("Net cash-flow"),
            linewidth=1.5, marker="o", markersize=3, label="Net cash-flow")
    ax.axhline(0.0, color="black", linewidth=0.6)
    ax.set_xlabel("Month")
    ax.set_ylabel("EUR")
    _apply_eur_yaxis(ax, econ)

    if "calendar_year" in monthly_cf.columns and not sub.empty:
        cal_year: int | None = int(sub["calendar_year"].iloc[0])
        _maybe_set_title(ax, f"Year-1 Monthly Cash-flow - {cal_year}")
    else:
        cal_year = None
        _maybe_set_title(ax, "Year-1 Monthly Cash-flow")

    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    apply_universal_margins(ax, skip_y=True)
    apply_month_axis(ax, months, months, year=cal_year)
    apply_financial_legend(ax, max_rows=2)
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
    if dt == "ppa_price":
        return f"€{value:.0f}/MWh"
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
        alpha=0.6, label="Base",
    )

    red = FINANCIAL_COLORS["tornado_neg"]
    green = FINANCIAL_COLORS["tornado_pos"]

    for i, (low, high) in enumerate(zip(lows, highs, strict=False)):
        left, right = sorted((low, high))
        # Map each segment end to the driver value of the SCENARIO that
        # produced that end's metric outcome.  ``low``/``high`` are the
        # metric outcomes of the low/high scenarios, so the pairing is
        # decided purely by which scenario's metric is smaller.  The
        # driver values' own numeric ordering must not enter the
        # pairing: cost drivers are stored as signed outflows
        # (negative), so their numeric order is inverted relative to
        # the magnitudes the labels display, and keying on it swapped
        # the CAPEX / OPEX endpoint labels.
        left_driver_text = right_driver_text = None
        ds = drivers.get(labels[i])
        if ds is not None:
            metric_low_to_high = high >= low
            if metric_low_to_high:
                lo_dv, hi_dv = ds.low_value, ds.high_value
            else:
                lo_dv, hi_dv = ds.high_value, ds.low_value
            # Non-monotonic guard: when low_outcome ~= high_outcome
            # but the driver values differ, or when one outcome equals
            # base while the other moves either way, the left/right
            # implication is ambiguous.  Warn so the developer adds an
            # explicit direction hint to the driver descriptor.
            if (
                abs(high - low) <= 1e-9
                and abs(ds.high_value - ds.low_value) > 1e-9
            ):
                logger.warning(
                    "Tornado driver %r maps both perturbations to the "
                    "same metric value (low=%g, high=%g): driver-end "
                    "annotation is ambiguous and may be mislabelled.",
                    labels[i], low, high,
                )
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
                ax, left, right, i,
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
            ax, left, right, i,
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
    ax.grid(True, axis="x", linestyle="--", alpha=0.5)
    # Tornado owns its 18% x-padding (above) and its fixed y-row
    # extent; the universal helper only adds defensive padding to
    # neither axis.
    apply_universal_margins(ax, skip_x=True, skip_y=True)
    legend_below(ax)
    apply_fine_ticks(ax, axis="x")

    return save_figure(out_path)


def _annotate_dumbbell_endpoints(
    ax,
    left: float,
    right: float,
    row: int,
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

    When ``*_driver_text`` is ``None`` (frames without driver
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
    start = int(
        econ.get("project_start_year",
                 PROJECT_SHEET_DEFAULTS["project_start_year"])
        or PROJECT_SHEET_DEFAULTS["project_start_year"]
    )
    n = int(
        econ.get("project_lifecycle_years",
                 PROJECT_SHEET_DEFAULTS["project_lifecycle_years"])
        or PROJECT_SHEET_DEFAULTS["project_lifecycle_years"]
    )
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
    title = f"NPV Sensitivity - {window}" if window else "NPV Sensitivity"
    if sens_df.empty:
        return _dumbbell_plot(
            pd.DataFrame(), base_npv, out_path,
            title=title, xlabel="NPV (EUR)",
            apply_eur_xaxis=True, econ=econ,
        )
    pivot = _build_tornado_pivot(sens_df, "npv_eur", base_npv)
    return _dumbbell_plot(
        pivot, base_npv, out_path,
        title=title,
        xlabel="NPV (EUR)",
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
    title = f"IRR Sensitivity - {window}" if window else "IRR Sensitivity"
    if sens_df.empty:
        return _dumbbell_plot(
            pd.DataFrame(), base_irr, out_path,
            title=title, xlabel="IRR (%)",
            drop_labels=("Discount rate",),
        )
    pivot = _build_tornado_pivot(sens_df, "irr_pct", base_irr)
    return _dumbbell_plot(
        pivot, base_irr, out_path,
        title=title,
        xlabel="IRR (%)",
        drop_labels=("Discount rate",),
        drivers=build_driver_sensitivities(sens_df, "irr_pct"),
    )
