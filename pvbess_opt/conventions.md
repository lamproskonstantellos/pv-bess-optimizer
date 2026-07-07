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

1. **Pre-fee gross**: per-step DAM + retail revenue minus the
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
cashflow_yearly['balancing_revenue_eur'] +
cashflow_yearly['balancing_aggregator_fee_eur']`.  The
`balancing_revenue_eur` column is GROSS; the optional balancing-aggregator
(BSP / route-to-market) fee is its own signed
`balancing_aggregator_fee_eur` column (≤ 0, identically zero when
`balancing_aggregator_fee_pct_revenue` is 0).  Balancing carries no
energy-aggregator fee, but may carry this separate, default-off BSP fee;
both fees are excluded from LCOE/LCOS by the existing convention.

## Perfect-foresight benchmark and the MC ensemble share one scope

The rolling-horizon Monte Carlo compares each seed's realised profit
against the perfect-foresight benchmark.  Both sides MUST share the
headline-KPI scope, or the comparison silently biases:

1. **Unavailability derate**: `pvbess_opt.rolling_horizon.
   rolling_horizon_dispatch` applies `apply_unavailability_derate`
   (using `params['unavailability_pct']`) to its returned KPIs, exactly
   as `pipeline._run_one` derates the Year-1 KPI dict it draws
   `pf_profit_eur` from.  `foresight_gap_pct = 100 * (1 - rh/pf)` is
   then derate-invariant.  Never compare a raw seed profit against the
   derated benchmark: with the default 1 % unavailability that alone
   pushes the gap ~1 pp negative ("imperfect foresight beats perfect
   foresight"), which is impossible.
2. **Year-close SOC condition**: when `terminal_soc_equal` is true the
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

The slack itself is handled by **benchmark re-tightening**: the annual
incumbent is only `mip_gap`-optimal while the 48 h windows solve
near-exactly, so a stitched dispatch can land inside the incumbent's
slack and read as a spurious negative gap.  `pipeline._run_one` then
re-solves the benchmark at 10x tighter gaps (floor `1e-6`) until it is
the best case, recomputes `foresight_gap_pct` and its percentiles
against the final benchmark, and records the gap of the solve that
produced the final benchmark as the `pf_benchmark_mip_gap` KPI.  Every
downstream artifact uses the re-tightened solution.  A re-solve is
accepted only if it improves the incumbent: when the `--time-limit`
terminates the search, a deterministic solver returns the identical
incumbent at any requested gap, so the guard keeps the previous
benchmark after one unimproved probe and advises a higher time limit
or a faster solver instead of burning the limit repeatedly.

The `mip_gap` is a REQUESTED target, not a guarantee: it competes with
the time limit, and the solver stops at whichever fires first. The run
therefore records two distinct KPIs — `pf_benchmark_mip_gap` (what was
requested) and `pf_benchmark_gap_achieved` (what the solver actually
proved, `|bound − incumbent| / |incumbent|`, matching the solver's own
printed gap). A publication quotes the ACHIEVED gap; the true optimum
is bracketed by `[incumbent, incumbent × (1 + achieved gap)]`, so the
reported foresight gap is a lower bound accurate to within it.

## PPA stream scope

The pay-as-produced PPA (`docs/ppa_design.md`) keeps one scope across
every consumer:

1. **Per-step columns**: `revenue_pv_ppa_eur` (contract leg) and
   `ppa_covered_dam_value_eur` (covered volume's counterfactual DAM
   value) are written by `add_economic_columns` only when a contract is
   active; both are PV-origin (`pv_factor` in the lifetime frame) and
   in the availability-derate list (derate exactly once).
2. **Profit / KPIs**: `profit_total_eur` includes the contract leg;
   `revenue_pv_ppa_eur` is the ninth canonical revenue aggregate.
3. **Cashflow**: `ppa_revenue_eur` is its own column (like
   `balancing_revenue_eur`): the strike leg escalates on
   `ppa_inflation_pct`, the CfD's DAM leg on `dam_inflation_pct`, the
   stream ends after `ppa_term_years`, and physical settlement then
   reverts the covered DAM value into the DAM revenue stream where the
   aggregator fee applies to it as market revenue.  While under
   contract the stream carries NO aggregator fee (bilateral offtake,
   mirroring the balancing/TSO convention) and stays out of LCOE/LCOS
   (revenue-agnostic Lazard metrics) and out of the lifetime frame's
   `revenue_eur_dam_retail` (per-step DAM+retail scope).

## Default inflation: balancing tracks CPI, DAM is held nominal

The schema defaults set `bm_inflation_pct = 2.0` on the `balancing`
sheet (balancing capacity payments are commonly indexed to inflation
by the system operator) while the `economics` sheet sets
`dam_inflation_pct = 0.0` (wholesale DAM stays at the nominal
user-supplied price unless explicitly overridden, since DAM price
forecasts already incorporate an inflation view in their trajectory).
Override each knob on its own sheet to model an explicit indexation
curve; `read_economic_params` merges all parameter sheets, so both
reach the cashflow through the same flat dict.
