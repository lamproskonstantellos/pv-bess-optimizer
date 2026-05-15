"""PV + BESS dispatch optimizer.

Public entry points:
    pvbess_opt.io.read_workbook(path)        — typed nested dict loader
    pvbess_opt.io.read_inputs(path)          — flat (params, ts) view
    pvbess_opt.optimization.run_scenario(params, ts, solver_name)
    pvbess_opt.optimization.verify_dispatch_invariants(res, params)
    pvbess_opt.kpis.compute_kpis(res, params)
    pvbess_opt.availability.apply_unavailability_derate(...)
    pvbess_opt.curtailment.build_per_step_curtailment_frac(...)
    pvbess_opt.rolling_horizon.rolling_horizon_dispatch(...)
    pvbess_opt.rolling_horizon.monte_carlo_rolling(...)
    pvbess_opt.plotting.* — figure generation per resolution

Two regulatory modes are supported:
    * vnb       — Greek Virtual Net Billing with co-located load.
    * merchant  — pure utility-scale DAM dispatch (no co-located load).

Three asset modes are first-class:
    * Hybrid PV+BESS — both pv_nameplate_kwp and bess_power_kw > 0.
    * PV-only        — bess_power_kw = 0.
    * BESS-only      — pv_nameplate_kwp = 0 (most useful with
                       allow_bess_grid_charging = TRUE).

Highlights:
    * Seven-sheet input workbook (project / pv / bess / economics /
      simulation / curtailment_profile / timeseries) — one theme per
      sheet for human readability.
    * Symmetric BESS power limit (bess_power_kw) and pinned energy
      capacity (bess_capacity_kwh).  e_cap is not a decision variable.
    * Hour-of-day curtailment cap profile (optional monthly axis).
    * DEVEX (per-asset development CAPEX), unavailability_pct,
      aggregator_fee_pct_revenue.
    * IRR tornado dumbbell with endpoint labels outside the dots
      and separate LCOE / LCOS summary PDFs against Lazard 2024
      benchmark bands.
"""

__version__ = "0.8.7"
