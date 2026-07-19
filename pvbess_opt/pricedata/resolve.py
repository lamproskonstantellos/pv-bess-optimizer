"""Tier-2 support-year re-solves (``scenario_projection_mode = 'resolve'``).

The reprice tier (engine.py) freezes the Year-1 dispatch; this tier
re-solves the MILP at the configured support years with that year's
scenario prices AND the degraded plant — the ``_run_midlife_resolve``
machinery (Eq. E53) applied per scenario year at a coarser grid
(``scenario_resolve_resolution``, hourly by default: a full-year hourly
MILP solves in seconds-to-minutes, while 15-minute re-solves across
6 scenarios x N years are infeasible as a default).

Factor construction — the double-degradation trap: the cashflow applies
``base_1 × f_s(y) × g_s[y]`` (Eq. E24 on the split streams), where
``f_s`` is the PV/BESS degradation factor.  A re-solved revenue
``R2_s(y)`` ALREADY carries the degraded plant, so the Tier-2 factor is
normalised by the analytic degradation:

    g2_s[y] = [ R2_s(y) / R2_s(1) ] / f_s(y)

with ``R2_s(1)`` a year-1 re-solve at the SAME resolution (the
resolution bias cancels in the ratio).  At a support year the cashflow
then reproduces ``base_1 × R2_s(y)/R2_s(1)`` exactly: the pure price
effect PLUS the dispatch adaptation the frozen-dispatch tier cannot
see (SOC re-timing under the new shape, cycle-cap interaction).

Between support years the factors interpolate log-linearly on the
factor level (``scenario_interp = 'loglinear'``); a non-positive
factor falls back to linear interpolation for that stream with a
WARNING.  The Tier-2 − Tier-1 factor delta per support year is
reported as a diagnostic table (the midlife/E53 style) and never
alters the Tier-1 outputs unless the mode is 'resolve'.

Scope: the re-solves refine the three DAM streams only.  Balancing
paths stay on the store's annual table (annual scalars need no
dispatch), intraday/imbalance scenario curves are out of scope by
design, and the re-solve runs the day-ahead stage with the balancing
and intraday blocks OFF — mirroring the midlife re-solve's
day-ahead-only contract.
"""

from __future__ import annotations

import copy
import logging
import math
import time
from typing import Any

import numpy as np
import pandas as pd

from pvbess_opt.marketdata import resample_intensive

from .engine import _revenue_eur, _volume_kwh
from .store import PriceDataError, ScenarioDeck

logger = logging.getLogger(__name__)

#: The streams the Tier-2 re-solves refine.
RESOLVE_STREAMS: tuple[str, ...] = (
    "revenue_dam_pv", "revenue_dam_bess_export", "expense_dam_bess_charge",
)


