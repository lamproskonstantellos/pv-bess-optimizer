"""BESS-specific revenue decomposition plots.

Three plots, all driven by the canonical revenue aggregate keys
produced by :func:`pvbess_opt.kpis.compute_kpis`:

* :func:`plot_bess_revenue_waterfall` — single waterfall stepping from
  the BESS's DAM-arbitrage segment through each balancing product to
  the total BESS revenue.  The terminal bar is drawn in a darker shade.
* :func:`plot_bess_capacity_vs_activation_split` — grouped-bar chart
  comparing capacity revenue and activation revenue per balancing
  product.  FCR has a single bar (capacity-only); aFRR / mFRR each get
  a paired (capacity, activation) bar in matching lighter / darker
  shades of the product's canonical colour.
* :func:`plot_bess_revenue_by_month` — 12 stacked bars (one per
  calendar month) showing the DAM-arbitrage segment plus the five
  balancing products' contribution to monthly BESS revenue.

These plots are entirely BESS-scoped — their content is only BESS
revenue — so the DAM-arbitrage segment is labelled ``"DAM"`` rather
than ``"BESS-DAM arbitrage"`` to avoid the redundant ``BESS-`` prefix.
The parent revenue-stack plot in :mod:`pvbess_opt.plotting.lifecycle`
also carries PV-DAM exports and keeps the BESS qualifier attached as
``"Export from BESS"``.

Colour mapping uses :data:`pvbess_opt.theme.BM_COLOURS` so the
balancing products are visually consistent across plots.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ..theme import BM_COLOURS, financial_color
from ._currency import euro_axis_formatter, format_eur, resolve_currency_format
from .helpers import title_prefix
from .style import (
    apply_fine_ticks,
    apply_universal_margins,
    get_scenario_label,
    reserve_legend_headroom,
    save_figure,
    show_titles,
)
from .style import (
    empty_placeholder as _empty_placeholder,
)

__all__ = [
    "plot_bess_capacity_vs_activation_split",
    "plot_bess_revenue_by_month",
    "plot_bess_revenue_waterfall",
]


# Single source of truth for the five balancing products + the
# DAM-arbitrage segment, in the order they appear in every plot in this
# module.  Keeping the order frozen makes the legend stable across
# plots and ensures the waterfall steps are deterministic.
#
# The plots in this module are entirely BESS-scoped, so the segment
# reads ``"DAM"`` rather than ``"BESS-DAM arbitrage"`` — the parent
# revenue-stack plot in :mod:`pvbess_opt.plotting.lifecycle` carries
# PV-DAM exports too and keeps the BESS qualifier attached via the
# ``"Export from BESS"`` label.
_BESS_DAM_LABEL = "DAM"
_BESS_DAM_COLOUR = financial_color("Export from BESS")
_BM_PRODUCTS: tuple[tuple[str, str, str], ...] = (
    ("revenue_bess_fcr_eur", "FCR", "fcr"),
    ("revenue_bess_afrr_up_eur", "aFRR-up", "afrr_up"),
    ("revenue_bess_afrr_dn_eur", "aFRR-down", "afrr_dn"),
    ("revenue_bess_mfrr_up_eur", "mFRR-up", "mfrr_up"),
    ("revenue_bess_mfrr_dn_eur", "mFRR-down", "mfrr_dn"),
)


def _shade(hex_colour: str, factor: float) -> str:
    """Return ``hex_colour`` lightened (factor>1) or darkened (factor<1).

    Used to derive a lighter "capacity" shade and a darker "activation"
    shade from each product's canonical colour.
    """
    hex_colour = hex_colour.lstrip("#")
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (0, 2, 4))
    if factor >= 1.0:
        r = int(r + (255 - r) * (factor - 1.0))
        g = int(g + (255 - g) * (factor - 1.0))
        b = int(b + (255 - b) * (factor - 1.0))
    else:
        r, g, b = (int(c * factor) for c in (r, g, b))
    return f"#{max(0, min(255, r)):02x}{max(0, min(255, g)):02x}{max(0, min(255, b)):02x}"


def plot_bess_revenue_waterfall(
    year1_kpis: dict[str, Any],
    out_path: Path,
    *,
    econ: dict[str, Any] | None = None,
) -> Path:
    """Render a horizontal-step waterfall chart.

    The first bar is the BESS's DAM-arbitrage segment (exports net of
    grid-charging expense, labelled ``DAM``); each subsequent bar is
    the per-product capacity + activation revenue.  When the fees are
    on, the battery's exact share of the energy-aggregator fee (a flat
    percentage of its gross DAM export revenue) and the BSP fee on
    gross balancing revenue step the total down before the final bar,
    so ``Total BESS revenue`` is net of both route-to-market fees.
    """
    out_path = Path(out_path)
    bess_dam = float(year1_kpis.get("revenue_bess_dam_eur", 0.0) or 0.0)
    products = [
        (label, float(year1_kpis.get(key, 0.0) or 0.0), colour_key)
        for key, label, colour_key in _BM_PRODUCTS
    ]
    if abs(bess_dam) <= 1e-9 and all(abs(v) <= 1e-9 for _, v, _ in products):
        return _empty_placeholder(
            out_path, "No BESS revenue (waterfall not rendered).",
        )

    # Energy-aggregator fee share attributable to the battery's DAM
    # exports.  The fee is a flat percentage of energy revenue, so the
    # per-source attribution is exact: fee_pct x the battery's GROSS
    # DAM export revenue (the grid-charging expense is a cost, not
    # revenue, and carries no fee).  Absent when the fee is off.
    energy_fee_frac = 0.0
    if econ is not None:
        energy_fee_frac = max(0.0, min(
            1.0,
            float(econ.get("aggregator_fee_pct_revenue", 0.0) or 0.0)
            / 100.0,
        ))
    bess_dam_gross = float(
        year1_kpis.get("profit_export_from_bess_eur", 0.0) or 0.0
    )
    energy_fee = -energy_fee_frac * max(bess_dam_gross, 0.0)

    # Optional balancing-aggregator (BSP) fee — a deduction on GROSS
    # balancing revenue (the five products; the DAM-arbitrage segment is
    # not balancing and carries no BSP fee).  Inserted as its own negative
    # step before the total so the waterfall total steps down by it; absent
    # when the fee is off (default 0).
    bal_fee_frac = 0.0
    if econ is not None:
        bal_fee_frac = max(0.0, min(
            1.0,
            float(econ.get("balancing_aggregator_fee_pct_revenue", 0.0) or 0.0)
            / 100.0,
        ))
    bm_gross = sum(v for _label, v, _ck in products)
    bal_fee = -bal_fee_frac * max(bm_gross, 0.0)

    steps: list[tuple[str, float, str]] = [
        (_BESS_DAM_LABEL, bess_dam, _BESS_DAM_COLOUR),
        *((label, v, BM_COLOURS[ck]) for label, v, ck in products),
    ]
    if energy_fee < -1e-9:
        steps.append((
            "Aggregator fee", energy_fee,
            financial_color("Aggregator fee"),
        ))
    if bal_fee < -1e-9:
        steps.append((
            "Balancing aggregator fee", bal_fee,
            financial_color("Balancing aggregator fee"),
        ))
    labels = [s[0] for s in steps] + ["Total BESS revenue"]
    values = [s[1] for s in steps] + [0.0]
    colours = [s[2] for s in steps] + [_shade(_BESS_DAM_COLOUR, 0.65)]
    cumulative = 0.0
    bottoms = []
    heights = []
    for i, v in enumerate(values):
        if i == len(values) - 1:
            total = cumulative
            bottoms.append(0.0)
            heights.append(total)
        else:
            bottoms.append(cumulative if v >= 0 else cumulative + v)
            heights.append(abs(v))
            cumulative += v

    fmt_mode = resolve_currency_format(econ)
    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    x = np.arange(len(labels))
    for i, (label, h, b, c) in enumerate(zip(labels, heights, bottoms, colours, strict=True)):
        ax.bar(x[i], h, bottom=b, color=c, edgecolor="black", linewidth=0.4, label=label)
        annotation_value = values[i] if i < len(values) - 1 else cumulative
        ax.text(
            x[i], b + h, format_eur(float(annotation_value), fmt_mode),
            ha="center", va="bottom", fontsize=7,
        )

    ax.axhline(0.0, color="black", linewidth=0.6)
    ax.set_xticks(x)
    # Rotated ticks: with all five balancing products plus the fee and
    # total columns present, horizontal labels collide at 7x4 inches.
    ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.set_ylabel("EUR")
    ax.yaxis.set_major_formatter(euro_axis_formatter(fmt_mode))
    if show_titles():
        ax.set_title(
            f"BESS revenue waterfall{title_prefix(get_scenario_label())} "
            f"(Year 1)"
        )
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    apply_universal_margins(ax)
    apply_fine_ticks(ax)
    return save_figure(out_path)


def plot_bess_capacity_vs_activation_split(
    year1_kpis: dict[str, Any],
    out_path: Path,
    *,
    econ: dict[str, Any] | None = None,
) -> Path:
    """Grouped bar chart: per balancing product, capacity vs activation.

    FCR gets a single capacity-only bar (FCR is symmetric and pays no
    activation in this model); the four aFRR / mFRR products each get
    a side-by-side (capacity, activation) pair.
    """
    out_path = Path(out_path)
    products = ["fcr", "afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn"]
    labels = ["FCR", "aFRR-up", "aFRR-down", "mFRR-up", "mFRR-down"]
    has_activation = {"fcr": False, "afrr_up": True, "afrr_dn": True,
                      "mfrr_up": True, "mfrr_dn": True}
    cap = [
        float(year1_kpis.get(f"bm_{p}_capacity_revenue_eur", 0.0) or 0.0)
        for p in products
    ]
    act = [
        float(year1_kpis.get(f"bm_{p}_activation_revenue_eur", 0.0) or 0.0)
        for p in products
    ]
    if all(abs(c) <= 1e-9 for c in cap) and all(abs(a) <= 1e-9 for a in act):
        return _empty_placeholder(
            out_path,
            "No balancing revenue (capacity-vs-activation plot not rendered).",
        )

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    x = np.arange(len(products))
    width = 0.35
    for i, p in enumerate(products):
        cap_colour = _shade(BM_COLOURS[p], 1.35)
        act_colour = _shade(BM_COLOURS[p], 0.7)
        if has_activation[p]:
            ax.bar(x[i] - width / 2, cap[i], width,
                   color=cap_colour, edgecolor="black", linewidth=0.4,
                   label="Capacity" if i == 1 else None)
            ax.bar(x[i] + width / 2, act[i], width,
                   color=act_colour, edgecolor="black", linewidth=0.4,
                   label="Activation" if i == 1 else None)
        else:
            ax.bar(x[i], cap[i], width,
                   color=cap_colour, edgecolor="black", linewidth=0.4,
                   label="Capacity (FCR)" if i == 0 else None)

    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.set_ylabel("EUR")
    ax.yaxis.set_major_formatter(euro_axis_formatter(resolve_currency_format(econ)))
    if show_titles():
        ax.set_title(
            f"Balancing revenue: capacity vs activation"
            f"{title_prefix(get_scenario_label())} (Year 1)"
        )
    reserve_legend_headroom(ax, loc="best")
    ax.legend(loc="best")
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    apply_universal_margins(ax, skip_y=True)
    apply_fine_ticks(ax)
    return save_figure(out_path)


def plot_bess_revenue_by_month(
    res_year1: pd.DataFrame,
    year1_kpis: dict[str, Any],
    out_path: Path,
    *,
    econ: dict[str, Any] | None = None,
) -> Path:
    """Twelve stacked bars showing per-month BESS revenue breakdown.

    The DAM-arbitrage segment is computed per-month from the dispatch
    frame (``profit_export_from_bess_eur`` minus
    ``expense_charge_bess_grid_eur``).  Each balancing product's annual
    revenue is allocated to months in proportion to the per-step
    reservation profile so the per-month breakdown reflects when
    capacity was reserved, not a flat split.  When the route-to-market
    fees are on, their exact monthly shares (flat percentages of the
    gross monthly DAM export revenue and of the monthly balancing
    allocation) draw as negative bars below zero, mirroring the
    waterfall's net total.
    """
    out_path = Path(out_path)
    if res_year1.empty or "timestamp" not in res_year1.columns:
        return _empty_placeholder(out_path, "No Year-1 dispatch frame.")

    ts = pd.to_datetime(res_year1["timestamp"])
    months = ts.dt.month
    by_month_dam = pd.Series(
        (res_year1.get("profit_export_from_bess_eur", 0.0).astype(float)
         - res_year1.get("expense_charge_bess_grid_eur", 0.0).astype(float)).to_numpy(),
        index=months.to_numpy(),
    ).groupby(level=0).sum().reindex(range(1, 13), fill_value=0.0)

    bm_per_month: dict[str, np.ndarray] = {}
    for key, label, _colour_key in _BM_PRODUCTS:
        annual = float(year1_kpis.get(key, 0.0) or 0.0)
        product = key.removeprefix("revenue_bess_").removesuffix("_eur")
        rcol = f"bm_reservation_{product}_kw"
        if rcol in res_year1.columns and abs(annual) > 1e-9:
            r = pd.Series(
                res_year1[rcol].astype(float).to_numpy(),
                index=months.to_numpy(),
            ).groupby(level=0).sum().reindex(range(1, 13), fill_value=0.0)
            denom = float(r.sum())
            share = (r / denom).to_numpy() if denom > 1e-9 else np.full(12, 1.0 / 12.0)
            bm_per_month[label] = annual * share
        else:
            bm_per_month[label] = np.zeros(12)

    has_data = abs(by_month_dam.to_numpy()).sum() > 1e-9 or any(
        abs(v).sum() > 1e-9 for v in bm_per_month.values()
    )
    if not has_data:
        return _empty_placeholder(
            out_path, "No BESS revenue in any month.",
        )

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    # Match the house monthly-axis convention (mdates "%m-%Y") by using
    # the Year-1 calendar year extracted from the dispatch timestamp.
    year_label = int(ts.dt.year.iloc[0])
    month_labels = [f"{m:02d}-{year_label}" for m in range(1, 13)]
    x = np.arange(12)
    dam_arr = by_month_dam.to_numpy()
    ax.bar(x, dam_arr, color=_BESS_DAM_COLOUR,
           edgecolor="black", linewidth=0.4, label=_BESS_DAM_LABEL)
    bottoms = dam_arr.copy()
    for _key, label, colour_key in _BM_PRODUCTS:
        arr = bm_per_month[label]
        if abs(arr).sum() <= 1e-9:
            continue
        ax.bar(x, arr, bottom=bottoms, color=BM_COLOURS[colour_key],
               edgecolor="black", linewidth=0.4, label=label)
        bottoms = bottoms + arr

    # Monthly shares of the two route-to-market fees, mirroring the
    # waterfall so the twelve months sum to the same net total.  Both
    # fees are flat percentages, so the monthly attribution is exact:
    # the energy-aggregator fee follows the battery's gross monthly DAM
    # export revenue and the BSP fee follows the monthly balancing
    # allocation.  Drawn as negative bars below the zero line.
    energy_fee_frac = 0.0
    bal_fee_frac = 0.0
    if econ is not None:
        energy_fee_frac = max(0.0, min(1.0, float(
            econ.get("aggregator_fee_pct_revenue", 0.0) or 0.0) / 100.0))
        bal_fee_frac = max(0.0, min(1.0, float(
            econ.get("balancing_aggregator_fee_pct_revenue", 0.0) or 0.0)
            / 100.0))
    dam_gross_monthly = pd.Series(
        res_year1.get("profit_export_from_bess_eur", 0.0)
        .astype(float).to_numpy(),
        index=months.to_numpy(),
    ).groupby(level=0).sum().reindex(range(1, 13), fill_value=0.0).to_numpy()
    bm_monthly_total = np.sum(
        [bm_per_month[label] for _k, label, _c in _BM_PRODUCTS], axis=0,
    )
    energy_fee_arr = -energy_fee_frac * np.maximum(dam_gross_monthly, 0.0)
    bal_fee_arr = -bal_fee_frac * np.maximum(bm_monthly_total, 0.0)
    neg_bottoms = np.zeros(12)
    for label, arr in (
        ("Aggregator fee", energy_fee_arr),
        ("Balancing aggregator fee", bal_fee_arr),
    ):
        if abs(arr).sum() <= 1e-9:
            continue
        ax.bar(x, arr, bottom=neg_bottoms,
               color=financial_color(label),
               edgecolor="black", linewidth=0.4, label=label)
        neg_bottoms = neg_bottoms + arr

    ax.axhline(0.0, color="black", linewidth=0.6)
    ax.set_xticks(x)
    ax.set_xticklabels(month_labels, rotation=30, ha="right")
    ax.set_xlabel("Month")
    ax.set_ylabel("EUR")
    ax.yaxis.set_major_formatter(euro_axis_formatter(resolve_currency_format(econ)))
    if show_titles():
        ax.set_title(
            f"BESS revenue by month{title_prefix(get_scenario_label())} "
            f"(Year 1)"
        )
    # Enforce the canonical legend order: DAM first, then each balancing
    # product in _BM_PRODUCTS order, then the fee deductions.
    # Independent of stacking order.
    handles, labels_drawn = ax.get_legend_handles_labels()
    by_label = dict(zip(labels_drawn, handles, strict=True))
    ordered_labels = [_BESS_DAM_LABEL] + [
        label for _key, label, _colour_key in _BM_PRODUCTS
    ] + ["Aggregator fee", "Balancing aggregator fee"]
    ordered = [
        (by_label[lbl], lbl) for lbl in ordered_labels if lbl in by_label
    ]
    if ordered:
        reserve_legend_headroom(ax, loc="best")
        ax.legend(
            [h for h, _ in ordered], [lbl for _, lbl in ordered],
            loc="best", fontsize=7,
        )
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    apply_universal_margins(ax, skip_y=True)
    apply_fine_ticks(ax)
    return save_figure(out_path)
