"""IEEE-styled emissions / 24/7 carbon-free-energy plots.

* :func:`plot_energy_sankey` — an annual energy-flow diagram (layered
  ribbons) routing the sources (PV, grid import) through the battery
  into the sinks (load, grid export, curtailment, battery losses),
  coloured with the canonical flow palette of the energy plots.
* :func:`plot_cfe_duration_curve` — the carbon-free fraction of the load
  sorted descending, the canonical 24/7 CFE view, with the annual
  time-coincident score marked.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import PathPatch, Rectangle
from matplotlib.path import Path as _MplPath

from ..emissions import cfe_score, hourly_cfe_fraction
from ..theme import COLORS, FINANCIAL_COLORS
from .style import (
    apply_universal_margins,
    empty_placeholder,
    legend_below,
    save_figure,
)

__all__ = [
    "energy_sankey_flows",
    "plot_cfe_duration_curve",
    "plot_energy_sankey",
]

def _sum_mwh(res: pd.DataFrame, name: str) -> float:
    if name in res.columns:
        return float(res[name].to_numpy(dtype=float).sum()) / 1000.0
    return 0.0


# Column order of the flow layout: sources | battery | sinks.  Nodes
# render only when they carry a non-zero flow, so a merchant run (no
# load) and a PV-only run (no battery) collapse to the columns they use.
_SANKEY_NODE_COLUMNS: dict[str, int] = {
    "PV generation": 0,
    "Grid import": 0,
    "BESS": 1,
    "Load": 2,
    "Grid export": 2,
    "Curtailed PV": 2,
    # Post-solve exogenous-curtailment sink (Eq. E48): registered in the
    # layout so the renderer can place it whenever the quota emits its
    # flows; nodes render only when they carry flow, so the default
    # (quota off) layout is unchanged.
    "Curtailed export": 2,
    "Losses": 2,
}
_SANKEY_COLUMN_ORDER: tuple[tuple[str, ...], ...] = (
    ("PV generation", "Grid import"),
    ("BESS",),
    ("Load", "Grid export", "Curtailed PV", "Curtailed export", "Losses"),
)
_SANKEY_NODE_COLOURS: dict[str, str] = {
    "PV generation": COLORS["PV generation"],
    "Grid import": COLORS["Grid to load"],
    "BESS": COLORS["BESS to load"],
    "Load": COLORS["Load demand"],
    "Grid export": COLORS["BESS to grid"],
    "Curtailed PV": COLORS["Curtailed PV"],
    # Post-solve exogenous-curtailment sink (Eq. E48); only drawn when the
    # quota is active, so the default node set is unchanged.
    "Curtailed export": COLORS["Curtailed PV"],
    "Losses": COLORS["BESS losses"],
}
_SANKEY_LOSSES_COLOUR = COLORS["BESS losses"]


def energy_sankey_flows(
    res: pd.DataFrame,
    availability_factor: float = 1.0,
    curtailment_factor: float = 1.0,
) -> list[tuple[str, str, float, str]]:
    """Return the ``(source, target, MWh, colour)`` flows of the Sankey.

    Pulled out of :func:`plot_energy_sankey` so the derate physics is
    unit-testable.  With ``availability_factor < 1`` every plant-side flow
    (PV, BESS, export) scales by the factor while ``grid_to_load`` rises by
    ``(1 - factor) * load`` — the grid covers the load the offline plant
    cannot serve — so the flows conserve energy against the true (never
    derated) demand.

    With ``curtailment_factor < 1`` (the exogenous-quota derate, Eq. E48) the
    two grid-INJECTION flows (``pv_to_grid``, ``bess_to_grid``) shrink by the
    factor so the ``Grid export`` node matches the curtailment-derated
    ``pv_export_mwh`` / ``bess_export_mwh`` KPIs; the curtailed injection is
    routed to a ``Curtailed export`` sink (distinct from the MILP's within-
    dispatch ``Curtailed PV``) so the diagram still conserves energy, and the
    round-trip ``losses`` are computed BEFORE the export haircut so they stay
    physical.  Both factors ``== 1`` returns the raw dispatch (bit-identical);
    the ``Curtailed export`` node appears only under active curtailment.
    """
    a = float(availability_factor)
    c = float(curtailment_factor)
    # Raw annual energy per flow (MWh) from the dispatch frame.
    pv_to_load = _sum_mwh(res, "pv_to_load_kwh")
    pv_to_bess = _sum_mwh(res, "pv_to_bess_kwh")
    pv_to_grid = _sum_mwh(res, "pv_to_grid_kwh")
    pv_curtail = _sum_mwh(res, "pv_curtail_kwh")
    grid_to_load = _sum_mwh(res, "grid_to_load_kwh")
    grid_to_bess = _sum_mwh(res, "bess_charge_grid_kwh")
    bess_to_load = _sum_mwh(res, "bess_dis_load_kwh")
    bess_to_grid = _sum_mwh(res, "bess_dis_grid_kwh")
    if a < 1.0:
        load_raw = pv_to_load + bess_to_load + grid_to_load
        u = 1.0 - a
        pv_to_load *= a
        pv_to_bess *= a
        pv_to_grid *= a
        pv_curtail *= a
        grid_to_bess *= a
        bess_to_load *= a
        bess_to_grid *= a
        grid_to_load = grid_to_load * a + u * load_raw
    charge = pv_to_bess + grid_to_bess
    discharge = bess_to_load + bess_to_grid
    # Round-trip losses reflect the physical dispatch and must be taken BEFORE
    # the post-solve export haircut, else curtailed export would be miscounted
    # as losses.
    losses = max(charge - discharge, 0.0)
    curtailed_pv_export = 0.0
    curtailed_bess_export = 0.0
    if c < 1.0:
        curtailed_pv_export = pv_to_grid * (1.0 - c)
        curtailed_bess_export = bess_to_grid * (1.0 - c)
        pv_to_grid *= c
        bess_to_grid *= c
    flows = [
        ("PV generation", "Load", pv_to_load, COLORS["PV to load"]),
        ("PV generation", "BESS", pv_to_bess, COLORS["PV to BESS"]),
        ("PV generation", "Grid export", pv_to_grid, COLORS["PV to grid"]),
        ("PV generation", "Curtailed PV", pv_curtail, COLORS["Curtailed PV"]),
        ("Grid import", "Load", grid_to_load, COLORS["Grid to load"]),
        ("Grid import", "BESS", grid_to_bess, COLORS["Grid to BESS"]),
        ("BESS", "Load", bess_to_load, COLORS["BESS to load"]),
        ("BESS", "Grid export", bess_to_grid, COLORS["BESS to grid"]),
        ("BESS", "Losses", losses, _SANKEY_LOSSES_COLOUR),
    ]
    if c < 1.0:
        flows.append((
            "PV generation", "Curtailed export", curtailed_pv_export,
            COLORS["Curtailed PV"],
        ))
        flows.append((
            "BESS", "Curtailed export", curtailed_bess_export,
            COLORS["Curtailed PV"],
        ))
    return flows


def plot_energy_sankey(
    res: pd.DataFrame,
    out_path: Path,
    *,
    availability_factor: float = 1.0,
    curtailment_factor: float = 1.0,
) -> Path:
    """Annual energy-flow diagram (MWh) for the solved dispatch.

    Layered ribbon layout: sources on the left (PV generation, grid
    import), the battery in the middle, sinks on the right (load, grid
    export, curtailment, battery round-trip losses).  Every ribbon
    reuses the canonical flow colour of the energy plots, so ``PV to
    BESS`` reads in the same gold here as in the daily dispatch view.
    Node labels carry the annual MWh totals.

    ``availability_factor`` and ``curtailment_factor`` (default ``1.0`` = no
    derate) make the diagram consistent with the derated annual KPIs / tables:
    every plant-side flow scales by availability, while the load is fixed
    exogenous demand, so ``grid_to_load`` RISES by ``(1 - factor) * load`` to
    cover the load the offline plant cannot serve; and the two grid-export
    flows additionally scale by the exogenous-curtailment factor, with the
    curtailed injection routed to a ``Curtailed export`` sink.  The load node
    therefore stays at the true demand and the diagram balances against it —
    matching the ``system_total_*`` / ``*_export_mwh`` KPIs derated by
    :func:`pvbess_opt.availability.apply_unavailability_derate` and
    :func:`pvbess_opt.availability.apply_curtailment_derate`.  Pass
    ``kpis['availability_factor']`` and ``kpis.get('curtailment_factor', 1.0)``.

    margins: delegated — the diagram turns its axes off and manages its
    own layout, so the universal axis margins do not apply.
    """
    flows = energy_sankey_flows(res, availability_factor, curtailment_factor)
    total = sum(v for _s, _t, v, _c in flows)
    eps = max(total, 1.0) * 1.0e-6
    flows = [f for f in flows if f[2] > eps]

    if not flows:
        return empty_placeholder(out_path, "No energy flows.")

    _fig, ax = plt.subplots(figsize=(7, 4))
    ax.axis("off")

    def node_total(name: str) -> float:
        return max(
            sum(v for s, _t, v, _c in flows if s == name),
            sum(v for _s, t, v, _c in flows if t == name),
        )

    active = {n for f in flows for n in (f[0], f[1])}
    col_x = {0: 0.06, 1: 0.48, 2: 0.90}
    node_w = 0.03
    col_totals = [
        sum(node_total(n) for n in names if n in active)
        for names in _SANKEY_COLUMN_ORDER
    ]
    scale = 0.82 / max(col_totals)
    gap = 0.07 * max(col_totals) * scale

    pos: dict[str, tuple[float, float, float]] = {}
    for c, names in enumerate(_SANKEY_COLUMN_ORDER):
        names = [n for n in names if n in active]
        if not names:
            continue
        col_h = sum(node_total(n) * scale for n in names) + gap * (len(names) - 1)
        y = 0.5 + col_h / 2
        if c == 1:
            # Centre the battery slightly below the midline so its
            # discharge ribbons flow naturally toward the lower sinks.
            y = 0.5 + node_total(names[0]) * scale / 2 - 0.06
        for n in names:
            h = node_total(n) * scale
            pos[n] = (col_x[c], y - h, h)
            y -= h + gap

    # Slot allocation walks the canonical flow order so ribbons attach
    # to their nodes in a stable order — except that very small flows
    # take the LOWEST slot on both of their nodes, so a thin
    # surplus-export strand hugs the bottom of the diagram instead of
    # cutting across the middle of the wide ribbons.  The draw z-order
    # is inverse to the flow size so a thin ribbon renders ON TOP of
    # the wide ones instead of half-hidden beneath them, where it
    # reads as a stray thread.
    max_v = max(f[2] for f in flows)
    small = [v < 0.03 * max_v for _s, _t, v, _c in flows]
    out_off = dict.fromkeys(pos, 0.0)
    in_off = dict.fromkeys(pos, 0.0)
    anchors: list[tuple[float, float]] = [(0.0, 0.0)] * len(flows)
    for i in sorted(range(len(flows)), key=lambda k: small[k]):
        s, t, v, _colour = flows[i]
        h = v * scale
        anchors[i] = (
            pos[s][1] + pos[s][2] - out_off[s] - h,
            pos[t][1] + pos[t][2] - in_off[t] - h,
        )
        out_off[s] += h
        in_off[t] += h
    for i, (s, t, v, colour) in enumerate(flows):
        h = v * scale
        x0 = pos[s][0] + node_w
        x1 = pos[t][0]
        y0, y1 = anchors[i]
        mx = (x0 + x1) / 2
        verts = [
            (x0, y0), (mx, y0), (mx, y1), (x1, y1), (x1, y1 + h),
            (mx, y1 + h), (mx, y0 + h), (x0, y0 + h), (x0, y0),
        ]
        codes = [
            _MplPath.MOVETO, _MplPath.CURVE4, _MplPath.CURVE4,
            _MplPath.LINETO, _MplPath.LINETO, _MplPath.CURVE4,
            _MplPath.CURVE4, _MplPath.LINETO, _MplPath.CLOSEPOLY,
        ]
        # Very small flows (a thin surplus-export strand next to
        # 20 GWh ribbons) draw fully opaque with a hairline edge so
        # they read as crisp, deliberate lines instead of a faint
        # translucent thread.
        ax.add_patch(PathPatch(
            _MplPath(verts, codes), facecolor=colour,
            alpha=1.0 if small[i] else 0.8,
            edgecolor=colour if small[i] else "none",
            linewidth=0.6 if small[i] else 0.0,
            zorder=1.0 + (1.0 - v / max_v),
        ))

    for n, (x, y, h) in pos.items():
        ax.add_patch(Rectangle(
            (x, y), node_w, h, facecolor=_SANKEY_NODE_COLOURS[n],
            edgecolor="black", linewidth=0.5, zorder=3,
        ))
        label = f"{n}\n({node_total(n):,.0f} MWh)"
        if _SANKEY_NODE_COLUMNS[n] == 2:
            ax.text(x + node_w + 0.012, y + h / 2, label,
                    va="center", ha="left", fontsize=7)
        elif _SANKEY_NODE_COLUMNS[n] == 0:
            ax.text(x - 0.012, y + h / 2, label,
                    va="center", ha="right", fontsize=7)
        else:
            ax.text(x + node_w / 2, y - 0.018, label,
                    va="top", ha="center", fontsize=7)

    ys = [p[1] for p in pos.values()]
    tops = [p[1] + p[2] for p in pos.values()]
    ax.set_xlim(-0.14, 1.12)
    ax.set_ylim(min(ys) - 0.1, max(tops) + 0.04)
    return save_figure(out_path)


def plot_cfe_duration_curve(res: pd.DataFrame, out_path: Path) -> Path:
    """Carbon-free fraction of the load, sorted descending (24/7 CFE curve)."""
    frac = np.sort(hourly_cfe_fraction(res))[::-1] * 100.0
    if frac.size == 0:
        return empty_placeholder(out_path, "No load to match.")
    _fig, ax = plt.subplots(figsize=(7, 4))
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
    # The share-of-time axis is a bounded 0-100 % scale: keep it edge to
    # edge (skip_x) and pad only the y headroom above the curve.
    ax.set_xlim(0.0, 100.0)
    ax.set_ylim(0.0, 100.0)
    apply_universal_margins(ax, skip_x=True)
    if has_legend:
        legend_below(ax)
    return save_figure(out_path)
