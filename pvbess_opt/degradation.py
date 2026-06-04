"""Battery degradation: Rainflow cycle counting, wear cost, SOH fade.

Two cheap, dispatch-aware degradation tools that avoid an expensive in-MILP
nonlinear model:

* :func:`derive_wear_cost_eur_per_mwh` — a calibrated €/MWh-throughput wear
  cost (replacement cost / cycle-life / usable energy).  The MILP objective
  subtracts ``wear_cost x discharge`` so the optimizer only cycles when the
  spread beats the wear cost.  It is a behavioural shadow price: it shapes
  dispatch but is **not** added to the reported cashflow / NPV (the
  replacement CAPEX in the finance layer already charges degradation), so
  the cost is never double-counted.
* :func:`rainflow_cycles` / :func:`equivalent_full_cycles` — ASTM-style
  Rainflow counting on the SOC trace gives DoD-weighted equivalent full
  cycles, a more accurate fade than a flat discharge-only cycle count.
  :func:`build_degradation_report` projects the resulting SOH trajectory and
  replacement schedule.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _reversals(series: Any) -> list[float]:
    """Return the turning points (peaks/valleys) of ``series``."""
    x = [float(v) for v in np.asarray(series, dtype=float).tolist()]
    if len(x) < 2:
        return x
    rev = [x[0]]
    for i in range(1, len(x) - 1):
        if (x[i] - x[i - 1]) * (x[i + 1] - x[i]) < 0.0:
            rev.append(x[i])
    rev.append(x[-1])
    return rev


def rainflow_cycles(series: Any) -> list[tuple[float, float]]:
    """ASTM E1049 three-point Rainflow counting.

    Returns a list of ``(range, count)`` pairs where ``count`` is 0.5 for a
    half cycle and 1.0 for a full cycle.
    """
    cycles: list[tuple[float, float]] = []
    stack: list[float] = []
    for value in _reversals(series):
        stack.append(value)
        while len(stack) >= 3:
            x = abs(stack[-1] - stack[-2])
            y = abs(stack[-2] - stack[-3])
            if x < y:
                break
            if len(stack) == 3:
                cycles.append((y, 0.5))
                stack.pop(0)
            else:
                cycles.append((y, 1.0))
                del stack[-3:-1]
    for i in range(len(stack) - 1):
        cycles.append((abs(stack[i + 1] - stack[i]), 0.5))
    return cycles


def equivalent_full_cycles(series: Any, full_amplitude: float) -> float:
    """DoD-weighted equivalent full cycles for an SOC trace.

    ``full_amplitude`` is the usable energy (one full charge-discharge swing,
    e.g. ``capacity_kwh x (soc_max - soc_min)``).
    """
    if full_amplitude <= 0.0:
        return 0.0
    total = sum(rng * count for rng, count in rainflow_cycles(series))
    return float(total / full_amplitude)


def derive_wear_cost_eur_per_mwh(
    replacement_cost_eur: float,
    cycle_life_cycles: float,
    usable_energy_mwh: float,
) -> float:
    """Calibrated wear cost (€ per MWh discharged).

    The replacement cost is amortised over the lifetime throughput
    (cycle-life x usable energy).  Returns 0 when degradation is not
    parameterised so the objective is unchanged by default.
    """
    if cycle_life_cycles <= 0.0 or usable_energy_mwh <= 0.0:
        return 0.0
    return float(replacement_cost_eur / (cycle_life_cycles * usable_energy_mwh))


def build_degradation_report(
    soc_kwh: Any,
    *,
    capacity_kwh: float,
    soc_min_frac: float,
    soc_max_frac: float,
    degradation_pct_per_cycle: float,
    project_years: int,
    start_year: int,
    end_of_life_soh_pct: float = 80.0,
) -> pd.DataFrame:
    """Project the SOH / capacity-fade trajectory and replacement schedule.

    Counts the Year-1 equivalent full cycles from the SOC trace (Rainflow),
    applies ``degradation_pct_per_cycle`` per year, and flags the first year
    SOH falls to ``end_of_life_soh_pct`` (where capacity resets).
    """
    usable_kwh = float(capacity_kwh) * (float(soc_max_frac) - float(soc_min_frac))
    efc_year = equivalent_full_cycles(soc_kwh, usable_kwh)
    rows: list[dict[str, Any]] = []
    cum_fade = 0.0
    for i in range(max(int(project_years), 0)):
        cum_fade += efc_year * float(degradation_pct_per_cycle)
        soh = max(0.0, 100.0 - cum_fade)
        replaced = soh <= float(end_of_life_soh_pct)
        if replaced:
            cum_fade = 0.0
            soh = 100.0
        rows.append({
            "project_year": i + 1,
            "calendar_year": int(start_year) + i,
            "equivalent_full_cycles": round(efc_year, 4),
            "soh_pct": round(soh, 4),
            "capacity_fade_pct": round(100.0 - soh, 4),
            "replacement": bool(replaced),
        })
    return pd.DataFrame(
        rows,
        columns=[
            "project_year", "calendar_year", "equivalent_full_cycles",
            "soh_pct", "capacity_fade_pct", "replacement",
        ],
    )
