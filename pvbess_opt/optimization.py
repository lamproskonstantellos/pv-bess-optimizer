"""Pyomo MILP for PV + BESS dispatch.

Two regulatory regimes are supported via the ``mode`` parameter:

* ``self_consumption`` — Greek Self-consumption.  Strictly enforced rules:
  load balance, hard PV→load priority (Section 2 of the spec), no
  simultaneous grid I/O (tight big-M), retail tariff for self-
  consumption, DAM for export.  A binary-free slack additionally
  enforces surplus-only export (Section 5).
* ``merchant`` — pure utility-scale dispatch with **no co-located load**.
  Load balance NOT enforced; load priority NOT enforced; the
  ``pv_to_load`` / ``bess_dis_load`` / ``grid_to_load`` flows are pinned
  to zero.  The static curtailment cap STILL applies (regulatory
  grid-connection limit per MD YPEN/DAPEEK/53563/1556/2023).

The single objective is **profit** maximisation.  When the user's
retail tariff exceeds the DAM price in the majority of hours (the
typical case for self_consumption projects with a co-located load), the profit
objective produces the same dispatch as a "green" objective —
self-consumption emerges from economics rather than being a hard
constraint.  The hard ``LOAD_PV_PRIORITY`` constraint still pins
``pv_to_load[t] == min(pv[t], load[t])`` exactly so the dispatch
is correct for any retail / DAM ratio the user supplies.  In merchant
mode there is no load to "be green about".

Tight big-M values
------------------

Big-Ms are derived per-instance using the symmetric ``bess_power_kw``
limit (the asymmetric p_charge_max / p_dis_max pair is not supported):

* ``M_imp = (load_max + bess_power_kw × dt_h) × 1.001``
* ``M_exp = p_grid_export_max × dt_h × max_injection_frac × 1.001``
  (gates the no-simultaneous grid-export binary only; the per-step
  injection cap is a direct ``<=`` to a constant and needs no big-M)
* ``M_charge = bess_power_kw × dt_h × 1.001`` (only when grid-charging)
* ``M_pv = max(pv_kwh) × 1.001`` (only when grid-charging)

Audit invariants
----------------

After every solve :func:`verify_dispatch_invariants` checks the nine
mandatory invariants.  Residuals are returned and logged at INFO; the
``--strict`` CLI flag turns violations into errors.

Module-level tuning constants
-----------------------------

The two weight terms below are tie-breakers / debug levers, not
project knobs.  They are intentionally NOT exposed in the workbook —
There is no ``# optimization`` group.  Solver
``mip_gap`` and ``time_limit`` are exposed via the CLI flags
``--mip-gap`` / ``--time-limit``; these two weights stay private to
keep the user-facing surface small.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal, overload

import numpy as np
import pandas as pd
import pyomo.environ as pyo
from pyomo.opt import SolverStatus, TerminationCondition

from .balancing import (
    PRODUCTS_ALL,
    PRODUCTS_DN,
    PRODUCTS_SYMMETRIC,
    PRODUCTS_UP,
    PRODUCTS_WITH_ACTIVATION,
    BalancingConfig,
    BalancingTimeseries,
    acceptance_probability,
    activation_probability,
    capacity_share_kw,
    resolve_balancing_config,
    resolve_balancing_timeseries,
)
from .constants import DEFAULT_MAX_INJECTION_PCT_HOURLY
from .kpis import ENERGY_TOLERANCE, _balancing_soc_drift
from .max_injection import build_per_step_max_injection_frac
from .modes import resolve_mode
from .timeutils import dt_hours_from

logger = logging.getLogger(__name__)

__all__ = [
    "build_model",
    "choose_solver",
    "configure_solver_options",
    "derive_tight_big_m",
    "model_to_dataframe",
    "run_scenario",
    "solve_model",
    "verify_dispatch_invariants",
]


# Tiny tie-breaker on ``pv_curtail`` for determinism under degeneracy.
# Set to 0.0 to disable.  NOT a constraint substitute.
_WEIGHT_CURTAIL_TIEBREAK_EUR_PER_KWH: float = 1.0e-5

# Battery wear cost (cycle degradation) is a per-MWh-throughput penalty
# read from params['bess_wear_cost_eur_per_mwh'] (default 0 = off) and
# subtracted in the objective; see pvbess_opt.degradation for the
# calibration helper.


# ---------------------------------------------------------------------------
# Solver helpers
# ---------------------------------------------------------------------------

_SOLVER_OPTIONS: dict[str, dict[str, str]] = {
    "gurobi": {
        "mip_gap": "MIPGap",
        "time_limit": "TimeLimit",
        "mip_focus": "MIPFocus",
        "threads": "Threads",
    },
    "highs": {
        "mip_gap": "mip_rel_gap",
        "time_limit": "time_limit",
        "threads": "threads",
    },
    "appsi_highs": {
        "mip_gap": "mip_rel_gap",
        "time_limit": "time_limit",
        "threads": "threads",
    },
    "cbc": {
        "mip_gap": "ratio",
        "time_limit": "sec",
        "threads": "threads",
    },
    "glpk": {
        "mip_gap": "mipgap",
        "time_limit": "tmlim",
    },
}


def choose_solver(name: str | None):
    """Return the first available Pyomo solver among (`name`, highs, cbc)."""
    name = (name or "highs").lower()
    candidates = [name, "highs", "cbc"]
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            solver = pyo.SolverFactory(candidate)
            if solver is not None and solver.available():
                return solver, candidate
        except (RuntimeError, ImportError, OSError) as exc:
            logger.debug("solver %s unavailable: %s", candidate, exc)
            continue
    raise RuntimeError("No LP/MIP solver found (install gurobi, highs, or cbc).")


def configure_solver_options(
    solver,
    solver_name: str,
    *,
    mip_gap: float = 0.001,
    time_limit_seconds: int = 1800,
    mip_focus: int | None = 2,
    threads: int = 0,
) -> None:
    """Set solver options using the names accepted by ``solver_name``."""
    mapping = _SOLVER_OPTIONS.get(solver_name.lower(), _SOLVER_OPTIONS["gurobi"])
    if "mip_gap" in mapping:
        solver.options[mapping["mip_gap"]] = mip_gap
    if "time_limit" in mapping:
        solver.options[mapping["time_limit"]] = time_limit_seconds
    if mip_focus is not None and "mip_focus" in mapping:
        solver.options[mapping["mip_focus"]] = mip_focus
    if "threads" in mapping:
        solver.options[mapping["threads"]] = threads


def _has_feasible_incumbent(model: pyo.ConcreteModel | None) -> bool:
    """Return True when the model carries a loaded (feasible) solution.

    Probes the SOC variable specifically — every active scenario in this
    codebase declares ``model.soc`` (the SOC trajectory) and the SOC
    variable is always loaded from the solver's incumbent when a
    feasible solution exists.  An unloaded model returns ``None`` for
    ``var.value``, which we treat as "no incumbent".  Probing a named
    variable instead of "first var encountered via
    ``component_data_objects``" makes the check robust to refactors that
    change the declaration order (Pass-2 P2.8).
    """
    if model is None:
        return False
    soc = getattr(model, "soc", None)
    try:
        if soc is not None:
            for v in soc.values():
                return v.value is not None
        # Fallback for tests that mock a barebones model with no .soc:
        # fall through to the first SOC-like variable, then to any var.
        for v in model.component_data_objects(pyo.Var, active=True):
            name = v.name or ""
            if name.startswith("soc"):
                return v.value is not None
        for v in model.component_data_objects(pyo.Var, active=True):
            return v.value is not None
    except (AttributeError, ValueError):
        return False
    return False


def _check_solver_status(result, solver_name: str, model=None) -> None:
    """Raise ``RuntimeError`` if the solver did not return an acceptable solution."""
    status = result.solver.status
    condition = result.solver.termination_condition
    optimality_conditions = {
        TerminationCondition.optimal,
        TerminationCondition.feasible,
        TerminationCondition.locallyOptimal,
        TerminationCondition.globallyOptimal,
    }
    soft_limit_conditions = {
        TerminationCondition.maxTimeLimit,
        TerminationCondition.maxIterations,
    }
    if status == SolverStatus.ok and condition in optimality_conditions:
        return
    if condition in soft_limit_conditions:
        # A soft limit is only acceptable if the solver actually loaded a
        # feasible incumbent; otherwise downstream pyo.value(...) calls
        # would surface the failure opaquely.
        if _has_feasible_incumbent(model):
            return
        raise RuntimeError(
            f"Solver '{solver_name}' hit {condition} with no feasible "
            "incumbent — increase --time-limit or relax --mip-gap."
        )
    raise RuntimeError(
        f"Solver '{solver_name}' did not produce an acceptable solution: "
        f"status={status}, termination_condition={condition}."
    )


# ---------------------------------------------------------------------------
# Tight big-M derivation
# ---------------------------------------------------------------------------


def _resolve_max_injection_per_step(
    params: dict[str, Any], ts: pd.DataFrame,
) -> np.ndarray:
    """Return a per-step max-injection fraction array aligned with ``ts``.

    The loader populates ``params["max_injection_profile"]`` with a
    (24,) or (24, 12) array of max-injection percentages (default flat
    profile when the workbook omits the sheet).  This resolver expands
    it to a per-timestep fraction in [0, 1].  When no profile is
    supplied the fallback is the canonical
    :data:`~pvbess_opt.constants.DEFAULT_MAX_INJECTION_PCT_HOURLY` (as a
    fraction) — matching the loader's default — rather than an
    inconsistent no-cap 1.0.
    """
    profile = params.get("max_injection_profile")
    if profile is not None and "timestamp" in ts.columns:
        return build_per_step_max_injection_frac(ts["timestamp"], profile)
    return np.full(
        len(ts), DEFAULT_MAX_INJECTION_PCT_HOURLY / 100.0, dtype=float,
    )


def _resolve_optional_max_injection_per_step(
    params: dict[str, Any], ts: pd.DataFrame, key: str,
) -> np.ndarray | None:
    """Per-step fraction for an optional per-source max-injection profile.

    Returns ``None`` when the profile is absent — no sub-cap for that source,
    only the combined cap binds.  Unlike the combined resolver there is no
    default profile: omission means an unconstrained sub-cap.
    """
    profile = params.get(key)
    if profile is None or "timestamp" not in ts.columns:
        return None
    return build_per_step_max_injection_frac(ts["timestamp"], profile)


# ---------------------------------------------------------------------------
# Balancing-market resolvers (shared between build_model and model_to_dataframe)
# ---------------------------------------------------------------------------


def _resolve_balancing_inputs(
    params: dict[str, Any],
    ts: pd.DataFrame,
    *,
    n_steps: int,
    bess_present: bool,
) -> tuple[BalancingConfig, BalancingTimeseries | None, bool]:
    """Return the parsed config, per-step prices, and an active flag.

    ``active`` is True only when ``balancing_enabled`` is set AND the
    project carries a BESS — there is nothing to reserve otherwise.
    """
    raw_cfg = params.get("balancing") or {}
    cfg = resolve_balancing_config(raw_cfg)
    if not cfg.balancing_enabled or not bess_present:
        return cfg, None, False
    bts = resolve_balancing_timeseries(ts, cfg, n_steps)
    return cfg, bts, True


def _alpha_beta(cfg: BalancingConfig, product: str) -> float:
    """Return alpha_k * beta_k for the expected-activation SOC drift."""
    return acceptance_probability(cfg, product) * activation_probability(cfg, product)


def derive_tight_big_m(
    params: dict[str, Any], ts: pd.DataFrame, *, dt_h: float, mode: str,
) -> dict[str, float]:
    """Compute the tight big-M values.

    Charge / discharge limits are symmetric — both come from
    ``bess_power_kw``.  The export big-M uses the worst-case (largest)
    max-injection share across the timeseries — a single global cap
    that over-bounds the per-step constraint.
    """
    p_export = float(params.get("p_grid_export_max_kw", 0.0) or 0.0)
    p_bess = float(params.get("bess_power_kw", 0.0) or 0.0)
    per_step_max_inj = _resolve_max_injection_per_step(params, ts)
    # Worst-case export ⇒ largest max-injection fraction.
    tightest_max_inj_frac = (
        float(per_step_max_inj.max()) if len(per_step_max_inj) else 1.0
    )

    if mode == "self_consumption" and "load_kwh" in ts.columns:
        load_max = float(ts["load_kwh"].max())
    else:
        load_max = 0.0
    pv_max = float(ts["pv_kwh"].max()) if "pv_kwh" in ts.columns else 0.0

    return {
        "M_imp": (load_max + p_bess * dt_h) * 1.001,
        "M_exp": p_export * dt_h * tightest_max_inj_frac * 1.001,
        "M_charge": p_bess * dt_h * 1.001,
        "M_pv": pv_max * 1.001,
    }


# ---------------------------------------------------------------------------
# Model construction
# ---------------------------------------------------------------------------


def build_model(
    params: dict[str, Any],
    ts: pd.DataFrame,
    *,
    initial_soc_kwh: float | None = None,
    terminal_soc_free: bool | None = None,
) -> pyo.ConcreteModel:
    """Construct the Pyomo MILP.

    Variable & constraint structure adapts to ``params['mode']``:

    * ``self_consumption``     — full set of variables, hard ``LOAD_PV_PRIORITY``
                    (Section 2 of the spec) plus a binary-free slack for
                    surplus-only export (Section 5), tight-big-M no-sim
                    grid I/O, retail-driven objective.
    * ``merchant``— ``pv_to_load``, ``bess_dis_load``, ``grid_to_load``
                    pinned to 0; load balance + load priority + no-sim
                    constraints omitted; objective DAM-only.

    Load priority is enforced by the hard ``LOAD_PV_PRIORITY``
    constraint (Section 2 of the spec):
    ``pv_to_load[t] == min(pv[t], load[t])`` exactly.  The slack-based
    ``LOAD_PRIORITY_SLACK_DEF`` enforces Section 5 (surplus-only
    export) — exports are gated by the same slack so an hour with
    ``grid_to_load > 0`` cannot also export.

    Parameters
    ----------
    initial_soc_kwh
        If supplied, overrides ``params["initial_soc_frac"] * e_cap`` for
        the ``soc[0]`` constraint.  Used by the rolling-horizon dispatcher
        to carry SOC across windows.
    terminal_soc_free
        If True, removes the closed-cycle ``soc[N] == soc[0]`` constraint
        (SOC is only bounded by ``soc_min`` / ``soc_max`` at the terminal
        step).  Default ``None`` means follow ``params['terminal_soc_equal']``.
        Used by the rolling-horizon dispatcher (a single window should not
        be forced to close its cycle).
    """
    dt_h = dt_hours_from(params)
    if dt_h <= 0:
        raise ValueError("dt_minutes must be positive")
    n_steps = len(ts)
    if n_steps == 0:
        raise ValueError("timeseries is empty; nothing to optimise.")
    time_index = range(n_steps)
    mode = resolve_mode(params)
    pv_present = float(params.get("pv_nameplate_kwp", 0.0) or 0.0) > 0.0
    bess_present = float(params.get("bess_power_kw", 0.0) or 0.0) > 0.0
    allow_grid_charge = (
        bool(params.get("allow_bess_grid_charging", False)) and bess_present
    )
    # Optional strict grid cap: when grid_cap_includes_load is set, the export
    # cap binds on TOTAL plant injection (load-serving flows plus surplus
    # export) instead of surplus export alone — a Virtual Net-Billing physical
    # injection cap.  It only takes effect in self_consumption mode (merchant
    # has no co-located load, so the basis collapses to surplus export).
    cap_includes_load = bool(params.get("grid_cap_includes_load", False))
    strict_injection_cap = cap_includes_load and mode == "self_consumption"

    if pd.api.types.is_datetime64_any_dtype(ts["timestamp"]):
        day_labels = ts["timestamp"].dt.date.tolist()
    else:
        day_labels = ["oneday"] * n_steps
    unique_days = list(pd.Index(day_labels).unique())
    day_to_idx = {d: [i for i, label in enumerate(day_labels) if label == d] for d in unique_days}

    load = {
        t: float(ts.loc[t, "load_kwh"]) if "load_kwh" in ts.columns else 0.0
        for t in time_index
    }
    if pv_present:
        pv = {t: float(ts.loc[t, "pv_kwh"]) for t in time_index}
    else:
        # PV not part of the project — override the timeseries column.
        pv = {t: 0.0 for t in time_index}
    dam_price = {
        t: float(ts.loc[t, "dam_price_eur_per_mwh"])
        if "dam_price_eur_per_mwh" in ts.columns
           and pd.notna(ts.loc[t, "dam_price_eur_per_mwh"])
        else 0.0
        for t in time_index
    }
    retail_default = float(params.get("retail_tariff_eur_per_mwh", 0.0) or 0.0)
    if "retail_price_eur_per_mwh" in ts.columns:
        retail_price = {
            t: float(ts.loc[t, "retail_price_eur_per_mwh"])
            if pd.notna(ts.loc[t, "retail_price_eur_per_mwh"])
            else retail_default
            for t in time_index
        }
    else:
        retail_price = {t: retail_default for t in time_index}

    max_injection_per_step = _resolve_max_injection_per_step(params, ts)
    p_export = float(params.get("p_grid_export_max_kw", 0.0) or 0.0)
    # Symmetric charge / discharge limit from bess_power_kw.
    p_bess = float(params.get("bess_power_kw", 0.0) or 0.0)
    bess_capacity_kwh = float(params.get("bess_capacity_kwh", 0.0) or 0.0)
    eta_c = float(params.get("efficiency_charge", 1.0) or 1.0)
    eta_d = float(params.get("efficiency_discharge", 1.0) or 1.0)
    wear_cost_eur_per_mwh = float(
        params.get("bess_wear_cost_eur_per_mwh", 0.0) or 0.0
    )

    # Per-step export cap derived from the max-injection profile.
    export_cap_kwh_per_step = {
        t: float(p_export * dt_h * max_injection_per_step[t])
        for t in time_index
    }
    # Optional per-source sub-caps (PV-origin / BESS-origin injection).  Same
    # p_export * dt_h * profile basis as the combined cap, on the same
    # connection nameplate; ``None`` means no sub-cap for that source.
    max_injection_pv_per_step = _resolve_optional_max_injection_per_step(
        params, ts, "max_injection_profile_pv",
    )
    max_injection_bess_per_step = _resolve_optional_max_injection_per_step(
        params, ts, "max_injection_profile_bess",
    )
    export_cap_pv_kwh_per_step = (
        {t: float(p_export * dt_h * max_injection_pv_per_step[t]) for t in time_index}
        if max_injection_pv_per_step is not None else None
    )
    export_cap_bess_kwh_per_step = (
        {t: float(p_export * dt_h * max_injection_bess_per_step[t]) for t in time_index}
        if max_injection_bess_per_step is not None else None
    )
    # In strict mode the PV load-serving flow is bounded by BOTH the combined
    # cap and (when present) the PV sub-cap, so the load-priority floor uses
    # the tighter of the two.
    if export_cap_pv_kwh_per_step is not None:
        strict_floor_cap_kwh_per_step = {
            t: min(export_cap_kwh_per_step[t], export_cap_pv_kwh_per_step[t])
            for t in time_index
        }
    else:
        strict_floor_cap_kwh_per_step = export_cap_kwh_per_step
    big_m = derive_tight_big_m(params, ts, dt_h=dt_h, mode=mode)

    m = pyo.ConcreteModel()
    m.T = pyo.RangeSet(0, n_steps - 1)
    m.mode = pyo.Param(initialize=mode, within=pyo.Any, mutable=False)

    # --- BESS energy capacity is a parameter, not a decision variable.
    # e_cap is pinned to bess_capacity_kwh: the MILP optimises *dispatch*
    # for a given size.  Capacity sizing is handled outside the MILP by
    # pvbess_opt.sizing, which sweeps (pv_kwp, bess_kw, bess_kwh) points
    # and re-runs this solve per point to build an efficient frontier.
    # When BESS is absent the value is 0 and the SOC pins below take effect.
    e_cap_param = bess_capacity_kwh if bess_present else 0.0
    m.soc = pyo.Var(m.T, domain=pyo.NonNegativeReals)

    m.pv_to_load = pyo.Var(m.T, domain=pyo.NonNegativeReals)
    m.pv_to_bess = pyo.Var(m.T, domain=pyo.NonNegativeReals)
    m.pv_to_grid = pyo.Var(m.T, domain=pyo.NonNegativeReals)
    m.pv_curtail = pyo.Var(m.T, domain=pyo.NonNegativeReals)

    m.bess_dis_load = pyo.Var(m.T, domain=pyo.NonNegativeReals)
    m.bess_dis_grid = pyo.Var(m.T, domain=pyo.NonNegativeReals)

    m.grid_to_load = pyo.Var(m.T, domain=pyo.NonNegativeReals)
    m.grid_to_bess = pyo.Var(m.T, domain=pyo.NonNegativeReals)

    if mode == "merchant":
        m.MERCHANT_NO_PV_TO_LOAD = pyo.Constraint(
            m.T, rule=lambda m, t: m.pv_to_load[t] == 0,
        )
        m.MERCHANT_NO_BESS_TO_LOAD = pyo.Constraint(
            m.T, rule=lambda m, t: m.bess_dis_load[t] == 0,
        )
        m.MERCHANT_NO_GRID_TO_LOAD = pyo.Constraint(
            m.T, rule=lambda m, t: m.grid_to_load[t] == 0,
        )

    # PV-only / BESS-only / hybrid asset support.
    # Pin all flows for an absent asset to zero.
    if not pv_present:
        m.NOPV_TO_LOAD = pyo.Constraint(
            m.T, rule=lambda m, t: m.pv_to_load[t] == 0,
        )
        m.NOPV_TO_BESS = pyo.Constraint(
            m.T, rule=lambda m, t: m.pv_to_bess[t] == 0,
        )
        m.NOPV_TO_GRID = pyo.Constraint(
            m.T, rule=lambda m, t: m.pv_to_grid[t] == 0,
        )
        m.NOPV_CURTAIL = pyo.Constraint(
            m.T, rule=lambda m, t: m.pv_curtail[t] == 0,
        )

    if bess_present and not allow_grid_charge:
        m.NO_GRID_CHARGE = pyo.Constraint(
            m.T, rule=lambda m, t: m.grid_to_bess[t] == 0,
        )

    m.y_charge = pyo.Var(m.T, domain=pyo.Binary)
    m.y_dis = pyo.Var(m.T, domain=pyo.Binary)
    if bess_present:
        # Section 4 of the Self-consumption spec — no charge + discharge simultaneously.
        m.MODE_LINK = pyo.Constraint(
            m.T, rule=lambda m, t: m.y_charge[t] + m.y_dis[t] <= 1,
        )

    if not bess_present:
        # BESS not part of the project — pin every BESS-related variable
        # to zero (incl. binary mode flags) and skip the BESS-only
        # constraints further down.  e_cap_param is already 0 in this
        # branch (see top of build_model); SOC pinned to 0.
        m.NOBESS_SOC = pyo.Constraint(
            m.T, rule=lambda m, t: m.soc[t] == 0,
        )
        if pv_present:
            # When PV is also absent we already pinned pv_to_bess above —
            # avoid a duplicate constraint name.
            m.NOBESS_PV_TO_BESS = pyo.Constraint(
                m.T, rule=lambda m, t: m.pv_to_bess[t] == 0,
            )
        m.NOBESS_GRID_TO_BESS = pyo.Constraint(
            m.T, rule=lambda m, t: m.grid_to_bess[t] == 0,
        )
        m.NOBESS_DIS_LOAD = pyo.Constraint(
            m.T, rule=lambda m, t: m.bess_dis_load[t] == 0,
        )
        m.NOBESS_DIS_GRID = pyo.Constraint(
            m.T, rule=lambda m, t: m.bess_dis_grid[t] == 0,
        )
        m.NOBESS_Y_CHARGE = pyo.Constraint(
            m.T, rule=lambda m, t: m.y_charge[t] == 0,
        )
        m.NOBESS_Y_DIS = pyo.Constraint(
            m.T, rule=lambda m, t: m.y_dis[t] == 0,
        )

    m.grid_export_total = pyo.Expression(
        m.T, rule=lambda m, t: m.pv_to_grid[t] + m.bess_dis_grid[t],
    )

    # Cap basis for EXPORT_CAP (see the comment block above the constraint).
    # Defined unconditionally so the expression — and the
    # grid_injection_total_kwh output column — always exist; the rule chooses
    # what the cap binds on.  Default: equals grid_export_total (surplus
    # export).  Strict (self_consumption + grid_cap_includes_load): the total
    # plant injection at the connection point.
    def _cap_basis_rule(m, t):
        if strict_injection_cap:
            return (
                m.pv_to_load[t] + m.bess_dis_load[t]
                + m.pv_to_grid[t] + m.bess_dis_grid[t]
            )
        return m.grid_export_total[t]

    m.grid_injection_total = pyo.Expression(m.T, rule=_cap_basis_rule)

    # --- PV split (always active) ----------------------------------------
    m.PV_SPLIT = pyo.Constraint(
        m.T,
        rule=lambda m, t: (
            m.pv_to_load[t] + m.pv_to_bess[t] + m.pv_to_grid[t] + m.pv_curtail[t]
            == pv[t]
        ),
    )

    # --- Load balance (self_consumption only) -----------------------------------------
    if mode == "self_consumption":
        m.LOAD_BAL = pyo.Constraint(
            m.T,
            rule=lambda m, t: (
                m.pv_to_load[t] + m.bess_dis_load[t] + m.grid_to_load[t] == load[t]
            ),
        )

        # Section 2 of the Self-consumption spec — strict load-coverage priority.
        # All available PV (up to the load) must be consumed by the load.
        # Combined with PV_SPLIT and LOAD_BAL this forces
        # pv_to_load[t] == min(pv[t], load[t]) exactly.  BESS-before-Grid
        # for the residual remains emergent through retail > DAM economics.
        #
        # Under the strict total-injection cap (grid_cap_includes_load=True) the
        # load-serving flow is itself injected at the connection point, so it is
        # bound by the per-step cap.  The priority floor becomes the largest
        # coverage the cap physically admits, min(pv, load, cap): load still
        # takes ABSOLUTE precedence over surplus export for the scarce injection
        # capacity — EXPORT_CAP then pins pv_to_load to exactly this floor when
        # the cap binds (no export possible until the load floor is served) —
        # but the model no longer demands an injection the cap cannot make.  The
        # uncovered remainder is met by grid_to_load (retail) and the surplus PV
        # is curtailed / stored.  In the default mode the cap binds on surplus
        # alone, so the floor stays min(pv, load).
        if strict_injection_cap:
            pv_load_priority = {
                t: min(pv[t], load[t], strict_floor_cap_kwh_per_step[t])
                for t in time_index
            }
        else:
            pv_load_priority = {t: min(pv[t], load[t]) for t in time_index}
        m.LOAD_PV_PRIORITY = pyo.Constraint(
            m.T,
            rule=lambda m, t: m.pv_to_load[t] >= pv_load_priority[t],
        )

    # --- Balancing-market block (gated on cfg.balancing_enabled) ---------
    # Variables and bounds are declared up-front because they enter the
    # SOC dynamics through the expected-activation drift; the power-
    # budget and SOC-headroom constraints, and the objective, attach
    # after the rest of the dispatch model is built.
    balancing_cfg, balancing_ts, balancing_active = _resolve_balancing_inputs(
        params, ts, n_steps=n_steps, bess_present=bess_present,
    )
    if balancing_active:
        product_caps = {
            k: capacity_share_kw(balancing_cfg, k, p_bess)
            for k in PRODUCTS_ALL
        }
        m.BALANCING_PRODUCTS = pyo.Set(initialize=list(PRODUCTS_ALL), ordered=True)
        m.r_balancing = pyo.Var(
            m.BALANCING_PRODUCTS, m.T, domain=pyo.NonNegativeReals,
            bounds=lambda _m, k, _t: (0.0, product_caps[k]),
        )

    # --- SOC dynamics ----------------------------------------------------
    def soc_dynamics(m, t):
        if t == n_steps - 1:
            return pyo.Constraint.Skip
        charge_eff = eta_c * (m.pv_to_bess[t] + m.grid_to_bess[t])
        discharge_raw = (m.bess_dis_load[t] + m.bess_dis_grid[t]) / eta_d
        # Expected-value activation drifts in kWh, deterministic from the
        # solver's point of view. FCR is symmetric in expectation so it
        # contributes zero net energy.
        if balancing_active:
            act_charge = eta_c * dt_h * sum(
                _alpha_beta(balancing_cfg, k) * m.r_balancing[k, t]
                for k in PRODUCTS_DN
            )
            act_discharge = (dt_h / eta_d) * sum(
                _alpha_beta(balancing_cfg, k) * m.r_balancing[k, t]
                for k in PRODUCTS_UP
            )
        else:
            act_charge = 0.0
            act_discharge = 0.0
        return m.soc[t + 1] == (
            m.soc[t] + charge_eff - discharge_raw + act_charge - act_discharge
        )

    m.SOC_DYN = pyo.Constraint(m.T, rule=soc_dynamics)
    m.SOC_MIN = pyo.Constraint(
        m.T,
        rule=lambda m, t: m.soc[t] >= params["soc_min_frac"] * e_cap_param,
    )
    m.SOC_MAX = pyo.Constraint(
        m.T,
        rule=lambda m, t: m.soc[t] <= params["soc_max_frac"] * e_cap_param,
    )

    if bess_present:
        if initial_soc_kwh is not None:
            m.SOC_INIT = pyo.Constraint(
                expr=m.soc[0] == float(initial_soc_kwh),
            )
        else:
            m.SOC_INIT = pyo.Constraint(
                expr=m.soc[0] == params["initial_soc_frac"] * e_cap_param,
            )

        final_charge = eta_c * (
            m.pv_to_bess[n_steps - 1] + m.grid_to_bess[n_steps - 1]
        )
        final_discharge = (
            m.bess_dis_load[n_steps - 1] + m.bess_dis_grid[n_steps - 1]
        ) / eta_d
        if balancing_active:
            t_final = n_steps - 1
            final_act_charge = eta_c * dt_h * sum(
                _alpha_beta(balancing_cfg, k) * m.r_balancing[k, t_final]
                for k in PRODUCTS_DN
            )
            final_act_discharge = (dt_h / eta_d) * sum(
                _alpha_beta(balancing_cfg, k) * m.r_balancing[k, t_final]
                for k in PRODUCTS_UP
            )
        else:
            final_act_charge = 0.0
            final_act_discharge = 0.0
        final_soc_expr = (
            m.soc[n_steps - 1] + final_charge - final_discharge
            + final_act_charge - final_act_discharge
        )

        if terminal_soc_free is None:
            terminal_soc_free = not bool(params.get("terminal_soc_equal", True))
        if not terminal_soc_free:
            m.SOC_TERM = pyo.Constraint(expr=final_soc_expr == m.soc[0])
        else:
            m.SOC_TERM_MIN = pyo.Constraint(
                expr=final_soc_expr >= params["soc_min_frac"] * e_cap_param,
            )
            m.SOC_TERM_MAX = pyo.Constraint(
                expr=final_soc_expr <= params["soc_max_frac"] * e_cap_param,
            )

        # --- Charge / discharge power limits (symmetric) -------------------
        bess_step_lim = p_bess * dt_h
        m.CH_LIM = pyo.Constraint(
            m.T,
            rule=lambda m, t: (
                m.pv_to_bess[t] + m.grid_to_bess[t]
                <= bess_step_lim * m.y_charge[t]
            ),
        )
        m.DIS_LIM = pyo.Constraint(
            m.T,
            rule=lambda m, t: (
                m.bess_dis_load[t] + m.bess_dis_grid[t]
                <= bess_step_lim * m.y_dis[t]
            ),
        )

        # --- Daily cycle limit ------------------------------------------------
        m.CYC = pyo.ConstraintList()
        for indices in day_to_idx.values():
            lhs = sum(m.bess_dis_load[t] + m.bess_dis_grid[t] for t in indices)
            m.CYC.add(lhs <= float(params["max_cycles_per_day"]) * e_cap_param)

    # --- Balancing-market constraints (gated) ---------------------------------
    if balancing_active:
        h_buf = float(balancing_cfg.bm_soc_headroom_pct) / 100.0
        d_fcr = float(balancing_cfg.fcr_required_duration_hours)
        bess_step_lim_bm = p_bess * dt_h

        # Per-direction power budget. FCR is symmetric so it counts in
        # both directions. r is in kW; multiply by dt_h to compare with
        # the kWh-per-step DAM flows.
        def _bm_power_dn(m, t):
            dn_share = sum(
                m.r_balancing[k, t]
                for k in PRODUCTS_DN + PRODUCTS_SYMMETRIC
            )
            return (
                m.pv_to_bess[t] + m.grid_to_bess[t] + dn_share * dt_h
                <= bess_step_lim_bm
            )

        def _bm_power_up(m, t):
            up_share = sum(
                m.r_balancing[k, t]
                for k in PRODUCTS_UP + PRODUCTS_SYMMETRIC
            )
            return (
                m.bess_dis_load[t] + m.bess_dis_grid[t] + up_share * dt_h
                <= bess_step_lim_bm
            )

        m.BM_POWER_DN = pyo.Constraint(m.T, rule=_bm_power_dn)
        m.BM_POWER_UP = pyo.Constraint(m.T, rule=_bm_power_up)

        # SOC headroom — must be able to honour a full settlement period
        # of activation in the worst case, with an extra safety buffer.
        def _bm_soc_up(m, t):
            asym = (1.0 + h_buf) * dt_h * sum(
                m.r_balancing[k, t] for k in PRODUCTS_UP
            ) / eta_d
            sym = (1.0 + h_buf) * d_fcr * sum(
                m.r_balancing[k, t] for k in PRODUCTS_SYMMETRIC
            ) / eta_d
            return m.soc[t] - params["soc_min_frac"] * e_cap_param >= asym + sym

        def _bm_soc_dn(m, t):
            asym = (1.0 + h_buf) * dt_h * sum(
                m.r_balancing[k, t] for k in PRODUCTS_DN
            ) * eta_c
            sym = (1.0 + h_buf) * d_fcr * sum(
                m.r_balancing[k, t] for k in PRODUCTS_SYMMETRIC
            ) * eta_c
            return (
                params["soc_max_frac"] * e_cap_param - m.soc[t] >= asym + sym
            )

        m.BM_SOC_UP = pyo.Constraint(m.T, rule=_bm_soc_up)
        m.BM_SOC_DN = pyo.Constraint(m.T, rule=_bm_soc_dn)

    # --- Hourly max-injection cap (HARD constraint, BOTH modes) ----------
    # Section 8 of the Self-consumption spec — regulatory grid-connection limit.
    # Applies in self_consumption AND merchant modes. Cap may vary by hour-of-day
    # (and optionally by month) via the ``max_injection_profile`` sheet.  The
    # per-step cap is p_grid_export_max_kw × dt_h × max_injection_per_step[t]:
    # ``p_grid_export_max_kw`` is the nameplate grid-connection limit and
    # ``max_injection_profile`` is the per-hour share of that limit available
    # for injection.
    #
    # What the cap binds on is grid_injection_total (the single cap basis built
    # above), which depends on grid_cap_includes_load:
    #
    #   * Default (False) — binds on SURPLUS EXPORT only: grid_injection_total
    #     == grid_export_total == pv_to_grid + bess_dis_grid.  A pure
    #     surplus-export connection limit; 100% backward compatible.
    #
    #   * Strict (True, self_consumption only) — binds on TOTAL PLANT INJECTION
    #     at the connection point: pv_to_load + bess_dis_load + pv_to_grid +
    #     bess_dis_grid.  Under Virtual Net-Billing the energy "virtually
    #     allocated" to a remote load is physically injected at the plant too,
    #     so the regulatory limit is a physical plant-injection cap, not merely
    #     a surplus-export cap.  Merchant has no co-located load, so the basis
    #     collapses to grid_export_total and the flag is a no-op there.
    #
    # Load priority stays strict.  LOAD_PV_PRIORITY pins pv_to_load to its floor
    # min(pv, load) (default) or min(pv, load, cap) (strict total-injection
    # cap).  In strict mode the load-serving injection competes with surplus
    # export for the same cap and wins: EXPORT_CAP plus the floor force the load
    # to take all available injection capacity before any export, the uncovered
    # remainder falling to grid_to_load (retail).  Priority is never traded for
    # market revenue; it is only ever bounded by the physical injection cap, so
    # no strict run is infeasible — it degrades to the maximum feasible coverage.
    #
    # The cap is a direct ``<=`` to a constant, so it needs no big-M; M_exp
    # gates the NO_SIM_GRID_EXPORT binary below and is independent of this cap.
    m.EXPORT_CAP = pyo.Constraint(
        m.T,
        rule=lambda m, t: m.grid_injection_total[t] <= export_cap_kwh_per_step[t],
    )

    # Optional per-source injection sub-caps.  The basis mirrors the combined
    # cap split by origin: strict mode counts the load-serving flow too, while
    # the default / merchant basis is surplus export only.  Active in BOTH
    # modes (merchant pins the load-serving flows to 0, so the basis collapses
    # to surplus).  Each constraint is attached only when its profile is given.
    if export_cap_pv_kwh_per_step is not None:
        def _pv_injection_basis(m, t):
            if strict_injection_cap:
                return m.pv_to_load[t] + m.pv_to_grid[t]
            return m.pv_to_grid[t]
        m.pv_injection_total = pyo.Expression(m.T, rule=_pv_injection_basis)
        m.EXPORT_CAP_PV = pyo.Constraint(
            m.T,
            rule=lambda m, t: (
                m.pv_injection_total[t] <= export_cap_pv_kwh_per_step[t]
            ),
        )
    if export_cap_bess_kwh_per_step is not None:
        def _bess_injection_basis(m, t):
            if strict_injection_cap:
                return m.bess_dis_load[t] + m.bess_dis_grid[t]
            return m.bess_dis_grid[t]
        m.bess_injection_total = pyo.Expression(m.T, rule=_bess_injection_basis)
        m.EXPORT_CAP_BESS = pyo.Constraint(
            m.T,
            rule=lambda m, t: (
                m.bess_injection_total[t] <= export_cap_bess_kwh_per_step[t]
            ),
        )

    # --- self_consumption-only constraints --------------------------------------------
    # Merchant mode intentionally omits the no-simultaneous-grid-IO
    # constraint (the y_grid_io binary below): the audit verified that
    # simultaneous import/export never occurs in practice for merchant
    # dispatch, so the constraint would be economically non-binding.
    if mode == "self_consumption":
        # Section 5 of the Self-consumption spec — surplus-only export.
        # Substituting PV_SPLIT (pv = pv_to_load + pv_to_bess + pv_to_grid +
        # pv_curtail) and LOAD_BAL (load = pv_to_load + bess_dis_load +
        # grid_to_load) into the slack RHS, the constraint reduces to
        # ``grid_to_load <= pv_to_bess + pv_curtail``, i.e. an hour can
        # only export when its load is fully covered without grid import.
        m.slack = pyo.Var(m.T, domain=pyo.NonNegativeReals)
        m.LOAD_PRIORITY_SLACK_DEF = pyo.Constraint(
            m.T,
            rule=lambda m, t: (
                m.slack[t]
                >= pv[t] + m.bess_dis_load[t] + m.bess_dis_grid[t] - load[t]
            ),
        )
        m.LOAD_PRIORITY_EXPORT = pyo.Constraint(
            m.T,
            rule=lambda m, t: (
                m.pv_to_grid[t] + m.bess_dis_grid[t] <= m.slack[t]
            ),
        )

        m.y_grid_io = pyo.Var(m.T, domain=pyo.Binary)
        m.NO_SIM_GRID_IMPORT = pyo.Constraint(
            m.T,
            rule=lambda m, t: (
                m.grid_to_load[t] + m.grid_to_bess[t]
                <= big_m["M_imp"] * m.y_grid_io[t]
            ),
        )
        m.NO_SIM_GRID_EXPORT = pyo.Constraint(
            m.T,
            rule=lambda m, t: (
                m.pv_to_grid[t] + m.bess_dis_grid[t]
                <= big_m["M_exp"] * (1 - m.y_grid_io[t])
            ),
        )

    if allow_grid_charge:
        # Section 6 of the Self-consumption spec — BESS may charge from grid only in
        # periods with pv ~ 0.  z_pv_active[t] is forced to 1 whenever
        # pv[t] > 0 by GRID_CHG_PV_GATE; GRID_CHARGE_GATE then drives
        # grid_to_bess[t] to 0 in those steps.
        m.z_pv_active = pyo.Var(m.T, domain=pyo.Binary)
        m.GRID_CHARGE_GATE = pyo.Constraint(
            m.T,
            rule=lambda m, t: (
                m.grid_to_bess[t] <= big_m["M_charge"] * (1 - m.z_pv_active[t])
            ),
        )
        m.GRID_CHG_PV_GATE = pyo.Constraint(
            m.T,
            rule=lambda m, t: pv[t] <= big_m["M_pv"] * m.z_pv_active[t],
        )

    # --- Objective: profit -----------------------------------------------
    curtail_tiebreak_term = _WEIGHT_CURTAIL_TIEBREAK_EUR_PER_KWH * sum(
        m.pv_curtail[t] for t in time_index
    )

    if mode == "self_consumption":
        avoided_cost = sum(
            retail_price[t] * (m.pv_to_load[t] + m.bess_dis_load[t]) / 1000.0
            for t in time_index
        )
        export_revenue = sum(
            dam_price[t] * (m.pv_to_grid[t] + m.bess_dis_grid[t]) / 1000.0
            for t in time_index
        )
    else:  # merchant
        avoided_cost = 0.0
        export_revenue = sum(
            dam_price[t] * (m.pv_to_grid[t] + m.bess_dis_grid[t]) / 1000.0
            for t in time_index
        )

    grid_charge_cost = sum(
        dam_price[t] * m.grid_to_bess[t] / 1000.0 for t in time_index
    )
    # Battery wear cost: penalise discharge throughput so the optimizer only
    # cycles when the spread beats the per-MWh degradation cost.  Default 0
    # (off).  Shadow price only — not added to the reported cashflow / NPV.
    wear_cost_term = wear_cost_eur_per_mwh * sum(
        (m.bess_dis_load[t] + m.bess_dis_grid[t]) / 1000.0 for t in time_index
    )
    profit_eur = avoided_cost + export_revenue - grid_charge_cost - wear_cost_term

    if balancing_active:
        # Expected capacity revenue across all five products.
        cap_terms = []
        for k in PRODUCTS_ALL:
            alpha = acceptance_probability(balancing_cfg, k)
            price_col = getattr(
                balancing_ts, f"{k}_capacity_price_eur_per_mwh",
            )
            cap_terms.append(
                alpha * dt_h * sum(
                    float(price_col[t]) * m.r_balancing[k, t]
                    for t in time_index
                ) / 1000.0
            )
        # Expected activation revenue across the four products that
        # carry an activation payment. Both up and down activation
        # prices enter as positive payments per the documented sign
        # convention; the user is responsible for sign-correctness of
        # the input prices.
        act_terms = []
        for k in PRODUCTS_WITH_ACTIVATION:
            alpha_beta = _alpha_beta(balancing_cfg, k)
            price_col = getattr(
                balancing_ts, f"{k}_activation_price_eur_per_mwh",
            )
            act_terms.append(
                alpha_beta * dt_h * sum(
                    float(price_col[t]) * m.r_balancing[k, t]
                    for t in time_index
                ) / 1000.0
            )
        m.balancing_revenue_expr = pyo.Expression(
            expr=sum(cap_terms) + sum(act_terms),
        )
        profit_eur = profit_eur + m.balancing_revenue_expr

    m.OBJ = pyo.Objective(
        expr=profit_eur - curtail_tiebreak_term, sense=pyo.maximize,
    )

    return m


# ---------------------------------------------------------------------------
# Solve and DataFrame conversion
# ---------------------------------------------------------------------------


def solve_model(
    model: pyo.ConcreteModel,
    solver_name: str,
    *,
    mip_gap: float = 0.001,
    time_limit_seconds: int = 1800,
    tee: bool = False,
) -> tuple[pyo.ConcreteModel, str]:
    """Solve ``model``; raise on failure; return ``(model, solver_name)``."""
    solver, resolved = choose_solver(solver_name)
    configure_solver_options(
        solver, resolved,
        mip_gap=mip_gap, time_limit_seconds=time_limit_seconds,
    )
    n_vars = int(model.nvariables())
    n_cons = int(model.nconstraints())
    logger.info(
        "[milp-solve] start: solver=%s vars=%d constraints=%d "
        "time_limit=%ds mip_gap=%.4g tee=%s",
        resolved, n_vars, n_cons, int(time_limit_seconds), float(mip_gap),
        bool(tee),
    )
    for h in logger.handlers + logging.getLogger().handlers:
        h.flush()
    t_solve_start = time.perf_counter()
    result = solver.solve(model, tee=tee)
    elapsed = time.perf_counter() - t_solve_start
    condition = result.solver.termination_condition
    logger.info(
        "[milp-solve] done: solver=%s elapsed=%.2fs termination=%s",
        resolved, elapsed, condition,
    )
    for h in logger.handlers + logging.getLogger().handlers:
        h.flush()
    _check_solver_status(result, resolved, model)
    return model, resolved


def model_to_dataframe(
    model: pyo.ConcreteModel,
    ts: pd.DataFrame,
    params: dict[str, Any],
    *,
    round_output: bool = True,
) -> pd.DataFrame:
    """Convert the solved model to a dispatch DataFrame.

    ``round_output`` rounds the numeric columns to 4 dp for the
    user-visible / persisted frame.  Pass ``False`` to keep full-precision
    model values — used for the dispatch-invariant checks so the sum-based
    invariant_4 is not polluted by round(4) accumulation across tens of
    thousands of rows.

    Rounding note: a 4-decimal-place round zeroes any sub-0.5 mW
    reservation (e.g. a balancing reservation that the MILP set to a
    fraction of a watt for a numerical tie-break).  Callers that need
    full precision -- the per-step energy-balance verifier and the
    invariant-4 RTE bound -- should pass
    ``return_unrounded=True`` to :func:`run_scenario` and read the
    full-precision frame.  Headline KPIs and downstream display use
    the rounded frame by design; see ``pvbess_opt/conventions.md``
    for the full rounding contract.
    """
    n_steps = len(ts)
    time_index = range(n_steps)
    dt_h = dt_hours_from(params)
    if dt_h <= 0:
        raise ValueError("dt_minutes must be positive")
    p_export = float(params.get("p_grid_export_max_kw", 0.0) or 0.0)
    max_injection_per_step = _resolve_max_injection_per_step(params, ts)
    export_cap_kwh_per_step = (
        p_export * dt_h * max_injection_per_step
    )
    bess_capacity_kwh = float(params.get("bess_capacity_kwh", 0.0) or 0.0)

    pv_present = float(params.get("pv_nameplate_kwp", 0.0) or 0.0) > 0.0

    res = pd.DataFrame(index=ts.index)
    res["timestamp"] = ts["timestamp"].values
    res["load_kwh"] = [
        float(ts.loc[t, "load_kwh"]) if "load_kwh" in ts.columns else 0.0
        for t in time_index
    ]
    res["pv_kwh"] = [
        float(ts.loc[t, "pv_kwh"]) if pv_present else 0.0
        for t in time_index
    ]
    res["pv_to_load_kwh"] = [pyo.value(model.pv_to_load[t]) for t in time_index]
    res["pv_to_bess_kwh"] = [pyo.value(model.pv_to_bess[t]) for t in time_index]
    res["bess_charge_grid_kwh"] = [pyo.value(model.grid_to_bess[t]) for t in time_index]
    res["bess_dis_load_kwh"] = [pyo.value(model.bess_dis_load[t]) for t in time_index]
    res["bess_dis_grid_kwh"] = [pyo.value(model.bess_dis_grid[t]) for t in time_index]
    res["pv_to_grid_kwh"] = [pyo.value(model.pv_to_grid[t]) for t in time_index]
    res["pv_curtail_kwh"] = [pyo.value(model.pv_curtail[t]) for t in time_index]
    res["grid_to_load_kwh"] = [pyo.value(model.grid_to_load[t]) for t in time_index]
    res["grid_export_total_kwh"] = [
        pyo.value(model.grid_export_total[t]) for t in time_index
    ]
    res["grid_export_cap_kwh"] = export_cap_kwh_per_step
    # Actual quantity the EXPORT_CAP binds on.  Equals grid_export_total_kwh in
    # the default mode; equals total plant injection (load-serving flows plus
    # surplus export) under grid_cap_includes_load in self_consumption mode.
    res["grid_injection_total_kwh"] = [
        pyo.value(model.grid_injection_total[t]) for t in time_index
    ]

    res["soc_kwh"] = [pyo.value(model.soc[t]) for t in time_index]
    if bess_capacity_kwh > 1e-9:
        res["soc_pct"] = res["soc_kwh"] / bess_capacity_kwh * 100.0
    else:
        res["soc_pct"] = 0.0

    if "dam_price_eur_per_mwh" in ts.columns:
        res["dam_price_eur_per_mwh"] = ts["dam_price_eur_per_mwh"].values
    if "retail_price_eur_per_mwh" in ts.columns:
        res["retail_price_eur_per_mwh"] = ts["retail_price_eur_per_mwh"].values
    # Optional per-step grid carbon intensity, echoed for emissions / 24/7
    # CFE accounting. Absent unless the user supplies the time-series column.
    if "grid_co2_kg_per_mwh" in ts.columns:
        res["grid_co2_kg_per_mwh"] = ts["grid_co2_kg_per_mwh"].values

    # Balancing reservations (kW per timestep). Only emitted when the
    # MILP carried the balancing block — keeping the dispatch frame
    # bit-identical to the previous release when balancing is OFF.
    if hasattr(model, "r_balancing"):
        for product in PRODUCTS_ALL:
            res[f"bm_reservation_{product}_kw"] = [
                pyo.value(model.r_balancing[product, t]) for t in time_index
            ]
        # Re-resolve the balancing-price columns from the workbook
        # configuration so the dispatch frame carries them even when
        # the timeseries DataFrame omits the optional columns.
        _, balancing_ts_view, _ = _resolve_balancing_inputs(
            params, ts, n_steps=n_steps,
            bess_present=float(params.get("bess_power_kw", 0.0) or 0.0) > 0.0,
        )
        if balancing_ts_view is not None:
            for col in (
                "fcr_capacity_price_eur_per_mwh",
                "afrr_up_capacity_price_eur_per_mwh",
                "afrr_dn_capacity_price_eur_per_mwh",
                "mfrr_up_capacity_price_eur_per_mwh",
                "mfrr_dn_capacity_price_eur_per_mwh",
                "afrr_up_activation_price_eur_per_mwh",
                "afrr_dn_activation_price_eur_per_mwh",
                "mfrr_up_activation_price_eur_per_mwh",
                "mfrr_dn_activation_price_eur_per_mwh",
            ):
                res[col] = getattr(balancing_ts_view, col)

    if round_output:
        numeric_cols = [c for c in res.columns if c != "timestamp"]
        res[numeric_cols] = res[numeric_cols].astype(float).round(4)
    return res


@overload
def run_scenario(
    params: dict[str, Any],
    ts: pd.DataFrame,
    solver_name: str = ...,
    *,
    mip_gap: float = ...,
    time_limit_seconds: int = ...,
    tee: bool = ...,
    initial_soc_kwh: float | None = ...,
    terminal_soc_free: bool | None = ...,
    return_unrounded: Literal[False] = ...,
) -> tuple[pd.DataFrame, str]: ...
@overload
def run_scenario(
    params: dict[str, Any],
    ts: pd.DataFrame,
    solver_name: str = ...,
    *,
    mip_gap: float = ...,
    time_limit_seconds: int = ...,
    tee: bool = ...,
    initial_soc_kwh: float | None = ...,
    terminal_soc_free: bool | None = ...,
    return_unrounded: Literal[True],
) -> tuple[pd.DataFrame, str, pd.DataFrame]: ...
def run_scenario(
    params: dict[str, Any],
    ts: pd.DataFrame,
    solver_name: str = "highs",
    *,
    mip_gap: float = 0.001,
    time_limit_seconds: int = 1800,
    tee: bool = False,
    initial_soc_kwh: float | None = None,
    terminal_soc_free: bool | None = None,
    return_unrounded: bool = False,
) -> tuple[pd.DataFrame, str] | tuple[pd.DataFrame, str, pd.DataFrame]:
    """Build, solve and extract dispatch for a single scenario.

    Returns ``(res, resolved_solver_name)``.  ``e_cap`` is a parameter
    pinned to ``params['bess_capacity_kwh']``: this solve optimises
    dispatch for a fixed size.  Capacity sizing is an outer sweep over
    this solve (see :mod:`pvbess_opt.sizing`); it is not a decision
    variable and is not returned.

    When ``return_unrounded`` is True the tuple is extended with a
    full-precision copy of the dispatch frame ``(res, resolved, res_full)``
    so callers can run the dispatch-invariant checks without round(4)
    accumulation polluting the sum-based invariant_4.
    """
    model = build_model(
        params, ts,
        initial_soc_kwh=initial_soc_kwh,
        terminal_soc_free=terminal_soc_free,
    )
    solved, resolved = solve_model(
        model, solver_name,
        mip_gap=mip_gap, time_limit_seconds=time_limit_seconds, tee=tee,
    )
    if return_unrounded:
        res_full = model_to_dataframe(solved, ts, params, round_output=False)
        numeric_cols = [c for c in res_full.columns if c != "timestamp"]
        res = res_full.copy()
        res[numeric_cols] = res[numeric_cols].astype(float).round(4)
        return res, resolved, res_full
    res = model_to_dataframe(solved, ts, params)
    return res, resolved


# ---------------------------------------------------------------------------
# Dispatch invariants — verify_dispatch_invariants
#
# 9 general-dispatch invariants + 6 balancing-market invariants
# (INV-B1..INV-B6).  The balancing block is verified iff
# ``params['balancing']['balancing_enabled']`` is true and the dispatch
# frame carries the per-product reservation columns; otherwise the
# corresponding residuals are reported as 0.0 (vacuously satisfied).
# ---------------------------------------------------------------------------


# Names of the six balancing-invariant residual keys returned by
# :func:`verify_dispatch_invariants`.  Anchored as a tuple so
# downstream consumers (``main._check_strict_invariants``) can refer to
# the canonical list without re-declaring the names.
BALANCING_INVARIANT_KEYS: tuple[str, ...] = (
    "invariant_b1_capacity_share_sum_pct_excess",
    "invariant_b2_reservation_share_cap_excess_kw",
    "invariant_b3_soc_headroom_up_excess_kwh",
    "invariant_b4_soc_headroom_dn_excess_kwh",
    "invariant_b5_power_budget_excess_kwh",
    "invariant_b6_off_invariants_max_residual",
)


def _balancing_invariants(
    res: pd.DataFrame,
    params: dict[str, Any],
    *,
    general_invariants: dict[str, float],
) -> dict[str, float]:
    """Compute the six INV-B1..INV-B6 balancing-invariant residuals.

    The residuals are returned as positive floats; 0.0 means satisfied
    within machine precision.  See the docstring of
    :func:`verify_dispatch_invariants` for the per-invariant definition.

    When the balancing block did not fire (master switch off or
    dispatch frame missing the reservation columns) every B1..B5
    residual is 0.0 and B6 carries the maximum of the nine
    general-dispatch invariants -- the property INV-B6 anchors -- so a
    balancing-OFF run still fails strict mode if any of the existing
    nine invariants is violated.
    """
    out: dict[str, float] = {k: 0.0 for k in BALANCING_INVARIANT_KEYS}

    raw_cfg = params.get("balancing") or {}
    cfg = resolve_balancing_config(raw_cfg)
    balancing_on = bool(cfg.balancing_enabled)
    have_reservations = all(
        f"bm_reservation_{p}_kw" in res.columns for p in PRODUCTS_ALL
    )

    if not (balancing_on and have_reservations):
        # INV-B6: balancing-OFF run must preserve the existing nine
        # invariants.  Surface the worst residual so strict mode rejects
        # a balancing-OFF dispatch that violates any of them.
        out["invariant_b6_off_invariants_max_residual"] = max(
            (v for v in general_invariants.values()), default=0.0,
        )
        return out

    bess_power_kw = float(params.get("bess_power_kw", 0.0) or 0.0)
    dt_h = dt_hours_from(params)
    eta_c = float(params.get("efficiency_charge", 1.0) or 1.0)
    eta_d = float(params.get("efficiency_discharge", 1.0) or 1.0)
    bess_kwh = float(params.get("bess_capacity_kwh", 0.0) or 0.0)
    # Strict key access — the MILP build path uses params["soc_min_frac"]
    # / params["soc_max_frac"] directly, so the loader always populates
    # both keys (io.py:1490-1491).  A silent .get fallback here would let
    # a hand-built ``params`` dict bypass the invariant check that build
    # would have rejected with KeyError.  Pass-2 P2.4.
    soc_min = float(params["soc_min_frac"]) * bess_kwh
    soc_max = float(params["soc_max_frac"]) * bess_kwh
    h_buf = cfg.bm_soc_headroom_pct / 100.0
    d_fcr = cfg.fcr_required_duration_hours

    # INV-B1: sum of per-product capacity shares + DAM share <= 100 %.
    share_total = (
        cfg.dam_capacity_share_pct
        + cfg.fcr_capacity_share_pct
        + cfg.afrr_up_capacity_share_pct
        + cfg.afrr_dn_capacity_share_pct
        + cfg.mfrr_up_capacity_share_pct
        + cfg.mfrr_dn_capacity_share_pct
    )
    out["invariant_b1_capacity_share_sum_pct_excess"] = float(
        max(0.0, share_total - 100.0)
    )

    # INV-B2: per-step reservation <= product share cap (kW).
    max_share_excess = 0.0
    for product in PRODUCTS_ALL:
        cap_kw = capacity_share_kw(cfg, product, bess_power_kw)
        r = res[f"bm_reservation_{product}_kw"].to_numpy(dtype=float)
        if r.size:
            max_share_excess = max(
                max_share_excess, float((r - cap_kw).max(initial=0.0)),
            )
    out["invariant_b2_reservation_share_cap_excess_kw"] = max_share_excess

    soc = res["soc_kwh"].to_numpy(dtype=float)
    r_afrr_up = res["bm_reservation_afrr_up_kw"].to_numpy(dtype=float)
    r_mfrr_up = res["bm_reservation_mfrr_up_kw"].to_numpy(dtype=float)
    r_afrr_dn = res["bm_reservation_afrr_dn_kw"].to_numpy(dtype=float)
    r_mfrr_dn = res["bm_reservation_mfrr_dn_kw"].to_numpy(dtype=float)
    r_fcr = res["bm_reservation_fcr_kw"].to_numpy(dtype=float)

    # INV-B3: soc_kwh - soc_min >= headroom_up.
    headroom_up = (
        (1.0 + h_buf) * dt_h * (r_afrr_up + r_mfrr_up) / eta_d
        + (1.0 + h_buf) * d_fcr * r_fcr / eta_d
    )
    up_excess = headroom_up - (soc - soc_min)
    out["invariant_b3_soc_headroom_up_excess_kwh"] = float(
        max(0.0, up_excess.max(initial=0.0))
    )

    # INV-B4: soc_max - soc_kwh >= headroom_dn.
    headroom_dn = (
        (1.0 + h_buf) * dt_h * (r_afrr_dn + r_mfrr_dn) * eta_c
        + (1.0 + h_buf) * d_fcr * r_fcr * eta_c
    )
    dn_excess = headroom_dn - (soc_max - soc)
    out["invariant_b4_soc_headroom_dn_excess_kwh"] = float(
        max(0.0, dn_excess.max(initial=0.0))
    )

    # INV-B5: per-direction power budget within p_bess * dt_h.
    bess_dis_load = res["bess_dis_load_kwh"].to_numpy(dtype=float)
    bess_dis_grid = res["bess_dis_grid_kwh"].to_numpy(dtype=float)
    pv_to_bess = res["pv_to_bess_kwh"].to_numpy(dtype=float)
    grid_to_bess = res["bess_charge_grid_kwh"].to_numpy(dtype=float)
    up_share_kw = r_fcr + r_afrr_up + r_mfrr_up
    dn_share_kw = r_fcr + r_afrr_dn + r_mfrr_dn
    budget = bess_power_kw * dt_h
    lhs_up = bess_dis_load + bess_dis_grid + up_share_kw * dt_h
    lhs_dn = pv_to_bess + grid_to_bess + dn_share_kw * dt_h
    out["invariant_b5_power_budget_excess_kwh"] = float(
        max(
            (lhs_up - budget).max(initial=0.0),
            (lhs_dn - budget).max(initial=0.0),
            0.0,
        )
    )

    # INV-B6 is vacuous when balancing is ON (the property is anchored
    # at the OFF case in the test contract); keep the residual at 0.
    return out


def verify_dispatch_invariants(
    res: pd.DataFrame,
    params: dict[str, Any],
    *,
    mode: str | None = None,
    tol_kwh: float = ENERGY_TOLERANCE,
) -> dict[str, float]:
    """Check the dispatch invariants on a solved dispatch.

    Verifies the nine general-dispatch invariants plus, when the
    balancing block fired, the six INV-B1..INV-B6 balancing-market
    invariants.  Returns a dict of named residuals; ``main.py``'s
    ``--strict`` mode rejects any residual above the energy tolerance.

    Returns
    -------
    dict[str, float]
        Nine general-dispatch keys:

        * ``invariant_1_pv_balance_kwh``
        * ``invariant_2_load_balance_kwh`` (self_consumption only; 0.0 in merchant)
        * ``invariant_3_soc_dynamics_kwh``
        * ``invariant_4_rte_bound_excess_kwh``
        * ``invariant_5_no_sim_grid_io_max_product_kwh2`` (self_consumption only)
        * ``invariant_6_load_priority_violations`` (self_consumption only)
        * ``invariant_7_curtail_behavior_kwh`` (BOTH modes)
        * ``invariant_8_soc_closed_cycle_kwh`` (when terminal_soc_equal)
        * ``invariant_9_pv_load_priority_kwh`` (self_consumption only; Section 2)

        Plus six balancing-invariant keys (always present; zero when the
        balancing block did not fire):

        * ``invariant_b1_capacity_share_sum_pct_excess`` (DAM + per-product shares <= 100 %)
        * ``invariant_b2_reservation_share_cap_excess_kw`` (per-step reservation <= share)
        * ``invariant_b3_soc_headroom_up_excess_kwh``
        * ``invariant_b4_soc_headroom_dn_excess_kwh``
        * ``invariant_b5_power_budget_excess_kwh`` (per-direction power budget)
        * ``invariant_b6_off_invariants_max_residual`` (worst general residual when OFF)
    """
    if mode is None:
        mode = resolve_mode(params)
    eta_c = float(params.get("efficiency_charge", 1.0) or 1.0)
    eta_d = float(params.get("efficiency_discharge", 1.0) or 1.0)

    pv = res["pv_kwh"].to_numpy(dtype=float)
    pv_to_load = res["pv_to_load_kwh"].to_numpy(dtype=float)
    pv_to_bess = res["pv_to_bess_kwh"].to_numpy(dtype=float)
    pv_to_grid = res["pv_to_grid_kwh"].to_numpy(dtype=float)
    pv_curtail = res["pv_curtail_kwh"].to_numpy(dtype=float)
    grid_to_bess = res["bess_charge_grid_kwh"].to_numpy(dtype=float)
    bess_dis_load = res["bess_dis_load_kwh"].to_numpy(dtype=float)
    bess_dis_grid = res["bess_dis_grid_kwh"].to_numpy(dtype=float)
    grid_to_load = res["grid_to_load_kwh"].to_numpy(dtype=float)
    soc = res["soc_kwh"].to_numpy(dtype=float)
    load = res["load_kwh"].to_numpy(dtype=float)

    if len(pv):
        inv_1 = float(abs(pv - pv_to_load - pv_to_bess - pv_to_grid - pv_curtail).max())
    else:
        inv_1 = 0.0

    if mode == "self_consumption":
        inv_2 = float(abs(load - pv_to_load - bess_dis_load - grid_to_load).max())
    else:
        inv_2 = 0.0

    # Reuse the balancing SOC drift helper across the three per-step
    # checks that depend on it (B3 dynamics, B4 rte bound, B8 closed cycle).
    bm_drift = _balancing_soc_drift(res, params)

    if len(soc) >= 2:
        expected_delta = (
            eta_c * (pv_to_bess[:-1] + grid_to_bess[:-1])
            - (bess_dis_load[:-1] + bess_dis_grid[:-1]) / eta_d
        )
        if bm_drift is not None:
            expected_delta = expected_delta + bm_drift[:-1]
        actual_delta = soc[1:] - soc[:-1]
        inv_3 = float(abs(actual_delta - expected_delta).max())
    else:
        inv_3 = 0.0

    total_charge = float((pv_to_bess + grid_to_bess).sum())
    total_discharge = float((bess_dis_load + bess_dis_grid).sum())
    soc0 = float(soc[0]) if len(soc) else 0.0
    if len(soc):
        final_state = (
            float(soc[-1])
            + eta_c * (float(pv_to_bess[-1]) + float(grid_to_bess[-1]))
            - (float(bess_dis_load[-1]) + float(bess_dis_grid[-1])) / eta_d
        )
        if bm_drift is not None:
            final_state += float(bm_drift[-1])
    else:
        final_state = 0.0
    # The activation drift acts on SOC in addition to DAM charge /
    # discharge, so the rte bound must absorb the total expected drift.
    drift_total = float(bm_drift.sum()) if bm_drift is not None else 0.0
    rte_bound = (
        eta_c * eta_d * total_charge
        + eta_d * (soc0 - final_state)
        + eta_d * drift_total
    )
    inv_4 = float(max(0.0, total_discharge - rte_bound))

    if mode == "self_consumption":
        grid_imp = grid_to_load + grid_to_bess
        grid_exp = pv_to_grid + bess_dis_grid
        inv_5 = float((grid_imp * grid_exp).max() if len(grid_imp) else 0.0)
    else:
        inv_5 = 0.0

    if mode == "self_consumption":
        export = pv_to_grid + bess_dis_grid
        violations = int(((export > tol_kwh) & (grid_to_load > tol_kwh)).sum())
        inv_6 = float(violations)
    else:
        inv_6 = 0.0

    # Invariant 7 — curtail behavior, checked in BOTH modes per spec:
    # cap not binding ⇒ curtail = 0.  Cap is per-step.  The residual must be
    # measured against the quantity the EXPORT_CAP actually binds on, i.e.
    # grid_injection_total: surplus export (pv_to_grid + bess_dis_grid) in the
    # default mode, but TOTAL plant injection (load-serving flows plus surplus)
    # under grid_cap_includes_load.  Using surplus export alone would report a
    # spurious "not binding" residual in the strict mode, where the cap is hit
    # by the total injection while surplus export still sits below it.
    # grid_injection_total_kwh == grid_export_total_kwh in the default mode, so
    # the default-mode result is unchanged.
    if "grid_export_cap_kwh" in res.columns:
        cap = res["grid_export_cap_kwh"].to_numpy(dtype=float)
    else:
        cap = np.zeros_like(pv_to_grid)
    if "grid_injection_total_kwh" in res.columns:
        cap_basis = res["grid_injection_total_kwh"].to_numpy(dtype=float)
    else:
        cap_basis = pv_to_grid + bess_dis_grid
    cap_residual = cap - cap_basis
    not_binding_violation = float(
        ((cap_residual > tol_kwh) & (pv_curtail > tol_kwh)).astype(float).sum()
    )
    inv_7 = not_binding_violation

    if params.get("terminal_soc_equal", True):
        if len(soc):
            final_state = (
                soc[-1]
                + eta_c * (pv_to_bess[-1] + grid_to_bess[-1])
                - (bess_dis_load[-1] + bess_dis_grid[-1]) / eta_d
            )
            if bm_drift is not None:
                final_state += float(bm_drift[-1])
            inv_8 = float(abs(final_state - soc[0]))
        else:
            inv_8 = 0.0
    else:
        inv_8 = 0.0

    # Invariant 9 — Section 2 of the spec: pv_to_load == its priority floor.
    # Default mode: min(pv, load).  Strict total-injection cap
    # (grid_cap_includes_load): min(pv, load, cap) — load priority is still
    # exact, but bounded by the per-step injection cap it must share.
    if mode == "self_consumption" and len(pv):
        strict_cap = bool(params.get("grid_cap_includes_load", False))
        if strict_cap and "grid_export_cap_kwh" in res.columns:
            cap_inj = res["grid_export_cap_kwh"].to_numpy(dtype=float)
            pv_load_priority = np.minimum(np.minimum(pv, load), cap_inj)
        else:
            pv_load_priority = np.minimum(pv, load)
        inv_9 = float(abs(pv_to_load - pv_load_priority).max())
    else:
        inv_9 = 0.0

    general_invariants: dict[str, float] = {
        "invariant_1_pv_balance_kwh": inv_1,
        "invariant_2_load_balance_kwh": inv_2,
        "invariant_3_soc_dynamics_kwh": inv_3,
        "invariant_4_rte_bound_excess_kwh": inv_4,
        "invariant_5_no_sim_grid_io_max_product_kwh2": inv_5,
        "invariant_6_load_priority_violations": inv_6,
        "invariant_7_curtail_behavior_kwh": inv_7,
        "invariant_8_soc_closed_cycle_kwh": inv_8,
        "invariant_9_pv_load_priority_kwh": inv_9,
    }
    balancing_invariants = _balancing_invariants(
        res, params,
        general_invariants=general_invariants,
    )
    return {**general_invariants, **balancing_invariants}
