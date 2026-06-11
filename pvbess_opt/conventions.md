# Cross-module conventions

This file is the single source of truth for the conventions that have to
stay in lockstep across multiple modules.  Keep it short, keep it accurate,
and cross-reference it from the call sites it governs.

## `expense_charge_bess_grid_eur` belongs to the BESS-DAM stream

`expense_charge_bess_grid_eur` is the DAM-priced expense of charging the
BESS directly from the grid.  By convention it is **bundled into the
BESS-side DAM revenue stream** (not the PV-side), because the cost is
incurred only when the BESS imports energy that it later re-exports for
arbitrage.  Two consequences flow from that:

1. **Cashflow construction** -- `pvbess_opt.economics.build_yearly_cashflow`
   (the `_has_breakdown` branch) computes
   ```
   rev1_dam_bess = profit_export_from_bess_eur - expense_charge_bess_grid_eur
   ```
   and degrades the result on `bess_factor` (the BESS capacity-fade curve),
   never on `pv_factor`.
2. **Lifetime aggregation** -- `pvbess_opt.lifetime.build_lifetime_dispatch`
   (via the `_BESS_REVENUE_COLUMNS` tuple) treats
   `expense_charge_bess_grid_eur` as a BESS-side column and scales it on
   `bess_factor` for every project year past Year 1.

The two sheets in `03_results.xlsx` (the cashflow projection and the
lifetime dispatch) must therefore agree on the convention.  Any future
attempt to attribute grid-charge expense to PV degradation would
desynchronise them.

## `params['dt_minutes']` -> hours per step

The MILP, the KPI helpers, the rolling-horizon engine, the balancing
module and the I/O loader all need to convert
`params['dt_minutes']` (integer minutes per timestep) into a per-step
duration expressed in hours.  Route every call through
`pvbess_opt.timeutils.dt_hours_from(params)` rather than re-deriving the
expression inline.  The helper preserves the legacy semantic of
treating a missing or zero / negative `dt_minutes` as 0.0 hours so
balancing-block guards (`dt_h <= 0.0 -> return out`) keep working.

## Per-step EUR columns are written by `compute_kpis` or `add_economic_columns`

The downstream financial pipeline (`derive_monthly_cashflow`,
`build_lifetime_dispatch`, `aggregate_lifetime_to_yearly`) reads the per-step
EUR columns enumerated in `pvbess_opt.kpis.ECONOMIC_COLUMNS`.  Run
`compute_kpis` (or `add_economic_columns` directly) before the financial
pipeline so revenue is never silently defaulted to zero.

## Rounding convention on the dispatch frame

`model_to_dataframe(round_output=True)` rounds every numeric column to
four decimal places.  Sub-0.5 mW reservations therefore round to zero on
the post-round frame.  Use `run_scenario(return_unrounded=True)` for the
full-precision dispatch when the per-step energy-balance check
(`verify_energy_balance`) needs to avoid round(4) accumulation, and read
the rounded frame for downstream display.

The KPI-aggregation step (`_compute_balancing_kpis` in
`pvbess_opt/kpis.py`) operates on the rounded frame by design: the
4-dp rounding is intentional for headline KPI display.

## Lifetime aggregates are pre-fee gross and exclude balancing revenue

`aggregate_lifetime_to_yearly` (`pvbess_opt/lifetime.py`) returns a
DataFrame whose revenue column is named `revenue_eur_dam_retail`.  Its
scope, exactly:

1. **Pre-fee gross** — per-step DAM + retail revenue minus the
   grid-charging expense, at the dispatch prices.  The aggregator fee
   is a project-level deduction applied only in the cashflow, so the
   reconciliation is `revenue_eur_dam_retail == revenue_eur -
   aggregator_fee_eur` (fee signed negative) whenever
   `retail_inflation_pct` and `dam_inflation_pct` are zero; with
   indexation on, the cashflow escalates per stream while the lifetime
   frame stays at Year-1 prices by construction.
2. **Excludes balancing revenue**: balancing settles per window via
   reservation × probability × price, not per step, and pulling it
   into the per-step physics frame would require restructuring the
   lifetime data model.

For total project revenue including balancing, use the cashflow
DataFrame: `cashflow_yearly['revenue_eur'] +
cashflow_yearly['balancing_revenue_eur']`.

## Perfect-foresight benchmark and the MC ensemble share one scope

The rolling-horizon Monte Carlo compares each seed's realised profit
against the perfect-foresight benchmark.  Both sides MUST share the
headline-KPI scope, or the comparison silently biases:

1. **Unavailability derate** — `pvbess_opt.rolling_horizon.
   rolling_horizon_dispatch` applies `apply_unavailability_derate`
   (using `params['unavailability_pct']`) to its returned KPIs, exactly
   as `pipeline._run_one` derates the Year-1 KPI dict it draws
   `pf_profit_eur` from.  `foresight_gap_pct = 100 * (1 - rh/pf)` is
   then derate-invariant.  Never compare a raw seed profit against the
   derated benchmark: with the default 1 % unavailability that alone
   pushes the gap ~1 pp negative ("imperfect foresight beats perfect
   foresight"), which is impossible.
2. **Year-close SOC condition** — when `terminal_soc_equal` is true the
   benchmark must return the battery to its initial SOC.  The rolling
   horizon enforces the same condition by pinning the post-final-step
   SOC of every window that reaches the end of the horizon to the
   year-initial SOC (`terminal_soc_target_kwh` in `build_model`).
   Without it the last window drains the battery for profit the
   benchmark is not allowed to take.

With both rules in place every seed's stitched dispatch is feasible for
the perfect-foresight MILP, so `seed profit <= pf profit` up to the
solver's `mip_gap` slack and the PF marker sits at or above the upper
tail of the Monte Carlo histogram.

## Default inflation: balancing tracks CPI, DAM is held nominal

The economics defaults set `bm_inflation_pct = 2.0` (Greek balancing
market historically tracks inflation as the TSO indexes capacity
payments) while `dam_inflation_pct = 0.0` (wholesale DAM stays at the
nominal user-supplied price unless explicitly overridden, since DAM
price forecasts already incorporate an inflation view in their
trajectory).  Override either knob in the `economics` sheet to model
an explicit indexation curve.
