"""Pyomo MILP for PV + BESS dispatch.

Two regulatory regimes are supported via the ``mode`` parameter:

* ``vnb`` — Greek Virtual Net Billing.  Strictly enforced rules:
  load balance, hard PV→load priority (Section 2 of the spec), no
  simultaneous grid I/O (tight big-M), retail tariff for self-
  consumption, DAM for export.  A binary-free slack additionally
  enforces surplus-only export (Section 5).
* ``merchant`` — pure utility-scale dispatch with **no co-located load**.
  Load balance NOT enforced; load priority NOT enforced; the
  ``pv_to_load`` / ``bess_dis_load`` / ``grid_to_load`` flows are pinned
  to zero.  The static curtailment cap STILL applies (regulatory
  grid-connection limit per MD YPEN/DAPEEK/53563/1556/2023).

The single objective is **profit** maximisation: under Greek VNB
economics retail (132 EUR/MWh) > DAM avg (~100 EUR/MWh) in >99 %
of hours, so the profit objective produces the same dispatch as a
"green" objective in this market.  Self-consumption is no longer
emergent: the hard ``LOAD_PV_PRIORITY`` constraint pins
``pv_to_load[t] == min(pv[t], load[t])`` exactly.  In merchant mode
there is no load to "be green about".

Tight big-M values
------------------

Big-Ms are derived per-instance:

* ``M_imp = (load_max + p_charge_max × dt_h) × 1.001``
* ``M_exp = p_grid_export_max × dt_h × (1 − curtailment_frac) × 1.001``
* ``M_charge = p_charge_max × dt_h × 1.001`` (only when grid-charging)
* ``M_pv = max(pv_kwh) × 1.001`` (only when grid-charging)

Audit invariants
----------------

After every solve :func:`verify_dispatch_invariants` checks the nine
mandatory invariants.  Residuals are returned and logged at INFO; the
``--strict`` CLI flag turns violations into errors.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd
import pyomo.environ as pyo
from pyomo.opt import SolverStatus, TerminationCondition

logger = logging.getLogger(__name__)


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
        except Exception:
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


def _check_solver_status(result, solver_name: str) -> None:
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
        return
    raise RuntimeError(
        f"Solver '{solver_name}' did not produce an acceptable solution: "
        f"status={status}, termination_condition={condition}."
    )


# ---------------------------------------------------------------------------
# Tight big-M derivation
# ---------------------------------------------------------------------------


def _resolve_curtailment_frac(value: Any) -> float:
    """Accept curtailment as fraction (0.27) or percent (27 → 0.27)."""
    raw = float(value or 0.0)
    if raw > 1.0:
        raw /= 100.0
    return max(0.0, min(1.0, raw))


def derive_tight_big_m(
    params: dict[str, Any], ts: pd.DataFrame, *, dt_h: float, mode: str,
) -> dict[str, float]:
    """Compute the tight big-M values."""
    p_export = float(params.get("p_grid_export_max_kw", 0.0) or 0.0)
    p_charge = float(params.get("p_charge_max_kw", 0.0) or 0.0)
    curtail_frac = _resolve_curtailment_frac(params.get("curtailment_frac", 0.0))

    if mode == "vnb" and "load_kwh" in ts.columns:
        load_max = float(ts["load_kwh"].max())
    else:
        load_max = 0.0
    pv_max = float(ts["pv_kwh"].max()) if "pv_kwh" in ts.columns else 0.0

    return {
        "M_imp": (load_max + p_charge * dt_h) * 1.001,
        "M_exp": p_export * dt_h * (1.0 - curtail_frac) * 1.001,
        "M_charge": p_charge * dt_h * 1.001,
        "M_pv": pv_max * 1.001,
    }


# ---------------------------------------------------------------------------
# Model construction
# ---------------------------------------------------------------------------


def _resolve_mode(params: dict[str, Any]) -> str:
    mode = str(params.get("mode", "vnb") or "vnb").strip().lower()
    if mode not in ("vnb", "merchant"):
        raise ValueError(f"Unknown mode {mode!r}; expected 'vnb' or 'merchant'.")
    return mode


def build_model(
    params: dict[str, Any],
    ts: pd.DataFrame,
    *,
    initial_soc_kwh: float | None = None,
    terminal_soc_free: bool | None = None,
) -> pyo.ConcreteModel:
    """Construct the Pyomo MILP.

    Variable & constraint structure adapts to ``params['mode']``:

    * ``vnb``     — full set of variables, hard ``LOAD_PV_PRIORITY``
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
    dt_h = params["dt_minutes"] / 60.0
    n_steps = len(ts)
    if n_steps == 0:
        raise ValueError("timeseries is empty; nothing to optimise.")
    time_index = range(n_steps)
    mode = _resolve_mode(params)
    allow_grid_charge = bool(params.get("allow_bess_grid_charging", False))

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
    pv = {t: float(ts.loc[t, "pv_kwh"]) for t in time_index}
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

    curtail_frac = _resolve_curtailment_frac(params.get("curtailment_frac", 0.0))
    p_export = float(params.get("p_grid_export_max_kw", 0.0) or 0.0)
    p_charge = float(params.get("p_charge_max_kw", 0.0) or 0.0)
    p_dis = float(params.get("p_dis_max_kw", 0.0) or 0.0)
    eta_c = float(params.get("efficiency_charge", 1.0) or 1.0)
    eta_d = float(params.get("efficiency_discharge", 1.0) or 1.0)

    export_cap_kwh = p_export * dt_h * (1.0 - curtail_frac)
    big_m = derive_tight_big_m(params, ts, dt_h=dt_h, mode=mode)

    m = pyo.ConcreteModel()
    m.T = pyo.RangeSet(0, n_steps - 1)
    m.mode = pyo.Param(initialize=mode, within=pyo.Any, mutable=False)

    # --- Decision variables (kWh per step) -------------------------------
    m.e_cap = pyo.Var(domain=pyo.NonNegativeReals)
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

    if not allow_grid_charge:
        m.NO_GRID_CHARGE = pyo.Constraint(
            m.T, rule=lambda m, t: m.grid_to_bess[t] == 0,
        )

    m.y_charge = pyo.Var(m.T, domain=pyo.Binary)
    m.y_dis = pyo.Var(m.T, domain=pyo.Binary)
    # Section 4 of the VNB spec — no charge + discharge simultaneously.
    m.MODE_LINK = pyo.Constraint(
        m.T, rule=lambda m, t: m.y_charge[t] + m.y_dis[t] <= 1,
    )

    m.grid_export_total = pyo.Expression(
        m.T, rule=lambda m, t: m.pv_to_grid[t] + m.bess_dis_grid[t],
    )

    # --- PV split (always active) ----------------------------------------
    m.PV_SPLIT = pyo.Constraint(
        m.T,
        rule=lambda m, t: (
            m.pv_to_load[t] + m.pv_to_bess[t] + m.pv_to_grid[t] + m.pv_curtail[t]
            == pv[t]
        ),
    )

    # --- Load balance (vnb only) -----------------------------------------
    if mode == "vnb":
        m.LOAD_BAL = pyo.Constraint(
            m.T,
            rule=lambda m, t: (
                m.pv_to_load[t] + m.bess_dis_load[t] + m.grid_to_load[t] == load[t]
            ),
        )

        # Section 2 of the VNB spec — strict load-coverage priority.
        # All available PV (up to the load) must be consumed by the load.
        # Combined with PV_SPLIT and LOAD_BAL this forces
        # pv_to_load[t] == min(pv[t], load[t]) exactly.  BESS-before-Grid
        # for the residual remains emergent through retail > DAM economics.
        pv_load_priority = {t: min(pv[t], load[t]) for t in time_index}
        m.LOAD_PV_PRIORITY = pyo.Constraint(
            m.T,
            rule=lambda m, t: m.pv_to_load[t] >= pv_load_priority[t],
        )

    # --- SOC dynamics ----------------------------------------------------
    def soc_dynamics(m, t):
        if t == n_steps - 1:
            return pyo.Constraint.Skip
        charge_eff = eta_c * (m.pv_to_bess[t] + m.grid_to_bess[t])
        discharge_raw = (m.bess_dis_load[t] + m.bess_dis_grid[t]) / eta_d
        return m.soc[t + 1] == m.soc[t] + charge_eff - discharge_raw

    m.SOC_DYN = pyo.Constraint(m.T, rule=soc_dynamics)
    m.SOC_MIN = pyo.Constraint(
        m.T, rule=lambda m, t: m.soc[t] >= params["soc_min_frac"] * m.e_cap,
    )
    m.SOC_MAX = pyo.Constraint(
        m.T, rule=lambda m, t: m.soc[t] <= params["soc_max_frac"] * m.e_cap,
    )

    if initial_soc_kwh is not None:
        m.SOC_INIT = pyo.Constraint(expr=m.soc[0] == float(initial_soc_kwh))
    else:
        m.SOC_INIT = pyo.Constraint(
            expr=m.soc[0] == params["initial_soc_frac"] * m.e_cap,
        )

    final_charge = eta_c * (
        m.pv_to_bess[n_steps - 1] + m.grid_to_bess[n_steps - 1]
    )
    final_discharge = (
        m.bess_dis_load[n_steps - 1] + m.bess_dis_grid[n_steps - 1]
    ) / eta_d
    final_soc_expr = m.soc[n_steps - 1] + final_charge - final_discharge

    if terminal_soc_free is None:
        terminal_soc_free = not bool(params.get("terminal_soc_equal", True))
    if not terminal_soc_free:
        m.SOC_TERM = pyo.Constraint(expr=final_soc_expr == m.soc[0])
    else:
        m.SOC_TERM_MIN = pyo.Constraint(
            expr=final_soc_expr >= params["soc_min_frac"] * m.e_cap,
        )
        m.SOC_TERM_MAX = pyo.Constraint(
            expr=final_soc_expr <= params["soc_max_frac"] * m.e_cap,
        )

    # --- Charge / discharge power limits ---------------------------------
    ch_lim = p_charge * dt_h
    dis_lim = p_dis * dt_h
    m.CH_LIM = pyo.Constraint(
        m.T,
        rule=lambda m, t: (
            m.pv_to_bess[t] + m.grid_to_bess[t] <= ch_lim * m.y_charge[t]
        ),
    )
    m.DIS_LIM = pyo.Constraint(
        m.T,
        rule=lambda m, t: (
            m.bess_dis_load[t] + m.bess_dis_grid[t] <= dis_lim * m.y_dis[t]
        ),
    )

    if params.get("battery_hours") is not None:
        m.EP = pyo.Constraint(
            expr=m.e_cap <= p_dis * float(params["battery_hours"]),
        )

    # --- Daily cycle limit ------------------------------------------------
    m.CYC = pyo.ConstraintList()
    for indices in day_to_idx.values():
        lhs = sum(m.bess_dis_load[t] + m.bess_dis_grid[t] for t in indices)
        m.CYC.add(lhs <= float(params["max_cycles_per_day"]) * m.e_cap)

    # --- Static curtailment cap (HARD constraint, BOTH modes) -------------
    # Section 8 of the VNB spec — regulatory grid-connection limit per
    # MD YPEN/DAPEEK/53563/1556/2023.  Applies in vnb AND merchant modes.
    m.EXPORT_CAP = pyo.Constraint(
        m.T, rule=lambda m, t: m.grid_export_total[t] <= export_cap_kwh,
    )

    # --- vnb-only constraints --------------------------------------------
    if mode == "vnb":
        # Section 5 of the VNB spec — surplus-only export.
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
        # Section 6 of the VNB spec — BESS may charge from grid only in
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
    weight_curtail = float(params.get("weight_curtail_tiebreak", 0.0) or 0.0)
    weight_cycles = float(params.get("weight_cycles_term", 0.0) or 0.0)

    curtail_tiebreak_term = weight_curtail * sum(m.pv_curtail[t] for t in time_index)

    if mode == "vnb":
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
    cycles_bonus = weight_cycles * sum(
        (m.bess_dis_load[t] + m.bess_dis_grid[t]) / 1000.0 for t in time_index
    )
    profit_eur = avoided_cost + export_revenue - grid_charge_cost + cycles_bonus

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
    result = solver.solve(model, tee=tee)
    _check_solver_status(result, resolved)
    return model, resolved


