"""Capacity sizing sweep, efficient frontier, and marginal value of storage.

The MILP optimises *dispatch* for a fixed ``(pv_kwp, bess_kw, bess_kwh)``.
This module is the outer loop the headline competitors (HOMER, Gridcog)
sell: it re-runs that solve over a grid of sizes, collects the financial
KPIs, ranks an efficient frontier, and derives the **marginal value of
storage** (dNPV/dMWh along the BESS-energy axis) and the **oversizing
break-even** (the energy where that slope crosses zero).

It reuses the resolved PV shape (and the PVGIS per-kWp cache), rescaling it
per PV size rather than re-fetching.
"""

from __future__ import annotations

import itertools
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_FRONTIER_COLUMNS: tuple[str, ...] = (
    "pv_nameplate_kwp",
    "bess_power_kw",
    "bess_capacity_kwh",
    "bess_capacity_mwh",
    "npv_eur",
    "irr_pct",
    "simple_payback_years",
    "lcoe_eur_per_mwh",
    "lcos_eur_per_mwh",
)


@dataclass
class SizingResult:
    """Outputs of a sizing sweep."""

    frontier: pd.DataFrame              # one row per size, ranked by NPV
    marginal_value: pd.DataFrame        # dNPV/dMWh along the BESS-energy axis
    oversizing_breakeven_mwh: float     # MWh where dNPV/dMWh crosses zero


# ---------------------------------------------------------------------------
# Grid parsing
# ---------------------------------------------------------------------------


def _axis_values(spec: Any) -> list[float]:
    """Parse one grid axis: an explicit list, a ``{min,max,step}`` mapping,
    or a scalar."""
    if isinstance(spec, (list, tuple)):
        return [float(x) for x in spec]
    if isinstance(spec, dict):
        lo = float(spec["min"])
        hi = float(spec["max"])
        step = float(spec.get("step", (hi - lo) or 1.0))
        if step <= 0.0:
            raise ValueError("sizing axis 'step' must be positive")
        out: list[float] = []
        x = lo
        while x <= hi + 1e-9:
            out.append(round(x, 6))
            x += step
        return out
    return [float(spec)]


def parse_sizing_grid(block: dict[str, Any]) -> list[tuple[float, float, float]]:
    """Cartesian product of the PV / BESS-power / BESS-capacity axes.

    Capacity may be given directly as ``bess_capacity_kwh`` or as
    ``bess_duration_hours`` (capacity = power x duration).
    """
    pv = _axis_values(block.get("pv_nameplate_kwp", block.get("pv_kwp", 0.0)))
    power = _axis_values(block.get("bess_power_kw", 0.0))
    if "bess_capacity_kwh" in block:
        cap = _axis_values(block["bess_capacity_kwh"])
        return [(p, w, c) for p, w, c in itertools.product(pv, power, cap)]
    if "bess_duration_hours" in block:
        dur = _axis_values(block["bess_duration_hours"])
        return [(p, w, w * d) for p, w, d in itertools.product(pv, power, dur)]
    return [(p, w, w) for p, w in itertools.product(pv, power)]


# ---------------------------------------------------------------------------
# Per-point evaluation + sweep
# ---------------------------------------------------------------------------


