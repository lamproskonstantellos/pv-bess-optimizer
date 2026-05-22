"""Annual unavailability derate helpers.

The optimizer assumes the asset is online for every step of the year.
In practice utility-scale PV+BESS plants spend a small fraction of the
year offline due to unscheduled outages and scheduled maintenance.

Two implementation choices are possible:

1. Multiply per-timestep PV / BESS-discharge variables by
   ``(1 - unavailability_pct/100)`` *inside* the MILP.
2. Apply the same factor as a *post-solve* derate on the headline
   revenue / generation / discharge totals (and on the lifetime
   aggregates that flow into NPV / IRR / LCOE / LCOS).

Option (2) is cleaner: it keeps the MILP clean, requires only a single
multiplication on a handful of KPIs and cashflow columns, and makes
the derate trivially auditable downstream.  The case is a one-line
substitution in :func:`apply_unavailability_derate`.

The canonical default is ``unavailability_pct = 1.0`` (= 99 % yearly
availability), in line with utility-scale industry benchmarks (NREL
ATB 2024 reports ~99 % availability for fixed-tilt PV).
"""

from __future__ import annotations

from collections.abc import Iterable


def availability_factor(unavailability_pct: float) -> float:
    """Return ``1 - unavailability_pct / 100`` clamped to [0, 1]."""
    raw = float(unavailability_pct or 0.0) / 100.0
    return max(0.0, min(1.0, 1.0 - raw))


def apply_unavailability_derate(
    kpis: dict[str, float],
    unavailability_pct: float,
    *,
    derated_keys: Iterable[str] = (
        "pv_generation_mwh",
        "bess_total_discharge_mwh",
        "system_total_export_mwh",
        "system_total_import_mwh",
        "bess_total_charge_mwh",
        "pv_to_bess_mwh",
        "bess_charge_grid_mwh",
        "pv_direct_to_load_mwh",
        "bess_to_load_mwh",
        "bess_green_to_load_mwh",
        "system_green_to_load_mwh",
        "pv_energy_curtailed_mwh",
        "profit_load_from_pv_eur",
        "profit_load_from_bess_eur",
        "profit_export_from_pv_eur",
        "profit_export_from_bess_eur",
        "expense_charge_bess_grid_eur",
        "profit_total_eur",
    ),
) -> dict[str, float]:
    """Return ``kpis`` with selected MWh/EUR keys derated by availability.

    The factor is also recorded under ``availability_factor`` so the
    downstream cashflow can re-apply it consistently.
    """
    factor = availability_factor(unavailability_pct)
    out = dict(kpis)
    for key in derated_keys:
        if key in out and isinstance(out[key], (int, float)):
            out[key] = float(out[key]) * factor
    out["availability_factor"] = float(factor)
    out["unavailability_pct"] = float(unavailability_pct)
    return out
