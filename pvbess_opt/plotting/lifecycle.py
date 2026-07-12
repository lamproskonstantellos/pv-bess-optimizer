"""Lifecycle plots.

* :func:`plot_revenue_stack_yearly` — stacked yearly revenue source
  decomposition with the net line overlaid.
* :func:`plot_lifetime_cycles` — equivalent BESS cycles per operating
  year (post-degradation).  Skipped when no BESS is in the project.
* :func:`plot_lcoe_summary` / :func:`plot_lcos_summary` — single-row
  horizontal-bar comparison panels written to separate PDFs, each
  showing the project sensitivity range over the Lazard 2024 industry
  benchmark band for the corresponding asset.  When the project does
  not include the asset the row collapses to an italic "N/A" message.

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

import logging
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ..constants import (
    BENCHMARK_LCOE_HIGH_EUR_PER_MWH,
    BENCHMARK_LCOE_LOW_EUR_PER_MWH,
    BENCHMARK_LCOS_HIGH_EUR_PER_MWH,
    BENCHMARK_LCOS_LOW_EUR_PER_MWH,
    DEFAULT_SENSITIVITY_DELTA_PCT,
)
from ..theme import FINANCIAL_COLORS, apply_financial_legend, financial_color
from ._currency import (
    euro_axis_formatter,
)
from ._currency import (
    resolve_currency_format as _resolve_currency_format,
)
from .financial import _integer_year_axis
from .style import (
    apply_fine_ticks,
    apply_universal_margins,
    legend_below,
    save_figure,
    save_figure_object,
    show_titles,
)
from .style import (
    empty_placeholder as _empty_placeholder,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BENCHMARK_LCOE_PV_UTILITY_EUR_PER_MWH",
    "BENCHMARK_LCOS_LITHIUM_ION_EUR_PER_MWH",
    "plot_lcoe_summary",
    "plot_lcos_summary",
    "plot_lifetime_cycles",
    "plot_revenue_stack_yearly",
]

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

BENCHMARK_LCOE_PV_UTILITY_EUR_PER_MWH: tuple[float, float] = (
    BENCHMARK_LCOE_LOW_EUR_PER_MWH, BENCHMARK_LCOE_HIGH_EUR_PER_MWH,
)

BENCHMARK_LCOS_LITHIUM_ION_EUR_PER_MWH: tuple[float, float] = (
    BENCHMARK_LCOS_LOW_EUR_PER_MWH, BENCHMARK_LCOS_HIGH_EUR_PER_MWH,
)




def plot_revenue_stack_yearly(
    yearly_cf: pd.DataFrame,
    year1_kpis: dict[str, Any],
    out_path: Path,
    *,
    econ: dict[str, Any] | None = None,
) -> Path:
    """Stacked bar per operating year of the four revenue sources minus
    the grid-charging cost, with the net line overlaid.

    Stacks are scaled per-stream so retail, DAM, and balancing
    indexation are rendered separately: retail-priced components
    (``Load from PV``, ``Load from BESS``) track the year-over-year
    ratio of ``yearly_cf['revenue_retail_eur']``; DAM-priced components
    (``Export from PV``, ``Export from BESS``, ``Grid-charging cost``)
    track ``yearly_cf['revenue_dam_eur']``.  Balancing per-product bars
    (``FCR``, ``aFRR-up/dn``, ``mFRR-up/dn``) scale by the BESS
    capacity-fade factor in ``yearly_cf['bess_capacity_factor']``
    indexed by ``econ['bm_inflation_pct']`` — the same growth
    :func:`build_yearly_cashflow` applies to the balancing-revenue
    cashflow column.  The aggregator-fee bar is read directly from
    ``yearly_cf['aggregator_fee_eur']``.  Fixtures lacking the
    per-stream columns fall back to a single ``revenue_eur``-based
    ratio applied uniformly.
    """
    out_path = Path(out_path)
    if yearly_cf.empty:
        return _empty_placeholder(out_path, "No cashflow data.")

    op = yearly_cf.loc[yearly_cf["project_year"] >= 1].copy()
    if op.empty:
        return _empty_placeholder(out_path, "No operating-year rows.")

    rev_load_pv_y1 = float(year1_kpis.get("profit_load_from_pv_eur", 0.0) or 0.0)
    rev_load_bess_y1 = float(year1_kpis.get("profit_load_from_bess_eur", 0.0) or 0.0)
    rev_exp_pv_y1 = float(year1_kpis.get("profit_export_from_pv_eur", 0.0) or 0.0)
    rev_exp_bess_y1 = float(year1_kpis.get("profit_export_from_bess_eur", 0.0) or 0.0)
    cost_grid_y1 = float(year1_kpis.get("expense_charge_bess_grid_eur", 0.0) or 0.0)

    y1_mask = op["project_year"] == 1
    has_streams = (
        "revenue_retail_eur" in op.columns
        and "revenue_dam_eur" in op.columns
    )
    if has_streams:
        y1_retail = float(op.loc[y1_mask, "revenue_retail_eur"].iloc[0])
        y1_dam = float(op.loc[y1_mask, "revenue_dam_eur"].iloc[0])
        if abs(y1_retail) > 1e-9:
            retail_ratio = op["revenue_retail_eur"].astype(float) / y1_retail
        else:
            retail_ratio = pd.Series(0.0, index=op.index, dtype=float)
        # Degenerate Year-1 DAM cases: a non-positive Year-1 base
        # (predominantly-negative DAM hours, or sign flip across years)
        # makes ``revenue_dam_eur / y1_dam`` either explode or invert
        # the per-year stack heights.  Fall back to the literal column
        # values so each year draws at its own height, and log a debug
        # so degenerate scenarios surface in the run log.
        dam_series = op["revenue_dam_eur"].astype(float)
        opposite_signs = (
            (y1_dam > 0) and (dam_series.min() < 0)
        ) or (
            (y1_dam < 0) and (dam_series.max() > 0)
        )
        if y1_dam > 1e-9 and not opposite_signs:
            dam_ratio = dam_series / y1_dam
        else:
            logger.debug(
                "plot_revenue_stack_yearly: degenerate DAM Year-1 base "
                "(y1_dam=%.3f, dam_min=%.3f, dam_max=%.3f); falling back "
                "to literal per-year values for the DAM stack.",
                y1_dam, float(dam_series.min()), float(dam_series.max()),
            )
            if abs(y1_dam) > 1e-9:
                dam_ratio = dam_series / y1_dam
            else:
                # Year-1 DAM is zero (e.g. self-consumption only).  The
                # DAM-side bar heights (rev_exp_pv_y1, rev_exp_bess_y1,
                # cost_grid_y1) are themselves zero in this regime, so
                # the multiplier is mathematically irrelevant; use a
                # unity series rather than zero, which previously also
                # squashed the balancing bars before they were given
                # their own scaling factor below.
                dam_ratio = pd.Series(1.0, index=op.index, dtype=float)
    else:
        y1_total = float(op.loc[y1_mask, "revenue_eur"].iloc[0])
        if abs(y1_total) > 1e-9:
            uniform_ratio = op["revenue_eur"].astype(float) / y1_total
        else:
            uniform_ratio = pd.Series(0.0, index=op.index, dtype=float)
        retail_ratio = uniform_ratio
        dam_ratio = uniform_ratio

    years = (
        op["calendar_year"].to_numpy(dtype=int)
        if "calendar_year" in op.columns
        else op["project_year"].to_numpy(dtype=int)
    )
    load_pv = (rev_load_pv_y1 * retail_ratio).to_numpy()
    load_bess = (rev_load_bess_y1 * retail_ratio).to_numpy()
    exp_pv = (rev_exp_pv_y1 * dam_ratio).to_numpy()
    exp_bess = (rev_exp_bess_y1 * dam_ratio).to_numpy()
    # Grid-charging cost is part of the DAM bundle in economics.py
    # (revenue_1_dam = exports - grid_charge), so it scales with the
    # DAM ratio rather than the retail one.  Drawn negative.
    cost = -((cost_grid_y1 * dam_ratio).to_numpy())

    # Aggregator-fee deduction.  yearly_cf's ``revenue_eur`` column is
    # post-fee while the stack components above are pre-fee, so without
    # this bar the stack sums ~aggregator_fee_pct above the net line
    # with no on-plot explanation.  Adding it as an explicit negative
    # component closes the gap.
    if "aggregator_fee_eur" in op.columns:
        # yearly_cf stores the fee as a signed value (negative when
        # aggregator_fee_pct_revenue > 0), so use it as-is.
        agg_fee = op["aggregator_fee_eur"].astype(float).to_numpy()
    else:
        agg_fee_frac = 0.0
        if econ is not None:
            agg_fee_frac = max(
                0.0,
                float(econ.get("aggregator_fee_pct_revenue", 0.0) or 0.0) / 100.0,
            )
        gross_y1 = (
            rev_load_pv_y1 + rev_load_bess_y1
            + rev_exp_pv_y1 + rev_exp_bess_y1
            - cost_grid_y1
        )
        agg_fee_y1 = gross_y1 * agg_fee_frac
        agg_fee = -((agg_fee_y1 * retail_ratio).to_numpy())

    # Optional balancing-aggregator (BSP) fee — read straight from the
    # cashflow column so the bar carries the exact deduction the DCF saw
    # (escalated on the BESS fade curve).  All-zero when the fee is off, so
    # the bar is simply not drawn.
    if "balancing_aggregator_fee_eur" in op.columns:
        bal_agg_fee = op["balancing_aggregator_fee_eur"].astype(float).to_numpy()
    else:
        bal_agg_fee = np.zeros_like(load_pv)

    # Structural market-access fees — read straight from their cashflow
    # columns (signed <= 0), exactly like the BSP fee above.  All-zero when
    # the knobs are off, so no bar (and no legend entry) is drawn and the
    # default figure stays bit-identical.
    if "route_to_market_fee_eur" in op.columns:
        rtm_fee = op["route_to_market_fee_eur"].astype(float).to_numpy()
    else:
        rtm_fee = np.zeros_like(load_pv)
    if "optimizer_fee_eur" in op.columns:
        opt_fee = op["optimizer_fee_eur"].astype(float).to_numpy()
    else:
        opt_fee = np.zeros_like(load_pv)
    if "grid_charging_fee_eur" in op.columns:
        gcf_fee = op["grid_charging_fee_eur"].astype(float).to_numpy()
    else:
        gcf_fee = np.zeros_like(load_pv)
    if "imbalance_cost_eur" in op.columns:
        imb_cost = op["imbalance_cost_eur"].astype(float).to_numpy()
    else:
        imb_cost = np.zeros_like(load_pv)
    # Tolling revenue (Eq. E29) — read straight from the cashflow
    # column.  Under 'zeroed' treatment (Eq. E29a) the BESS-origin
    # merchant streams are zero in toll years: the column-derived
    # ratios above absorb that for the DAM/retail stack sums (the
    # per-component attribution inside a blended ratio is approximate,
    # as it already is for the pv/bess split), but the per-product
    # balancing segments are reconstructed analytically below, so they
    # carry an explicit merchant gate — zero exactly in the years the
    # toll column is non-zero.
    if "toll_revenue_eur" in op.columns:
        toll_arr = op["toll_revenue_eur"].astype(float).to_numpy()
    else:
        toll_arr = np.zeros_like(load_pv)
    # Optimizer floor top-up (Eq. E30) — a >= 0 contract payment read
    # straight from its cashflow column, exactly like the fees.
    if "optimizer_floor_topup_eur" in op.columns:
        floor_topup_arr = (
            op["optimizer_floor_topup_eur"].astype(float).to_numpy()
        )
    else:
        floor_topup_arr = np.zeros_like(load_pv)
    # State support and its signed two-way netting (Eqs. E31/E31a).
    if "state_support_eur" in op.columns:
        support_arr = op["state_support_eur"].astype(float).to_numpy()
    else:
        support_arr = np.zeros_like(load_pv)
    if "state_support_clawback_eur" in op.columns:
        support_net_arr = (
            op["state_support_clawback_eur"].astype(float).to_numpy()
        )
    else:
        support_net_arr = np.zeros_like(load_pv)
    # Capacity-market payment (Eq. E32).
    if "capacity_market_revenue_eur" in op.columns:
        capacity_market_arr = (
            op["capacity_market_revenue_eur"].astype(float).to_numpy()
        )
    else:
        capacity_market_arr = np.zeros_like(load_pv)
    # Revenue levy on gross market turnover (Eq. E33), signed <= 0.
    if "revenue_levy_eur" in op.columns:
        levy_arr = op["revenue_levy_eur"].astype(float).to_numpy()
    else:
        levy_arr = np.zeros_like(load_pv)
    # Curtailment compensation (Eq. E49), an administered payment >= 0.
    if "curtailment_compensation_eur" in op.columns:
        curtailment_arr = (
            op["curtailment_compensation_eur"].astype(float).to_numpy()
        )
    else:
        curtailment_arr = np.zeros_like(load_pv)
    # GO revenue (Eq. E54), certificate income on the PV injection.
    if "go_revenue_eur" in op.columns:
        go_arr = op["go_revenue_eur"].astype(float).to_numpy()
    else:
        go_arr = np.zeros_like(load_pv)
    # Support settlement (Eqs. E55-E57), signed per year.
    if "support_settlement_eur" in op.columns:
        support_settlement_arr = (
            op["support_settlement_eur"].astype(float).to_numpy()
        )
    else:
        support_settlement_arr = np.zeros_like(load_pv)
    # Intraday venue (Eqs. E58/E59): margin band + venue fee.
    if "intraday_revenue_eur" in op.columns:
        intraday_arr = op["intraday_revenue_eur"].astype(float).to_numpy()
    else:
        intraday_arr = np.zeros_like(load_pv)
    if "intraday_fee_eur" in op.columns:
        intraday_fee_arr = op["intraday_fee_eur"].astype(float).to_numpy()
    else:
        intraday_fee_arr = np.zeros_like(load_pv)
    _toll_treatment = str(
        (econ or {}).get("bess_toll_merchant_treatment", "zeroed")
        or "zeroed"
    ).strip().lower()
    if _toll_treatment == "zeroed":
        merchant_gate = np.where(np.abs(toll_arr) > 1e-9, 0.0, 1.0)
    else:
        merchant_gate = np.ones_like(toll_arr)

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
    if np.any(agg_fee < -1e-9):
        # Stack the aggregator fee bar BELOW the grid-charging-cost bar
        # so the two negative components sit side by side rather than
        # one obscuring the other.
        ax.bar(
            years, agg_fee, bottom=cost,
            color=financial_color("Energy aggregator fee"),
            edgecolor="black", linewidth=0.4,
            label="Energy aggregator fee",
        )
    if np.any(bal_agg_fee < -1e-9):
        # The balancing-aggregator (BSP) fee sits below the energy
        # aggregator-fee bar so the two deduction lines stay side by side.
        ax.bar(
            years, bal_agg_fee, bottom=cost + agg_fee,
            color=financial_color("Balancing aggregator fee"),
            edgecolor="black", linewidth=0.4,
            label="Balancing aggregator fee",
        )
    if np.any(rtm_fee < -1e-9):
        # Route-to-market fee stacks below the BSP fee — every deduction
        # keeps its own slot in the negative stack.
        ax.bar(
            years, rtm_fee, bottom=cost + agg_fee + bal_agg_fee,
            color=financial_color("Route-to-market fee"),
            edgecolor="black", linewidth=0.4,
            label="Route-to-market fee",
        )
    if np.any(opt_fee < -1e-9):
        ax.bar(
            years, opt_fee, bottom=cost + agg_fee + bal_agg_fee + rtm_fee,
            color=financial_color("Optimizer fee"),
            edgecolor="black", linewidth=0.4,
            label="Optimizer fee",
        )
    if np.any(gcf_fee < -1e-9):
        # Charging-side grid fee (Eq. E27) — the regulated wedge next to
        # the energy cost of the same grid-charged volume.
        ax.bar(
            years, gcf_fee,
            bottom=cost + agg_fee + bal_agg_fee + rtm_fee + opt_fee,
            color=financial_color("Grid-charging fee"),
            edgecolor="black", linewidth=0.4,
            label="Grid-charging fee",
        )
    if np.any(imb_cost < -1e-9):
        # Imbalance settlement (Eq. E28) — its own deduction slot.
        ax.bar(
            years, imb_cost,
            bottom=cost + agg_fee + bal_agg_fee + rtm_fee + opt_fee
            + gcf_fee,
            color=financial_color("Imbalance cost"),
            edgecolor="black", linewidth=0.4,
            label="Imbalance cost",
        )
    if np.any(levy_arr < -1e-9):
        # Revenue levy (Eq. E33) — its own deduction slot below the
        # imbalance band.
        ax.bar(
            years, levy_arr,
            bottom=cost + agg_fee + bal_agg_fee + rtm_fee + opt_fee
            + gcf_fee + imb_cost,
            color=financial_color("Revenue levy"),
            edgecolor="black", linewidth=0.4,
            label="Revenue levy",
        )

    # PPA contract leg — drawn straight from the cashflow column so the
    # bar carries the exact stream NPV/IRR saw (term cutoff, its own
    # ppa_inflation_pct escalation, and the post-term reversion are all
    # already in the column).  A CfD leg can flip sign across years:
    # the positive part stacks with the revenue components, the
    # negative part below the cost/fee stack.
    if "ppa_revenue_eur" in op.columns:
        ppa_arr = op["ppa_revenue_eur"].astype(float).to_numpy()
        ppa_pos = np.clip(ppa_arr, 0.0, None)
        ppa_neg = np.clip(ppa_arr, None, 0.0)
        ppa_neg_total = ppa_neg
        drew_ppa_label = False
        if np.any(ppa_pos > 1e-9):
            ax.bar(
                years, ppa_pos, bottom=bottoms,
                color=financial_color("PPA revenue"),
                edgecolor="black", linewidth=0.4, label="PPA revenue",
            )
            bottoms = bottoms + ppa_pos
            drew_ppa_label = True
        if np.any(ppa_neg < -1e-9):
            ax.bar(
                years, ppa_neg,
                bottom=cost + agg_fee + bal_agg_fee + rtm_fee + opt_fee
                + gcf_fee + imb_cost + levy_arr,
                color=financial_color("PPA revenue"),
                edgecolor="black", linewidth=0.4,
                label=None if drew_ppa_label else "PPA revenue",
            )

    # Balancing-revenue segments — one stacked bar per product on top
    # of the DAM/retail revenue stack.  Year-1 values come from the
    # canonical revenue aggregates in ``year1_kpis``; subsequent years
    # scale by the BESS capacity-fade factor indexed by the balancing
    # inflation rate, which is the same growth ``build_yearly_cashflow``
    # applies to the cashflow's ``balancing_revenue_eur`` rows.
    # Previously this used ``dam_ratio``, which drove the bars to zero
    # for self-consumption projects (Year-1 DAM revenue = 0) and drifted
    # against the net line whenever ``bm_inflation_pct`` differed from
    # ``dam_inflation_pct``.
    bm_infl = 0.0
    if econ is not None:
        bm_infl = float(econ.get("bm_inflation_pct", 0.0) or 0.0) / 100.0
    project_years_arr = op["project_year"].to_numpy(dtype=int)
    if "bess_capacity_factor" in op.columns:
        bess_factor_arr = op["bess_capacity_factor"].astype(float).to_numpy()
    else:
        bess_factor_arr = np.ones_like(project_years_arr, dtype=float)
    balancing_ratio = pd.Series(
        bess_factor_arr
        * np.power(1.0 + bm_infl, project_years_arr - 1)
        * merchant_gate,
        index=op.index, dtype=float,
    )

    bm_segments = [
        ("revenue_bess_fcr_eur", "FCR", "fcr"),
        ("revenue_bess_afrr_up_eur", "aFRR-up", "afrr_up"),
        ("revenue_bess_afrr_dn_eur", "aFRR-down", "afrr_dn"),
        ("revenue_bess_mfrr_up_eur", "mFRR-up", "mfrr_up"),
        ("revenue_bess_mfrr_dn_eur", "mFRR-down", "mfrr_dn"),
    ]
    bm_arrays: list[tuple[str, str, np.ndarray]] = []
    for kpi_key, label, colour_key in bm_segments:
        y1_val = float(year1_kpis.get(kpi_key, 0.0) or 0.0)
        if abs(y1_val) <= 1e-9:
            continue
        seg = (y1_val * balancing_ratio).to_numpy()
        bm_arrays.append((label, colour_key, seg))
        ax.bar(
            years, seg, bottom=bottoms,
            color=financial_color(label),
            edgecolor="black", linewidth=0.4,
            label=label,
        )
        bottoms = bottoms + seg

    # Tolling revenue band (Eq. E29) — the contracted stream that
    # replaces the gated merchant segments in toll years; drawn only
    # when the column carries value so default figures are unchanged.
    if np.any(np.abs(toll_arr) > 1e-9):
        ax.bar(
            years, np.clip(toll_arr, 0.0, None), bottom=bottoms,
            color=financial_color("Tolling revenue"),
            edgecolor="black", linewidth=0.4,
            label="Tolling revenue",
        )
        bottoms = bottoms + np.clip(toll_arr, 0.0, None)
    if np.any(np.abs(floor_topup_arr) > 1e-9):
        ax.bar(
            years, floor_topup_arr, bottom=bottoms,
            color=financial_color("Optimizer floor top-up"),
            edgecolor="black", linewidth=0.4,
            label="Optimizer floor top-up",
        )
        bottoms = bottoms + floor_topup_arr
    if np.any(np.abs(support_arr) > 1e-9):
        ax.bar(
            years, support_arr, bottom=bottoms,
            color=financial_color("State support"),
            edgecolor="black", linewidth=0.4,
            label="State support",
        )
        bottoms = bottoms + support_arr
    if np.any(np.abs(capacity_market_arr) > 1e-9):
        ax.bar(
            years, capacity_market_arr, bottom=bottoms,
            color=financial_color("Capacity-market revenue"),
            edgecolor="black", linewidth=0.4,
            label="Capacity-market revenue",
        )
        bottoms = bottoms + capacity_market_arr
    if np.any(np.abs(curtailment_arr) > 1e-9):
        # Curtailment compensation (Eq. E49) — administered payment
        # next to the other non-market revenue bands.
        ax.bar(
            years, curtailment_arr, bottom=bottoms,
            color=financial_color("Curtailment compensation"),
            edgecolor="black", linewidth=0.4,
            label="Curtailment compensation",
        )
        bottoms = bottoms + curtailment_arr
    if np.any(np.abs(go_arr) > 1e-9):
        ax.bar(
            years, go_arr, bottom=bottoms,
            color=financial_color("GO revenue"),
            edgecolor="black", linewidth=0.4,
            label="GO revenue",
        )
        bottoms = bottoms + go_arr
    if np.any(np.abs(support_settlement_arr) > 1e-9):
        # Signed band: positive premium years stack with the revenue,
        # two-way repayment years below the fee stack (the
        # state-support-netting pattern; drawn at the revenue base for
        # simplicity since the lifecycle stack has no running negative
        # base at this point).
        ax.bar(
            years, support_settlement_arr,
            bottom=np.where(support_settlement_arr >= 0.0, bottoms, 0.0),
            color=financial_color("Support settlement (FiP/CfD)"),
            edgecolor="black", linewidth=0.4,
            label="Support settlement (FiP/CfD)",
        )
        bottoms = bottoms + np.clip(support_settlement_arr, 0.0, None)
    if np.any(np.abs(intraday_arr) > 1e-9):
        # Intraday margin (Eq. E58): >= 0 by construction — a plain
        # positive band next to the market streams.
        ax.bar(
            years, intraday_arr, bottom=bottoms,
            color=financial_color("Intraday revenue"),
            edgecolor="black", linewidth=0.4,
            label="Intraday revenue",
        )
        bottoms = bottoms + np.clip(intraday_arr, 0.0, None)
    if np.any(intraday_fee_arr < -1e-9):
        # Venue fee (Eq. E59): its own deduction slot below the levy
        # (the imbalance-cost pattern — explicit accumulated bottom).
        ax.bar(
            years, intraday_fee_arr,
            bottom=cost + agg_fee + bal_agg_fee + rtm_fee + opt_fee
            + gcf_fee + imb_cost + levy_arr,
            color=financial_color("Intraday fee"),
            edgecolor="black", linewidth=0.4,
            label="Intraday fee",
        )
    if np.any(np.abs(support_net_arr) > 1e-9):
        # Signed netting (Eq. E31a): compensation years stack with the
        # revenue components, clawback years below the fee stack (and
        # below any negative CfD PPA leg) — element-wise side
        # assignment, one band.
        if "ppa_revenue_eur" in op.columns:
            _ppa_neg_part = ppa_neg_total
        else:
            _ppa_neg_part = np.zeros_like(load_pv)
        _neg_stack_base = (
            cost + agg_fee + bal_agg_fee + rtm_fee + opt_fee
            + gcf_fee + imb_cost + levy_arr + _ppa_neg_part
        )
        ax.bar(
            years, support_net_arr,
            bottom=np.where(
                support_net_arr >= 0.0, bottoms, _neg_stack_base,
            ),
            color=financial_color("State-support netting"),
            edgecolor="black", linewidth=0.4,
            label="State-support netting",
        )
        bottoms = bottoms + np.clip(support_net_arr, 0.0, None)

    net = (op["revenue_eur"].astype(float)).to_numpy()
    if "balancing_revenue_eur" in op.columns:
        net = net + op["balancing_revenue_eur"].astype(float).to_numpy()
    # balancing_revenue_eur is gross, so step the net line down by the BSP
    # fee (bal_agg_fee <= 0) to keep it on top of the drawn stack.  The two
    # structural fees enter the yearly net the same way (their columns are
    # not folded into revenue_eur), so the line steps down by them too.
    net = net + bal_agg_fee + rtm_fee + opt_fee + gcf_fee + imb_cost
    net = (
        net + toll_arr + floor_topup_arr + support_arr + support_net_arr
        + capacity_market_arr + levy_arr + curtailment_arr + go_arr
        + support_settlement_arr
        + intraday_arr + intraday_fee_arr
    )
    if "ppa_revenue_eur" in op.columns:
        net = net + op["ppa_revenue_eur"].astype(float).to_numpy()
    # IEEE-friendly emphasis line: near-black solid markers.  The
    # universality rule forbids markeredgecolor="white" rings; line
    # contrast comes from the charcoal colour itself.
    ax.plot(
        years, net,
        color=financial_color("Net revenue"),
        linewidth=1.5,
        marker="o", markersize=3,
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
            color=financial_color("Real net revenue"),
            linewidth=1.2,
            linestyle="--", marker="", alpha=0.85,
            label="Real net revenue",
        )

    ax.set_xlabel("Year")
    ax.set_ylabel("EUR")
    ax.yaxis.set_major_formatter(euro_axis_formatter(_resolve_currency_format(econ)))
    if show_titles():
        ax.set_title(f"Revenue stack - {int(years[0])}-{int(years[-1])}")
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    apply_universal_margins(ax, skip_y=True)
    _integer_year_axis(ax, years)
    apply_fine_ticks(ax)
    apply_financial_legend(ax)
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
            out_path, "BESS not part of this project (no cycle plot).",
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
    ax.set_xlabel("Year")
    ax.set_ylabel("Equivalent cycles per year")
    if show_titles():
        ax.set_title(
            f"BESS Equivalent Cycles - total {total:.0f} over "
            f"{int(years[0])}-{int(years[-1])}"
        )
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    apply_universal_margins(ax)
    _integer_year_axis(ax, years)
    apply_fine_ticks(ax)
    return save_figure(out_path)


def _sensitivity_deltas(econ: dict[str, Any]) -> tuple[float, float]:
    """Return the (capex, opex) relative deltas as fractions in [0, 1]."""
    capex_d = float(
        econ.get("sensitivity_capex_delta_pct", DEFAULT_SENSITIVITY_DELTA_PCT)
    ) / 100.0
    opex_d = float(
        econ.get("sensitivity_opex_delta_pct", DEFAULT_SENSITIVITY_DELTA_PCT)
    ) / 100.0
    return capex_d, opex_d


def _levelized_sensitivity_range(
    fin_kpis: dict[str, Any],
    capex_key: str, opex_key: str, mwh_key: str,
    capex_d: float, opex_d: float,
) -> tuple[float, float] | None:
    """Compute the (low, high) range for a levelized metric correctly.

    Uses the algebraic
    ``(disc_capex * (1 +/- capex_d) + disc_opex * (1 +/- opex_d)) / disc_mwh``
    so the displayed range reflects the actual NREL ATB / Lazard
    decomposition of the metric.  The previous
    ``base * (1 +/- capex_d) * (1 +/- opex_d)`` multiplicative
    approximation overshoots the true range by the
    capex_d * opex_d cross term and ignores the relative weight of
    CAPEX vs. OPEX in the numerator.

    Returns ``None`` when any required discounted component is
    missing from ``fin_kpis`` so the caller can fall back.
    """
    disc_capex = fin_kpis.get(capex_key)
    disc_opex = fin_kpis.get(opex_key)
    disc_mwh = fin_kpis.get(mwh_key)
    if disc_capex is None or disc_opex is None or disc_mwh is None:
        return None
    disc_capex = float(disc_capex)
    disc_opex = float(disc_opex)
    disc_mwh = float(disc_mwh)
    if disc_mwh <= 1e-9:
        return None
    low = (
        disc_capex * (1.0 - capex_d) + disc_opex * (1.0 - opex_d)
    ) / disc_mwh
    high = (
        disc_capex * (1.0 + capex_d) + disc_opex * (1.0 + opex_d)
    ) / disc_mwh
    return float(low), float(high)


def plot_lcoe_summary(
    fin_kpis: dict[str, Any],
    sensitivity_df: pd.DataFrame | None,
    capacities: dict[str, float],
    econ: dict[str, Any],
    out_path: Path,
) -> Path:
    """LCOE comparison panel vs Lazard 2024 utility-scale PV band.

    Single-row horizontal bar with the project sensitivity range, a
    centred base tick, and the Lazard band shaded behind.  Every
    numeric value (base, sensitivity span, benchmark span) is reported
    in the legend; the plot face holds no bbox annotations, italic
    captions, or diamond markers, and the row carries no rotated
    y-axis label — the panel context is implicit from the filename
    and legend entries.

    margins: delegated.  ``_draw_benchmark_row`` applies its own 12 %
    x-padding and fixes y-range to ``(-0.6, 0.6)`` so the universal
    helper would over-pad.
    """
    out_path = Path(out_path)
    pv_kwp = float(capacities.get("pv_kwp", 0.0) or 0.0)
    base_lcoe = float(fin_kpis.get("lcoe_eur_per_mwh", float("nan")))
    capex_d, opex_d = _sensitivity_deltas(econ)
    _ = sensitivity_df  # kept for API symmetry; range derived from fin_kpis
    pv_present = pv_kwp > 0.0 and not np.isnan(base_lcoe)
    benchmark = (
        float(econ.get("benchmark_lcoe_low_eur_per_mwh",
                       BENCHMARK_LCOE_PV_UTILITY_EUR_PER_MWH[0])),
        float(econ.get("benchmark_lcoe_high_eur_per_mwh",
                       BENCHMARK_LCOE_PV_UTILITY_EUR_PER_MWH[1])),
    )
    rng = _levelized_sensitivity_range(
        fin_kpis,
        "lcoe_disc_pv_capex_eur",
        "lcoe_disc_pv_opex_eur",
        "lcoe_disc_pv_mwh",
        capex_d, opex_d,
    )
    if pv_present and rng is not None:
        low_val, high_val = rng
    else:
        # Fallback: when the discounted components are missing (older
        # KPI dicts) keep the legacy multiplicative range so the plot
        # still renders something usable.
        low_val = (
            base_lcoe * (1.0 - capex_d) * (1.0 - opex_d)
            if pv_present else float("nan")
        )
        high_val = (
            base_lcoe * (1.0 + capex_d) * (1.0 + opex_d)
            if pv_present else float("nan")
        )

    fig, ax = plt.subplots(figsize=(7, 3.0))
    _draw_benchmark_row(
        ax,
        base=base_lcoe,
        low=low_val,
        high=high_val,
        bar_colour=FINANCIAL_COLORS["lcoe_bar"],
        benchmark=benchmark,
        label="LCOE", asset_present=pv_present,
        absent_message="PV not part of this project - LCOE N/A",
    )
    ax.set_xlabel("EUR/MWh")
    apply_fine_ticks(ax, axis="x")
    if show_titles():
        fig.suptitle("Levelized Cost of Energy - Lazard 2024 benchmark")
    return save_figure_object(fig, out_path)


def plot_lcos_summary(
    fin_kpis: dict[str, Any],
    sensitivity_df: pd.DataFrame | None,
    capacities: dict[str, float],
    econ: dict[str, Any],
    out_path: Path,
) -> Path:
    """LCOS comparison panel vs Lazard 2024 utility-scale BESS band.

    Single-row horizontal bar; layout and conventions mirror
    :func:`plot_lcoe_summary`.

    margins: delegated.
    """
    out_path = Path(out_path)
    bess_kw = float(capacities.get("bess_kw", 0.0) or 0.0)
    base_lcos = float(fin_kpis.get("lcos_eur_per_mwh", float("nan")))
    capex_d, opex_d = _sensitivity_deltas(econ)
    _ = sensitivity_df
    bess_present = bess_kw > 0.0 and not np.isnan(base_lcos)
    benchmark = (
        float(econ.get("benchmark_lcos_low_eur_per_mwh",
                       BENCHMARK_LCOS_LITHIUM_ION_EUR_PER_MWH[0])),
        float(econ.get("benchmark_lcos_high_eur_per_mwh",
                       BENCHMARK_LCOS_LITHIUM_ION_EUR_PER_MWH[1])),
    )
    rng = _levelized_sensitivity_range(
        fin_kpis,
        "lcos_disc_bess_capex_eur",
        "lcos_disc_bess_opex_eur",
        "lcos_disc_bess_mwh",
        capex_d, opex_d,
    )
    if bess_present and rng is not None:
        low_val, high_val = rng
    else:
        low_val = (
            base_lcos * (1.0 - capex_d) * (1.0 - opex_d)
            if bess_present else float("nan")
        )
        high_val = (
            base_lcos * (1.0 + capex_d) * (1.0 + opex_d)
            if bess_present else float("nan")
        )

    fig, ax = plt.subplots(figsize=(7, 3.0))
    _draw_benchmark_row(
        ax,
        base=base_lcos,
        low=low_val,
        high=high_val,
        bar_colour=FINANCIAL_COLORS["lcos_bar"],
        benchmark=benchmark,
        label="LCOS", asset_present=bess_present,
        absent_message="BESS not part of this project - LCOS N/A",
    )
    ax.set_xlabel("EUR/MWh")
    apply_fine_ticks(ax, axis="x")
    if show_titles():
        fig.suptitle("Levelized Cost of Storage - Lazard 2024 benchmark")
    return save_figure_object(fig, out_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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

    Every numeric value (benchmark band, project range, base) is
    reported in the legend; the plot face holds no bbox annotations, no
    italic captions, and no diamond markers.  Each row uses its own
    x-axis scaled to the union of the benchmark band and the project
    sensitivity range with a 12 % margin.  No rotated y-axis label is
    drawn — LCOE and LCOS are rendered as separate PDFs, so the panel
    context is implicit from the filename and legend entries.
    """
    if not asset_present or np.isnan(base):
        ax.text(
            0.5, 0.5, absent_message, ha="center", va="center",
            fontsize=10, transform=ax.transAxes,
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

    # Benchmark band behind the project bar.  Legend entries carry
    # names only; the band edges are readable off the EUR/MWh axis.
    ax.barh(
        [0], [bench_high - bench_low], left=bench_low, height=0.6,
        color=FINANCIAL_COLORS["benchmark_band"], alpha=0.45,
        edgecolor="grey", linewidth=0.4,
        label=f"Lazard 2024 {label} band",
        zorder=1,
    )

    # Project sensitivity range (saturated colour).
    ax.barh(
        [0], [bar_high - bar_low], left=bar_low, height=0.35,
        color=bar_colour, alpha=0.85, edgecolor="black", linewidth=0.6,
        label=f"{label} project range",
        zorder=3,
    )

    # Base value drawn as a vertical line (no diamond, no marker-edge
    # ring).  Legend entries carry names only; the numeric values are
    # readable off the axis and quoted in SUMMARY.md.
    ax.plot(
        [base, base], [-0.25, 0.25],
        color=FINANCIAL_COLORS["base_marker"], linewidth=1.4,
        solid_capstyle="butt", zorder=5,
        label=f"Base {label}",
    )

    ax.set_yticks([])
    # The single row spans y in [-0.25, 0.25]; the legend hangs below
    # the strip (house rule), so the band is framed symmetrically.
    ax.set_ylim(-0.6, 0.6)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(True, axis="x", linestyle="--", alpha=0.5)

    # Per-row independent x-axis: span the union of (benchmark, project
    # range) with 12 % padding on each side so legend / labels never
    # get clipped.
    span_lo = min(bench_low, bar_low)
    span_hi = max(bench_high, bar_high)
    pad = 0.12 * max(span_hi - span_lo, 1.0)
    ax.set_xlim(max(0.0, span_lo - pad), span_hi + pad)
    legend_below(ax, max_rows=1, y_offset=-0.25)