def parse_support_years(raw: str, n_years: int) -> list[int]:
    """Parse ``scenario_resolve_years`` (CSV of operating years).

    Year 1 is forced in (it anchors the factor ratios), duplicates
    collapse, years beyond the lifecycle are rejected loudly.
    """
    years: set[int] = {1}
    for token in str(raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            year = int(float(token))
        except ValueError as exc:
            raise PriceDataError(
                f"scenario_resolve_years: {token!r} is not a year."
            ) from exc
        if year < 1 or year > n_years:
            raise PriceDataError(
                f"scenario_resolve_years: year {year} is outside the "
                f"project lifecycle 1..{n_years}."
            )
        years.add(year)
    return sorted(years)


def build_resolve_grid(
    ts: pd.DataFrame, dt_minutes: int, resolution_minutes: int,
) -> pd.DataFrame:
    """Resample the workbook timeseries onto the re-solve grid.

    Energy columns sum (extensive), price columns mean (intensive);
    balancing / intraday / imbalance columns are dropped — the
    re-solve runs the day-ahead stage only.  A resolution finer than
    the workbook cadence is rejected (detail cannot be invented).
    """
    if resolution_minutes < dt_minutes:
        raise PriceDataError(
            f"scenario_resolve_resolution = {resolution_minutes} min is "
            f"finer than the workbook cadence ({dt_minutes} min); "
            "re-solves cannot invent sub-step detail."
        )
    if resolution_minutes % dt_minutes != 0:
        raise PriceDataError(
            f"scenario_resolve_resolution = {resolution_minutes} min is "
            f"not a multiple of the workbook cadence ({dt_minutes} min)."
        )
    keep = [
        column for column in
        ("timestamp", "pv_kwh", "load_kwh", "dam_price_eur_per_mwh",
         "retail_price_eur_per_mwh")
        if column in ts.columns
    ]
    frame = ts[keep].set_index("timestamp")
    aggregated = frame.resample(f"{resolution_minutes}min").agg({
        column: ("sum" if column.endswith("_kwh") else "mean")
        for column in frame.columns
    })
    return aggregated.reset_index()


def _resolve_year_prices(
    deck: ScenarioDeck, year: int, dt_minutes: int,
    resolution_minutes: int,
) -> np.ndarray:
    """The deck's year curve resampled onto the re-solve grid."""
    notes: set[str] = set()
    return resample_intensive(
        deck.dam_curve(year), dt_minutes, resolution_minutes,
        column="dam_price_eur_per_mwh", notes=notes,
        context=f"resolve year {year}",
    )


def derive_resolve_trajectories(
    deck: ScenarioDeck,
    params: dict[str, Any],
    ts: pd.DataFrame,
    econ: dict[str, Any],
    *,
    n_years: int,
    support_years: list[int],
    resolution_minutes: int,
    interp: str = "loglinear",
    solver_opts: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], pd.DataFrame]:
    """Support-year re-solve factors for the three DAM streams.

    Returns ``(trajectories, support_table)``: replace-mode blocks over
    all ``n_years`` (interpolated between support years) and the raw
    per-support-year table (factors + solve wall-clock) for the delta
    diagnostic.
    """
    from pvbess_opt.kpis import compute_kpis
    from pvbess_opt.lifetime import factors_for_year
    from pvbess_opt.optimization import run_scenario

    dt_minutes = int(params.get("dt_minutes", 0) or 0)
    grid = build_resolve_grid(ts, dt_minutes, resolution_minutes)
    capacity_mwh = float(params.get("bess_capacity_kwh", 0.0) or 0.0) / 1000.0
    year1_discharge = float(
        econ.get("_resolve_year1_discharge_mwh", 0.0) or 0.0
    )

    revenues: dict[str, dict[int, float]] = {s: {} for s in RESOLVE_STREAMS}
    factors_deg: dict[int, tuple[float, float]] = {}
    rows: list[dict[str, Any]] = []
    for year in support_years:
        pv_f, bess_f = factors_for_year(
            econ, year=year,
            year1_discharge_mwh=year1_discharge,
            capacity_mwh=capacity_mwh,
        )
        factors_deg[year] = (pv_f, bess_f)
        params_y = copy.deepcopy(params)
        params_y["dt_minutes"] = int(resolution_minutes)
        params_y["bess_capacity_kwh"] = (
            float(params.get("bess_capacity_kwh", 0.0) or 0.0) * bess_f
        )
        # Day-ahead stage only (the midlife contract): the balancing
        # and intraday blocks stay off at the re-solve cadence.
        if isinstance(params_y.get("balancing"), dict):
            params_y["balancing"]["balancing_enabled"] = False
        if isinstance(params_y.get("intraday"), dict):
            params_y["intraday"]["id_enabled"] = False
        ts_y = grid.copy()
        if "pv_kwh" in ts_y.columns:
            ts_y["pv_kwh"] = ts_y["pv_kwh"].astype(float) * pv_f
        ts_y["dam_price_eur_per_mwh"] = _resolve_year_prices(
            deck, year, dt_minutes, resolution_minutes,
        )
        started = time.perf_counter()
        res_y, _solver, _full = run_scenario(
            params_y, ts_y, return_unrounded=True, **(solver_opts or {}),
        )
        compute_kpis(res_y, params_y, verify_balance=False)
        elapsed = time.perf_counter() - started
        n_steps = len(res_y)
        price_y = ts_y["dam_price_eur_per_mwh"].to_numpy(dtype=float)
        stream_revenues = {
            "revenue_dam_pv": _revenue_eur(
                _volume_kwh(res_y, "pv_to_grid_kwh", n_steps), price_y,
            ),
            "revenue_dam_bess_export": _revenue_eur(
                _volume_kwh(res_y, "bess_dis_grid_kwh", n_steps), price_y,
            ),
            "expense_dam_bess_charge": _revenue_eur(
                _volume_kwh(res_y, "bess_charge_grid_kwh", n_steps),
                price_y,
            ),
        }
        for stream, value in stream_revenues.items():
            revenues[stream][year] = value
        logger.info(
            "[pricedata] Tier-2 re-solve: scenario %r year %d at "
            "%d min (%d steps, pv_f=%.4f, bess_f=%.4f) took %.1f s.",
            deck.name, year, resolution_minutes, n_steps, pv_f, bess_f,
            elapsed,
        )
        rows.append({
            "project_year": year,
            "solve_seconds": round(elapsed, 2),
            "pv_factor": pv_f,
            "bess_factor": bess_f,
            **{f"revenue_{s}": v for s, v in stream_revenues.items()},
        })

    trajectories: dict[str, dict[str, Any]] = {}
    support_table = pd.DataFrame(rows)
    for stream in RESOLVE_STREAMS:
        base = revenues[stream][1]
        support_factors: dict[int, float] = {}
        for year in support_years:
            if abs(base) < 1e-9:
                support_factors[year] = 1.0
                continue
            pv_f, bess_f = factors_deg[year]
            degradation = pv_f if stream == "revenue_dam_pv" else bess_f
            if degradation <= 0.0:
                support_factors[year] = 0.0
                continue
            support_factors[year] = (
                revenues[stream][year] / base
            ) / degradation
        support_factors[1] = 1.0
        values = interpolate_support_factors(
            support_factors, n_years, interp=interp, stream=stream,
        )
        trajectories[stream] = {"mode": "replace", "values": values}
        support_table[f"g2_{stream}"] = [
            support_factors[year] for year in support_years
        ]
    return trajectories, support_table


