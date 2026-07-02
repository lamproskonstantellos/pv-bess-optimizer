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
  cycles, a more accurate cycle count than a flat discharge-only tally.
  :func:`build_degradation_report` projects the SOH / capacity-fade
  trajectory and replacement schedule using the same calendar-plus-cycle
  fade model as the finance layer (:func:`pvbess_opt.lifetime._bess_factor`)
  so the plotted SOH agrees with the dispatch / NPV; the Rainflow count is
  carried alongside as a diagnostic column.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .lifetime import bess_capacity_factors


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
    degradation_annual_pct: float = 0.0,
    year1_discharge_mwh: float | None = None,
    end_of_life_soh_pct: float = 80.0,
    replacement_year: int = 0,
) -> pd.DataFrame:
    """Project the SOH / capacity-fade trajectory and replacement schedule.

    The state-of-health curve is the **same capacity-fade model the finance
    layer uses** (:func:`pvbess_opt.lifetime._bess_factor`): a multiplicative
    calendar fade minus an additive cycle fade, so the plotted SOH agrees
    with the ``bess_factor`` that scales dispatch / revenue and with the
    ``bess_total_fade_pct_y_final`` KPI::

        soh = (1 - degradation_annual_pct/100) ** years_since_install
              - (degradation_pct_per_cycle/100) * cumulative_full_cycles

    ``cumulative_full_cycles`` accrues the degraded annual discharge
    throughput (``year1_discharge_mwh`` scaled by the running capacity
    factor) over nameplate energy, matching
    :func:`pvbess_opt.economics.build_yearly_cashflow`.  When the discharge
    throughput is not supplied it falls back to the Rainflow throughput so
    the cycle term is still populated.  The DoD-weighted Rainflow
    ``equivalent_full_cycles`` from the SOC trace is reported as a separate
    diagnostic column; it does not drive the SOH curve.

    The capacity resets to a fresh 100 % pack on a replacement, governed by:

    * **A scheduled replacement** -- when ``replacement_year > 0`` the curve
      resets in that project year and degrades fresh from there, exactly as
      ``_bess_factor`` resets the calendar fade at ``bess_replacement_year``
      (the cashflow charges the replacement CAPEX in the same year), so the
      SOH plot stays consistent regardless of how lightly the battery cycles.
    * **End of life** -- when no replacement year is configured
      (``replacement_year <= 0``) the pack is instead swapped the first year
      SOH falls to ``end_of_life_soh_pct``.
    """
    usable_kwh = float(capacity_kwh) * (float(soc_max_frac) - float(soc_min_frac))
    efc_year = equivalent_full_cycles(soc_kwh, usable_kwh)
    d_annual = float(degradation_annual_pct) / 100.0
    d_cycle = float(degradation_pct_per_cycle) / 100.0
    scheduled_year = int(replacement_year or 0)
    capacity_mwh = float(capacity_kwh) / 1000.0
    # Throughput driving the cycle-fade term.  Prefer the dispatch discharge
    # (keeps the curve identical to the finance layer's bess_factor); fall
    # back to the Rainflow throughput when called without it.
    if year1_discharge_mwh is None:
        throughput_mwh = efc_year * usable_kwh / 1000.0
    else:
        throughput_mwh = float(year1_discharge_mwh)

    rows: list[dict[str, Any]] = []
    if scheduled_year > 0:
        # Scheduled replacement: the SOH curve is exactly the shared
        # capacity-factor sequence the finance and lifetime layers use
        # (single source of truth: lifetime.bess_capacity_factors).
        factors = bess_capacity_factors(
            max(int(project_years), 0),
            d_bess_annual=d_annual,
            d_bess_per_cycle=d_cycle,
            year1_discharge_mwh=throughput_mwh,
            capacity_mwh=capacity_mwh,
            replacement_year=scheduled_year,
        )
        for i, factor in enumerate(factors):
            year = i + 1
            soh = factor * 100.0
            rows.append({
                "project_year": year,
                "calendar_year": int(start_year) + i,
                "equivalent_full_cycles": round(efc_year, 4),
                "soh_pct": round(soh, 4),
                "capacity_fade_pct": round(100.0 - soh, 4),
                "replacement": bool(year == scheduled_year),
            })
    else:
        install_year = 1        # project year the in-service pack was installed
        cumulative_cycles = 0.0  # full-equivalent cycles accrued since install
        for i in range(max(int(project_years), 0)):
            year = i + 1
            # Capacity fade = multiplicative calendar fade minus additive
            # cycle fade -- same model as lifetime.bess_capacity_factors,
            # with the pack swapped the first year SOH reaches the
            # end-of-life threshold (so that year already shows 100 %).
            calendar = (1.0 - d_annual) ** (year - install_year)
            factor = max(0.0, calendar - d_cycle * cumulative_cycles)
            soh = factor * 100.0
            if soh <= float(end_of_life_soh_pct):
                install_year = year
                cumulative_cycles = 0.0
                factor = 1.0
                soh = 100.0
                replaced = True
            else:
                replaced = False
            # Accrue this year's degraded throughput for next year's cycle
            # term: cumulative += discharge * factor / capacity.
            if capacity_mwh > 1e-12:
                cumulative_cycles += throughput_mwh * factor / capacity_mwh
            rows.append({
                "project_year": year,
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
