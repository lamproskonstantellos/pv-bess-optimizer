"""Lifecycle plots — added in v0.6.

* :func:`plot_revenue_stack_yearly` — stacked yearly revenue source
  decomposition with the net line overlaid.
* :func:`plot_lifetime_cycles` — equivalent BESS cycles per operating
  year (post-degradation).  Skipped when no BESS is in the project.
* :func:`plot_lcoe_lcos_summary` — two-panel summary card (LCOE on
  the left, LCOS on the right) with base markers and shaded ranges
  derived from the sensitivity DataFrame.

EUR axes use the compact ``EUR 12.3M`` / ``EUR 45k`` formatter via
:func:`pvbess_opt.plotting._currency.euro_axis_formatter`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ._currency import euro_axis_formatter
from .style import save_figure, show_titles

_COLOR_LOAD_PV = "#2E7D32"      # green
_COLOR_LOAD_BESS = "#388E3C"    # darker green
_COLOR_EXPORT_PV = "#1565C0"    # blue
_COLOR_EXPORT_BESS = "#0D47A1"  # darker blue
_COLOR_GRID_COST = "#C62828"    # red (negative stack)
_COLOR_NET = "#6A1B9A"          # purple


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
        (load_pv, _COLOR_LOAD_PV, "Load from PV"),
        (load_bess, _COLOR_LOAD_BESS, "Load from BESS"),
        (exp_pv, _COLOR_EXPORT_PV, "Export from PV"),
        (exp_bess, _COLOR_EXPORT_BESS, "Export from BESS"),
    ]:
        if np.any(arr > 1e-9):
            ax.bar(years, arr, bottom=bottoms, color=colour,
                   edgecolor="black", linewidth=0.4, label=label)
            bottoms = bottoms + arr
    if np.any(cost < -1e-9):
        ax.bar(years, cost, color=_COLOR_GRID_COST,
               edgecolor="black", linewidth=0.4,
               label="Grid-charging cost")
    net = (op["revenue_eur"].astype(float)).to_numpy()
    ax.plot(years, net, color=_COLOR_NET, linewidth=1.5,
            marker="o", markersize=3, label="Net revenue")
    ax.axhline(0.0, color="black", linewidth=0.6)

    ax.set_xlabel(
        "Calendar year" if "calendar_year" in op.columns else "Project year"
    )
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
    """Two-panel summary card with LCOE on the left, LCOS on the right.

    Each panel shows the base-case marker plus a shaded range derived
    by re-evaluating the LCOE / LCOS formula with ±CAPEX / ±OPEX
    deltas from the sensitivity configuration.  PV-only / BESS-only
    projects show an N/A placeholder for the missing panel.
    """
    out_path = Path(out_path)
    pv_kwp = float(capacities.get("pv_kwp", 0.0) or 0.0)
    bess_kw = float(capacities.get("bess_kw", 0.0) or 0.0)
    base_lcoe = float(fin_kpis.get("lcoe_eur_per_mwh", float("nan")))
    base_lcos = float(fin_kpis.get("lcos_eur_per_mwh", float("nan")))
    capex_d = float(econ.get("sensitivity_capex_delta_pct", 10.0)) / 100.0
    opex_d = float(econ.get("sensitivity_opex_delta_pct", 10.0)) / 100.0
    # Combined ± range: high = (1+capex_d)(1+opex_d) - 1, low symmetric.
    high_factor = (1.0 + capex_d) * (1.0 + opex_d)
    low_factor = (1.0 - capex_d) * (1.0 - opex_d)
    _ = sensitivity_df  # kept for API symmetry; range derived directly above

    fig, axes = plt.subplots(1, 2, figsize=(8, 4))
    _draw_panel(
        axes[0], base_lcoe, low_factor, high_factor,
        title="LCOE", unit="EUR/MWh", asset_present=pv_kwp > 0.0,
        absent_message="No PV — LCOE N/A",
    )
    _draw_panel(
        axes[1], base_lcos, low_factor, high_factor,
        title="LCOS", unit="EUR/MWh", asset_present=bess_kw > 0.0,
        absent_message="No BESS — LCOS N/A",
    )
    if show_titles():
        fig.suptitle("Levelized Cost Summary")
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


def _draw_panel(
    ax,
    base_value: float,
    low_factor: float,
    high_factor: float,
    *,
    title: str,
    unit: str,
    asset_present: bool,
    absent_message: str,
) -> None:
    ax.set_title(title, fontsize=10)
    if not asset_present or np.isnan(base_value):
        ax.text(0.5, 0.5, absent_message, ha="center", va="center",
                fontsize=10, transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])
        return
    low = base_value * low_factor
    high = base_value * high_factor
    ax.barh([0], [high - low], left=low, height=0.4,
            color="#90CAF9", edgecolor="black", linewidth=0.6,
            label=f"±CAPEX·±OPEX range")
    ax.scatter([base_value], [0], color="#0D47A1", s=80, zorder=5,
               label="Base")
    ax.set_yticks([])
    ax.set_xlabel(unit)
    ax.legend(loc="upper right", framealpha=0.9, fontsize=7)
    ax.grid(True, axis="x", linestyle="--", alpha=0.5)
    ax.text(
        0.02, 0.95, f"Base: {base_value:,.1f} {unit}",
        ha="left", va="top", fontsize=8, transform=ax.transAxes,
        bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "grey",
              "linewidth": 0.5},
    )
