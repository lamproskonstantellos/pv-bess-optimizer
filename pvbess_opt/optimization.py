"""Pyomo MILP for PV + BESS dispatch.

Two regulatory regimes are supported via the ``mode`` parameter:

* ``self_consumption`` — self-consumption with co-located load.  Strictly enforced rules:
  load balance, hard PV→load priority (Section 2 of the spec), no
  simultaneous grid I/O (tight big-M), retail tariff for self-
  consumption, DAM for export.  A binary-free slack additionally
  enforces surplus-only export (Section 5).
* ``merchant`` — pure utility-scale dispatch with **no co-located load**.
  Load balance NOT enforced; load priority NOT enforced; the
  ``pv_to_load`` / ``bess_dis_load`` / ``grid_to_load`` flows are pinned
  to zero.  The static curtailment cap STILL applies (regulatory
  user-configured grid-connection limit).

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

* ``M_imp = (load_max + bess_power_kw × dt_h) × 1.001`` (tightened to
  the finite grid-import cap ``p_grid_import_max_kw × dt_h × 1.001``
  when that is smaller — Eq. S35)
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
import math
import tempfile
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
    activation_probability_curve,
    capacity_share_kw,
    resolve_balancing_config,
    resolve_balancing_timeseries,
)
from .constants import DEFAULT_MAX_INJECTION_PCT_HOURLY
from .intraday import (
    DA_POSITION_COLUMNS,
    IntradayConfig,
    resolve_intraday_config,
)
from .kpis import ENERGY_TOLERANCE, _balancing_soc_drift
from .max_injection import build_per_step_max_injection_frac
from .modes import resolve_mode
from .ppa import resolve_ppa_config
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

# Tiny tie-breaker on traded intraday volume so a zero-spread step
# deterministically trades nothing (Eq. I3 gains 0 there and the
# complementarity alone leaves one-sided volume degenerate).  An order
# of magnitude below the curtail tie-break; a spread must exceed
# ~0.001 EUR/MWh to beat it, far under any economic signal.
_WEIGHT_ID_TIEBREAK_EUR_PER_KWH: float = 1.0e-6

# Once-per-process latch for the merchant grid_cap_includes_load no-op
# warning — build_model runs per rolling-horizon window, so an unlatched
# warning would repeat hundreds of times per Monte Carlo seed.
_MERCHANT_CAP_FLAG_WARNED = False

# Same latch pattern for a grid-charging fee that can never bind because
# grid charging itself is disallowed (Eq. E26 wedge set, but
# allow_bess_grid_charging is FALSE).
_GRID_FEE_INERT_WARNED = False


def _warn_inert_grid_fee() -> None:
    global _GRID_FEE_INERT_WARNED
    if _GRID_FEE_INERT_WARNED:
        return
    logger.warning(
        "grid_charging_fee_eur_per_mwh is set but "
        "allow_bess_grid_charging is FALSE: the BESS never grid-charges, "
        "so the charging-side wedge (Eq. E26) cannot bind. Set "
        "allow_bess_grid_charging to TRUE or remove the fee."
    )
    _GRID_FEE_INERT_WARNED = True

# Battery wear cost (cycle degradation) is a per-MWh-throughput penalty
# read from params['bess_wear_cost_eur_per_mwh'] (default 0 = off) and
# subtracted in the objective; see pvbess_opt.degradation for the
# calibration helper.

# Penalty on missing the rolling-horizon year-close SOC target, per kWh
# of shortfall.  Far above any plausible energy value (DAM/retail are
# ~0.05-0.30 EUR/kWh), so the shortfall stays at the physical minimum:
# it activates ONLY when the target is unreachable (e.g. a winter year
# end where the remaining PV surplus cannot recharge the battery and
# grid charging is off), where a hard equality would be infeasible.
YEAR_CLOSE_SHORTFALL_PENALTY_EUR_PER_KWH: float = 10.0


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

#: Memory-safety defaults applied per solver on top of the standard
#: knobs.  ``NodefileStart`` makes Gurobi spill the branch-and-bound
#: tree to disk (compressed node files under ``NodefileDir``) once the
#: in-memory tree exceeds the threshold in GB, instead of letting the
#: OS kill the process on memory exhaustion.  Node files are
#: transparent to the search: the branching decisions, incumbents and
#: bounds are identical with or without them -- below the threshold the
#: parameter is entirely dormant, above it only node storage moves to
#: disk (slower, but alive).
_SOLVER_MEMORY_DEFAULTS: dict[str, dict[str, Any]] = {
    "gurobi": {
        "NodefileStart": 8,
        "NodefileDir": tempfile.gettempdir(),
    },
}


def _probe_solver(name: str):
    """Return the Pyomo solver object when ``name`` is usable, else None.

    Any probe failure (unknown plugin, missing binary, licence error --
    Pyomo raises a mix of ApplicationError / RuntimeError / OSError
    depending on the path) means the same thing: not available.
    """
    try:
        solver = pyo.SolverFactory(name)
        if solver is not None and solver.available():
            return solver
    except Exception as exc:  # the availability probe must never crash
        logger.debug("solver %s unavailable: %s", name, exc)
    return None


def choose_solver(name: str | None):
    """Return the REQUESTED Pyomo solver, or fail fast.

    The solver is part of the results' provenance (`[verify] solver=`
    in the run log, the SUMMARY.md header, and any solver statement in
    a publication), so an unavailable request is a hard error rather
    than a silent substitution: a run asked to use Gurobi must never
    quietly produce HiGHS results.  The error lists the solvers that
    ARE installed so the fix is one flag (or one install) away.
    """
    requested = (name or "highs").lower()
    solver = _probe_solver(requested)
    if solver is not None:
        return solver, requested
    installed = [
        candidate for candidate in ("gurobi", "highs", "cbc")
        if candidate != requested and _probe_solver(candidate) is not None
    ]
    hint = (
        f"installed alternatives: {', '.join(installed)}"
        if installed else "no other LP/MIP solver is installed either"
    )
    raise RuntimeError(
        f"Requested solver {requested!r} is not available ({hint}). "
        "The solver is part of the results' provenance, so it is never "
        "substituted silently: install the requested solver (Gurobi "
        "needs 'pip install gurobipy' plus a licence; HiGHS needs "
        "'pip install highspy') or pass --solver with an installed one."
    )


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
    for key, value in _SOLVER_MEMORY_DEFAULTS.get(
        solver_name.lower(), {},
    ).items():
        solver.options[key] = value


def _has_feasible_incumbent(model: pyo.ConcreteModel | None) -> bool:
    """Return True when the model carries a loaded (feasible) solution.

    Probes the SOC variable specifically — every active scenario in this
    codebase declares ``model.soc`` (the SOC trajectory) and the SOC
    variable is always loaded from the solver's incumbent when a
    feasible solution exists.  An unloaded model returns ``None`` for
    ``var.value``, which we treat as "no incumbent".  Probing a named
    variable instead of "first var encountered via
    ``component_data_objects``" makes the check robust to refactors that
    change the declaration order.
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


