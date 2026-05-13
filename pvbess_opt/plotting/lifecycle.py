"""Lifecycle plots — added in v0.6, redesigned in v0.8.

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

from ..config import FINANCIAL_COLORS
from ._currency import euro_axis_formatter
from .financial import _integer_year_axis
from .style import save_figure, show_titles

# ---------------------------------------------------------------------------
# Industry benchmark bands (Lazard 2024 — update annually)
# ---------------------------------------------------------------------------

# Lazard *Levelized Cost of Energy+ 2024*, utility-scale PV (EUR/MWh).
BENCHMARK_LCOE_PV_UTILITY_EUR_PER_MWH: tuple[float, float] = (30.0, 50.0)

# Lazard *Levelized Cost of Storage v9 2024*, four-hour
# lithium-ion utility-scale (EUR/MWh).
BENCHMARK_LCOS_LITHIUM_ION_EUR_PER_MWH: tuple[float, float] = (100.0, 250.0)


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
    for arr, colour, label in [
        (load_pv, FINANCIAL_COLORS["load_from_pv"], "Load from PV"),
        (load_bess, FINANCIAL_COLORS["load_from_bess"], "Load from BESS"),
        (exp_pv, FINANCIAL_COLORS["export_from_pv"], "Export from PV"),
        (exp_bess, FINANCIAL_COLORS["export_from_bess"], "Export from BESS"),
    ]:
        if np.any(arr > 1e-9):
            ax.bar(years, arr, bottom=bottoms, color=colour,
                   edgecolor="black", linewidth=0.4, label=label)
            bottoms = bottoms + arr
    if np.any(cost < -1e-9):
        ax.bar(years, cost, color=FINANCIAL_COLORS["grid_charge_cost"],
               edgecolor="black", linewidth=0.4,
               label="Grid-charging cost")
    net = (op["revenue_eur"].astype(float)).to_numpy()
    ax.plot(years, net, color=FINANCIAL_COLORS["discounted"], linewidth=1.5,
            marker="o", markersize=3, label="Net revenue")
    ax.axhline(0.0, color="black", linewidth=0.6)

    ax.set_xlabel(
        "Calendar year" if "calendar_year" in op.columns else "Project year"
    )
    _integer_year_axis(ax)
    ax.set_ylabel("EUR")
    ax.yaxis.set_major_formatter(euro_axis_formatter(_resolve_currency_format(econ)))
    if show_titles():
        ax.set_title(f"Revenue stack — {int(years[0])}-{int(years[-1])}")
    ax.legend(loc="best", framealpha=0.9, fontsize=7, ncol=2)
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
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
           color="#1565C0", edgecolor="black", linewidth=0.4)
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
    # Footer total annotation.
    ax.text(
        0.99, 0.95, f"Total: {total:.0f} cycles",
        ha="right", va="top", fontsize=7, transform=ax.transAxes,
        bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "grey",
              "linewidth": 0.5},
    )
    return save_figure(out_path)


def plot_lcoe_lcos_summary(
    fin_kpis: dict[str, Any],
    sensitivity_df: pd.DataFrame | None,
    capacities: dict[str, float],
    econ: dict[str, Any],
    out_path: Path,
) -> Path:
    """Single horizontal-bar comparison panel with industry benchmark bands.

    LCOE on the top row, LCOS on the bottom row, both rendered against
    the same EUR/MWh axis.  Each row shows:

    * a saturated bar over the project's sensitivity range
      ``[base × low_factor, base × high_factor]``;
    * a black diamond marker at the base value;
    * a light-grey shaded band behind the bar showing the Lazard 2024
      industry benchmark range (LCOE: 30–50, LCOS: 100–250 EUR/MWh).

    PV-only projects show an italic "BESS not part of this project —
    LCOS N/A" line in place of the LCOS row; BESS-only swaps the
    other way.  Hybrid projects render both rows at figsize=(7, 4);
    single-row projects render at (7, 2.5).
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

    fig, axes = plt.subplots(2, 1, figsize=figsize, sharex=True)
    _draw_benchmark_row(
        axes[0],
        base=base_lcoe,
        low=base_lcoe * low_factor if pv_present else float("nan"),
        high=base_lcoe * high_factor if pv_present else float("nan"),
        bar_colour=FINANCIAL_COLORS["lcoe_bar"],
        benchmark=BENCHMARK_LCOE_PV_UTILITY_EUR_PER_MWH,
        label="LCOE", asset_present=pv_present,
        absent_message="PV not part of this project — LCOE N/A",
    )
    _draw_benchmark_row(
        axes[1],
        base=base_lcos,
        low=base_lcos * low_factor if bess_present else float("nan"),
        high=base_lcos * high_factor if bess_present else float("nan"),
        bar_colour=FINANCIAL_COLORS["lcos_bar"],
        benchmark=BENCHMARK_LCOS_LITHIUM_ION_EUR_PER_MWH,
        label="LCOS", asset_present=bess_present,
        absent_message="BESS not part of this project — LCOS N/A",
    )
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
    """Single LCOE/LCOS row: benchmark band + project bar + base marker."""
    if not asset_present or np.isnan(base):
        ax.text(
            0.5, 0.5, absent_message, ha="center", va="center",
            fontsize=9, fontstyle="italic", transform=ax.transAxes,
        )
        ax.set_yticks([])
        ax.set_ylim(-0.5, 0.5)
        ax.set_xlabel("")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.set_ylabel(label, rotation=0, ha="right", va="center", labelpad=20)
        return

    bench_low, bench_high = float(benchmark[0]), float(benchmark[1])
    bar_low = float(min(low, high))
    bar_high = float(max(low, high))

    # Reference benchmark band behind the project bar.
    ax.barh(
        [0], [bench_high - bench_low], left=bench_low, height=0.6,
        color=FINANCIAL_COLORS["benchmark_band"], alpha=0.45, edgecolor="grey", linewidth=0.4,
        label=f"Lazard 2024 {label} band ({bench_low:.0f}–{bench_high:.0f} EUR/MWh)",
        zorder=1,
    )
    # Project sensitivity range (saturated colour).
    ax.barh(
        [0], [bar_high - bar_low], left=bar_low, height=0.35,
        color=bar_colour, edgecolor="black", linewidth=0.6,
        label=f"{label} range",
        zorder=3,
    )
    # Base marker.
    ax.scatter(
        [base], [0], marker="D", s=64,
        color=FINANCIAL_COLORS["base_marker"], edgecolor="white", linewidth=0.5,
        label=f"Base {label}",
        zorder=5,
    )

    # Right-aligned annotation past the high end of the project bar.
    label_text = (
        f"{base:.0f} EUR/MWh "
        f"(range {bar_low:.0f}–{bar_high:.0f})"
    )
    ax.annotate(
        label_text, xy=(bar_high, 0), xytext=(8, 0),
        textcoords="offset points",
        ha="left", va="center", fontsize=7,
        bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "grey",
              "linewidth": 0.5, "boxstyle": "round,pad=0.2"},
    )

    ax.set_yticks([])
    ax.set_ylim(-0.5, 0.5)
    ax.set_ylabel(label, rotation=0, ha="right", va="center", labelpad=20)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(True, axis="x", linestyle="--", alpha=0.5)
    ax.legend(loc="upper right", framealpha=0.9, fontsize=6, ncol=1)
    # Pad the x-axis so the annotation has room.
    xmin = min(bench_low, bar_low) * 0.85 if bench_low > 0 else min(bench_low, bar_low) - 5
    xmax = max(bench_high, bar_high) * 1.35
    ax.set_xlim(xmin, xmax)
