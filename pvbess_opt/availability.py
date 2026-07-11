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
    "apply_curtailment_derate",
    "apply_operating_derates",
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
    # Export split by origin (route-to-market fee bases) — derate with the
    # total they compose.
    "pv_export_mwh",
    "bess_export_mwh",
    # Fee-exempt covered export under the PPA negative-price suspension
    # clause (Eqs. P6/P7): a metered-export volume that scales linearly
    # with availability exactly like pv_export_mwh, whose fee role it
    # refines.  Absent unless the clause is on (missing keys are
    # skipped).
    "ppa_fee_exempt_export_mwh",
    # Charging-side grid fee (Eq. E26): DERATE — proportional to the
    # grid-charged throughput (bess_charge_grid_mwh), which scales with
    # availability like every dispatch-energy key; during downtime the
    # BESS neither charges nor pays the wedge.
    "expense_grid_charging_fee_eur",
    # Imbalance settlement (Eqs. U6-U9): DERATE — deviation volume
    # scales with operating throughput exactly like the revenues it
    # corrects.  Known limitation (documented): real forced outages
    # INCREASE imbalance; the uniform factor keeps the hedge value
    # derate-invariant since it cancels in the paired difference.
    "imbalance_cost_eur",
    "imbalance_cost_pv_only_eur",
    "bess_imbalance_hedge_value_eur",
    "imbalance_short_mwh",
    "imbalance_long_mwh",
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
    # Baseload PPA (Eqs. P9/E45): the contract's fixed-volume leg is
    # PRODUCTION-DECOUPLED — the offtaker settles Q_t regardless of
    # plant availability — so the two PPA EUR keys must NOT derate for
    # that structure.  The structure is detected from the P10
    # diagnostic KPI, which exists if and only if the baseload branch
    # wrote the columns (the bit-identity contract makes it a reliable
    # marker), so no call site has to thread the ppa config through.
    # The shortfall/excess diagnostics themselves stay RAW everywhere
    # (shortfall RISES with unavailability; the exact correction needs
    # per-step recomputation — the bess_utilization_diagnostics
    # precedent).
    if "ppa_baseload_shortfall_mwh" in kpis:
        derated_keys = tuple(
            k for k in derated_keys
            if k not in ("revenue_pv_ppa_eur", "ppa_covered_dam_value_eur")
        )
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
    # matching the previous behaviour.  See ``docs/economics_design.md`` (E8a).
    _load = out.get("load_energy_mwh")
    if _load is not None and "system_total_import_mwh" in out:
        out["system_total_import_mwh"] = (
            float(out["system_total_import_mwh"]) + (1.0 - factor) * float(_load)
        )
    out["availability_factor"] = float(factor)
    out["unavailability_pct"] = float(unavailability_pct)
    return out


# Export-side keys scaled by the exogenous-curtailment quota (Eq. E48).
# The quota models system-operator curtailment of grid INJECTION, so
# only metered-export volumes and the EUR streams they earn scale;
# per-key reasoning for what stays untouched:
#
# * load-side flows / self-consumption savings — behind the meter, the
#   operator curtails injection, not on-site consumption;
# * grid import, expense_charge_bess_grid_eur,
#   expense_grid_charging_fee_eur — withdrawals, not injections;
# * balancing bm_* keys — reservations are committed CAPACITY, not
#   scheduled injection (activation settles through the TSO);
# * pv_generation_mwh / bess_total_discharge_mwh — the plant still
#   generates for load and charging in curtailed hours (quota mode is
#   a post-solve convention; the signal mode re-dispatches instead);
# * imbalance keys — deviations settle on the nominated schedule, and
#   the quota is not a forecast error.
_CURTAILMENT_DERATED_KEYS: tuple[str, ...] = (
    "system_total_export_mwh",
    "pv_export_mwh",
    "bess_export_mwh",
    # Fee-exempt covered export (Eqs. P6/P7): a metered-export volume —
    # falls with the export it refines.
    "ppa_fee_exempt_export_mwh",
    "profit_export_from_pv_eur",
    "profit_export_from_bess_eur",
    "revenue_pv_dam_eur",
    "revenue_bess_dam_eur",
    # Pay-as-produced PPA settles on metered export, so the generator
    # bears curtailment (documented assumption); the baseload fixed
    # leg is exempted below via the P10 marker.
    "revenue_pv_ppa_eur",
    "ppa_covered_dam_value_eur",
)

