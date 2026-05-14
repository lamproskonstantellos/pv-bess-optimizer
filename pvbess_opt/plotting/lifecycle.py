"""Lifecycle plots.

* :func:`plot_revenue_stack_yearly` — stacked yearly revenue source
  decomposition with the net line overlaid.
* :func:`plot_lifetime_cycles` — equivalent BESS cycles per operating
  year (post-degradation).  Skipped when no BESS is in the project.
* :func:`plot_lcoe_lcos_summary` — single horizontal-bar comparison
  panel (LCOE top, LCOS bottom) with the project sensitivity range
  overlaid on Lazard 2024 industry benchmark bands.  PV-only / BESS-
  only projects show an italic "N/A" line for the missing row.

Industry benchmark constants (update annually):

* :data:`BENCHMARK_LCOE_PV_UTILITY_EUR_PER_MWH`
  — Lazard *Levelized Cost of Energy+ 2024*, utility-scale PV band.
* :data:`BENCHMARK_LCOS_LITHIUM_ION_EUR_PER_MWH`
  — Lazard *Levelized Cost of Storage v9 2024*, four-hour
  lithium-ion utility-scale band.

These are hard-coded at module level so the engineering team can
update them once a year without re-running an external lookup.

EUR axes use the compact ``EUR 12.3M`` / ``EUR 45k`` formatter via
:func:`pvbess_opt.plotting._currency.euro_axis_formatter`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ..config import FINANCIAL_COLORS, apply_financial_legend, financial_color
from ._currency import euro_axis_formatter
from .financial import _integer_year_axis
from .style import (
    annotate_value_safe,
    apply_universal_margins,
    save_figure,
    show_titles,
)

# ---------------------------------------------------------------------------
# Industry benchmark bands (Lazard 2024 — update annually)
# ---------------------------------------------------------------------------
#
# Source: Lazard *Levelized Cost of Energy+ v17* (LCOE) and *Levelized
# Cost of Storage v9* (LCOS), both 2024 edition.  Lazard publishes in
# USD; bands below are EUR-equivalent at ~1.08 EUR/USD (mid-2024).
#
# * LCOE: utility-scale PV, unsubsidised band USD 29-92/MWh.  Rounded
#   to EUR 30-85/MWh.
# * LCOS: 100 MW / 4-hour utility-scale Li-ion BESS, unsubsidised band
#   USD 170-296/MWh.  Rounded to EUR 157-274/MWh.
#
# Workbook overrides: the four benchmark_lcoe_* / benchmark_lcos_* keys
# in the economics sheet override these per-project.

BENCHMARK_LCOE_PV_UTILITY_EUR_PER_MWH: tuple[float, float] = (30.0, 85.0)

BENCHMARK_LCOS_LITHIUM_ION_EUR_PER_MWH: tuple[float, float] = (157.0, 274.0)


def _resolve_currency_format(econ: dict[str, Any] | None) -> str:
    if econ is None:
        return "auto"
    raw = str(econ.get("currency_format", "auto") or "auto").strip().lower()
    if raw not in ("auto", "millions", "raw"):
        return "auto"
    return raw


def plot_revenue_stack_yearly(
    yearly_cf: pd.DataFrame,
    year1_kpis: dict[str, Any],
    out_path: Path,
    *,
    econ: dict[str, Any] | None = None,
) -> Path:
    """Stacked bar per operating year of the four revenue sources minus
    the grid-charging cost, with the net line overlaid.

    Stacks are derived by scaling the Year-1 revenue components by the
    yearly cashflow's ``revenue_eur`` column — that keeps the plot
    consistent with the NPV/IRR pipeline.
    """
    out_path = Path(out_path)
    if yearly_cf.empty:
        return _empty_placeholder(out_path, "No cashflow data.")

    op = yearly_cf.loc[yearly_cf["project_year"] >= 1].copy()
    if op.empty:
        return _empty_placeholder(out_path, "No operating-year rows.")

    y1_total = float(op.loc[op["project_year"] == 1, "revenue_eur"].iloc[0])
    rev_load_pv_y1 = float(year1_kpis.get("profit_load_from_pv_eur", 0.0) or 0.0)
    rev_load_bess_y1 = float(year1_kpis.get("profit_load_from_bess_eur", 0.0) or 0.0)
    rev_exp_pv_y1 = float(year1_kpis.get("profit_export_from_pv_eur", 0.0) or 0.0)
    rev_exp_bess_y1 = float(year1_kpis.get("profit_export_from_bess_eur", 0.0) or 0.0)
    cost_grid_y1 = float(year1_kpis.get("expense_charge_bess_grid_eur", 0.0) or 0.0)

    if abs(y1_total) > 1e-9:
        ratio = op["revenue_eur"].astype(float) / y1_total
    else:
        ratio = pd.Series(0.0, index=op.index, dtype=float)

    years = (
        op["calendar_year"].to_numpy(dtype=int)
        if "calendar_year" in op.columns
        else op["project_year"].to_numpy(dtype=int)
    )
    load_pv = (rev_load_pv_y1 * ratio).to_numpy()
    load_bess = (rev_load_bess_y1 * ratio).to_numpy()
    exp_pv = (rev_exp_pv_y1 * ratio).to_numpy()
    exp_bess = (rev_exp_bess_y1 * ratio).to_numpy()
    cost = -((cost_grid_y1 * ratio).to_numpy())  # drawn negative

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    bottoms = np.zeros_like(load_pv)
    for arr, label in [
        (load_pv, "Load from PV"),
        (load_bess, "Load from BESS"),
        (exp_pv, "Export from PV"),
        (exp_bess, "Export from BESS"),
    ]:
        if np.any(arr > 1e-9):
            ax.bar(years, arr, bottom=bottoms, color=financial_color(label),
                   edgecolor="black", linewidth=0.4, label=label)
            bottoms = bottoms + arr
    if np.any(cost < -1e-9):
        ax.bar(years, cost, color=financial_color("Grid-charging cost"),
               edgecolor="black", linewidth=0.4,
               label="Grid-charging cost")
    net = (op["revenue_eur"].astype(float)).to_numpy()
    # IEEE-friendly emphasis line: near-black solid markers.  Round-3
    # universality rule forbids markeredgecolor="white" rings; line
    # contrast comes from the charcoal colour itself.
    ax.plot(
        years, net,
        color=financial_color("Net revenue"),
        linewidth=1.5,
        marker="o", markersize=4,
        markerfacecolor=financial_color("Net revenue"),
        label="Net revenue",
    )
    ax.axhline(0.0, color="black", linewidth=0.6)

    # Optional dashed real-EUR (deflated) trajectory — only meaningful
    # when nominal revenue is being inflated year on year.  Helps the
    # reader distinguish "stack growing because of inflation" from
    # "stack growing because of generation".  The deflator follows the
    # retail inflation index (CPI proxy) since the DAM index is
    # typically 0; the plot is a CPI-purchasing-power view.
    rev_infl_pct = 0.0
    if econ is not None:
        rev_infl_pct = float(econ.get("retail_inflation_pct", 0.0) or 0.0)
    if rev_infl_pct > 1.0e-9:
        infl = rev_infl_pct / 100.0
        project_years = op["project_year"].to_numpy(dtype=int)
        deflator = 1.0 / np.power(1.0 + infl, project_years - 1)
        real_net = net * deflator
        # Dashed companion line distinguishes itself by linestyle; no
        # markers — standard IEEE convention for "derived" series.
        ax.plot(
            years, real_net,
            color=financial_color("Real-EUR net (deflated)"),
            linewidth=1.2,
            linestyle="--", marker="", alpha=0.85,
            label="Real-EUR net (deflated)",
        )

    ax.set_xlabel(
        "Calendar year" if "calendar_year" in op.columns else "Project year"
    )
    _integer_year_axis(ax)
    ax.set_ylabel("EUR")
    ax.yaxis.set_major_formatter(euro_axis_formatter(_resolve_currency_format(econ)))
    if show_titles():
        ax.set_title(f"Revenue stack — {int(years[0])}-{int(years[-1])}")
    apply_financial_legend(ax)
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    apply_universal_margins(ax)
    return save_figure(out_path)


def plot_lifetime_cycles(
    lifetime_yearly: pd.DataFrame,
    bess_kwh: float,
    out_path: Path,
    *,
    bess_present: bool = True,
) -> Path:
    """Bar chart of equivalent BESS cycles per operating year."""
    out_path = Path(out_path)
    if not bess_present or bess_kwh <= 0.0:
        return _empty_placeholder(
            out_path, "BESS not part of this project — no cycle plot.",
        )
    if lifetime_yearly.empty or "bess_discharge_mwh" not in lifetime_yearly.columns:
        return _empty_placeholder(out_path, "No lifetime data.")

    df = lifetime_yearly.copy()
    df["cycles"] = df["bess_discharge_mwh"] * 1000.0 / float(bess_kwh)
    years = (
        df["calendar_year"].to_numpy(dtype=int)
        if "calendar_year" in df.columns
        else df["project_year"].to_numpy(dtype=int)
    )

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    ax.bar(years, df["cycles"].to_numpy(dtype=float),
           color=FINANCIAL_COLORS["net"],
           edgecolor="black", linewidth=0.4)
    total = float(df["cycles"].sum())
    ax.axhline(0.0, color="black", linewidth=0.6)
    ax.set_xlabel(
        "Calendar year" if "calendar_year" in df.columns else "Project year"
    )
    _integer_year_axis(ax)
    ax.set_ylabel("Equivalent cycles per year")
    if show_titles():
        ax.set_title(
            f"BESS Equivalent Cycles — total {total:.0f} over "
            f"{int(years[0])}-{int(years[-1])}"
        )
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    annotate_value_safe(
        ax, 0.99, 0.95, f"Total: {total:.0f} cycles",
        transform=ax.transAxes,
        ha="right", va="top", fontsize=7,
        bbox_alpha=0.8,
    )
    apply_universal_margins(ax)
    return save_figure(out_path)


def plot_lcoe_lcos_summary(
    fin_kpis: dict[str, Any],
    sensitivity_df: pd.DataFrame | None,
    capacities: dict[str, float],
    econ: dict[str, Any],
    out_path: Path,
) -> Path:
    """Single horizontal-bar comparison panel with industry benchmark bands.

    LCOE on the top row, LCOS on the bottom row.  Round-3 redesign:
    every numeric value (base, sensitivity range, benchmark range) is
    reported in the row legend so the plot face stays free of bbox
    annotations, italic prose captions, and diamond markers.

    Each row shows:

    * a light-grey shaded benchmark band (Lazard 2024 EUR-equivalent
      range, labelled in the legend);
    * a saturated bar over the project's sensitivity range
      ``[base × low_factor, base × high_factor]``, labelled in the
      legend with its numeric span;
    * a black vertical line at the base value, labelled in the legend
      with its numeric value (no diamond, no white marker edge ring).

    PV-only projects show a plain "BESS not part of this project —
    LCOS N/A" line in place of the LCOS row; BESS-only swaps the
    other way.  Hybrid projects render both rows at figsize=(7, 4);
    single-row projects render at (7, 2.5).

    margins: delegated.  Each row sets its own 12% x-padding inside
    ``_draw_benchmark_row`` and a fixed y-range of (-0.6, 0.6) wider
    than the bar height — the universal helper would over-pad.
    """
    out_path = Path(out_path)
    pv_kwp = float(capacities.get("pv_kwp", 0.0) or 0.0)
    bess_kw = float(capacities.get("bess_kw", 0.0) or 0.0)
    base_lcoe = float(fin_kpis.get("lcoe_eur_per_mwh", float("nan")))
    base_lcos = float(fin_kpis.get("lcos_eur_per_mwh", float("nan")))
    capex_d = float(econ.get("sensitivity_capex_delta_pct", 10.0)) / 100.0
    opex_d = float(econ.get("sensitivity_opex_delta_pct", 10.0)) / 100.0
    high_factor = (1.0 + capex_d) * (1.0 + opex_d)
    low_factor = (1.0 - capex_d) * (1.0 - opex_d)
    _ = sensitivity_df  # kept for API symmetry; range derived directly above

    pv_present = pv_kwp > 0.0 and not np.isnan(base_lcoe)
    bess_present = bess_kw > 0.0 and not np.isnan(base_lcos)
    figsize = (7, 4) if (pv_present and bess_present) else (7, 2.5)

    # Workbook overrides.  When the economics sheet carries
    # benchmark_* keys, use them; otherwise fall back to the module
    # constants (Lazard 2024 EUR-equivalent).
    lcoe_band = (
        float(econ.get("benchmark_lcoe_low_eur_per_mwh",
                       BENCHMARK_LCOE_PV_UTILITY_EUR_PER_MWH[0])),
        float(econ.get("benchmark_lcoe_high_eur_per_mwh",
                       BENCHMARK_LCOE_PV_UTILITY_EUR_PER_MWH[1])),
    )
    lcos_band = (
        float(econ.get("benchmark_lcos_low_eur_per_mwh",
                       BENCHMARK_LCOS_LITHIUM_ION_EUR_PER_MWH[0])),
        float(econ.get("benchmark_lcos_high_eur_per_mwh",
                       BENCHMARK_LCOS_LITHIUM_ION_EUR_PER_MWH[1])),
    )

    # Independent x-axes per row.  LCOS values (~1500 EUR/MWh) and LCOE
    # values (~100 EUR/MWh) span very different ranges; sharing the
    # x-axis crushed the LCOE row into <5% of the panel.
    fig, axes = plt.subplots(2, 1, figsize=figsize, sharex=False)
    _draw_benchmark_row(
        axes[0],
        base=base_lcoe,
        low=base_lcoe * low_factor if pv_present else float("nan"),
        high=base_lcoe * high_factor if pv_present else float("nan"),
        bar_colour=FINANCIAL_COLORS["lcoe_bar"],
        benchmark=lcoe_band,
        label="LCOE", asset_present=pv_present,
        absent_message="PV not part of this project — LCOE N/A",
    )
    _draw_benchmark_row(
        axes[1],
        base=base_lcos,
        low=base_lcos * low_factor if bess_present else float("nan"),
        high=base_lcos * high_factor if bess_present else float("nan"),
        bar_colour=FINANCIAL_COLORS["lcos_bar"],
        benchmark=lcos_band,
        label="LCOS", asset_present=bess_present,
        absent_message="BESS not part of this project — LCOS N/A",
    )
    axes[0].set_xlabel("EUR/MWh")
    axes[1].set_xlabel("EUR/MWh")

    if show_titles():
        fig.suptitle("Levelized Cost Summary — Lazard 2024 benchmark")
    fig.tight_layout()
    out = Path(out_path).with_suffix(".pdf")
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, format="pdf", bbox_inches="tight")
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_placeholder(out_path: Path, message: str) -> Path:
    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    ax.text(0.5, 0.5, message, ha="center", va="center", fontsize=10,
            transform=ax.transAxes)
    ax.set_xticks([])
    ax.set_yticks([])
    return save_figure(out_path)


def _draw_benchmark_row(
    ax,
    *,
    base: float,
    low: float,
    high: float,
    bar_colour: str,
    benchmark: tuple[float, float],
    label: str,
    asset_present: bool,
    absent_message: str,
) -> None:
    """Single LCOE/LCOS row: benchmark band + project bar + base line.

    Round-3 redesign: every numeric value (benchmark band, project
    range, base) is reported in the legend; the plot face holds no
    bbox annotations, no italic captions, and no diamond markers.
    Each row uses its own x-axis scaled to the union of the benchmark
    band and the project sensitivity range with a 12 % margin.
    """
    ax.set_ylabel(label, rotation=0, ha="right", va="center",
                  labelpad=20, fontweight="bold")

    if not asset_present or np.isnan(base):
        ax.text(
            0.5, 0.5, absent_message, ha="center", va="center",
            fontsize=9, transform=ax.transAxes,
        )
        ax.set_yticks([])
        ax.set_ylim(-0.5, 0.5)
        ax.set_xlabel("")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        return

    bench_low, bench_high = float(benchmark[0]), float(benchmark[1])
    bar_low = float(min(low, high))
    bar_high = float(max(low, high))

    # Benchmark band behind the project bar.  Numeric range carried
    # in the legend label.
    ax.barh(
        [0], [bench_high - bench_low], left=bench_low, height=0.6,
        color=FINANCIAL_COLORS["benchmark_band"], alpha=0.45,
        edgecolor="grey", linewidth=0.4,
        label=(
            f"Lazard 2024 {label} band: "
            f"{bench_low:.0f}–{bench_high:.0f} EUR/MWh"
        ),
        zorder=1,
    )

    # Project sensitivity range (saturated colour).  Numeric range
    # carried in the legend label.
    ax.barh(
        [0], [bar_high - bar_low], left=bar_low, height=0.35,
        color=bar_colour, alpha=0.85, edgecolor="black", linewidth=0.6,
        label=(
            f"{label} project range: "
            f"{bar_low:.0f}–{bar_high:.0f} EUR/MWh"
        ),
        zorder=3,
    )

    # Base value drawn as a vertical line (no diamond, no marker-edge
    # ring).  Numeric value carried in the legend label.
    ax.plot(
        [base, base], [-0.25, 0.25],
        color=FINANCIAL_COLORS["base_marker"], linewidth=1.4,
        solid_capstyle="butt", zorder=5,
        label=f"Base {label}: {base:.0f} EUR/MWh",
    )

    ax.set_yticks([])
    ax.set_ylim(-0.6, 0.6)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(True, axis="x", linestyle="--", alpha=0.5)
    ax.legend(loc="upper right", framealpha=0.9, fontsize=6, ncol=1)

    # Per-row independent x-axis: span the union of (benchmark, project
    # range) with 12 % padding on each side so legend / labels never
    # get clipped.
    span_lo = min(bench_low, bar_low)
    span_hi = max(bench_high, bar_high)
    pad = 0.12 * max(span_hi - span_lo, 1.0)
    ax.set_xlim(max(0.0, span_lo - pad), span_hi + pad)
