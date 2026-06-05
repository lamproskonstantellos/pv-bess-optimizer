"""Grid emissions and 24/7 carbon-free-energy (CFE) accounting.

Post-solve, dispatch-aware reporting that never feeds back into the MILP or
the NPV — it is purely a diagnostic on the solved dispatch.  Two views:

* **24/7 CFE score** — the time-coincident carbon-free-energy match of the
  load (Google's 24/7 CFE convention).  For every step the carbon-free
  supply serving the load is ``pv_to_load`` plus the share of battery
  discharge that originated from PV (``bess_dis_load_green``); battery
  energy charged from the grid is *not* counted as carbon-free.  The annual
  score is the energy-weighted ratio of that carbon-free supply to the load,
  i.e. a granular hour-by-hour match rather than a loose annual volumetric
  one.
* **Emissions** — residual emissions from grid imports and avoided
  emissions from the carbon-free energy the project delivers (self-consumed
  plus exported), valued at the grid carbon intensity.  The intensity can be
  a flat scalar or a per-step series (a ``grid_co2_kg_per_mwh`` column on the
  dispatch frame) for a time-varying grid; an optional annual decline models
  a decarbonising grid over the project life.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# Columns the report reads off the solved dispatch frame.  The green-energy
# columns are attached unconditionally by :func:`pvbess_opt.kpis.compute_kpis`
# (via ``attribute_green_discharge``), so they are always present here.
_LOAD = "load_kwh"
_PV_TO_LOAD = "pv_to_load_kwh"
_PV_TO_GRID = "pv_to_grid_kwh"
_GRID_TO_LOAD = "grid_to_load_kwh"
_GRID_TO_BESS = "bess_charge_grid_kwh"
_BESS_DIS_LOAD_GREEN = "bess_dis_load_green_kwh"
_BESS_DIS_GRID_GREEN = "bess_dis_grid_green_kwh"
_GRID_CI_COLUMN = "grid_co2_kg_per_mwh"


def _column(res: pd.DataFrame, name: str) -> np.ndarray:
    """Return a column as a float array, or zeros when it is absent."""
    if name in res.columns:
        return res[name].to_numpy(dtype=float)
    return np.zeros(len(res), dtype=float)


def grid_ci_series(res: pd.DataFrame, scalar_ci_kg_per_mwh: float) -> np.ndarray:
    """Per-step grid carbon intensity (kg/MWh).

    A per-step ``grid_co2_kg_per_mwh`` column on the dispatch frame wins over
    the flat ``scalar_ci_kg_per_mwh`` so a time-varying grid can be modelled.
    """
    if _GRID_CI_COLUMN in res.columns:
        return res[_GRID_CI_COLUMN].to_numpy(dtype=float)
    return np.full(len(res), float(scalar_ci_kg_per_mwh), dtype=float)


def carbon_free_to_load_kwh(res: pd.DataFrame) -> np.ndarray:
    """Per-step carbon-free supply serving the load (kWh).

    PV consumed directly plus the PV-sourced ("green") share of battery
    discharge that serves the load.  Grid-charged battery energy is excluded.
    """
    cf: np.ndarray = _column(res, _PV_TO_LOAD) + _column(res, _BESS_DIS_LOAD_GREEN)
    return cf


def cfe_score(res: pd.DataFrame) -> float:
    """Annual 24/7 carbon-free-energy score (%) — time-coincident match.

    ``sum(carbon_free_to_load) / sum(load)`` over every step.  Returns NaN
    when there is no load to match (e.g. a merchant run).
    """
    load = _column(res, _LOAD)
    total_load = float(load.sum())
    if total_load <= 0.0:
        return float("nan")
    return float(carbon_free_to_load_kwh(res).sum() / total_load * 100.0)


def hourly_cfe_fraction(res: pd.DataFrame) -> np.ndarray:
    """Per-step carbon-free fraction of the load, in [0, 1].

    Steps with no load are dropped (the fraction is undefined there); the
    result feeds the carbon-free-energy duration curve.
    """
    load = _column(res, _LOAD)
    cf = carbon_free_to_load_kwh(res)
    mask = load > 0.0
    frac = np.zeros(int(mask.sum()), dtype=float)
    if mask.any():
        frac = np.clip(cf[mask] / load[mask], 0.0, 1.0)
    return frac


def build_emissions_report(
    res: pd.DataFrame,
    *,
    grid_ci_kg_per_mwh: float,
    project_years: int,
    start_year: int,
    grid_ci_annual_decline_pct: float = 0.0,
) -> pd.DataFrame:
    """Project the annual emissions / 24/7 CFE report from the dispatch.

    The Year-1 dispatch is held constant across the project life (matching
    the degradation report's convention); only the grid carbon intensity is
    scaled, declining by ``grid_ci_annual_decline_pct`` per year to model a
    decarbonising grid.  Energy figures are MWh, emissions are tonnes CO2e.
    """
    ci = grid_ci_series(res, grid_ci_kg_per_mwh)

    cf_to_load_kwh = carbon_free_to_load_kwh(res)
    grid_to_load_kwh = _column(res, _GRID_TO_LOAD)
    grid_charge_kwh = _column(res, _GRID_TO_BESS)
    clean_exported_kwh = _column(res, _PV_TO_GRID) + _column(res, _BESS_DIS_GRID_GREEN)
    clean_delivered_kwh = cf_to_load_kwh + clean_exported_kwh
    grid_import_kwh = grid_to_load_kwh + grid_charge_kwh

    # kWh x (kg/MWh) -> kg, then /1000 -> tonnes; MWh = kWh / 1000.
    def _tonnes(energy_kwh: np.ndarray) -> float:
        return float((energy_kwh * ci).sum() / 1.0e6)

    def _mwh(energy_kwh: np.ndarray) -> float:
        return float(energy_kwh.sum() / 1000.0)

    avoided_t = _tonnes(clean_delivered_kwh)
    induced_t = _tonnes(grid_charge_kwh)
    residual_t = _tonnes(grid_to_load_kwh)
    grid_import_t = _tonnes(grid_import_kwh)

    load_mwh = _mwh(_column(res, _LOAD))
    cf_to_load_mwh = _mwh(cf_to_load_kwh)
    grid_import_mwh = _mwh(grid_import_kwh)
    clean_delivered_mwh = _mwh(clean_delivered_kwh)
    score_pct = cfe_score(res)
    # Carbon-free-energy-weighted mean intensity, for the per-year echo.
    weight = float(clean_delivered_kwh.sum())
    mean_ci = float((ci * clean_delivered_kwh).sum() / weight) if weight > 0.0 else float(ci.mean())

    decline = float(grid_ci_annual_decline_pct) / 100.0
    rows: list[dict[str, Any]] = []
    for i in range(max(int(project_years), 0)):
        factor = (1.0 - decline) ** i
        rows.append({
            "project_year": i + 1,
            "calendar_year": int(start_year) + i,
            "cfe_score_pct": round(score_pct, 4),
            "load_mwh": round(load_mwh, 4),
            "carbon_free_to_load_mwh": round(cf_to_load_mwh, 4),
            "grid_import_mwh": round(grid_import_mwh, 4),
            "clean_delivered_mwh": round(clean_delivered_mwh, 4),
            "grid_ci_kg_per_mwh": round(mean_ci * factor, 4),
            "avoided_emissions_t": round(avoided_t * factor, 4),
            "induced_emissions_t": round(induced_t * factor, 4),
            "net_avoided_emissions_t": round((avoided_t - induced_t) * factor, 4),
            "residual_load_emissions_t": round(residual_t * factor, 4),
            "grid_import_emissions_t": round(grid_import_t * factor, 4),
        })
    return pd.DataFrame(
        rows,
        columns=[
            "project_year", "calendar_year", "cfe_score_pct",
            "load_mwh", "carbon_free_to_load_mwh", "grid_import_mwh",
            "clean_delivered_mwh", "grid_ci_kg_per_mwh",
            "avoided_emissions_t", "induced_emissions_t",
            "net_avoided_emissions_t", "residual_load_emissions_t",
            "grid_import_emissions_t",
        ],
    )