def evaluate_sizing_point(
    base_params: dict[str, Any],
    base_ts: pd.DataFrame,
    base_pv: dict[str, Any],
    base_xlsx: str | Path,
    point: tuple[float, float, float],
    *,
    solver_opts: dict[str, Any],
) -> dict[str, float]:
    """Solve one size point and return its frontier row (sizes + KPIs)."""
    from .availability import apply_operating_derates
    from .kpis import compute_kpis
    from .optimization import run_scenario
    from .pipeline import _build_financials

    pv_kwp, bess_kw, bess_kwh = (float(point[0]), float(point[1]), float(point[2]))
    params = dict(base_params)
    params["pv_nameplate_kwp"] = pv_kwp
    params["bess_power_kw"] = bess_kw
    params["bess_capacity_kwh"] = bess_kwh

    # Scale the resolved base PV profile by the nameplate ratio so the PV-size
    # axis is physically meaningful.  The base column is the absolute
    # generation at ``base_pv['pv_nameplate_kwp']``; a point at ``pv_kwp``
    # scales it linearly (shape preserved).  When the base carries no PV
    # nameplate there is no shape to scale, so the column passes through.
    base_nameplate = float(base_pv.get("pv_nameplate_kwp", 0.0) or 0.0)
    if base_nameplate > 0.0 and "pv_kwh" in base_ts.columns:
        ts_pt = base_ts.copy()
        ts_pt["pv_kwh"] = (
            base_ts["pv_kwh"].astype(float) * (pv_kwp / base_nameplate)
        )
    else:
        ts_pt = base_ts

    res, _solver, _res_full = run_scenario(
        params, ts_pt, return_unrounded=True, **solver_opts,
    )
    kpis = compute_kpis(res, params, verify_balance=False)
    kpis = apply_operating_derates(kpis, params)
    bundle = _build_financials(
        Path(base_xlsx), params, ts_pt, kpis, res,
        solver_opts=solver_opts, in_sizing_sweep=True,
    )
    fin = bundle.get("fin_kpis") or {}

    def _f(key: str) -> float:
        try:
            return float(fin.get(key, float("nan")))
        except (TypeError, ValueError):
            return float("nan")

    return {
        "pv_nameplate_kwp": pv_kwp,
        "bess_power_kw": bess_kw,
        "bess_capacity_kwh": bess_kwh,
        "bess_capacity_mwh": bess_kwh / 1000.0,
        "npv_eur": _f("npv_eur"),
        "irr_pct": _f("irr_pct"),
        "simple_payback_years": _f("simple_payback_years"),
        "lcoe_eur_per_mwh": _f("lcoe_eur_per_mwh"),
        "lcos_eur_per_mwh": _f("lcos_eur_per_mwh"),
    }


def run_sizing_sweep(
    base_params: dict[str, Any],
    base_ts: pd.DataFrame,
    base_pv: dict[str, Any],
    base_xlsx: str | Path,
    grid: list[tuple[float, float, float]],
    *,
    solver_opts: dict[str, Any],
) -> pd.DataFrame:
    """Evaluate every grid point and return the ranked efficient frontier."""
    rows = [
        evaluate_sizing_point(
            base_params, base_ts, base_pv, base_xlsx, pt, solver_opts=solver_opts,
        )
        for pt in grid
    ]
    frontier = pd.DataFrame(rows, columns=list(_FRONTIER_COLUMNS))
    return rank_frontier(frontier)


