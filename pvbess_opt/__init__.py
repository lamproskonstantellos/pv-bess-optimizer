"""PV + BESS dispatch optimizer.

Public entry points:
    pvbess_opt.io.read_workbook(path)        — typed nested dict loader
    pvbess_opt.io.read_inputs(path)          — flat (params, ts) view
    pvbess_opt.optimization.run_scenario(params, ts, solver_name)
    pvbess_opt.optimization.verify_dispatch_invariants(res, params)
    pvbess_opt.kpis.compute_kpis(res, params)
    pvbess_opt.rolling_horizon.rolling_horizon_dispatch(...)
    pvbess_opt.rolling_horizon.monte_carlo_rolling(...)
    pvbess_opt.plotting.* — figure generation per resolution

Two regulatory modes are supported:
    * vnb       — Greek Virtual Net Billing with co-located load.
    * merchant  — pure utility-scale DAM dispatch (no co-located load).

The hard static curtailment cap on grid-bound flows is enforced in
both modes per MD YPEN/DAPEEK/53563/1556/2023.
"""

__version__ = "0.5.0"
