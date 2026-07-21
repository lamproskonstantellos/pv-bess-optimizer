# PV & BESS Optimizer

[![license](https://img.shields.io/badge/license-All%20Rights%20Reserved-red)](LICENSE)
[![version](https://img.shields.io/badge/version-1.0.0-blue)](pvbess_opt/__init__.py)
[![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](pyproject.toml)
[![ci](https://github.com/lamproskonstantellos/pv-bess-optimizer/actions/workflows/ci.yml/badge.svg)](https://github.com/lamproskonstantellos/pv-bess-optimizer/actions/workflows/ci.yml)
[![docs](https://readthedocs.org/projects/pv-bess-optimizer/badge/?version=latest)](https://pv-bess-optimizer.readthedocs.io/en/latest/)

Created and developed by **Lampros Konstantellos**. Full manual at
[pv-bess-optimizer.readthedocs.io](https://pv-bess-optimizer.readthedocs.io/en/latest/).

## What it does

Mixed-integer linear programming model for co-located PV + BESS dispatch
at 15-minute resolution (auto-detected cadence), with a multi-year
project-finance pipeline, stochastic balancing-market participation
(FCR / aFRR / mFRR), a PPA contract engine (pay-as-produced and
baseload structures), an intraday venue settled by two-stage
re-dispatch around the committed day-ahead position, and
rolling-horizon Monte Carlo for uncertainty analysis.

Two regulatory regimes are supported:

* `self_consumption`: co-located load with self-consumption priority.
  The load balance enforces a hard PV→load priority, surplus-only
  export through a binary-free slack, and no simultaneous grid I/O via
  a tight big-M. Self-consumption is settled at the retail tariff;
  surplus is settled at the day-ahead market price.
* `merchant`: utility-scale dispatch with no co-located load. PV and
  BESS dispatch entirely to the DAM.

Two optional revenue layers stack on either regime, and both ship
disabled (opt in via their master switch): stochastic balancing-market
participation (FCR / aFRR / mFRR; requires a BESS, is settled by the
TSO with no aggregator fee, and respects the SOC safety buffer) and a
pay-as-produced PPA on PV export. Balancing is a property of the
battery, not of the market regime, so it is available in
self-consumption and merchant alike; leave `balancing_enabled = FALSE`
on the `balancing` sheet wherever the asset does not offer the service.

A third opt-in market layer is merchant-only: the intraday venue
(`id_enabled` on the `intraday` sheet). The day-ahead solve commits a
position; a second solve of the same model then re-dispatches against
the intraday price (`ida_price_eur_per_mwh`) around that committed
position, under a per-step deviation budget, an anti-wash-trading
rule, per-origin (PV / BESS) trade tracking, and the unchanged
physical plant limits. The margin is settled in spread form —
deviations earn the intraday-minus-day-ahead spread net of the venue
fee — so the day-ahead revenue stream stays intact and the venue adds
only the re-dispatch margin. See
[`docs/intraday_design.md`](docs/intraday_design.md).

Two opt-in price layers complete the market picture (both ship
disabled; defaults are bit-identical). The `market_data` sheet fetches
historical day-ahead, intraday-auction, balancing and imbalance prices
for a selectable bidding zone (most SDAC zones — GR, DE-LU, FR, ES, the
Nordic and Italian zones, and more — with EIC codes in
`pvbess_opt.marketdata.ZONES`) from ENTSO-E — with the Greek balancing
gap covered by the ADMIE file API and a HEnEx cross-check — and
*replaces* the workbook price columns with the fetched reference year
(override semantics, provenance recorded, the input snapshot re-runs
the exact prices offline). The
`scenario_engine` + `price_scenarios` sheets then project years 2..N
on per-scenario price decks instead of flat inflation: the frozen
Year-1 dispatch is repriced year by year into per-stream escalation
factors (PV capture-price cannibalization, BESS spread evolution,
per-product balancing paths), optionally refined by full MILP
re-solves at support years, and a weighted scenario ensemble reports
E[NPV] / E[IRR] with P10/P50/P90 on one shared debt sizing. See
[`docs/market_scenarios_design.md`](docs/market_scenarios_design.md).

On top of the market regimes, the economics layer models the Greek
contracted-revenue and fiscal landscape (all opt-in, neutral
mechanisms): a BESS tolling agreement with merchant zeroing, an
optimizer floor + share-above-floor structure, RRF-style state
support with a two-way clawback against realised market revenue, a
capacity-market payment with duration derating, a levy on gross
market turnover (the Greek 3 % RES levy pattern), a sliding
feed-in-premium / two-way CfD support engine with reference-period
settlement and a negative-hour suspension clause,
guarantees-of-origin revenue on PV export, and a depreciation
+ corporate tax engine with loss carry-forward that reports post-tax
NPV / IRR / equity IRR alongside the pre-tax baseline.

The dispatch and asset layers carry further opt-in levers: a grid
import capacity limit, hour-of-day injection cap profiles (shared or
per source), exogenous curtailment with optional compensation, daily
and annual cycle caps with a warranty-basis switch (nameplate or
faded energy), BESS overbuild and scheduled augmentation plans priced
on a declining cost curve, and a mid-life re-solve that validates the
analytic year-N scaling against a full re-optimisation. On the risk
side, the Monte Carlo ensemble reports VaR / CVaR tail metrics, and an
imbalance settlement prices each seed's committed-vs-actual
deviations.

Three asset configurations are supported in both regimes: `hybrid`
(PV + BESS), `pv_only`, and `bess_only`.

**Scope.** This tool optimizes dispatch for a given PV + BESS size and
computes the resulting project finances. It does not search the
capacity space. Full techno-economic sizing tools (HOMER, Gridcog)
sweep capacities to find an optimum; here the PV nameplate and the
BESS power and capacity are inputs. The market model covers a
self-consumption regime and a merchant day-ahead regime, with
optional balancing-market participation and an optional intraday
venue.

## Installation

```bash
git clone https://github.com/lamproskonstantellos/pv-bess-optimizer.git
cd pv-bess-optimizer
pip install -r requirements/dev.txt
```

HiGHS is the default solver (`pip install highspy`). Gurobi
(`pip install gurobipy` plus a licence) and CBC also work. The solver
is part of the results' provenance, so the requested solver is never
substituted silently: if it is not available the run stops with an
error listing the installed alternatives
(`pvbess_opt.optimization.choose_solver`), and the run log's
`[verify] solver=` line records what actually solved. Solver knobs are
CLI flags: `--solver`, `--mip-gap`, `--time-limit`, and `--tee` for
live solver output. Gurobi solves carry a memory-safety default
(`NodefileStart` 8 GB): a branch-and-bound tree that outgrows RAM
spills to disk instead of the OS killing the run — node files are
transparent to the search, so results are identical.

## Quickstart

```bash
python main.py inputs/input.xlsx --outdir results/
```

The runner reads the workbook, solves the MILP, computes KPIs and the
multi-year cashflow, runs the rolling-horizon Monte Carlo (when
enabled in the `simulation` sheet), runs the sensitivity tornado (when
enabled in the `economics` sheet), and writes:

* a multi-sheet results workbook,
* the IEEE-styled PDF report under `results/<run>/04_financial_plots/`,
* the energy plots under `results/<run>/05_energy_plots/`,
* uncertainty diagnostics under `results/<run>/06_uncertainty_plots/`.

Override the workbook value at the CLI:

```bash
python main.py inputs/input.xlsx --mode merchant --outdir results/merchant
```

## Configuration surfaces

There are three configuration surfaces. They accept the same keys and
produce identical results; `tests/test_input_surface_parity.py` checks
the parity.

1. **The workbook** (`inputs/input.xlsx`) is the primary surface.
   Every parameter is a row on one of the ten kv sheets. The sheets
   are migrated to the canonical schema by
   `python scripts/polish_input_workbook.py`, which drops removed
   keys, appends new ones in template order, creates missing sheets,
   and preserves existing values by key.
2. **A YAML / JSON config** (`pvbess --config run.yaml`). Sections
   mirror the sheets key for key. The timeseries comes from a
   `timeseries_path` CSV / Parquet file or an inline list. The config
   is materialized to a real workbook that re-enters the same read
   path, so results are identical by construction. Unknown or
   misplaced keys warn and are ignored, exactly like the workbook
   loader.
3. **Scenario overrides.** The workbook `scenarios` sheet and a
   `--scenarios file.yaml` share one resolution path. Every
   `<sheet>.<key>` dotted target (plus the documented aliases and the
   `balancing` / `capex_multiplier` specials) is reachable, and an
   unknown target raises before any solver time is spent.

## Input workbook reference

The canonical workbook is `inputs/input.xlsx`. Every sheet's first row
uses the shared header style: white bold text on a navy `#1F3864` fill
with a thin `#BFBFBF` bottom border, frozen so it stays visible while
scrolling, plus AutoFit column widths. Every workbook the tool writes
goes through the same styler (`pvbess_opt/io_style.py`), so inputs and
outputs are formatted identically.

### `timeseries`

15-minute series of `timestamp`, `pv_kwh`, optionally `load_kwh`
(required for `self_consumption`, ignored for `merchant`),
`dam_price_eur_per_mwh`, the nine optional per-product balancing
price columns (`fcr_capacity_price_eur_per_mwh`,
`afrr_up_capacity_price_eur_per_mwh`,
`afrr_up_activation_price_eur_per_mwh`, etc.), and the optional
`ida_price_eur_per_mwh` intraday price (required when the intraday
venue is enabled). `pv_kwh` is the single
PV column: fill it to source PV from the timeseries, or leave it empty
and set a location on the `pv` sheet to source it from PVGIS instead.

### `project`

Project-level scalars including `mode`
(`self_consumption` | `merchant`), `project_lifecycle_years`,
`project_start_year`, `p_grid_export_max_kw`, the optional grid
import limit `p_grid_import_max_kw`,
`retail_tariff_eur_per_mwh`, `allow_bess_grid_charging`, the
grid-charging fee wedge (`grid_charging_fee_eur_per_mwh`,
`grid_charging_fee_exempt`), the exogenous-curtailment block
(`curtailment_pct`, `curtailment_compensated_pct`,
`curtailment_compensation_price_eur_per_mwh`),
`grid_cap_includes_load`, `unavailability_pct`, and the site-wide lump
sums (`site_capex_eur`, `site_devex_eur`). Two presentation knobs also
live here: `currency_format` (`auto` | `millions` | `raw`, the axis /
label currency scaling) and `show_titles` (render plot titles; off by
default). The balancing master switch (`balancing_enabled`) lives on
the `balancing` sheet.

### `pv`

`pv_source` (`auto` | `file` | `pvgis`), the PVGIS location / geometry
(`latitude`, `longitude`, `tilt`, `azimuth`, `losses_pct`,
`weather_year`, `raddatabase`, `timeseries_path`), `pv_nameplate_kwp`, and the
degradation coefficients. The `pv_kwh` column is consumed verbatim
(absolute kWh per step); `pv_nameplate_kwp` is metadata for per-kW
CAPEX / OPEX and the sizing sweep. `auto` uses the `pv_kwh` column
when it is filled and otherwise fetches the profile from the location,
so one input file covers both a user-supplied PV series and a bare
location.

### `bess`

`bess_power_kw` (symmetric charge / discharge limit),
`bess_capacity_kwh`, the one-way efficiencies (`efficiency_charge` and
`efficiency_discharge`, default 0.95 each, round-trip 0.9025), SOC
bounds, the cycle caps (`max_cycles_per_day` and the optional
`max_cycles_per_year` with `cycle_cap_basis` ∈
`nameplate | faded` — the warranty basis the annual cap counts
against), and the calendar and per-cycle fade
coefficients. The capacity plan is also configured here:
`bess_overbuild_pct` (Year-1 energy overbuild above nameplate) and
scheduled augmentations (`bess_augmentation_years`,
`bess_augmentation_kwh`, `bess_augmentation_mode` ∈
`top_up | fixed_kwh`), priced on a declining cost curve
(`bess_cost_decline_pct_per_year`) with `bess_replacement_cost_pct`
scaling the replacement outlay. `capex_bess_eur_per_kwh` is the full installed BESS
CAPEX per kWh of nameplate energy capacity (default 250; Lazard
benchmark band 215-315 EUR/kWh); DEVEX and OPEX stay per kW of the
power block. The replacement policy is `bess_replacement_year`:
N = replace in project year N (the SOH threshold is then ignored),
blank or `auto` = replace in the first year SOH reaches
`bess_eol_soh_pct` (the replacement CAPEX is charged in the cashflow
in that year), 0 = never replace. `bess_wear_cost_eur_per_mwh`
(default 10) is a dispatch shadow price only and is never charged in
the cashflow.

### `economics`

Discount rate, OPEX inflation, per-stream revenue indexation
(`retail_inflation_pct`, `dam_inflation_pct`), the energy-aggregator fee
(`aggregator_fee_pct_revenue`, on DAM + retail) and the optional,
separate balancing-aggregator / BSP fee
(`balancing_aggregator_fee_pct_revenue`, on gross balancing revenue;
default 0), the structural market-access fees
(`route_to_market_fee_eur_per_mwh`, a per-MWh representation charge on
exported energy, and `optimizer_revenue_share_pct`, a share of the
positive BESS trading margin; both default 0),
LCOE / LCOS benchmark-band overrides, the five
sensitivity-tornado deltas (CAPEX / OPEX / revenue / discount-rate /
PPA-price), the debt layer (`gearing_pct`, `debt_interest_rate_pct`,
`debt_tenor_years`, `debt_repayment` incl. a DSCR-level `sculpted`
profile, plus target-DSCR debt sizing via `debt_sizing_mode` /
`target_dscr` — debt sized to a lender covenant with gearing reported
as an output — and the P90 production lender case
`production_p90_factor_pct` / `lender_cases_enabled`), and
grid-emissions intensity
for the optional 24/7-CFE accounting. The contracted-revenue and
fiscal blocks live here too, all shipped off: BESS tolling
(`bess_toll_*`), the optimizer floor + share
(`optimizer_floor_*`, `optimizer_margin_basis`, term window), state
support with a two-way clawback (`state_support_*`), the capacity
market (`capacity_market_*`), the revenue levy (`revenue_levy_pct`),
the guarantees-of-origin price (`go_price_eur_per_mwh`), and
depreciation + corporate tax (`depreciation_years_*`,
`corporate_tax_rate_pct`, `tax_loss_carryforward_years`, plus the
`sensitivity_tax_rate_delta_pp` tornado driver). Per-asset
CAPEX / DEVEX / OPEX
live on the `pv` and `bess` sheets; the site-wide lump sums on
`project`.

### `simulation`

Master uncertainty switch, per-source enable flags and log-normal
sigmas (including the intraday pair `uncertainty_ida_enabled` /
`uncertainty_sigma_ida`), the rolling-horizon window / commit
geometry, plot-scope flags
(`plot_daily_scope` / `plot_monthly_scope` / `plot_yearly_scope`
∈ `none | year1_only | all`), the uncertainty diagnostics flag, the
imbalance settlement block (`imbalance_enabled`, `imbalance_pricing`,
the long / short price multipliers), the tail-risk metrics
(`risk_metrics_enabled`, `risk_alpha_pct` — VaR / CVaR on the Monte
Carlo ensemble), and `midlife_resolve_year` (full re-optimisation of
a later operating year to validate the analytic scaling).

### `max_injection_profile`

Hour-of-day cap profile (24 rows), optionally with one column per
calendar month, expressing the share of `p_grid_export_max_kw`
available for export. The default is 100 %, which means no
curtailment; opt in to curtailment by editing the sheet. If the sheet
is missing the loader falls back to a flat 100 % and logs INFO. Two
optional per-source sheets, `max_injection_profile_pv` and
`max_injection_profile_bess`, share the identical schema and impose a
sub-cap on the PV and BESS export legs respectively (the combined cap
still binds); omit them for a single shared cap.

### `balancing`

36 keys covering the master switch (`balancing_enabled`), per-product
capacity shares of `bess_power_kw` (`fcr_capacity_share_pct`,
`afrr_up_capacity_share_pct`, `afrr_dn_capacity_share_pct`,
`mfrr_up_capacity_share_pct`, `mfrr_dn_capacity_share_pct`),
acceptance and activation probabilities, fallback capacity and
activation prices, the FCR sustained-duration requirement, the SOC
safety buffer, a balancing-revenue inflation rate, the reservation
block structure (`bm_block_hours` — reservations committed in fixed
multi-hour blocks rather than per step), an aFRR merit-order
acceptance curve (`bm_merit_order_enabled` — acceptance probability
falling with the offered volume share), and the Monte Carlo
price sigmas, scenario count (`bm_mc_scenarios`) and seed. See
[`docs/balancing_market_design.md`](docs/balancing_market_design.md)
for the design document.

### `ppa`

PPA contract engine mirroring the `balancing` master-switch pattern:
`ppa_enabled`, `ppa_structure` (`pay_as_produced` on a share of the
PV export, or `baseload` — a contracted flat band settled financially
against total export, with raw shortfall/excess coverage KPIs),
`ppa_settlement` (`physical` | `cfd`; baseload is cfd-only),
`ppa_price_eur_per_mwh`, `ppa_volume_share_pct`, `ppa_term_years`,
`ppa_inflation_pct`, `ppa_negative_price_rule` (negative-hour
suspension clause), `ppa_baseload_mw`. Ships disabled: until the
switch is set, outputs are bit-identical to a build without the PPA
engine. See [`docs/ppa_design.md`](docs/ppa_design.md) for the design
note (structures, settlements, dispatch treatment, fee and LCOE
scope). The state-support settlement engine also lives on this sheet
(mutually exclusive with an active PPA): `support_scheme` ∈
`none | sliding_fip | cfd_two_way`, `support_strike_eur_per_mwh`,
`support_ref_period`, `support_term_years`, and
`support_negative_hour_suspension`.

### `intraday`

The intraday venue (merchant-only): `id_enabled` master switch,
`id_max_deviation_frac_of_cap` (the per-step deviation budget as a
share of the export cap, default 0.25), `id_allow_purchases` (allow
buy-backs of the committed position), `id_fee_eur_per_mwh` (venue fee
on traded volume), and `id_inflation_pct` (margin indexation in the
multi-year cashflow). Requires the `ida_price_eur_per_mwh` timeseries
column, and in this release is mutually exclusive with balancing, the
PPA engine, a support scheme, the imbalance settlement, and the
mid-life re-solve. Ships disabled: until the switch is set, outputs
are bit-identical to a build without the venue. See
[`docs/intraday_design.md`](docs/intraday_design.md) for the design
note.

### `sizing`

Optional capacity-sweep grid, columnar: one column per axis
(`pv_nameplate_kwp`, `bess_power_kw`, and either `bess_capacity_kwh`
or `bess_duration_hours`), one value per row, gated by an `enabled`
TRUE / FALSE toggle in the first data row. Ships disabled with a
worked example. Set `enabled` to `TRUE` to sweep the Cartesian product
of the axes, rank an efficient frontier by NPV, and emit `sizing.xlsx`
plus the frontier / break-even plots. A YAML / JSON config expresses
the same sweep as a `sizing:` block.

### `scenarios`

Optional batch comparison, tidy / long (one override per row, grouped
by `name`; blank `name` cells inherit the row above), gated by an
`enabled` TRUE / FALSE toggle in the first data row. Ships disabled
with a worked example. Each row's `target` is a dotted path
(`project.mode`, `bess.power_kw`) or a bare special (`balancing`,
`capex_multiplier`), and `inherits` clones another scenario. Set
`enabled` to `TRUE` to run every named variant in one pass and emit a
styled `scenario_comparison.xlsx` plus comparison plots.
`--scenarios file.yaml` is the config equivalent. The `sizing` and
`scenarios` sheets are mutually exclusive.

### `trajectories`

Optional per-year stream shaping, tidy / long (one row per
`(stream, year)` multiplier; blank `stream` / `mode` cells inherit the
row above), gated by an `enabled` TRUE / FALSE toggle in the first
data row and shipped disabled with a worked example. It shapes a
revenue or cost stream year by year instead of the flat
`(1 + i)^(y-1)` compounding — a price-cannibalisation path, an
ancillary-services price decay, a stepped OPEX profile. Each stream's
`mode` is `replace` (the vector is the escalation index) or `overlay`
(the vector multiplies the compounded index). Combine with the
scenario price decks: the deck sets the Year-1 price level (dispatch
re-solves), the trajectory sets the years-2+ shape. A YAML / JSON
config expresses the same block as `trajectories:`.

### `market_data`

Optional key / value sheet selecting the Year-1 price basis
(`docs/market_scenarios_design.md`, Layer A). With every source at its
`file` default the workbook columns are used untouched. `price_source
= entsoe` fetches the `price_reference_year` day-ahead series for the
selected `bidding_zone` from the ENTSO-E Transparency Platform and
replaces `dam_price_eur_per_mwh` wholesale; `intraday_source = entsoe`
does the same for `ida_price_eur_per_mwh` from the selected SIDC
intraday auction (`intraday_auction`: `ida1` / `ida2` / `ida3`; the
continuous intraday market is exchange-proprietary and not fetchable);
`balancing_source` / `imbalance_source` accept `auto` (per-zone
registry: GR → ADMIE, else ENTSO-E) or an explicit provider. Fetched
prices are intensive quantities resampled onto the model grid per
`price_resample_policy` (`step_hold`: a coarser native price is held
across the finer steps, a finer one averaged). Fetches
cache on disk (`market_cache_dir`, `market_fetch_mode`: `cache_first`
/ `refresh` / `offline`). The ENTSO-E token comes from the
`entsoe_token` cell or the environment variable named by
`entsoe_token_env` — the shipped template keeps the cell empty; never
commit a token.

### `scenario_engine`

Optional key / value sheet arming the multi-year price-scenario layer
(`docs/market_scenarios_design.md`, Layer B). `price_scenarios_enabled
= FALSE` (the default) keeps years 2..N on the flat inflation indices,
bit-identical. Armed, `scenario_projection_mode` picks the projection
tier: `reprice` revalues the frozen Year-1 dispatch against each
year's scenario curve, `resolve` additionally re-solves the MILP at
`scenario_resolve_years` on a coarser grid
(`scenario_resolve_resolution`, hourly by default) with factors
interpolated between support years (`scenario_interp`), and
`trajectory_only` leaves the declared `trajectories` sheet in charge.
`price_basis` / `price_base_year` / `cpi_pct` bridge real vendor decks
to the nominal cashflow; `debt_sizing_scenario` names the scenario the
debt is sized on (every ensemble member inherits that schedule);
`support_ref_follows_scenario` decides whether CfD / FiP reference
legs follow the scenario path (default) or stay on the plain
`dam_inflation_pct` index.

### `price_scenarios`

Optional tidy sheet (gated by the first data row's `enabled` cell)
listing the scenario decks: `name`, `provider` (`file` for a
ready-made store directory, `parametric` for the three-knob generator
driven by the workbook's own Year-1 prices, `tyndp` for the free
ENTSO-E TYNDP milestone curves), `vintage`, `weight_pct` (must sum to
100), `store_path` (resolved against the workbook), `notes`. Each
store is a directory with `meta.yaml`, per-year `dam.csv` curves and
an optional `balancing_annual.csv` — see the design doc for the
schema.

## Output reference

Each run writes a self-contained folder
`results/<input>_<scenario>_<timestamp>/`:

```
00_summary/          SUMMARY.md (headline digest), run_log.txt
01_inputs/           input_snapshot.xlsx, assumptions_summary.txt
02_dispatch/         dispatch_timeseries.xlsx (one sheet per calendar year)
03_results.xlsx      kpis_year1 | kpis_monthly_year1 | dispatch_year1 |
                     cashflow_yearly | cashflow_quarterly | cashflow_monthly |
                     financial_kpis | sensitivity_analysis |
                     lifetime_dispatch_yearly | economic_assumptions |
                     degradation (+ debt_schedule / lender_cases /
                     emissions / rolling-horizon sheets when enabled;
                     market_data_provenance on API-sourced runs;
                     scenario_price_paths / scenario_resolve_delta /
                     price_scenario_ensemble when price scenarios are
                     armed)
04_financial_plots/  revenue stack, BESS waterfall/by-month/split,
                     balancing reservation + MC, lifetime cycles,
                     cumulative + monthly cashflow, payback, NPV/IRR
                     tornados, NPV waterfall, LCOE/LCOS, SOH, plus the
                     DSCR profile (levered runs), the intraday venue
                     figures (intraday runs), and the 24/7-CFE duration
                     curve when emissions accounting is on (full list
                     below)
05_energy_plots/     Year-1 energy-flow diagram (every run) and the
                     lifetime summary chart, plus daily / monthly /
                     yearly dispatch views per year
06_uncertainty_plots/ per-source forecast bands and seasonal boxplots,
                     DAM heatmap, calibration diagnostics
```

### KPIs

`compute_kpis` returns a flat dict with the headline year-1 figures
plus per-product balancing breakdowns and nine canonical revenue
aggregates used by the financial plots:

* `revenue_pv_dam_eur`: PV → DAM exports (under a physical PPA, the
  uncovered share only).
* `revenue_pv_ppa_eur`: the PPA contract leg on the covered share of
  PV export (`0.0` without an active contract).
* `revenue_bess_dam_eur`: BESS-DAM arbitrage net of grid charging.
* `revenue_self_consumption_eur`: load coverage from PV-direct and
  BESS-discharge; `0.0` in merchant mode.
* `revenue_bess_fcr_eur`: FCR capacity revenue.
* `revenue_bess_afrr_up_eur`: aFRR-up capacity + activation.
* `revenue_bess_afrr_dn_eur`: aFRR-dn capacity + activation.
* `revenue_bess_mfrr_up_eur`: mFRR-up capacity + activation.
* `revenue_bess_mfrr_dn_eur`: mFRR-dn capacity + activation.

### Lifetime projection

`build_lifetime_dispatch` and `aggregate_lifetime_to_yearly` produce
the per-step / per-year frame consumed by `compute_financial_kpis`
to derive NPV, IRR, ROI, BCR, LCOE, LCOS, and payback year.

### Monte Carlo

`pvbess_opt.rolling_horizon.monte_carlo_rolling` samples log-normal
forecast noise on DAM price, PV, and load beyond the commit horizon
and re-optimises a rolling-window dispatch per seed, evaluating
realised KPIs against the noise-free actuals. Output is one row per
seed (`profit_total_eur`, grid import/export, curtailment, cycles, and
the `foresight_gap_pct` against the perfect-foresight benchmark); the
pipeline reports its P10 / P50 / P90. Seed KPIs share the headline-KPI
scope (the same unavailability derate and the same year-close SOC
condition as the benchmark), so `foresight_gap_pct` is non-negative up
to solver tolerance — and the pipeline enforces the bound in practice:
if any realisation lands above the benchmark incumbent (inside its
`mip_gap` slack), the benchmark is re-solved at tighter gaps until it
is the best case, the gap column and percentiles are recomputed, and
the gap used is reported as the `pf_benchmark_mip_gap` KPI — so the
perfect-foresight marker bounds the distribution from above (see
`pvbess_opt/conventions.md`). The escalation stops after one
unimproved re-solve — an identical incumbent means the `--time-limit`
terminated the search, and the log then advises a higher limit or a
faster solver instead of repeating it. `--mip-gap` is a requested
target, not a guarantee (the time limit can bind first), so the run
records both `pf_benchmark_mip_gap` (requested) and
`pf_benchmark_gap_achieved` (what the solver actually proved — the
number to quote in a publication). Balancing
capacity / activation prices are perturbed separately by
`pvbess_opt.rolling_horizon.monte_carlo_balancing`.

Three opt-in extensions refine the ensemble. With
`risk_metrics_enabled` the profit distribution is additionally
summarised as VaR / CVaR at the `risk_alpha_pct` tail. With
`imbalance_enabled` each seed's committed-vs-actual volume deviations
settle at an imbalance price (single or dual pricing with long / short
multipliers) instead of being valued at the DAM price. On intraday
runs the ensemble is two-stage: every seed's committed schedule is
re-dispatched once against the actual intraday prices, the benchmark
becomes the two-stage perfect-foresight profit (so the foresight gap
stays a like-for-like comparison), and a per-seed
`id_net_revenue_eur` column reports the venue margin.

### PDF report

Generated under `results/<run>/04_financial_plots/`:

* Yearly revenue stack (PV-load, BESS-load, PV-DAM, BESS-DAM, the PPA
  contract leg when a contract is on, 5 balancing products, and every
  fee that is set — energy aggregator, balancing aggregator,
  route-to-market, optimizer share — plus the grid-charging cost) with
  net line.
* BESS revenue waterfall: one chart stepping from BESS-DAM through
  every balancing product, then down by the battery's exact share of
  each route-to-market fee, to the total BESS revenue.
* BESS revenue capacity-vs-activation split: grouped bar per product.
* BESS revenue by month: 12 stacked bars of BESS-DAM + 5 balancing
  products per calendar month, with the two fee shares as negative
  bars below zero.
* Lifetime cycles per operating year.
* Battery state-of-health trajectory (calendar plus cycle fade, with
  the replacement reset when one is scheduled).
* Yearly cashflow bars (revenue / OPEX / CAPEX stacked, net line).
* Cumulative cashflow + payback marker.
* Monthly cashflow Year 1 (CAPEX / DEVEX events booked in month 12, so
  the monthly and yearly DCFs agree).
* NPV / IRR tornado plots. Drivers: CAPEX, OPEX, revenue, discount
  rate (NPV only), and PPA price when a contract is on.
* NPV waterfall.
* LCOE / LCOS summary with Lazard 2024 benchmark band.
* Debt-service coverage profile over the tenor (levered runs only),
  with the P90 lender-case line and the target-DSCR reference when
  those are active.
* Rolling-horizon Monte Carlo distribution.
* Balancing reservation profile + Monte Carlo distribution per
  product.
* Day-ahead vs intraday price duration curves and the per-step
  intraday net position, emitted only when the intraday venue ran.
* 24/7 carbon-free energy duration curve, emitted only when emissions
  accounting is on (`grid_co2_intensity_kg_per_mwh > 0`).

Energy plots under `results/<run>/05_energy_plots/`: the Year-1
energy-flow diagram (PV / BESS / grid / load flows, rendered for every
run) and the lifetime summary chart
(`lifetime_summary_<start>-<end>.pdf`), plus daily / monthly / yearly
supply, surplus, combined, dispatch, SOC, and revenue views, and the
merchant trio when the mode is `merchant`.

## Results gallery

Real output from three scenarios rendered on the shipped `inputs/input.xlsx`
(PV 15 MWp, BESS 15 MW / 30 MWh, 15 MW grid-export cap, 20-year
horizon, 7 % discount, retail 120 EUR/MWh); the export script
additionally enables BESS grid charging (`allow_bess_grid_charging =
TRUE`, shipped as `FALSE`) so the figures show the complete feature
set. Regenerate with `python scripts/export_readme_figures.py`, which
renders the PDF report figures as PNG through the same styler
(`set_figure_format`).

### Merchant + balancing (`--mode merchant`, `balancing_enabled = TRUE`, `allow_bess_grid_charging = TRUE`)

PV + BESS dispatching to the day-ahead market with FCR / aFRR / mFRR
participation stacked on the battery and grid-charging arbitrage
enabled (the battery may buy cheap hours and resell expensive ones).

![merchant energy flows](docs/assets/merchant_energy_flow.png)

*Year-1 energy flows: PV generation and grid purchases routed through
the battery to the grid, with round-trip losses shown explicitly (no
PV is curtailed in this scenario, so no curtailment sink is drawn).
Ribbon colours match the dispatch plots, so each flow reads the same
across the whole report.*

![merchant daily dispatch and SOC](docs/assets/merchant_daily_dispatch_soc.png)

*A representative summer day: dispatch per 15-minute step with the
battery state of charge overlaid.*

![merchant yearly revenue stack](docs/assets/merchant_revenue_stack.png)

*Yearly revenue stack per operating year with the five balancing
products stacked on the battery, and the deduction bands below zero:
grid-charging cost, the balancing-aggregator (BSP) fee, the per-MWh
route-to-market fee on exports, and the optimizer revenue share on the
battery's positive trading margin.*

![merchant BESS revenue waterfall](docs/assets/merchant_bess_revenue_waterfall.png)

*BESS revenue waterfall: DAM arbitrage plus each balancing product,
stepped down by the battery's exact share of each route-to-market fee
to the total battery revenue.*

![merchant monthly cashflow](docs/assets/merchant_monthly_cashflow.png)

*Year-1 monthly net cashflow: revenue and OPEX bars with the net line.*

![merchant cumulative cashflow](docs/assets/merchant_cumulative_cashflow.png)

*Cumulative undiscounted and discounted cashflow over the project
life; each payback marker is drawn only when its curve crosses zero.*

![merchant NPV waterfall](docs/assets/merchant_npv_waterfall.png)

*Discounted yearly contributions to the total NPV with the cumulative
NPV line.*

![merchant NPV tornado](docs/assets/merchant_npv_tornado.png)

*NPV sensitivity tornado: one-at-a-time CAPEX, revenue, discount-rate
and OPEX perturbations around the base case.*

![merchant LCOE band](docs/assets/merchant_lcoe_band.png)

*Levelised cost of energy for the PV side against the Lazard 2024
utility-scale PV band.*

![merchant LCOS band](docs/assets/merchant_lcos_band.png)

*Levelised cost of storage against the Lazard 2024 LCOS benchmark
band.*

![merchant battery state of health](docs/assets/merchant_soh_trajectory.png)

*Battery state of health over the project life: calendar plus cycle
fade, with the SOH-triggered replacement (the pack crosses the 70 %
threshold in year 9 under grid-charging duty) resetting to 100 %.*

![Merchant foresight-gap distribution](docs/assets/merchant_foresight_distribution.png)

*Rolling-horizon Monte Carlo profit distribution (8 seeds, 48 h
window / 24 h commit) against the perfect-foresight benchmark, with
balancing participation and grid charging active. Produced by the
gallery export script's rolling-horizon run (equivalent to
`--rolling-horizon --monte-carlo 8`).*

### Self-consumption (`--mode self_consumption`, `allow_bess_grid_charging = TRUE`)

Behind-the-meter PV + BESS serving a co-located load at the retail
tariff and exporting only the surplus to the DAM, with no balancing.
Grid charging is enabled, so the battery may also top up from the
grid in cheap hours to cover later load.

![self-consumption energy flows](docs/assets/self_consumption_energy_flow.png)

*Year-1 energy flows: PV serving the load directly, charging the battery and exporting the surplus, grid imports covering the residual load, and the battery's round-trip losses made visible. Ribbon colours match the dispatch
plots, so each flow reads the same across the whole report.*

![self-consumption daily dispatch and SOC](docs/assets/self_consumption_daily_dispatch_soc.png)

*A representative summer day: dispatch per 15-minute step with the
battery state of charge overlaid.*

![self-consumption yearly revenue stack](docs/assets/self_consumption_revenue_stack.png)

*Yearly revenue stack per operating year with the deduction bands below
zero: grid-charging cost and the per-MWh route-to-market fee on exports
(the optimizer share clamps to zero here — a grid-charging battery's
trading margin is negative, and an optimizer never invoices a share of
a loss).*

![self-consumption BESS revenue waterfall](docs/assets/self_consumption_bess_revenue_waterfall.png)

*BESS revenue waterfall: DAM arbitrage,
stepped down by the battery's exact share of each route-to-market fee
to the total battery revenue.*

![self-consumption monthly cashflow](docs/assets/self_consumption_monthly_cashflow.png)

*Year-1 monthly net cashflow: revenue and OPEX bars with the net line.*

![self-consumption cumulative cashflow](docs/assets/self_consumption_cumulative_cashflow.png)

*Cumulative undiscounted and discounted cashflow over the project
life; each payback marker is drawn only when its curve crosses zero.*

![self-consumption NPV waterfall](docs/assets/self_consumption_npv_waterfall.png)

*Discounted yearly contributions to the total NPV with the cumulative
NPV line.*

![self-consumption NPV tornado](docs/assets/self_consumption_npv_tornado.png)

*NPV sensitivity tornado: one-at-a-time CAPEX, revenue, discount-rate
and OPEX perturbations around the base case.*

![self-consumption LCOE band](docs/assets/self_consumption_lcoe_band.png)

*Levelised cost of energy for the PV side against the Lazard 2024
utility-scale PV band.*

![self-consumption LCOS band](docs/assets/self_consumption_lcos_band.png)

*Levelised cost of storage against the Lazard 2024 LCOS benchmark
band.*

![self-consumption battery state of health](docs/assets/self_consumption_soh_trajectory.png)

*Battery state of health over the project life: calendar plus cycle
fade, with the SOH-triggered replacement (the pack crosses the 70 %
threshold in year 9 under grid-charging duty) resetting to 100 %.*

![Self-consumption foresight-gap distribution](docs/assets/self_consumption_foresight_distribution.png)

*Rolling-horizon Monte Carlo profit distribution (8 seeds, 48 h
window / 24 h commit) against the perfect-foresight benchmark: the
realistic-forecast dispatch lands within about one percent of the
theoretical optimum (grid-charging arbitrage widens the gap versus
the shipped no-grid-charging workbook, whose median gap is about half
a percent). Produced by the gallery export script's rolling-horizon
run (equivalent to `--rolling-horizon --monte-carlo 8`).*

### Merchant + intraday venue (`--mode merchant`, `id_enabled = TRUE`)

The two-stage re-dispatch on top of the committed day-ahead schedule.
The shipped deck carries day-ahead prices only, so the export script
derives an illustrative intraday deck from it — an evening scarcity
premium (+15 EUR/MWh, 17:00-21:00) and a midday PV-glut discount
(−12 EUR/MWh, 10:00-15:00) — and enables the venue with its default
deviation budget (25 % of the export cap per step) and a 1 EUR/MWh
venue fee. Balancing is off (the venue and balancing are mutually
exclusive in this release).

![merchant intraday price duration](docs/assets/merchant_intraday_price_duration.png)

*Day-ahead vs intraday price duration curves, each sorted descending
over the share of time: the venue spread that the second-stage
re-dispatch monetises.*

![merchant intraday net position](docs/assets/merchant_intraday_position.png)

*Per-step intraday net position — sells positive, buys negative —
bounded by the deviation budget on either side.*

![merchant intraday revenue stack](docs/assets/merchant_intraday_revenue_stack.png)

*Yearly revenue stack with the intraday margin stacked above zero and
the venue fee among the deduction bands below zero; the day-ahead
streams are unchanged by construction of the spread-form settlement.*


## Methodology & conventions

The dispatch MILP is solved once for a representative Year 1; Years
2..N are derived analytically. Commercial tools (Gridcog, Aurora,
HOMER) use the same fast-mode approach. The following conventions
apply to every sheet, KPI, and plot:

* **Year convention.** Year 0 carries CAPEX + DEVEX only at calendar
  `project_start_year - 1`; Year 1 is the first operating year with
  degradation factor 1.0. Escalation indices use `(1 + i)^(y-1)`;
  discounting uses `1/(1+r)^y` (end-of-year), refined to end-of-month
  `1/(1+r)^((y-1)+m/12)` on the monthly sheet.
* **Investment outlays.** `initial_investment_eur` is the Year-0
  outlay (per-asset CAPEX + DEVEX + site lump sums) and matches the
  Year-0 bar in the plots; `total_capex_eur` / `total_capex_devex_eur`
  are lifecycle totals that also include the BESS replacement CAPEX.
  `roi_pct` = operating net cashflow (Years 1..N) over
  `|initial_investment_eur|`.
* **Degradation.** PV: Year-2 LID then linear. BESS: multiplicative
  calendar fade minus additive cycle fade, with an optional
  replacement (scheduled year N, or automatic in the first year SOH
  reaches `bess_eol_soh_pct`; the fade reset and the replacement CAPEX
  land in the same year). One implementation (`lifetime._bess_factor`)
  drives the cashflow, the lifetime scaling, the SOH diagnostic, and
  the fade decomposition; a cross-module test sweep keeps them
  numerically identical.
* **Availability.** `unavailability_pct` is applied once, post-solve,
  to the Year-1 energy / revenue KPIs and the lifetime aggregates.
* **Energy aggregator fee.** A non-negative deduction applied once to
  the gross DAM + retail revenue only; balancing revenue (TSO-settled)
  and PPA revenue (bilateral offtake) never carry it. Balancing
  revenue may instead carry its own optional route-to-market fee
  (`balancing_aggregator_fee_pct_revenue`, default 0).
* **PPA stream.** The contract leg is its own cashflow column with its
  own indexation. After `ppa_term_years` the covered volume's DAM
  value reverts into the DAM stream (where the fee applies), and a
  disabled contract leaves every output bit-identical to a build
  without the PPA engine.
* **LCOE / LCOS.** Lazard-style: per-asset CAPEX / DEVEX / OPEX (plus
  discounted BESS replacement) over discounted delivered MWh.
  Site-wide lump sums, balancing revenue, and PPA revenue are excluded
  by convention. `capex_bess_eur_per_kwh` is the full installed cost
  per kWh of nameplate energy capacity, so the LCOS numerator is
  directly comparable to the Lazard band (215-315 EUR/kWh for
  utility-scale 4-hour Li-ion).
* **Battery wear cost.** An optional €/MWh shadow price (default 10)
  that shapes dispatch only. It is never added to the cashflow, so
  degradation is not double-counted with the replacement CAPEX.

Limitations: dispatch is optimised for a given size (no capacity
search beyond the optional sweep); years 2..N are scaled, not
re-solved; the regulatory model covers the self-consumption and
merchant regimes; balancing participation is expected-value in the
MILP with Monte Carlo realisation ex-post.

## Documentation map

The mathematical specification lives in the domain design documents
under [`docs/`](docs/README.md). All documents follow one template,
share one notation table, and map every numbered equation to its
implementing symbol:

* [`docs/self_consumption_design.md`](docs/self_consumption_design.md):
  the self-consumption MILP. Variables, objective, every hard
  constraint, and the ten audit invariants (machine-checked against
  the built model by `tests/test_logic_spec_conformance.py`).
* [`docs/merchant_design.md`](docs/merchant_design.md): the merchant
  regime. Pinning constraints, cap semantics, the merchant objective.
* [`docs/balancing_market_design.md`](docs/balancing_market_design.md):
  FCR / aFRR / mFRR reservations, expected-value MILP terms, the six
  balancing invariants, and the verification appendix.
* [`docs/ppa_design.md`](docs/ppa_design.md): the PPA engine.
  Settlements, the `(1-s)·DAM + s·strike` dispatch price, term and
  reversion.
* [`docs/intraday_design.md`](docs/intraday_design.md): the intraday
  venue. The committed day-ahead position, the deviation budget, the
  origin split and anti-wash-trading rules, the spread-form margin,
  the fee applicability matrix, and the two-stage Monte Carlo.
* [`docs/economics_design.md`](docs/economics_design.md): year
  conventions, the nine revenue aggregates, fee clamp, degradation
  factors, debt, NPV/IRR/payback, LCOE/LCOS.
* [`docs/market_scenarios_design.md`](docs/market_scenarios_design.md):
  the market-data ingestion layer (zones, ENTSO-E / ADMIE / HEnEx
  providers, the calendar contract, bypass semantics) and the
  multi-year price-scenario layer (scenario stores, the parametric /
  TYNDP adapters, Tier-1 repricing and Tier-2 support-year re-solves,
  capture KPIs, the weighted ensemble).
* [`docs/uncertainty_design.md`](docs/uncertainty_design.md):
  rolling-horizon MC, the foresight gap, balancing MC, imbalance
  settlement, VaR / CVaR, sensitivity drivers.

The Sphinx manual is published at
[pv-bess-optimizer.readthedocs.io](https://pv-bess-optimizer.readthedocs.io/en/latest/)
(build locally with `make -C docs html`). It carries the user's guide
(installation, workbook reference, outputs, CLI) and links to the
design docs for the formulation; cross-module consistency rules live
in [`pvbess_opt/conventions.md`](pvbess_opt/conventions.md).

## Citing

If this tool contributes to academic work, please cite it as:

```
Konstantellos, L. (2026). PV & BESS Optimizer (v1.0.0): MILP dispatch
and project-finance pipeline for co-located PV and battery systems.
https://github.com/lamproskonstantellos/pv-bess-optimizer
```

BibTeX:

```bibtex
@software{konstantellos_pv_bess_optimizer_2026,
  author  = {Konstantellos, Lampros},
  title   = {{PV} \& {BESS} Optimizer: {MILP} dispatch and
             project-finance pipeline for co-located {PV} and battery
             systems},
  version = {1.0.0},
  year    = {2026},
  url     = {https://github.com/lamproskonstantellos/pv-bess-optimizer}
}
```

A [`CITATION.cff`](CITATION.cff) ships at the repository root, so
GitHub's "Cite this repository" button serves the same metadata.

## License

© 2025-2026 Lampros Konstantellos. All rights reserved. The source is
published for reading and for non-commercial academic evaluation only
(peer review and the verification or reproduction of published
results); reproduction, redistribution, modification, sublicensing,
sale, or any other use requires the prior written consent of the
copyright holder. See [LICENSE](LICENSE) for the exact terms.

## Development

### Solver

HiGHS via `highspy` is the default. Gurobi and CBC are picked up
automatically if installed.

### Running tests

```bash
pip install -r requirements/dev.txt
pytest                       # fast lane (default)
pytest -m slow               # opt-in real-scale workbook suite (minutes wall-clock)
```

### Code style

`ruff check pvbess_opt tests scripts` is the project lint pass.
The project rule set is `F,E,I,B,UP,ARG,RUF` with `RUF001/002/003`
ignored (the codebase uses Unicode intentionally: `→` in energy-flow
labels, `€` in prices, Greek-letter docstring math).

## Quality

Every module is covered by the test suite in `tests/` (fast lane:
`python -m pytest`; slow lane: `python -m pytest -m slow`). CI runs
ruff, mypy, vulture, and the fast test lane on every push, plus the
slow lane nightly and a docs build.
