"""PV + BESS dispatch optimizer.

Public entry points:
    pvbess_opt.io.read_workbook(path)        — typed nested dict loader
    pvbess_opt.io.read_inputs(path)          — flat (params, ts) view
    pvbess_opt.optimization.run_scenario(params, ts, solver_name)
    pvbess_opt.optimization.verify_dispatch_invariants(res, params)
    pvbess_opt.kpis.compute_kpis(res, params)
    pvbess_opt.availability.apply_unavailability_derate(...)

Ordering contract:
    ``compute_kpis`` (via ``kpis.add_economic_columns``) writes the
    per-step EUR columns onto the dispatch frame.  The financial
    pipeline — ``economics.derive_monthly_cashflow``,
    ``lifetime.build_lifetime_dispatch``,
    ``lifetime.aggregate_lifetime_to_yearly`` — depends on those
    columns and raises ``ValueError`` if they are missing rather than
    silently defaulting revenue to zero.  Always call ``compute_kpis``
    before any of those financial entry points.

    pvbess_opt.max_injection.build_per_step_max_injection_frac(...)
    pvbess_opt.rolling_horizon.rolling_horizon_dispatch(...)
    pvbess_opt.rolling_horizon.monte_carlo_rolling(...)
    pvbess_opt.plotting.* — figure generation per resolution

Two regulatory modes are supported:
    * self_consumption       — Greek Self-consumption with co-located load.
    * merchant  — pure utility-scale DAM dispatch (no co-located load).

Three asset modes are first-class:
    * Hybrid PV+BESS — both pv_nameplate_kwp and bess_power_kw > 0.
    * PV-only        — bess_power_kw = 0.
    * BESS-only      — pv_nameplate_kwp = 0 (most useful with
                       allow_bess_grid_charging = TRUE).

Highlights:
    * Themed input workbook (timeseries / project / pv / bess /
      economics / simulation / balancing / max_injection_profile, plus
      optional per-source injection caps and the sizing / scenarios
      sweep sheets) — one theme per sheet for human readability.  The
      ``pv_kwh`` column is consumed verbatim (absolute kWh per step);
      ``pv_nameplate_kwp`` is metadata for per-kW CAPEX/OPEX and the
      sizing sweep, never a rescale target.  Alternatively the PV
      profile can be fetched from PVGIS by latitude / longitude.
    * Symmetric BESS power limit (bess_power_kw) and pinned energy
      capacity (bess_capacity_kwh).  e_cap is not a decision variable.
    * Hour-of-day max-injection cap profile (optional monthly axis).
    * DEVEX (per-asset development CAPEX), unavailability_pct,
      aggregator_fee_pct_revenue.
    * IRR tornado dumbbell with endpoint labels outside the dots
      and separate LCOE / LCOS summary PDFs against Lazard 2024
      benchmark bands.
"""

from .pipeline import Results, RunConfig, run

__all__ = ["Results", "RunConfig", "__version__", "run"]

__version__ = "1.0.0"
