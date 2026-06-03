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
   (see the `_has_breakdown` branch around `pvbess_opt/economics.py:271-274`)
   computes
   ```
   rev1_dam_bess = profit_export_from_bess_eur - expense_charge_bess_grid_eur
   ```
   and degrades the result on `bess_factor` (the BESS capacity-fade curve),
   never on `pv_factor`.
2. **Lifetime aggregation** -- `pvbess_opt.lifetime.build_lifetime_dispatch`
   (see `_BESS_REVENUE_COLUMNS` at `pvbess_opt/lifetime.py:86-90`) treats
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

The KPI-aggregation step (`_compute_balancing_kpis` at
`pvbess_opt/kpis.py:631`) operates on the rounded frame by design: the
4-dp rounding is intentional for headline KPI display.

## Lifetime aggregates exclude balancing revenue

`aggregate_lifetime_to_yearly` (`pvbess_opt/lifetime.py`) returns a
DataFrame whose revenue column is named `revenue_eur_dam_retail`.  It
is the per-step DAM + retail aggregate (matching the cashflow's
`revenue_eur` column scope) and **deliberately excludes balancing
revenue**: balancing settles per window via reservation × probability
× price, not per step, and pulling it into the per-step physics frame
would require restructuring the lifetime data model.

For total project revenue including balancing, use the cashflow
DataFrame: `cashflow_yearly['revenue_eur'] +
cashflow_yearly['balancing_revenue_eur']`.

## Default inflation: balancing tracks CPI, DAM is held nominal

The economics defaults set `bm_inflation_pct = 2.0` (Greek balancing
market historically tracks inflation as the TSO indexes capacity
payments) while `dam_inflation_pct = 0.0` (wholesale DAM stays at the
nominal user-supplied price unless explicitly overridden, since DAM
price forecasts already incorporate an inflation view in their
trajectory).  Override either knob in the `economics` sheet to model
an explicit indexation curve.

## PPA (with merchant tail) is a parallel revenue stream

A power purchase agreement reprices the **grid-export stream**
(`pv_to_grid + bess_dis_grid`) only; energy serving load
(retail-priced) is never touched.  It is modelled as an **additive
premium** on top of the existing DAM-priced export, written per step by
`pvbess_opt.kpis.add_economic_columns` (only when `ppa_enabled`):

```
ppa_premium_eur[t] = contracted[t] / 1000 * (ppa_price_y1 - dam[t])
```

where `contracted[t]` is `ppa_coverage_fraction * export[t]`
(`pay_as_produced`) or `min(export[t], ppa_baseload_mw * 1000 * dt_h)`
(`baseload`; v1 is **as-available up to target**, firm baseload with
spot-buy firming is out of scope).  The premium is split pro-rata by the
PV vs BESS export share so PV-origin premium degrades on the PV factor
and BESS-origin premium on the BESS factor.

The premium is a **parallel** stream, handled exactly like balancing
revenue:

1. It is **not** added to `profit_total_eur` and **not** part of the
   retail/DAM Year-1 breakdown, so the
   `pvbess_opt.economics.build_yearly_cashflow` reconciliation guard is
   never tripped.  A separate reporting KPI `project_revenue_total_eur`
   rolls up `profit_total_eur + PPA premium + balancing total`.
2. It is **not** subject to the aggregator fee (the fee is charged on
   the retail + DAM gross only).
3. It is **not** part of LCOE or LCOS — those read no PPA column, so
   toggling the PPA leaves both identical (they stay revenue-agnostic,
   Lazard-comparable).
4. It has its **own** escalation index `ppa_escalation_pct` (parallel to
   balancing's `bm_inflation_pct`), applied only in the multi-year
   cashflow `ppa_revenue_eur` column, never in dispatch or the Year-1
   repricing.
5. It is projected **analytically** in `build_yearly_cashflow`, not in
   the per-step lifetime frame: the per-step PPA columns are deliberately
   excluded from `pvbess_opt.kpis.ECONOMIC_COLUMNS`, so
   `pvbess_opt.lifetime` is unchanged.

By default the PPA is a **post-dispatch repricing** (the MILP is
untouched, so the physical dispatch is bit-identical to a non-PPA run).
Optionally, **dispatch-aware pay-as-produced** values the export term in
the objective at the blended price `f*ppa_price + (1 - f)*dam[t]`
(Year-1 price, never escalated in dispatch); this changes only objective
coefficients, so every dispatch invariant still holds.  Baseload
dispatch-aware is out of scope (financial-only).

## `zero_feed_in` is a flat 0 % max-injection cap

`zero_feed_in` (self-consumption mode only) is the export-prohibition
("zero feed-in") option of the same Greek Ministerial Decision the
net-billing regime references.  It is implemented as a single chokepoint:
`pvbess_opt.optimization._resolve_max_injection_per_step` returns an
all-zero per-step fraction array when `params["zero_feed_in"]` is set,
which **overrides** the `max_injection_profile` sheet.

Every downstream consumer flows from that one array, so no new constraint
and no objective edit are needed: `build_model` derives
`export_cap_kwh_per_step = 0` for every step (so the existing
`EXPORT_CAP` constraint forces `pv_to_grid + bess_dis_grid = 0`),
`model_to_dataframe` reports `grid_export_cap_kwh = 0`, and
`derive_tight_big_m` yields `M_exp = 0`.  PV surplus beyond load + BESS
charging is curtailed.

Invariant compatibility: because the effective cap is zero, the
curtailment invariant (cap-not-binding ⇒ no curtailment) sees
`cap_residual = cap - export = 0` and does **not** flag the expected
surplus curtailment, so every dispatch invariant continues to hold for
PV-only, BESS-only and hybrid.  `zero_feed_in` is rejected by the loader
in merchant mode (a merchant installation exports its whole output, so an
export prohibition is meaningless), and a warning is logged when it is
combined with an enabled PPA (there is no export to contract).