# The scaled keys that are algebraic components of profit_total_eur
# (kpis.compute_kpis): the headline profit is recomposed from their
# deltas plus the compensation line.
_CURTAILMENT_PROFIT_COMPONENTS: tuple[str, ...] = (
    "profit_export_from_pv_eur",
    "profit_export_from_bess_eur",
    "revenue_pv_ppa_eur",
)


def apply_curtailment_derate(
    kpis: dict[str, float],
    curtailment_pct: float,
    *,
    compensated_pct: float = 0.0,
    compensation_price_eur_per_mwh: float = 0.0,
) -> dict[str, float]:
    """Scale export-side KPIs by the exogenous-curtailment quota (E48).

    Mirrors :func:`apply_unavailability_derate` and runs AFTER it, so
    the combined scaling is ``availability x curtailment`` — two
    multiplicative factors whose order cannot matter.  Every key in
    :data:`_CURTAILMENT_DERATED_KEYS` scales by ``1 - q``; the
    compensated share of the curtailed export volume earns the
    administered price (Eq. E49, on the availability-derated Year-1
    export base BEFORE the quota scaling)::

        R_curt = q x E_export x c x p_comp

    recorded under ``curtailment_compensation_eur``;
    ``profit_total_eur`` is recomposed from the deltas of its scaled
    components plus the compensation.  With ``curtailment_pct = 0``
    the dict is returned unchanged (no new keys — bit-identity).
    Under the baseload PPA structure the contract's fixed-volume leg
    is exempt exactly as in the availability derate (the P10 marker).
    """
    q = max(0.0, min(1.0, float(curtailment_pct or 0.0) / 100.0))
    out = dict(kpis)
    if q <= 0.0:
        return out
    keys: tuple[str, ...] = _CURTAILMENT_DERATED_KEYS
    if "ppa_baseload_shortfall_mwh" in out:
        keys = tuple(
            k for k in keys
            if k not in ("revenue_pv_ppa_eur", "ppa_covered_dam_value_eur")
        )
    factor = 1.0 - q
    curtailed_export_mwh = q * float(
        out.get("system_total_export_mwh", 0.0) or 0.0
    )
    profit_delta = 0.0
    for key in keys:
        if key in out and isinstance(out[key], (int, float)):
            old = float(out[key])
            out[key] = old * factor
            if key in _CURTAILMENT_PROFIT_COMPONENTS:
                profit_delta += out[key] - old
    compensation = (
        curtailed_export_mwh
        * max(0.0, min(1.0, float(compensated_pct or 0.0) / 100.0))
        * max(0.0, float(compensation_price_eur_per_mwh or 0.0))
    )
    out["curtailment_compensation_eur"] = float(compensation)
    if "profit_total_eur" in out:
        out["profit_total_eur"] = (
            float(out["profit_total_eur"]) + profit_delta + compensation
        )
    out["curtailment_factor"] = float(factor)
    out["curtailment_pct"] = float(q * 100.0)
    return out


def apply_operating_derates(
    kpis: dict[str, float], params: dict[str, float],
) -> dict[str, float]:
    """Availability then curtailment, both read from ``params``.

    The single post-solve derate entry point every caller uses, so the
    two factors can never be applied in different combinations across
    the pipeline, the scenario batch, the sizing sweep and the
    rolling-horizon seeds.  Both features off (the defaults) returns
    the availability behaviour bit-identically.
    """
    out = apply_unavailability_derate(
        kpis, float(params.get("unavailability_pct", 0.0) or 0.0),
    )
    return apply_curtailment_derate(
        out,
        float(params.get("curtailment_pct", 0.0) or 0.0),
        compensated_pct=float(
            params.get("curtailment_compensated_pct", 0.0) or 0.0
        ),
        compensation_price_eur_per_mwh=float(
            params.get(
                "curtailment_compensation_price_eur_per_mwh", 0.0,
            ) or 0.0
        ),
    )