def model_to_dataframe(
    model: pyo.ConcreteModel,
    ts: pd.DataFrame,
    params: dict[str, Any],
) -> tuple[pd.DataFrame, float]:
    """Convert the solved model to a dispatch DataFrame.

    Returns ``(res, e_cap_kwh)``.
    """
    n_steps = len(ts)
    time_index = range(n_steps)
    dt_h = params["dt_minutes"] / 60.0
    p_export = float(params.get("p_grid_export_max_kw", 0.0) or 0.0)
    curtail_frac = _resolve_curtailment_frac(params.get("curtailment_frac", 0.0))
    export_cap_kwh = p_export * dt_h * (1.0 - curtail_frac)

    res = pd.DataFrame(index=ts.index)
    res["timestamp"] = ts["timestamp"].values
    res["load_kwh"] = [
        float(ts.loc[t, "load_kwh"]) if "load_kwh" in ts.columns else 0.0
        for t in time_index
    ]
    res["pv_kwh"] = [float(ts.loc[t, "pv_kwh"]) for t in time_index]
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
    res["grid_export_cap_kwh"] = export_cap_kwh

    e_cap_kwh = float(pyo.value(model.e_cap))
    res["soc_kwh"] = [pyo.value(model.soc[t]) for t in time_index]
    if e_cap_kwh > 1e-9:
        res["soc_pct"] = res["soc_kwh"] / e_cap_kwh * 100.0
    else:
        res["soc_pct"] = 0.0

    if "dam_price_eur_per_mwh" in ts.columns:
        res["dam_price_eur_per_mwh"] = ts["dam_price_eur_per_mwh"].values
    if "retail_price_eur_per_mwh" in ts.columns:
        res["retail_price_eur_per_mwh"] = ts["retail_price_eur_per_mwh"].values

    numeric_cols = [c for c in res.columns if c != "timestamp"]
    res[numeric_cols] = res[numeric_cols].astype(float).round(4)
    return res, e_cap_kwh


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
) -> tuple[pd.DataFrame, float, str]:
    """Build, solve and extract dispatch for a single scenario.

    Returns ``(res, e_cap_kwh, resolved_solver_name)``.
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
    res, e_cap_kwh = model_to_dataframe(solved, ts, params)
    return res, e_cap_kwh, resolved


# ---------------------------------------------------------------------------
# 8 audit invariants — verify_dispatch_invariants
# ---------------------------------------------------------------------------


def verify_dispatch_invariants(
    res: pd.DataFrame,
    params: dict[str, Any],
    *,
    mode: str | None = None,
    tol_kwh: float = 1.0e-3,
) -> dict[str, float]:
    """Check the nine audit invariants on a solved dispatch.

    Returns a dict of named residuals.

    Returns
    -------
    dict[str, float]
        Keys:
            ``invariant_1_pv_balance_kwh``
            ``invariant_2_load_balance_kwh``      (vnb only; 0.0 in merchant)
            ``invariant_3_soc_dynamics_kwh``
            ``invariant_4_rte_bound_excess_kwh``
            ``invariant_5_no_sim_grid_io_max_product_kwh2``  (vnb only)
            ``invariant_6_load_priority_violations``         (vnb only)
            ``invariant_7_curtail_behavior_kwh``  (BOTH modes)
            ``invariant_8_soc_closed_cycle_kwh``  (when terminal_soc_equal)
            ``invariant_9_pv_load_priority_kwh``  (vnb only; Section 2)
    """
    if mode is None:
        mode = _resolve_mode(params)
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

    if mode == "vnb":
        inv_2 = float(abs(load - pv_to_load - bess_dis_load - grid_to_load).max())
    else:
        inv_2 = 0.0

    if len(soc) >= 2:
        expected_delta = (
            eta_c * (pv_to_bess[:-1] + grid_to_bess[:-1])
            - (bess_dis_load[:-1] + bess_dis_grid[:-1]) / eta_d
        )
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
    else:
        final_state = 0.0
    rte_bound = eta_c * eta_d * total_charge + eta_d * (soc0 - final_state)
    inv_4 = float(max(0.0, total_discharge - rte_bound))

    if mode == "vnb":
        grid_imp = grid_to_load + grid_to_bess
        grid_exp = pv_to_grid + bess_dis_grid
        inv_5 = float((grid_imp * grid_exp).max() if len(grid_imp) else 0.0)
    else:
        inv_5 = 0.0

    if mode == "vnb":
        export = pv_to_grid + bess_dis_grid
        violations = int(((export > tol_kwh) & (grid_to_load > tol_kwh)).sum())
        inv_6 = float(violations)
    else:
        inv_6 = 0.0

    # Invariant 7 — curtail behavior, checked in BOTH modes per spec:
    # cap not binding ⇒ curtail = 0.
    cap = float(res["grid_export_cap_kwh"].iloc[0]) if "grid_export_cap_kwh" in res.columns else 0.0
    export = pv_to_grid + bess_dis_grid
    cap_residual = cap - export
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
            inv_8 = float(abs(final_state - soc[0]))
        else:
            inv_8 = 0.0
    else:
        inv_8 = 0.0

    # Invariant 9 — Section 2 of the spec: pv_to_load == min(pv, load).
    if mode == "vnb" and len(pv):
        pv_load_priority = np.minimum(pv, load)
        inv_9 = float(abs(pv_to_load - pv_load_priority).max())
    else:
        inv_9 = 0.0

    return {
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
