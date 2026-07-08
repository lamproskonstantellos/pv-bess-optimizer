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

Generation, storage, export and revenue all scale down by the
availability factor.  Grid import is the sole exception: it scales *up*,
because the load is fixed exogenous demand that the grid must cover in
full while the plant is offline (a PV+BESS site whose plant trips simply
draws the shortfall from the grid).  Import is therefore set to
``factor * import_raw + (1 - factor) * load`` so the derated annual
energy balance closes against the never-derated load -- and so the
annual energy Sankey (which applies the same rule) balances with the
real demand rather than a shrunk one.

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
    # ``system_total_import_mwh`` is scaled by ``factor`` here like the rest,
    # then corrected in ``apply_unavailability_derate`` to add the downtime
    # load the grid must cover (it RISES with unavailability, unlike the
    # generation-side keys around it).
    "system_total_import_mwh",
    "bess_total_charge_mwh",
    "pv_to_bess_mwh",
    "bess_charge_grid_mwh",
    "pv_direct_to_load_mwh",
    "bess_to_load_mwh",
    "bess_green_to_load_mwh",
    "system_green_to_load_mwh",
    "pv_energy_curtailed_mwh",
    # Equivalent-full-cycle KPIs scale linearly with discharge, so they
    # derate together with bess_total_discharge_mwh: headline cycles
    # reconcile with bess_lifetime_cycles / years (both derated).  The
    # nested bess_utilization_diagnostics dict deliberately stays raw.
    "bess_equivalent_cycles_total",
    "bess_equivalent_cycles_per_day",
    # Raw per-stream EUR keys (the canonical aggregates downstream are
    # algebraic combinations of these).
    "profit_load_from_pv_eur",
    "profit_load_from_bess_eur",
    "profit_export_from_pv_eur",
    "profit_export_from_bess_eur",
    "expense_charge_bess_grid_eur",
    # PPA contract leg + the covered volume's counterfactual DAM value
    # (both PV-origin EUR streams; scale with availability like the
    # market revenue they replace / shadow).
    "revenue_pv_ppa_eur",
    "ppa_covered_dam_value_eur",
    "profit_total_eur",
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

    ``system_total_import_mwh`` is the one energy key that is NOT scaled
    linearly: grid import RISES with unavailability because during plant
    downtime the grid must serve the load the offline plant cannot.  It
    is set to ``factor * import_raw + (1 - factor) * load`` so the derated
    energy balance closes against the (never-derated) exogenous load.
    Grid import is not a monetised stream, so this leaves every financial
    KPI unchanged.  When no ``load_energy_mwh`` key is present it falls
    back to the uniform derate.

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
    # Grid import is the one energy flow that must NOT simply scale down with
    # availability.  During plant downtime the grid covers the full load the
    # offline plant would otherwise have served, so annual import RISES rather
    # than falls.  The uniform ``factor`` step above left it at
    # ``factor * import_raw`` (the grid-charging leg, which genuinely stops
    # when the plant is down, is correctly captured by that step); here we add
    # back the ``(1 - factor) * load`` the grid imports to serve the load while
    # the plant is out.  Net: ``import = factor * import_raw + u * load``
    # (u = 1 - factor).  The load is exogenous demand and is never derated, so
    # the derated energy balance closes exactly against it.  Revenue is
    # unaffected -- grid import is not a monetised stream; the self-consumption
    # savings (which ARE derated) already carry the downtime cost.  Absent a
    # ``load_energy_mwh`` key (a merchant run has no co-located load, and
    # partial KPI dicts may omit it) the import stays uniformly derated,
    # matching the previous behaviour.  See ``docs/economics_design.md`` (E9).
    _load = out.get("load_energy_mwh")
    if _load is not None and "system_total_import_mwh" in out:
        out["system_total_import_mwh"] = (
            float(out["system_total_import_mwh"]) + (1.0 - factor) * float(_load)
        )
    out["availability_factor"] = float(factor)
    out["unavailability_pct"] = float(unavailability_pct)
    return out
