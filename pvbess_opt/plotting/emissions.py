"""IEEE-styled emissions / 24/7 carbon-free-energy plots.

* :func:`plot_energy_sankey` — an annual energy-balance Sankey routing the
  two sources (PV, grid import) into the sinks (load, export, curtailment)
  with a balancing storage/losses term.
* :func:`plot_cfe_duration_curve` — the carbon-free fraction of the load
  sorted descending, the canonical 24/7 CFE view, with the annual
  time-coincident score marked.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.sankey import Sankey

from ..emissions import cfe_score, hourly_cfe_fraction
from ..theme import FINANCIAL_COLORS
from .style import (
    apply_universal_margins,
    attach_legend_clear_of_data,
    save_figure,
)

__all__ = ["plot_cfe_duration_curve", "plot_energy_sankey"]

# Fixed top/straight/bottom routing per node, so the diagram reads the same
# way regardless of which flows happen to be non-zero for a given run.
_SANKEY_ORIENTATION: dict[str, int] = {
    "PV": 1,
    "Grid import": -1,
    "Load": 0,
    "Export": -1,
    "Curtail": 1,
    "Storage/losses": 1,
}


def _sum_mwh(res: pd.DataFrame, name: str) -> float:
    if name in res.columns:
        return float(res[name].to_numpy(dtype=float).sum()) / 1000.0
    return 0.0


def plot_energy_sankey(res: pd.DataFrame, out_path: Path) -> Path:
    """Annual energy-balance Sankey (MWh) for the solved dispatch.

    margins: delegated — the Sankey diagram turns its axes off and manages
    its own layout, so the universal axis margins do not apply.
    """
    pv = _sum_mwh(res, "pv_kwh")
    grid_import = _sum_mwh(res, "grid_to_load_kwh") + _sum_mwh(res, "bess_charge_grid_kwh")
    load = _sum_mwh(res, "load_kwh")
    export = _sum_mwh(res, "pv_to_grid_kwh") + _sum_mwh(res, "bess_dis_grid_kwh")
    curtail = _sum_mwh(res, "pv_curtail_kwh")

    # Sources positive, sinks negative; a residual storage/losses term closes
    # the balance so the flows sum to zero exactly (Sankey requires it).
    candidates = [
        ("PV", pv),
        ("Grid import", grid_import),
        ("Load", -load),
        ("Export", -export),
        ("Curtail", -curtail),
    ]
    total = pv + grid_import + load + export + curtail
    eps = max(total, 1.0) * 1.0e-6
    kept = [(name, val) for name, val in candidates if abs(val) > eps]
    residual = -sum(val for _name, val in kept)
    if abs(residual) > eps:
        kept.append(("Storage/losses", residual))

    fig = plt.figure(figsize=(7, 4))
    ax = fig.add_subplot(1, 1, 1)
    ax.axis("off")
    if not kept:
        ax.text(0.5, 0.5, "No energy flows.", ha="center", va="center")
        return save_figure(out_path)

    flows = [val for _name, val in kept]
    labels = [name for name, _val in kept]
    orientations = [_SANKEY_ORIENTATION.get(name, 0) for name, _val in kept]
    scale = 1.0 / max(abs(f) for f in flows)
    Sankey(
        ax=ax, scale=scale, unit=" MWh", format="%.0f",
        gap=0.4, shoulder=0.0,
    ).add(
        flows=flows, labels=labels, orientations=orientations,
        facecolor=FINANCIAL_COLORS["net"], edgecolor=FINANCIAL_COLORS["net_revenue_line"],
    ).finish()
    return save_figure(out_path)


def plot_cfe_duration_curve(res: pd.DataFrame, out_path: Path) -> Path:
    """Carbon-free fraction of the load, sorted descending (24/7 CFE curve)."""
    frac = np.sort(hourly_cfe_fraction(res))[::-1] * 100.0
    _fig, ax = plt.subplots(figsize=(7, 4))
    if frac.size == 0:
        ax.text(0.5, 0.5, "No load to match.", ha="center", va="center",
                transform=ax.transAxes)
        return save_figure(out_path)
    x = np.arange(1, frac.size + 1) / frac.size * 100.0
    ax.plot(x, frac, color=FINANCIAL_COLORS["revenue"], linewidth=1.2)
    ax.fill_between(x, 0.0, frac, color=FINANCIAL_COLORS["revenue"], alpha=0.15)
    score = cfe_score(res)
    has_legend = np.isfinite(score)
    if has_legend:
        ax.axhline(
            score, color=FINANCIAL_COLORS["net_revenue_line"],
            linewidth=0.8, linestyle="--",
            label="24/7 CFE score",
        )
    ax.set_xlabel("Share of time (%)")
    ax.set_ylabel("Carbon-free share of load (%)")
    ax.set_xlim(0.0, 100.0)
    ax.set_ylim(0.0, 100.0)
    apply_universal_margins(ax)
    if has_legend:
        # Measured placement: headroom may grow above 100, but the
        # percentage ticks stay pinned at the 0..100 scale.
        attach_legend_clear_of_data(
            ax, loc="upper right", tick_ceiling=100.0,
        )
    return save_figure(out_path)
