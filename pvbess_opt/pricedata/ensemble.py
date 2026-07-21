"""Weighted price-scenario ensemble (one dispatch, N cashflows).

Under the reprice tier every price scenario shares the SAME Year-1
dispatch (the scenario curves change the projection, not the Year-1
prices), so the ensemble never re-solves the MILP: each enabled
scenario applies its auto-trajectories to a fresh copy of the economic
inputs and rebuilds the yearly cashflow + financial KPIs.  Under the
resolve tier the applied scenario's support-year re-solves are already
in hand; the OTHER scenarios fall back to their reprice factors (a
documented approximation — re-solving every scenario is the
scenarios-harness path, not the in-run ensemble).

Debt is sized ONCE on ``debt_sizing_scenario`` (the single-run applied
scenario) and every ensemble member inherits the frozen sized-debt
keys, so the table compares OPERATING outcomes on one committed capital
structure — pick a downside scenario for bankable sizing.

Weighted statistics: E[NPV] / E[IRR] are the weight-averaged moments;
P10/P50/P90 are weighted empirical-CDF percentiles over the DISCRETE
scenario set (the smallest scenario value whose cumulative weight
reaches the level).  Monte Carlo stays Year-1 forecast-error-only:
price-LEVEL risk is the scenario dimension — no double counting.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .engine import (
    build_scenario_deck,
    derive_reprice_trajectories,
    merge_auto_trajectories,
)
from .store import PriceDataError

logger = logging.getLogger(__name__)

#: Frozen debt-sizing keys every ensemble member inherits verbatim.
_FROZEN_DEBT_KEYS: tuple[str, ...] = (
    "_sized_debt_eur", "_debt_capacity_eur", "_gearing_sized_pct",
    "_binding_dscr_year", "_dscr_target_met",
)


@dataclass
class EnsembleResult:
    """The per-scenario table plus the weighted headline statistics."""

    table: pd.DataFrame
    stats: dict[str, float]
    summary_lines: list[str]


def weighted_percentile(
    values: list[float], weights: list[float], level_pct: float,
) -> float:
    """Weighted empirical-CDF percentile over a discrete scenario set.

    Scenarios sort by value; the percentile is the smallest value whose
    cumulative weight share reaches ``level_pct`` — deterministic and
    exact for the discrete distribution the weights define (no
    interpolation between scenarios: a P90 that never occurred in any
    scenario would be an invented outcome).
    """
    if not values:
        return float("nan")
    total = float(sum(weights))
    if total <= 0.0:
        return float("nan")
    order = np.argsort(np.asarray(values, dtype=float))
    cumulative = 0.0
    for index in order:
        cumulative += float(weights[index]) / total * 100.0
        if cumulative >= level_pct - 1e-9:
            return float(values[index])
    return float(np.asarray(values)[order[-1]])


def run_price_scenario_ensemble(
    econ_base: dict[str, Any],
    year1_kpis: dict[str, Any],
    capacities: dict[str, float],
    ts: pd.DataFrame,
    res: pd.DataFrame,
    *,
    base_dir: Path,
    applied_trajectories: dict[str, dict[str, Any]] | None = None,
    applied_name: str = "",
    lifetime_yearly: pd.DataFrame | None = None,
) -> EnsembleResult | None:
    """Evaluate every enabled scenario into a weighted comparison table.

    ``econ_base`` is the SINGLE-RUN econ dict AFTER debt sizing (the
    frozen debt keys are inherited by every member) and after the
    applied scenario's trajectories were merged; the applied scenario
    reuses them verbatim (``applied_trajectories`` — including Tier-2
    factors under resolve mode), the other members derive their own
    reprice factors.
    """
    from pvbess_opt.economics import (
        build_yearly_cashflow,
        compute_financial_kpis,
    )

    if not bool(econ_base.get("price_scenarios_enabled", False)):
        return None
    scenarios = econ_base.get("price_scenarios") or []
    if not scenarios:
        return None

    n_years = int(econ_base.get("project_lifecycle_years", 0) or 0)
    user_trajectories = {
        stream: spec
        for stream, spec in (econ_base.get("trajectories") or {}).items()
        if applied_trajectories is None
        or stream not in applied_trajectories
    }
    rows: list[dict[str, Any]] = []
    for entry in scenarios:
        name = str(entry["name"])
        econ_s = copy.deepcopy(econ_base)
        if name == applied_name and applied_trajectories is not None:
            generated = applied_trajectories
        else:
            deck = build_scenario_deck(
                entry,
                base_dir=base_dir, ts=ts, n_steps=len(ts),
                dt_minutes=_dt_minutes_from_frame(ts),
                n_years=n_years,
                start_year=_start_year(econ_base),
                engine_basis=str(
                    econ_base.get("price_basis", "nominal") or "nominal",
                ),
                engine_base_year=int(
                    econ_base.get("price_base_year", 0) or 0,
                ),
                cpi_pct=float(econ_base.get("cpi_pct", 0.0) or 0.0),
            )
            generated, _paths = derive_reprice_trajectories(
                deck, res, n_years=n_years,
            )
        econ_s["trajectories"] = merge_auto_trajectories(
            dict(user_trajectories), generated, scenario=name,
        )
        # One committed capital structure: the debt sized on the
        # applied (debt-sizing) scenario is inherited verbatim.
        for key in _FROZEN_DEBT_KEYS:
            if key in econ_base:
                econ_s[key] = econ_base[key]
        cashflow = build_yearly_cashflow(year1_kpis, econ_s, capacities)
        fin = compute_financial_kpis(
            cashflow, econ_s,
            capacities=capacities,
            lifetime_yearly=lifetime_yearly,
            year1_kpis=year1_kpis,
        )
        rows.append({
            "scenario": name,
            "provider": str(entry.get("provider", "")),
            "weight_pct": float(entry.get("weight_pct", 0.0) or 0.0),
            "applied": name == applied_name,
            "npv_eur": float(fin.get("npv_eur", float("nan"))),
            "irr_pct": float(fin.get("irr_pct", float("nan"))),
            "simple_payback_years": float(
                fin.get("simple_payback_years", float("nan")),
            ),
            "min_dscr": float(fin.get("min_dscr", float("nan"))),
        })
    table = pd.DataFrame(rows)
    total_weight = float(table["weight_pct"].sum())
    if abs(total_weight - 100.0) > 1e-6:
        raise PriceDataError(
            f"price scenario weights must sum to 100 %; the ensemble "
            f"got {total_weight:g}."
        )

    npvs = table["npv_eur"].tolist()
    weights = table["weight_pct"].tolist()
    irr_values = [
        (irr, w) for irr, w in zip(
            table["irr_pct"].tolist(), weights, strict=True,
        )
        if np.isfinite(irr)
    ]
    stats: dict[str, float] = {
        "expected_npv_eur": float(
            sum(v * w for v, w in zip(npvs, weights, strict=True)) / 100.0
        ),
        "npv_p10_eur": weighted_percentile(npvs, weights, 10.0),
        "npv_p50_eur": weighted_percentile(npvs, weights, 50.0),
        "npv_p90_eur": weighted_percentile(npvs, weights, 90.0),
    }
    if irr_values:
        irr_weight = sum(w for _v, w in irr_values)
        stats["expected_irr_pct"] = float(
            sum(v * w for v, w in irr_values) / irr_weight
        ) if irr_weight > 0 else float("nan")
    summary_lines = [
        f"- Scenario ensemble ({len(table)} weighted scenario(s)): "
        f"E[NPV] {stats['expected_npv_eur']:,.0f} EUR, "
        f"P10/P50/P90 {stats['npv_p10_eur']:,.0f} / "
        f"{stats['npv_p50_eur']:,.0f} / {stats['npv_p90_eur']:,.0f} EUR",
    ]
    if "expected_irr_pct" in stats:
        summary_lines.append(
            f"- E[IRR]: {stats['expected_irr_pct']:.2f} % "
            "(shared debt sized on the applied scenario)"
        )
    logger.info(
        "[pricedata] weighted ensemble: %s",
        "; ".join(line.lstrip("- ") for line in summary_lines),
    )
    return EnsembleResult(
        table=table, stats=stats, summary_lines=summary_lines,
    )


def _dt_minutes_from_frame(ts: pd.DataFrame) -> int:
    n_steps = len(ts)
    if n_steps == 0 or n_steps % 365 != 0:
        raise PriceDataError(
            f"ensemble needs a whole non-leap-year timeseries; got "
            f"{n_steps} steps."
        )
    return (24 * 60) // (n_steps // 365)


def _start_year(econ: dict[str, Any]) -> int:
    """Project start year with the pipeline-wide schema default.

    Mirrors ``engine.apply_price_scenarios``: a blank/zero cell must not
    collapse to calendar year 0 and silently zero real-basis / TYNDP
    curves through the deflator bridge.
    """
    from pvbess_opt.io import PROJECT_SHEET_DEFAULTS

    start_year = int(
        econ.get("project_start_year")
        or PROJECT_SHEET_DEFAULTS["project_start_year"]
    )
    if start_year <= 0:
        raise PriceDataError(
            "price scenarios need a positive project_start_year (got "
            f"{start_year!r}); set project_start_year on the project sheet."
        )
    return start_year
