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

from .balancing import PRODUCTS_ALL, PRODUCTS_WITH_ACTIVATION

__all__ = [
    "apply_unavailability_derate",
    "availability_factor",
]


def availability_factor(unavailability_pct: float) -> float:
    """Return ``1 - unavailability_pct / 100`` clamped to [0, 1]."""
    raw = float(unavailability_pct or 0.0) / 100.0
    return max(0.0, min(1.0, 1.0 - raw))


# Base set of energy + raw revenue keys that scale linearly with
# availability.  Balancing per-product capacity / activation revenue keys
# and canonical revenue aggregates are added dynamically by
# :func:`_default_derated_keys` so the list stays in sync with the
# balancing product taxonomy.
_BASE_DERATED_KEYS: tuple[str, ...] = (
    # Energy MWh keys
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
    # Raw per-stream EUR keys (the canonical aggregates downstream are
    # algebraic combinations of these).
    "profit_load_from_pv_eur",
    "profit_load_from_bess_eur",
    "profit_export_from_pv_eur",
    "profit_export_from_bess_eur",
    "expense_charge_bess_grid_eur",
    "profit_total_eur",
    # PPA premium (parallel revenue stream) — scales with the export
    # streams it reprices, exactly like the per-stream profit components.
    # project_revenue_total_eur is the sum of profit_total_eur + the PPA
    # premium + balancing, all scaled by the same factor, so derating it
    # directly keeps the identity exact.
    "ppa_premium_total_eur",
    "ppa_premium_pv_eur",
    "ppa_premium_bess_eur",
    "project_revenue_total_eur",
    # Balancing expected-activation energies (kWh) — they scale with the
    # reservation throughput which the derate applies to.
    "bm_expected_activation_energy_up_kwh",
    "bm_expected_activation_energy_dn_kwh",
)


def _default_derated_keys() -> tuple[str, ...]:
    """Assemble the full default derate list including balancing keys."""
    keys: list[str] = list(_BASE_DERATED_KEYS)
    # Per-product capacity revenue (every balancing product).
    for product in PRODUCTS_ALL:
        keys.append(f"bm_{product}_capacity_revenue_eur")
    # Per-product activation revenue (every product that earns activation).
    for product in PRODUCTS_WITH_ACTIVATION:
        keys.append(f"bm_{product}_activation_revenue_eur")
    # Balancing totals.
    keys.extend(
        [
            "bm_total_capacity_revenue_eur",
            "bm_total_activation_revenue_eur",
            "bm_total_balancing_revenue_eur",
        ]
    )
    # Canonical revenue aggregates consumed by the financial pipeline.
    keys.extend(
        [
            "revenue_pv_dam_eur",
            "revenue_bess_dam_eur",
            "revenue_self_consumption_eur",
            "revenue_bess_fcr_eur",
            "revenue_bess_afrr_up_eur",
            "revenue_bess_afrr_dn_eur",
            "revenue_bess_mfrr_up_eur",
            "revenue_bess_mfrr_dn_eur",
            "revenue_ppa_premium_eur",
        ]
    )
    return tuple(keys)


def apply_unavailability_derate(
    kpis: dict[str, float],
    unavailability_pct: float,
    *,
    derated_keys: Iterable[str] | None = None,
) -> dict[str, float]:
    """Return ``kpis`` with every revenue-bearing key scaled by availability.

    Post-condition: every revenue-bearing top-level EUR key is scaled by
    ``availability_factor`` -- this covers the raw per-stream profit
    components (``profit_*_eur``, ``expense_charge_bess_grid_eur``), the
    per-product balancing capacity and activation revenues
    (``bm_<product>_capacity_revenue_eur`` /
    ``bm_<product>_activation_revenue_eur``), the balancing totals
    (``bm_total_capacity_revenue_eur``,
    ``bm_total_activation_revenue_eur``,
    ``bm_total_balancing_revenue_eur``) and the canonical revenue
    aggregates (``revenue_pv_dam_eur``, ``revenue_bess_dam_eur``,
    ``revenue_self_consumption_eur`` and the per-product
    ``revenue_bess_<product>_eur``).  Because every component AND every
    aggregate is multiplied by the same scalar the algebraic identities
    that the KPI builders establish (e.g.
    ``revenue_bess_dam_eur = profit_export_from_bess_eur -
    expense_charge_bess_grid_eur``) are preserved after the derate.

    The nested-dict ``bess_utilization_diagnostics`` (see
    :func:`pvbess_opt.kpis.compute_kpis`) is intentionally NOT derated:
    it reports raw Year-1 dispatch utilisation for the audit log, not
    derated lifetime numbers.  This asymmetry with the surrounding
    top-level MWh keys is by design.

    The factor is also recorded under ``availability_factor`` so the
    downstream cashflow can re-apply it consistently.
    """
    if derated_keys is None:
        derated_keys = _default_derated_keys()
    factor = availability_factor(unavailability_pct)
    out = dict(kpis)
    for key in derated_keys:
        if key in out and isinstance(out[key], (int, float)):
            out[key] = float(out[key]) * factor
    out["availability_factor"] = float(factor)
    out["unavailability_pct"] = float(unavailability_pct)
    return out