def _resolve_intraday_inputs(
    params: dict[str, Any],
    ts: pd.DataFrame,
    *,
    mode: str,
) -> tuple[IntradayConfig, dict[str, dict[int, float]] | None, bool]:
    """Return the parsed intraday config, per-step data, and an active flag.

    The intraday block attaches ONLY on the Stage-2 solve of a two-stage
    run: ``id_enabled`` must be set, the mode merchant, and the four
    committed day-ahead position columns (Eq. I1) plus the IDA price
    present in the timeseries.  A Stage-1 solve of the same run carries
    the price but not the position columns, so it builds the unchanged
    day-ahead model — bit-identical topology.
    """
    cfg = resolve_intraday_config(params.get("intraday") or {})
    if not cfg.id_enabled or mode != "merchant":
        return cfg, None, False
    if cfg.id_max_deviation_frac_of_cap <= 0.0:
        # Zero deviation budget disables trading; the caller skips the
        # Stage-2 solve (see intraday.redispatch_intraday), and a
        # zero-slack equality block would only inject infeasibility.
        return cfg, None, False
    if "ida_price_eur_per_mwh" not in ts.columns:
        return cfg, None, False
    if any(col not in ts.columns for col in DA_POSITION_COLUMNS):
        return cfg, None, False
    time_index = range(len(ts))
    data = {
        "ida_price": {
            t: float(ts.loc[t, "ida_price_eur_per_mwh"])
            if pd.notna(ts.loc[t, "ida_price_eur_per_mwh"]) else 0.0
            for t in time_index
        },
        "da_pv_export": {
            t: float(ts.loc[t, "id_da_pv_export_kwh"]) for t in time_index
        },
        "da_bess_export": {
            t: float(ts.loc[t, "id_da_bess_export_kwh"]) for t in time_index
        },
        "da_grid_charge": {
            t: float(ts.loc[t, "id_da_grid_charge_kwh"]) for t in time_index
        },
    }
    return cfg, data, True


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

    # A finite import cap (Eq. S35) is a valid upper bound on the
    # NO_SIM_GRID_IMPORT left-hand side (exactly the capped sum), so it
    # tightens M_imp; with the cap unlimited the expression is untouched
    # (bit-identity for cap-absent runs).
    m_imp = (load_max + p_bess * dt_h) * 1.001
    raw_import_cap = params.get("p_grid_import_max_kw")
    p_import = (
        float("inf") if raw_import_cap is None else float(raw_import_cap)
    )
    if np.isfinite(p_import):
        m_imp = min(m_imp, p_import * dt_h * 1.001)

    return {
        "M_imp": m_imp,
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
    terminal_soc_target_kwh: float | None = None,
    annual_cycle_budget_kwh: float | None = None,
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
    terminal_soc_target_kwh
        If supplied, pins the post-final-step SOC to this explicit kWh
        value instead of ``soc[0]`` (overrides ``terminal_soc_free``).
        Used by the rolling-horizon dispatcher on the window(s) that
        reach the end of the horizon: the year-initial SOC is passed in
        so the stitched dispatch honours the same closed-cycle condition
        as the annual perfect-foresight benchmark — without it the last
        window drains the battery and the foresight comparison is
        biased in the rolling horizon's favour.
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
    if cap_includes_load and mode != "self_consumption":
        # Merchant has no co-located load, so the strict total-injection
        # basis collapses to surplus export — the flag is a clean no-op.
        # Say so once, loudly, so a user who set it expecting an effect
        # is not silently ignored.
        global _MERCHANT_CAP_FLAG_WARNED
        if not _MERCHANT_CAP_FLAG_WARNED:
            logger.warning(
                "grid_cap_includes_load=True has no effect in merchant "
                "mode: there is no co-located load, so the injection cap "
                "already binds on total (surplus) export. The flag is "
                "ignored."
            )
            _MERCHANT_CAP_FLAG_WARNED = True

    if pd.api.types.is_datetime64_any_dtype(ts["timestamp"]):
        day_labels = ts["timestamp"].dt.date.tolist()
    else:
        day_labels = ["oneday"] * n_steps
    unique_days = list(pd.Index(day_labels).unique())
    day_to_idx = {d: [i for i, label in enumerate(day_labels) if label == d] for d in unique_days}

    if mode == "self_consumption" and "load_kwh" not in ts.columns:
        # The workbook loader normally raises this in
        # io._normalise_timeseries; guard direct build_model callers too so
        # self_consumption never silently optimises against zero load (the
        # behaviour the self-consumption design doc promises).
        raise ValueError(
            "self_consumption mode requires a 'load_kwh' column in the "
            "timeseries; none was provided."
        )
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
    # Per-step exogenous-curtailment signal (Eq. E48 companion, the
    # re-dispatch mode): a [0, 1] share multiplying the export caps so
    # the MILP charges or curtails around operator curtailment instead
    # of spilling at the derate stage.  Absent column = factor 1
    # everywhere (bit-identical); mutual exclusivity with the quota
    # keys is enforced by the loader.
    if "curtailment_signal" in ts.columns:
        _signal = {
            t: min(1.0, max(0.0, float(ts.loc[t, "curtailment_signal"])))
            for t in time_index
        }
        export_cap_kwh_per_step = {
            t: export_cap_kwh_per_step[t] * _signal[t] for t in time_index
        }
        if export_cap_pv_kwh_per_step is not None:
            export_cap_pv_kwh_per_step = {
                t: export_cap_pv_kwh_per_step[t] * _signal[t]
                for t in time_index
            }
        if export_cap_bess_kwh_per_step is not None:
            export_cap_bess_kwh_per_step = {
                t: export_cap_bess_kwh_per_step[t] * _signal[t]
                for t in time_index
            }
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

    # Connection-point import cap (Eq. S35): inf / absent = unlimited
    # (no constraint attached below — model topology unchanged).
    raw_import_cap = params.get("p_grid_import_max_kw")
    p_import = (
        float("inf") if raw_import_cap is None else float(raw_import_cap)
    )
    if np.isfinite(p_import) and mode == "self_consumption":
        # Defense-in-depth re-check of the loader's infeasibility
        # certificate (io.read_workbook) for direct build_model callers:
        # a step whose load exceeds PV + BESS power + the cap makes
        # LOAD_BAL infeasible for every state of charge.
        p_bess_cert = float(params.get("bess_power_kw", 0.0) or 0.0)
        for t in time_index:
            if load[t] > pv[t] + (p_bess_cert + p_import) * dt_h + 1e-9:
                raise ValueError(
                    "p_grid_import_max_kw makes the load balance "
                    f"infeasible at step {t}: load {load[t]:.1f} kWh "
                    f"exceeds PV {pv[t]:.1f} kWh + BESS power "
                    f"{p_bess_cert:.0f} kW + import cap {p_import:.0f} "
                    "kW for the step regardless of the battery state "
                    "of charge."
                )
    big_m = derive_tight_big_m(params, ts, dt_h=dt_h, mode=mode)

    # Intraday Stage-2 inputs (Eqs. I1-I5) — resolved up-front because
    # the cycle caps below need the committed day-ahead discharge; the
    # block's variables and constraints attach before the objective.
    intraday_cfg, intraday_data, intraday_active = _resolve_intraday_inputs(
        params, ts, mode=mode,
    )

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
    # Merit-order activation curve (Eq. B10): per-step deterministic
    # beta_k(t) coefficients keep the MILP linear.  None (default)
    # keeps the CONSTANT-beta code path at every consumer below, so a
    # disabled curve is bit-identical (the constant form multiplies
    # the summed expression once; distributing a per-step coefficient
    # would change floating-point association).
    _beta_merit: dict[str, Any] | None = None
    if balancing_active and getattr(
        balancing_cfg, "bm_merit_order_enabled", False,
    ):
        _merit_curve = (
            (params.get("balancing") or {}).get("bm_merit_order_curve")
            or None
        )
        if _merit_curve:
            _beta_merit = {
                k: activation_probability_curve(
                    balancing_cfg, _merit_curve, k,
                    getattr(
                        balancing_ts,
                        f"{k}_activation_price_eur_per_mwh",
                    ),
                )
                for k in PRODUCTS_WITH_ACTIVATION
            }
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
        # Multi-hour reservation blocks (Eq. B9): European capacity
        # auctions clear in blocks (e.g. 4 h), so every per-product
        # reservation is pinned to its block-anchor value — a pure
        # restriction of the per-step feasible set; the power-budget,
        # SOC-headroom constraints and the objective are untouched.
        # Blocks anchor on the hour-of-year of the step's timestamp, so
        # rolling-horizon windows that bisect a block stay aligned with
        # the year grid instead of drifting per window (the committed
        # prefix fixes the block level).
        block_hours = int(
            getattr(balancing_cfg, "bm_block_hours", 0) or 0
        )
        if block_hours > 0:
            if pd.api.types.is_datetime64_any_dtype(ts["timestamp"]):
                _stamps = pd.to_datetime(ts["timestamp"])
                _hours = (
                    (_stamps.dt.dayofyear - 1) * 24
                    + _stamps.dt.hour
                    + _stamps.dt.minute / 60.0
                ).to_numpy(dtype=float)
            else:
                _hours = np.arange(n_steps, dtype=float) * dt_h
            _block_ids = np.floor(_hours / block_hours + 1e-9).astype(int)
            _anchor_of_block: dict[int, int] = {}
            for _t in range(n_steps):
                _anchor_of_block.setdefault(int(_block_ids[_t]), _t)
            m.BM_BLOCK_LINK = pyo.ConstraintList()
            for _t in range(n_steps):
                _a = _anchor_of_block[int(_block_ids[_t])]
                if _a == _t:
                    continue
                for k in PRODUCTS_ALL:
                    m.BM_BLOCK_LINK.add(
                        m.r_balancing[k, _t] == m.r_balancing[k, _a]
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
        if balancing_active and _beta_merit is not None:
            act_charge = eta_c * dt_h * sum(
                acceptance_probability(balancing_cfg, k)
                * float(_beta_merit[k][t]) * m.r_balancing[k, t]
                for k in PRODUCTS_DN
            )
            act_discharge = (dt_h / eta_d) * sum(
                acceptance_probability(balancing_cfg, k)
                * float(_beta_merit[k][t]) * m.r_balancing[k, t]
                for k in PRODUCTS_UP
            )
        elif balancing_active:
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
        if balancing_active and _beta_merit is not None:
            t_final = n_steps - 1
            final_act_charge = eta_c * dt_h * sum(
                acceptance_probability(balancing_cfg, k)
                * float(_beta_merit[k][t_final]) * m.r_balancing[k, t_final]
                for k in PRODUCTS_DN
            )
            final_act_discharge = (dt_h / eta_d) * sum(
                acceptance_probability(balancing_cfg, k)
                * float(_beta_merit[k][t_final]) * m.r_balancing[k, t_final]
                for k in PRODUCTS_UP
            )
        elif balancing_active:
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
        if terminal_soc_target_kwh is not None:
            # Rolling-horizon year-close: the window's own soc[0] is the
            # carried-over SOC, so the closed-cycle condition must pin the
            # terminal state to the explicit year-initial target instead.
            # The target is relaxed by a heavily penalised shortfall
            # variable: a hard equality is infeasible when the remaining
            # horizon physically cannot recharge to the target (winter
            # year end, surplus-only charging).  The penalty keeps the
            # shortfall at its physical minimum (zero whenever the
            # target is reachable), so the reachable case is unchanged.
            m.year_close_shortfall = pyo.Var(
                domain=pyo.NonNegativeReals,
                bounds=(0.0, float(params["soc_max_frac"]) * e_cap_param),
            )
            m.SOC_TERM = pyo.Constraint(
                expr=final_soc_expr
                == float(terminal_soc_target_kwh) - m.year_close_shortfall,
            )
        elif not terminal_soc_free:
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
        # Stage-2 headroom: a stitched rolling-horizon commitment can
        # exceed the daily cap across window seams (each window caps
        # its OWN partial-day slice), so the pinned day-ahead discharge
        # lifts the day's budget to the committed level — the
        # re-dispatch can never ADD cycling beyond the operational cap,
        # and the pinned schedule stays feasible.  Inactive venue (or a
        # cap-respecting deterministic commitment) leaves the cap
        # bit-identical.
        m.CYC = pyo.ConstraintList()
        for indices in day_to_idx.values():
            lhs = sum(m.bess_dis_load[t] + m.bess_dis_grid[t] for t in indices)
            _cap_day = float(params["max_cycles_per_day"]) * e_cap_param
            if intraday_active:
                assert intraday_data is not None
                _cap_day = max(
                    _cap_day,
                    sum(
                        max(intraday_data["da_bess_export"][t], 0.0)
                        for t in indices
                    ),
                )
            m.CYC.add(lhs <= _cap_day)

        # --- Annual throughput cap (Eq. E46) ----------------------------------
        # Warranty limits are quoted in cycles per YEAR; the Year-1
        # constraint is sufficient because nameplate- and faded-basis
        # coincide at the Year-1 factor of 1 and the projected years
        # are checked analytically (Eq. E47, degradation report).
        raw_annual_cycles = params.get("max_cycles_per_year")
        max_cycles_per_year = (
            0.0 if raw_annual_cycles is None else float(raw_annual_cycles)
        )
        # ``annual_cycle_budget_kwh`` (supplied only by the rolling horizon)
        # overrides the nominal ``max_cycles_per_year * e_cap`` with the
        # budget REMAINING for this window, so the annual warranty cap is
        # enforced ACROSS window seams rather than per window — each 48h
        # window otherwise sees the full annual budget and the cap never
        # binds, letting the stitched dispatch exceed it (and beat the
        # perfect-foresight benchmark).  A ``None`` budget with a positive
        # ``max_cycles_per_year`` keeps the historical full-year cap, so the
        # single-solve (benchmark / non-rolling) path is bit-identical.
        _cap_annual: float | None
        if annual_cycle_budget_kwh is not None:
            _cap_annual = float(annual_cycle_budget_kwh)
        elif max_cycles_per_year > 0.0:
            _cap_annual = max_cycles_per_year * e_cap_param
        else:
            _cap_annual = None
        if _cap_annual is not None:
            if intraday_active:
                # Same committed-schedule headroom as the daily cap.
                assert intraday_data is not None
                _cap_annual = max(
                    _cap_annual,
                    sum(
                        max(intraday_data["da_bess_export"][t], 0.0)
                        for t in range(n_steps)
                    ),
                )
            m.CYC_ANNUAL = pyo.Constraint(expr=(
                sum(
                    m.bess_dis_load[t] + m.bess_dis_grid[t]
                    for t in range(n_steps)
                )
                <= _cap_annual
            ))

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

    # Connection-point import cap (Eq. S35): grid-to-load plus
    # grid-to-BESS charging per step.  A direct <= to a constant like
    # EXPORT_CAP (no big-M); attached ONLY when the cap is finite, so an
    # absent / unlimited key leaves the model topology bit-identical.
    # Merchant mode pins grid_to_load to zero, so the cap collapses to a
    # grid-charging power limit there (inert without
    # allow_bess_grid_charging).
    if np.isfinite(p_import):
        import_cap_kwh = float(p_import * dt_h)
        m.IMPORT_CAP = pyo.Constraint(
            m.T,
            rule=lambda m, t: (
                m.grid_to_load[t] + m.grid_to_bess[t] <= import_cap_kwh
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
        # NB: the component is named ``export_slack`` (not ``slack``) on
        # purpose — pyomo's APPSI result loader treats a model attribute
        # literally named ``slack`` as a reserved import Suffix and calls
        # ``.import_enabled()`` on it, which crashes on a decision Var
        # (``'IndexedVar' object has no attribute 'import_enabled'``) and
        # made ``--solver appsi_highs`` unusable in self_consumption mode.
        m.export_slack = pyo.Var(m.T, domain=pyo.NonNegativeReals)
        m.LOAD_PRIORITY_SLACK_DEF = pyo.Constraint(
            m.T,
            rule=lambda m, t: (
                m.export_slack[t]
                >= pv[t] + m.bess_dis_load[t] + m.bess_dis_grid[t] - load[t]
            ),
        )
        m.LOAD_PRIORITY_EXPORT = pyo.Constraint(
            m.T,
            rule=lambda m, t: (
                m.pv_to_grid[t] + m.bess_dis_grid[t] <= m.export_slack[t]
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

    # --- Intraday re-dispatch block (Stage 2 only; Eqs. I1-I5) ------------
    # Attaches only when the committed day-ahead position is pinned in
    # the timeseries (see _resolve_intraday_inputs): the Stage-1 solve
    # of a two-stage run builds the unchanged day-ahead model.  Every
    # intraday trade is a physical flow change (Eq. I5): the linking
    # constraints equate each origin's Stage-2 flow to its committed
    # day-ahead leg plus the origin's intraday delta, so the unchanged
    # EXPORT_CAP / SOC / charge-limit families bound the combined
    # DA + ID operation (the S15/S16 basis is reused, Eq. I2).
    if intraday_active:
        assert intraday_data is not None
        if not np.isfinite(p_export) or p_export <= 0.0:
            # The loader rejects this combination; re-checked for
            # direct build_model callers because the deviation cap is
            # defined as a fraction of the export cap.
            raise ValueError(
                "id_enabled requires a finite positive "
                "p_grid_export_max_kw (the intraday deviation cap is a "
                "fraction of it)."
            )
        _ida_price = intraday_data["ida_price"]
        _da_pv = intraday_data["da_pv_export"]
        _da_bess = intraday_data["da_bess_export"]
        _da_charge = intraday_data["da_grid_charge"]
        # Per-step deviation budget (Eq. I2): a fraction of the
        # connection-cap energy per step.  Also the tight big-M of the
        # sell/buy gates below.
        id_dev_cap_kwh = float(
            intraday_cfg.id_max_deviation_frac_of_cap * p_export * dt_h
        )
        m.id_sell_pv = pyo.Var(
            m.T, domain=pyo.NonNegativeReals, bounds=(0.0, id_dev_cap_kwh),
        )
        m.id_sell_bess = pyo.Var(
            m.T, domain=pyo.NonNegativeReals, bounds=(0.0, id_dev_cap_kwh),
        )
        m.id_buy_pv = pyo.Var(
            m.T, domain=pyo.NonNegativeReals, bounds=(0.0, id_dev_cap_kwh),
        )
        m.id_buy_bess = pyo.Var(
            m.T, domain=pyo.NonNegativeReals, bounds=(0.0, id_dev_cap_kwh),
        )
        # Origin linking (Eqs. I1/I4): PV-origin export deviates from
        # the committed PV leg by the PV-side trades; the BESS-origin
        # net position (discharge minus grid charge) deviates from its
        # committed net leg by the BESS-side trades.  Summing both
        # recovers the net-position identity
        # g_t - g_DA_t = id_sell_t - id_buy_t.
        m.ID_LINK_PV = pyo.Constraint(
            m.T,
            rule=lambda m, t: (
                m.pv_to_grid[t]
                == _da_pv[t] + m.id_sell_pv[t] - m.id_buy_pv[t]
            ),
        )
        m.ID_LINK_BESS = pyo.Constraint(
            m.T,
            rule=lambda m, t: (
                m.bess_dis_grid[t] - m.grid_to_bess[t]
                == (_da_bess[t] - _da_charge[t])
                + m.id_sell_bess[t] - m.id_buy_bess[t]
            ),
        )
        # Deviation cap + no-wash-trading complementarity (Eqs. I2/I5):
        # one binary per step gates sells and buys to disjoint steps,
        # and the shared big-M IS the deviation budget, so the pair
        # jointly enforces id_sell_t + id_buy_t <= delta * P^G * dt.
        m.y_id = pyo.Var(m.T, domain=pyo.Binary)
        m.ID_SELL_GATE = pyo.Constraint(
            m.T,
            rule=lambda m, t: (
                m.id_sell_pv[t] + m.id_sell_bess[t]
                <= id_dev_cap_kwh * m.y_id[t]
            ),
        )
        m.ID_BUY_GATE = pyo.Constraint(
            m.T,
            rule=lambda m, t: (
                m.id_buy_pv[t] + m.id_buy_bess[t]
                <= id_dev_cap_kwh * (1 - m.y_id[t])
            ),
        )
        if not intraday_cfg.id_allow_purchases:
            m.ID_NO_BUY = pyo.Constraint(
                m.T,
                rule=lambda m, t: m.id_buy_pv[t] + m.id_buy_bess[t] == 0,
            )

    # --- Objective: profit -----------------------------------------------
    curtail_tiebreak_term = _WEIGHT_CURTAIL_TIEBREAK_EUR_PER_KWH * sum(
        m.pv_curtail[t] for t in time_index
    )

    # Pay-as-produced PPA: the covered share s of PV export earns the
    # contract strike instead of (physical) or on top of (CfD) the DAM,
    # and both settlements total (1-s)·DAM + s·strike per exported kWh —
    # one effective PV-export price serves both.  This deliberately lets
    # covered PV keep exporting through negative-DAM hours while the
    # uncovered share curtails (the documented behaviour of
    # generation-settled as-produced contracts; see docs/ppa_design.md)
    # — UNLESS the negative-price suspension clause opts out of it: with
    # ppa_negative_price_rule = 'suspend', every step with DAM < 0
    # (strict, Eq. P6) settles the covered volume at spot too, so the
    # effective export price collapses to the DAM (Eq. P8) and the MILP
    # rationally curtails or routes PV into the BESS instead of
    # exporting at a loss.  The mask derives from THIS call's dam_price,
    # so rolling-horizon windows recompute it per window.
    #
    # The BASELOAD structure deliberately does not reshape this price
    # (Eq. P11): its fixed-volume leg Q_t·(strike − DAM_t) contains no
    # decision variables, so appending it to the objective would be an
    # additive constant — merchant-optimal dispatch is already
    # baseload-optimal, and pv_export_price stays the DAM alias.  This
    # gate changes only if a v2 firming incentive lands (a shortfall
    # variable d_t >= Q_t − delivered_t priced at an asymmetric
    # imbalance premium; recorded in docs/ppa_design.md).
    ppa_cfg = resolve_ppa_config(params.get("ppa"))
    if ppa_cfg.active and ppa_cfg.reshapes_dispatch_price and pv_present:
        s_ppa = ppa_cfg.share_frac
        strike = float(ppa_cfg.ppa_price_eur_per_mwh)
        if ppa_cfg.suspension_active:
            pv_export_price = {
                t: (
                    dam_price[t]
                    if dam_price[t] < 0.0
                    else (1.0 - s_ppa) * dam_price[t] + s_ppa * strike
                )
                for t in time_index
            }
        else:
            pv_export_price = {
                t: (1.0 - s_ppa) * dam_price[t] + s_ppa * strike
                for t in time_index
            }
    else:
        pv_export_price = dam_price

    if mode == "self_consumption":
        avoided_cost = sum(
            retail_price[t] * (m.pv_to_load[t] + m.bess_dis_load[t]) / 1000.0
            for t in time_index
        )
    else:  # merchant
        avoided_cost = 0.0
    export_revenue = sum(
        (
            pv_export_price[t] * m.pv_to_grid[t]
            + dam_price[t] * m.bess_dis_grid[t]
        ) / 1000.0
        for t in time_index
    )

    # Charging-side grid fee (Eq. E26): a regulated EUR/MWh wedge on the
    # buy price of grid-charged energy (network charges + levies on
    # storage charging where not exempt).  It MUST enter the objective —
    # not just the cashflow — because thin arbitrage spreads flip sign
    # with the wedge: dispatch decided on the energy-only price would
    # grid-charge at a real-world loss.
    if bool(params.get("grid_charging_fee_exempt", False)):
        grid_fee_wedge = 0.0
    else:
        grid_fee_wedge = float(
            params.get("grid_charging_fee_eur_per_mwh", 0.0) or 0.0
        )
    if grid_fee_wedge > 0.0 and not bool(
        params.get("allow_bess_grid_charging", False)
    ):
        _warn_inert_grid_fee()
    grid_charge_cost = sum(
        (dam_price[t] + grid_fee_wedge) * m.grid_to_bess[t] / 1000.0
        for t in time_index
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
            price_col = getattr(
                balancing_ts, f"{k}_activation_price_eur_per_mwh",
            )
            if _beta_merit is not None:
                # Eq. B10: the B7/B8 constant beta generalises to the
                # per-step merit-order coefficient beta_k(t).
                _alpha_k = acceptance_probability(balancing_cfg, k)
                act_terms.append(
                    _alpha_k * dt_h * sum(
                        float(_beta_merit[k][t]) * float(price_col[t])
                        * m.r_balancing[k, t]
                        for t in time_index
                    ) / 1000.0
                )
            else:
                alpha_beta = _alpha_beta(balancing_cfg, k)
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

    if intraday_active:
        # Stage-2 settlement in spread form (Eq. I3): the export /
        # grid-charge terms above already price every PHYSICAL flow at
        # the DAM, and dam*physical + (ida-dam)*(sell-buy) ==
        # dam*g_DA + ida*(g - g_DA) — the committed position settles
        # day-ahead, only the deviation trades at the IDA price.  The
        # venue fee (Eq. E59) charges both trade directions; the wear
        # term above already prices the INCREMENTAL Stage-2 throughput
        # because it runs on physical discharge.
        _id_fee = float(intraday_cfg.id_fee_eur_per_mwh)
        m.intraday_margin_expr = pyo.Expression(
            expr=(
                sum(
                    (float(_ida_price[t]) - float(dam_price[t]))
                    * (
                        m.id_sell_pv[t] + m.id_sell_bess[t]
                        - m.id_buy_pv[t] - m.id_buy_bess[t]
                    ) / 1000.0
                    for t in time_index
                )
                - _id_fee * sum(
                    m.id_sell_pv[t] + m.id_sell_bess[t]
                    + m.id_buy_pv[t] + m.id_buy_bess[t]
                    for t in time_index
                ) / 1000.0
            ),
        )
        id_tiebreak_term = _WEIGHT_ID_TIEBREAK_EUR_PER_KWH * sum(
            m.id_sell_pv[t] + m.id_sell_bess[t]
            + m.id_buy_pv[t] + m.id_buy_bess[t]
            for t in time_index
        )
        profit_eur = profit_eur + m.intraday_margin_expr - id_tiebreak_term

    if hasattr(m, "year_close_shortfall"):
        # Missing the year-close SOC target costs far more than any
        # energy price, so the shortfall stays at its physical minimum.
        profit_eur = profit_eur - (
            YEAR_CLOSE_SHORTFALL_PENALTY_EUR_PER_KWH * m.year_close_shortfall
        )

    m.OBJ = pyo.Objective(
        expr=profit_eur - curtail_tiebreak_term, sense=pyo.maximize,
    )

    return m


# ---------------------------------------------------------------------------
# Solve and DataFrame conversion
# ---------------------------------------------------------------------------


def _achieved_gap_from_result(result: Any) -> float | None:
    """Relative optimality gap the solver actually PROVED, or None.

    This is the certified distance between the incumbent and the best
    bound at termination -- distinct from the REQUESTED ``mip_gap``:
    when the time limit binds before the target gap is reached, a
    deterministic solver returns whatever gap it had proven so far
    (e.g. requesting 1e-5 but stopping at 5e-4).  Publications must
    report this achieved gap, not the requested one.

    The dispatch objective is always a MAXIMISE (profit), so Pyomo's
    ``lower_bound`` is the incumbent (best feasible) and ``upper_bound``
    is the relaxation bound.  The gap is ``|upper - lower| / |lower|`` --
    relative to the incumbent -- which reproduces the solver's own
    printed relative gap (e.g. Gurobi's ``gap 0.0516%``) to rounding, so
    the KPI equals what the run log shows.

    This is solver-agnostic: it reads the Pyomo results' problem bounds,
    which Gurobi, HiGHS and CBC all populate.  Returns None -- so callers
    record no certified gap rather than inventing one -- when a bound is
    missing or non-finite (a backend that omits the relaxation bound),
    OR when the incumbent objective is ~0, where a relative gap is not
    meaningful (a tiny absolute bound difference would otherwise blow up
    the ratio; a degenerate scenario whose optimum is zero has no
    meaningful relative certification).
    """
    try:
        problem = result.problem[0]
        lower = problem.lower_bound
        upper = problem.upper_bound
    except (AttributeError, IndexError, TypeError):
        return None
    if lower is None or upper is None:
        return None
    try:
        incumbent = float(lower)
        bound = float(upper)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(incumbent) and math.isfinite(bound)):
        return None
    if abs(incumbent) < 1.0:
        return None
    return abs(bound - incumbent) / abs(incumbent)


def solve_model(
    model: pyo.ConcreteModel,
    solver_name: str,
    *,
    mip_gap: float = 0.001,
    time_limit_seconds: int = 1800,
    tee: bool = False,
) -> tuple[pyo.ConcreteModel, str, float | None]:
    """Solve ``model``; raise on failure.

    Returns ``(model, solver_name, achieved_gap)`` where ``achieved_gap``
    is the relative optimality gap the solver PROVED at termination
    (see :func:`_achieved_gap_from_result`), or None when the backend
    did not report bounds.
    """
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
    achieved_gap = _achieved_gap_from_result(result)
    logger.info(
        "[milp-solve] done: solver=%s elapsed=%.2fs termination=%s "
        "achieved_gap=%s",
        resolved, elapsed, condition,
        f"{achieved_gap:.3g}" if achieved_gap is not None else "n/a",
    )
    for h in logger.handlers + logging.getLogger().handlers:
        h.flush()
    _check_solver_status(result, resolved, model)
    return model, resolved, achieved_gap


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
    # Mirror the build_model signal composition (re-dispatch curtailment
    # mode) so the reported cap column — and invariant_7's headroom test
    # on it — sees the cap the solver actually faced.
    if "curtailment_signal" in ts.columns:
        export_cap_kwh_per_step = export_cap_kwh_per_step * np.clip(
            ts["curtailment_signal"].to_numpy(dtype=float), 0.0, 1.0,
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
    # Import cap (Eq. S35): written ONLY when the cap is finite, so
    # cap-absent dispatch frames stay bit-identical (contrast the export
    # cap column, unconditional because that cap always exists).
    raw_import_cap = params.get("p_grid_import_max_kw")
    p_import = (
        float("inf") if raw_import_cap is None else float(raw_import_cap)
    )
    if np.isfinite(p_import):
        res["grid_import_cap_kwh"] = float(p_import * dt_h)
    # Actual quantity the EXPORT_CAP binds on.  Equals grid_export_total_kwh in
    # the default mode; equals total plant injection (load-serving flows plus
    # surplus export) under grid_cap_includes_load in self_consumption mode.
    res["grid_injection_total_kwh"] = [
        pyo.value(model.grid_injection_total[t]) for t in time_index
    ]
    # Per-source injection caps — present only when the optional profile was
    # supplied.  Surfaced for transparency and consumed by the invariant
    # checks (the PV sub-cap can bind PV injection / the priority floor).
    mi_pv = _resolve_optional_max_injection_per_step(
        params, ts, "max_injection_profile_pv",
    )
    if mi_pv is not None:
        cap_pv = p_export * dt_h * mi_pv
        if "curtailment_signal" in ts.columns:
            cap_pv = cap_pv * np.clip(
                ts["curtailment_signal"].to_numpy(dtype=float), 0.0, 1.0,
            )
        res["grid_export_cap_pv_kwh"] = cap_pv
    mi_bess = _resolve_optional_max_injection_per_step(
        params, ts, "max_injection_profile_bess",
    )
    if mi_bess is not None:
        cap_bess = p_export * dt_h * mi_bess
        if "curtailment_signal" in ts.columns:
            cap_bess = cap_bess * np.clip(
                ts["curtailment_signal"].to_numpy(dtype=float), 0.0, 1.0,
            )
        res["grid_export_cap_bess_kwh"] = cap_bess

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
    # Intraday auction price, echoed only when the venue's column exists
    # so non-intraday dispatch frames stay bit-identical.
    if "ida_price_eur_per_mwh" in ts.columns:
        res["ida_price_eur_per_mwh"] = ts["ida_price_eur_per_mwh"].values

    # Intraday trades (kWh per timestep, Eqs. I2-I5).  Only emitted when
    # the MILP carried the Stage-2 intraday block; the committed
    # day-ahead position columns (Eq. I1) are echoed alongside so the
    # invariants and the settlement re-derivation are self-contained.
    if hasattr(model, "id_sell_pv"):
        res["id_sell_pv_kwh"] = [
            pyo.value(model.id_sell_pv[t]) for t in time_index
        ]
        res["id_sell_bess_kwh"] = [
            pyo.value(model.id_sell_bess[t]) for t in time_index
        ]
        res["id_buy_pv_kwh"] = [
            pyo.value(model.id_buy_pv[t]) for t in time_index
        ]
        res["id_buy_bess_kwh"] = [
            pyo.value(model.id_buy_bess[t]) for t in time_index
        ]
        res["id_buy_kwh"] = [
            pyo.value(model.id_buy_pv[t]) + pyo.value(model.id_buy_bess[t])
            for t in time_index
        ]
        for col in DA_POSITION_COLUMNS:
            res[col] = ts[col].values

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
    terminal_soc_target_kwh: float | None = ...,
    annual_cycle_budget_kwh: float | None = ...,
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
    terminal_soc_target_kwh: float | None = ...,
    annual_cycle_budget_kwh: float | None = ...,
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
    terminal_soc_target_kwh: float | None = None,
    annual_cycle_budget_kwh: float | None = None,
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
        terminal_soc_target_kwh=terminal_soc_target_kwh,
        annual_cycle_budget_kwh=annual_cycle_budget_kwh,
    )
    solved, resolved, achieved_gap = solve_model(
        model, solver_name,
        mip_gap=mip_gap, time_limit_seconds=time_limit_seconds, tee=tee,
    )
    if return_unrounded:
        res_full = model_to_dataframe(solved, ts, params, round_output=False)
        numeric_cols = [c for c in res_full.columns if c != "timestamp"]
        res = res_full.copy()
        res[numeric_cols] = res[numeric_cols].astype(float).round(4)
        # The certified optimality gap rides on the frame's metadata so
        # the public tuple signature is unchanged; the pipeline reads it
        # off the benchmark frame to report pf_benchmark_gap_achieved.
        res.attrs["solver_gap_achieved"] = achieved_gap
        res_full.attrs["solver_gap_achieved"] = achieved_gap
        return res, resolved, res_full
    res = model_to_dataframe(solved, ts, params)
    res.attrs["solver_gap_achieved"] = achieved_gap
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
# downstream consumers (``pvbess_opt.pipeline._check_strict_invariants``)
# can refer to the canonical list without re-declaring the names.
BALANCING_INVARIANT_KEYS: tuple[str, ...] = (
    "invariant_b1_capacity_share_sum_pct_excess",
    "invariant_b2_reservation_share_cap_excess_kw",
    "invariant_b3_soc_headroom_up_excess_kwh",
    "invariant_b4_soc_headroom_dn_excess_kwh",
    "invariant_b5_power_budget_excess_kwh",
    "invariant_b6_off_invariants_max_residual",
)

# Names of the four intraday-invariant residual keys (Eqs. I1-I5).
# Reported as 0.0 (vacuously satisfied) whenever the Stage-2 intraday
# block did not fire — the stable-contract convention of the balancing
# family above.
INTRADAY_INVARIANT_KEYS: tuple[str, ...] = (
    "invariant_i1_position_link_kwh",
    "invariant_i2_deviation_cap_excess_kwh",
    "invariant_i3_sell_buy_overlap_kwh2",
    "invariant_i4_origin_split_kwh",
)


def _intraday_invariants(
    res: pd.DataFrame, params: dict[str, Any],
) -> dict[str, float]:
    """Compute the four INV-I1..INV-I4 intraday-invariant residuals.

    * INV-I1 — net-position link (Eq. I1): the physical net grid
      position deviates from the committed day-ahead position by
      exactly ``id_sell - id_buy`` in every step.
    * INV-I2 — deviation cap (Eq. I2): total traded volume per step
      within ``id_max_deviation_frac_of_cap * p_grid_export_max_kw *
      dt``.
    * INV-I3 — no wash trading (Eq. I5): sells and buys never overlap
      in a step (max of the per-step product, kWh^2 like invariant 5).
    * INV-I4 — origin split (Eq. I4): each origin's Stage-2 flow equals
      its committed day-ahead leg plus the origin's intraday delta.

    Residuals are 0.0 when the dispatch frame carries no intraday
    columns (venue off, or a Stage-1 frame of a two-stage run).
    """
    out: dict[str, float] = {k: 0.0 for k in INTRADAY_INVARIANT_KEYS}
    needed = (
        "id_sell_pv_kwh", "id_sell_bess_kwh",
        "id_buy_pv_kwh", "id_buy_bess_kwh",
        "id_da_position_kwh", "id_da_pv_export_kwh",
        "id_da_bess_export_kwh", "id_da_grid_charge_kwh",
    )
    if any(col not in res.columns for col in needed):
        return out

    def _col(name: str) -> np.ndarray:
        if name in res.columns:
            return res[name].to_numpy(dtype=float)
        return np.zeros(len(res), dtype=float)

    sell = _col("id_sell_pv_kwh") + _col("id_sell_bess_kwh")
    buy = _col("id_buy_pv_kwh") + _col("id_buy_bess_kwh")
    physical_net = (
        _col("pv_to_grid_kwh") + _col("bess_dis_grid_kwh")
        - _col("bess_charge_grid_kwh")
    )
    da_pos = _col("id_da_position_kwh")

    out["invariant_i1_position_link_kwh"] = float(
        np.abs(physical_net - da_pos - (sell - buy)).max(initial=0.0)
    )

    cfg = resolve_intraday_config(params.get("intraday") or {})
    p_export = float(params.get("p_grid_export_max_kw", 0.0) or 0.0)
    dt_h = dt_hours_from(params)
    if np.isfinite(p_export) and p_export > 0.0:
        dev_cap = float(cfg.id_max_deviation_frac_of_cap * p_export * dt_h)
        out["invariant_i2_deviation_cap_excess_kwh"] = float(
            np.maximum(0.0, sell + buy - dev_cap).max(initial=0.0)
        )

    out["invariant_i3_sell_buy_overlap_kwh2"] = float(
        (sell * buy).max(initial=0.0)
    )

    pv_link = np.abs(
        _col("pv_to_grid_kwh") - _col("id_da_pv_export_kwh")
        - _col("id_sell_pv_kwh") + _col("id_buy_pv_kwh")
    )
    bess_link = np.abs(
        (_col("bess_dis_grid_kwh") - _col("bess_charge_grid_kwh"))
        - (_col("id_da_bess_export_kwh") - _col("id_da_grid_charge_kwh"))
        - _col("id_sell_bess_kwh") + _col("id_buy_bess_kwh")
    )
    out["invariant_i4_origin_split_kwh"] = float(
        max(pv_link.max(initial=0.0), bess_link.max(initial=0.0))
    )
    return out


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
    # both keys (io._typed_to_flat).  A silent .get fallback here would let
    # a hand-built ``params`` dict bypass the invariant check that build
    # would have rejected with KeyError.
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

    Verifies the ten general-dispatch invariants plus, when the
    balancing block fired, the six INV-B1..INV-B6 balancing-market
    invariants.  Returns a dict of named residuals; the pipeline's
    ``--strict`` mode rejects any residual above the energy tolerance.

    Returns
    -------
    dict[str, float]
        Ten general-dispatch keys:

        * ``invariant_1_pv_balance_kwh``
        * ``invariant_2_load_balance_kwh`` (self_consumption only; 0.0 in merchant)
        * ``invariant_3_soc_dynamics_kwh``
        * ``invariant_4_rte_bound_excess_kwh``
        * ``invariant_5_no_sim_grid_io_max_product_kwh2`` (self_consumption only)
        * ``invariant_6_load_priority_violations`` (self_consumption only)
        * ``invariant_7_curtail_behavior_count`` (BOTH modes)
        * ``invariant_8_soc_closed_cycle_kwh`` (when terminal_soc_equal)
        * ``invariant_9_pv_load_priority_kwh`` (self_consumption only; Section 2)
        * ``invariant_10_import_cap_excess_kwh`` (Eq. S35; 0.0
          vacuously when the cap is unlimited)

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
    # the default-mode result is unchanged.  A PV sub-cap can also bind PV
    # injection while the combined cap still has slack, so curtailment is
    # legitimate when EITHER cap is binding.
    if "grid_export_cap_kwh" in res.columns:
        cap = res["grid_export_cap_kwh"].to_numpy(dtype=float)
    else:
        cap = np.zeros_like(pv_to_grid)
    if "grid_injection_total_kwh" in res.columns:
        cap_basis = res["grid_injection_total_kwh"].to_numpy(dtype=float)
    else:
        cap_basis = pv_to_grid + bess_dis_grid
    pv_can_inject_more = (cap - cap_basis) > tol_kwh
    if "grid_export_cap_pv_kwh" in res.columns:
        cap_pv = res["grid_export_cap_pv_kwh"].to_numpy(dtype=float)
        strict_cap = (
            mode == "self_consumption"
            and bool(params.get("grid_cap_includes_load", False))
        )
        pv_injection = pv_to_load + pv_to_grid if strict_cap else pv_to_grid
        pv_can_inject_more = pv_can_inject_more & ((cap_pv - pv_injection) > tol_kwh)
    # Curtailment with cap headroom is only anomalous when injecting the
    # curtailed PV would have been profitable.  At a non-positive DAM export
    # price the optimizer curtails surplus PV rather than export at a loss
    # (the curtail tie-breaker resolves the zero-price tie toward export, so a
    # real solve only curtails with headroom when the price is strictly
    # negative); that curtailment is the profit-maximising optimum, not a
    # violation.  Only a strictly positive export price makes idle cap
    # headroom an unambiguous "lazy curtailment" defect.  When the DAM column
    # is absent there is no export revenue, so no curtailment can be anomalous.
    if "dam_price_eur_per_mwh" in res.columns:
        export_profitable = res["dam_price_eur_per_mwh"].to_numpy(dtype=float) > 0.0
    else:
        export_profitable = np.zeros_like(pv_curtail, dtype=bool)
    inv_7 = float(
        (pv_can_inject_more & (pv_curtail > tol_kwh) & export_profitable)
        .astype(float).sum()
    )

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
    # (grid_cap_includes_load): min(pv, load, cap_total, cap_pv) — load
    # priority is still exact, but bounded by the combined cap and (when
    # supplied) the PV sub-cap it must share.
    if mode == "self_consumption" and len(pv):
        strict_cap = bool(params.get("grid_cap_includes_load", False))
        if strict_cap and "grid_export_cap_kwh" in res.columns:
            cap_inj = res["grid_export_cap_kwh"].to_numpy(dtype=float)
            if "grid_export_cap_pv_kwh" in res.columns:
                cap_inj = np.minimum(
                    cap_inj, res["grid_export_cap_pv_kwh"].to_numpy(dtype=float),
                )
            pv_load_priority = np.minimum(np.minimum(pv, load), cap_inj)
        else:
            pv_load_priority = np.minimum(pv, load)
        inv_9 = float(abs(pv_to_load - pv_load_priority).max())
    else:
        inv_9 = 0.0

    # Invariant 10 — import cap (Eq. S35): per-step grid-to-load plus
    # grid-to-BESS never exceeds the cap.  Vacuously 0.0 when the cap
    # column is absent (cap unlimited) — a stable-contract key like the
    # balancing residuals.
    if "grid_import_cap_kwh" in res.columns:
        import_cap = res["grid_import_cap_kwh"].to_numpy(dtype=float)
        inv_10 = float(
            np.maximum(
                0.0, grid_to_load + grid_to_bess - import_cap,
            ).max() if len(import_cap) else 0.0
        )
    else:
        inv_10 = 0.0

    general_invariants: dict[str, float] = {
        "invariant_1_pv_balance_kwh": inv_1,
        "invariant_2_load_balance_kwh": inv_2,
        "invariant_3_soc_dynamics_kwh": inv_3,
        "invariant_4_rte_bound_excess_kwh": inv_4,
        "invariant_5_no_sim_grid_io_max_product_kwh2": inv_5,
        "invariant_6_load_priority_violations": inv_6,
        "invariant_7_curtail_behavior_count": inv_7,
        "invariant_8_soc_closed_cycle_kwh": inv_8,
        "invariant_9_pv_load_priority_kwh": inv_9,
        "invariant_10_import_cap_excess_kwh": inv_10,
    }
    balancing_invariants = _balancing_invariants(
        res, params,
        general_invariants=general_invariants,
    )
    intraday_invariants = _intraday_invariants(res, params)
    return {
        **general_invariants,
        **balancing_invariants,
        **intraday_invariants,
    }