def rank_frontier(frontier: pd.DataFrame) -> pd.DataFrame:
    """Sort the frontier by NPV (descending)."""
    return frontier.sort_values("npv_eur", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Marginal value of storage + oversizing break-even
# ---------------------------------------------------------------------------


def compute_marginal_value_of_storage(frontier: pd.DataFrame) -> pd.DataFrame:
    """dNPV/dMWh along the BESS-energy axis, per ``(pv, power)`` group."""
    rows: list[dict[str, float]] = []
    grouped = frontier.groupby(["pv_nameplate_kwp", "bess_power_kw"], sort=True)
    for _key, grp in grouped:
        g = grp.sort_values("bess_capacity_mwh")
        pv_val = float(g["pv_nameplate_kwp"].iloc[0])
        power_val = float(g["bess_power_kw"].iloc[0])
        mwh = g["bess_capacity_mwh"].to_numpy(dtype=float)
        npv = g["npv_eur"].to_numpy(dtype=float)
        if mwh.size < 2:
            continue
        d_mwh = np.diff(mwh)
        with np.errstate(divide="ignore", invalid="ignore"):
            slope = np.where(d_mwh != 0.0, np.diff(npv) / d_mwh, np.nan)
        mid = (mwh[:-1] + mwh[1:]) / 2.0
        for m, s in zip(mid, slope, strict=False):
            rows.append({
                "pv_nameplate_kwp": pv_val,
                "bess_power_kw": power_val,
                "bess_capacity_mwh": float(m),
                "marginal_npv_eur_per_mwh": float(s),
            })
    return pd.DataFrame(
        rows,
        columns=[
            "pv_nameplate_kwp", "bess_power_kw",
            "bess_capacity_mwh", "marginal_npv_eur_per_mwh",
        ],
    )


def find_oversizing_breakeven(mwh: Any, npv: Any) -> float:
    """Return the BESS energy (MWh) where dNPV/dMWh first crosses to <= 0.

    Linearly interpolates the slope to zero between the bracketing points;
    returns ``nan`` when storage NPV never stops rising over the range.
    """
    mwh_arr = np.asarray(mwh, dtype=float)
    npv_arr = np.asarray(npv, dtype=float)
    order = np.argsort(mwh_arr)
    mwh_arr = mwh_arr[order]
    npv_arr = npv_arr[order]
    if mwh_arr.size < 2:
        return float("nan")
    # Guard duplicate capacity points (zero spacing): an unguarded
    # ``np.diff(npv)/np.diff(mwh)`` divides by zero (RuntimeWarning) and
    # yields a spurious crossing.  ``np.errstate`` silences the eager
    # division and ``np.where`` maps the zero-spacing segments to NaN,
    # which the ``<= 0`` / ``> 0`` crossing tests then skip.
    d_mwh = np.diff(mwh_arr)
    with np.errstate(divide="ignore", invalid="ignore"):
        slope = np.where(d_mwh != 0.0, np.diff(npv_arr) / d_mwh, np.nan)
    mid = (mwh_arr[:-1] + mwh_arr[1:]) / 2.0
    for i in range(slope.size):
        if slope[i] <= 0.0:
            if i > 0 and slope[i - 1] > 0.0 and slope[i - 1] != slope[i]:
                frac = slope[i - 1] / (slope[i - 1] - slope[i])
                return float(mid[i - 1] + frac * (mid[i] - mid[i - 1]))
            return float(mid[i])
    return float("nan")


def _breakeven_for_best_group(frontier: pd.DataFrame) -> float:
    if frontier.empty:
        return float("nan")
    top = frontier.iloc[0]
    sl = frontier[
        (frontier["pv_nameplate_kwp"] == top["pv_nameplate_kwp"])
        & (frontier["bess_power_kw"] == top["bess_power_kw"])
    ].sort_values("bess_capacity_mwh")
    return find_oversizing_breakeven(
        sl["bess_capacity_mwh"].to_numpy(dtype=float),
        sl["npv_eur"].to_numpy(dtype=float),
    )


# ---------------------------------------------------------------------------
# Output + orchestration
# ---------------------------------------------------------------------------


def write_sizing_workbook(out_path: str | Path, result: SizingResult) -> Path:
    """Write the frontier + marginal-value + summary to a styled workbook."""
    from .io_style import style_workbook

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary = pd.DataFrame(
        [{"metric": "oversizing_breakeven_mwh",
          "value": result.oversizing_breakeven_mwh}],
    )
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        result.frontier.to_excel(writer, sheet_name="sizing_frontier", index=False)
        if not result.marginal_value.empty:
            result.marginal_value.to_excel(
                writer, sheet_name="marginal_value", index=False,
            )
        summary.to_excel(writer, sheet_name="sizing_summary", index=False)
        style_workbook(writer.book)
    return out_path


def _parse_sizing_sheet(df: pd.DataFrame) -> tuple[bool, dict[str, Any]]:
    """Parse the optional columnar ``sizing`` sheet into ``(enabled, block)``.

    Each axis column (``pv_nameplate_kwp``, ``bess_power_kw``, and either
    ``bess_capacity_kwh`` or ``bess_duration_hours``) contributes its
    non-blank numeric cells as a list; the ``enabled`` toggle is read from
    the first non-blank cell of the ``enabled`` column.  The returned block
    is in the shape :func:`parse_sizing_grid` consumes (``bess_capacity_kwh``
    wins over ``bess_duration_hours`` when both carry values).
    """
    from .io import _parse_bool

    cols = {str(c).strip().lower(): c for c in df.columns}

    def axis(name: str) -> list[float]:
        key = cols.get(name)
        if key is None:
            return []
        series = pd.to_numeric(df[key], errors="coerce").dropna()
        return [float(x) for x in series.tolist()]

    enabled = False
    enabled_key = cols.get("enabled")
    if enabled_key is not None:
        nonnull = df[enabled_key].dropna()
        if not nonnull.empty:
            enabled = _parse_bool(nonnull.iloc[0], False)

    block: dict[str, Any] = {}
    pv = axis("pv_nameplate_kwp")
    power = axis("bess_power_kw")
    if pv:
        block["pv_nameplate_kwp"] = pv
    if power:
        block["bess_power_kw"] = power
    capacity = axis("bess_capacity_kwh")
    duration = axis("bess_duration_hours")
    if capacity:
        block["bess_capacity_kwh"] = capacity
    elif duration:
        block["bess_duration_hours"] = duration
    return enabled, block


def read_sizing_block(path: str | Path) -> dict[str, Any] | None:
    """Return the sizing-sweep grid for ``path``, or None when not enabled.

    A YAML / JSON config supplies the grid under a ``sizing`` mapping (its
    presence enables the sweep).  An Excel workbook supplies it on the
    columnar ``sizing`` sheet, gated by the ``enabled`` TRUE/FALSE toggle.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml", ".json"):
        text = path.read_text(encoding="utf-8")
        if suffix == ".json":
            raw = json.loads(text)
        else:
            import yaml

            raw = yaml.safe_load(text)
        if isinstance(raw, dict):
            block = raw.get("sizing")
            if isinstance(block, dict):
                return block
        return None
    if suffix in (".xlsx", ".xls"):
        if not path.exists():
            return None
        try:
            with pd.ExcelFile(path) as _xl:
                sheets = set(_xl.sheet_names)
        except (ValueError, OSError):
            return None
        if "sizing" not in sheets:
            return None
        enabled, block = _parse_sizing_sheet(
            pd.read_excel(path, sheet_name="sizing"),
        )
        return block if enabled else None
    return None


def run_sizing(config: Any, sizing_block: dict[str, Any]) -> SizingResult:
    """Run a sizing sweep for ``config`` over ``sizing_block`` and write the
    frontier workbook + plots under the output directory."""
    from .io import _typed_to_flat, read_workbook
    from .io_read import is_structured_config, materialize_to_xlsx
    from .plotting import (
        apply_ieee_style,
        plot_efficient_frontier,
        plot_npv_vs_capacity,
    )

    src = Path(config.excel)
    tmp = Path(tempfile.mkdtemp(prefix="pvbess_sizing_"))
    base_xlsx = materialize_to_xlsx(src, tmp) if is_structured_config(src) else src
    base_typed = read_workbook(base_xlsx)
    base_params, base_ts = _typed_to_flat(base_typed)
    if getattr(config, "mode", None) is not None:
        base_params["mode"] = config.mode

    grid = parse_sizing_grid(sizing_block)
    if not grid:
        raise ValueError("sizing block produced an empty grid")
    solver_opts = {
        "solver_name": config.solver,
        "mip_gap": config.mip_gap,
        "time_limit_seconds": config.time_limit,
        "tee": config.tee,
    }

    apply_ieee_style()
    frontier = run_sizing_sweep(
        base_params, base_ts, base_typed["pv"], base_xlsx, grid,
        solver_opts=solver_opts,
    )
    # The per-point financials have consumed the materialised base workbook;
    # drop the temp dir before assembling outputs so a sweep does not leak it.
    shutil.rmtree(tmp, ignore_errors=True)
    marginal = compute_marginal_value_of_storage(frontier)
    breakeven = _breakeven_for_best_group(frontier)
    result = SizingResult(
        frontier=frontier,
        marginal_value=marginal,
        oversizing_breakeven_mwh=breakeven,
    )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(config.outdir) / f"{src.stem}_sizing_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_sizing_workbook(out_dir / "sizing.xlsx", result)
    plot_efficient_frontier(frontier, out_dir / "efficient_frontier.pdf")
    plot_npv_vs_capacity(frontier, breakeven, out_dir / "npv_vs_capacity.pdf")
    logger.info(
        "[sizing] %d points evaluated; outputs under %s", len(frontier), out_dir,
    )
    return result