def interpolate_support_factors(
    support: dict[int, float],
    n_years: int,
    *,
    interp: str = "loglinear",
    stream: str = "",
) -> list[float]:
    """Fill years 1..N from the support-year factors.

    ``loglinear`` interpolates on ``log g`` (multiplicative paths stay
    multiplicative — factor monotonicity between support years is
    preserved); a non-positive support factor makes the log undefined,
    so the stream falls back to LINEAR interpolation with a WARNING.
    Years beyond the last support year hold its factor.
    """
    years = sorted(support)
    factors = [float(support[y]) for y in years]
    mode = str(interp or "loglinear").strip().lower()
    use_log = mode == "loglinear" and all(f > 0.0 for f in factors)
    if mode == "loglinear" and not use_log:
        logger.warning(
            "[pricedata] stream %s carries a non-positive support-year "
            "factor; log-linear interpolation is undefined — falling "
            "back to linear for this stream.",
            stream,
        )
    xs = np.asarray(years, dtype=float)
    if use_log:
        ys = np.log(np.asarray(factors))
        out = np.exp(np.interp(np.arange(1, n_years + 1), xs, ys))
    else:
        out = np.interp(
            np.arange(1, n_years + 1), xs, np.asarray(factors),
        )
    values = [float(v) for v in out]
    values[0] = 1.0
    if any(not math.isfinite(v) for v in values):
        raise PriceDataError(
            f"stream {stream}: non-finite interpolated factor."
        )
    return values


def build_resolve_delta(
    tier1: dict[str, dict[str, Any]],
    tier2: dict[str, dict[str, Any]],
    support_years: list[int],
) -> pd.DataFrame:
    """Tier-2 − Tier-1 factor delta at the support years (diagnostic).

    The E53 midlife style: one row per (support year, stream) with both
    factors and the delta, so the report shows exactly where dispatch
    adaptation departs from the frozen-dispatch approximation.
    """
    rows: list[dict[str, Any]] = []
    for stream in RESOLVE_STREAMS:
        g1 = tier1.get(stream, {}).get("values") or []
        g2 = tier2.get(stream, {}).get("values") or []
        for year in support_years:
            index = year - 1
            value1 = float(g1[index]) if index < len(g1) else float("nan")
            value2 = float(g2[index]) if index < len(g2) else float("nan")
            rows.append({
                "project_year": year,
                "stream": stream,
                "g_tier1_reprice": round(value1, 6),
                "g_tier2_resolve": round(value2, 6),
                "delta": round(value2 - value1, 6),
                "delta_pct": round(
                    100.0 * (value2 - value1) / value1, 4,
                ) if value1 not in (0.0,) and math.isfinite(value1)
                else float("nan"),
            })
    return pd.DataFrame(rows)
