# PV & BESS Optimizer

[![license](https://img.shields.io/badge/license-All%20Rights%20Reserved-red)](LICENSE)
[![version](https://img.shields.io/badge/version-0.9.0-blue)](pvbess_opt/__init__.py)
[![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](pyproject.toml)
[![ci](https://github.com/lamproskonstantellos/pv-bess-optimizer/actions/workflows/ci.yml/badge.svg)](https://github.com/lamproskonstantellos/pv-bess-optimizer/actions/workflows/ci.yml)

## What it does

Mixed-integer linear programming model for co-located PV + BESS dispatch
at 15-minute resolution (auto-detected cadence), with a multi-year
project-finance pipeline, stochastic balancing-market participation
(FCR / aFRR / mFRR), a pay-as-produced PPA contract engine, and
rolling-horizon Monte Carlo for uncertainty analysis.

Two regulatory regimes are supported:

* `self_consumption` — co-located load with self-consumption priority.
  Load balance enforces a hard PV→load priority, surplus-only export
  through a binary-free slack, and no simultaneous grid I/O via a tight
  big-M.  Self-consumption is settled at the retail tariff; surplus is
  settled at the day-ahead market price.
* `merchant` — utility-scale dispatch with **no co-located load**.  PV
  and BESS dispatch entirely to the DAM.

Two optional revenue layers stack on **either** regime and both ship
**disabled** (opt-in via their master switch): stochastic
balancing-market participation (FCR / aFRR / mFRR — requires a BESS, and
is TSO-settled fee-free with the SOC safety buffer respected) and a
pay-as-produced PPA on PV export.  Balancing is a property of the battery,
not of the market regime, so it is available in self-consumption and
merchant alike; leave `balancing_enabled = FALSE` on the `balancing` sheet
wherever the asset does not offer the service.

Three asset configurations are supported in both regimes: `hybrid`
(PV + BESS), `pv_only`, and `bess_only`.

**Scope.** This tool optimizes *dispatch* for a **given** PV + BESS size
and computes the resulting project finances — it does not search the
capacity space. Full techno-economic sizing tools (HOMER, Gridcog) sweep
capacities to find an optimum; here the PV nameplate and BESS power and
capacity are inputs. The market model targets the Greek regulatory
regimes (self-consumption and merchant day-ahead, with optional
balancing-market participation).

## Installation

```bash
git clone https://github.com/lamproskonstantellos/pv-bess-optimizer.git
cd pv-bess-optimizer
pip install -r requirements/dev.txt
```

HiGHS is the default solver (`pip install highspy`).  Gurobi and CBC
work too — the solver search order is set in
`pvbess_opt.optimization.choose_solver` (requested solver, then HiGHS,
then CBC).  Solver knobs are CLI flags: `--solver`, `--mip-gap`,
`--time-limit`, `--tee` for live solver output.

## Quickstart

```bash
python main.py inputs/input.xlsx --outdir results/
```

The runner reads the workbook, solves the MILP, computes KPIs and the
multi-year cashflow, runs the rolling-horizon Monte Carlo (when
enabled in the `simulation` sheet), exercises the sensitivity tornado
(when enabled in the `economics` sheet), and writes:

* a multi-sheet results workbook,
* the IEEE-styled PDF report under `results/<run>/04_financial_plots/`,
* the energy plots under `results/<run>/05_energy_plots/`,
* uncertainty diagnostics under `results/<run>/06_uncertainty_plots/`.

Override the workbook value at the CLI:

```bash
python main.py inputs/input.xlsx --mode merchant --outdir results/merchant
```

## Configuration surfaces

Three configuration surfaces exist, and they are exact,
regression-locked mirrors of one another
(`tests/test_input_surface_parity.py`):

1. **The workbook** (`inputs/input.xlsx`) — the primary surface.
   Every parameter is a row on one of the seven kv sheets; the sheets
   are migrated to the canonical schema by
   `python scripts/polish_input_workbook.py` (drops removed keys,
   appends new ones in template order, creates missing sheets,
   preserves your values by key).
2. **A YAML / JSON config** (`pvbess --config run.yaml`) — sections
   mirror the sheets key-for-key, the timeseries comes from a
   `timeseries_path` CSV / Parquet (or an inline list), and the config
   is materialized to a real workbook that re-enters the same read
   path, so results are identical by construction.  Unknown or
   misplaced keys warn and are ignored, exactly like the workbook
   loader.
3. **Scenario overrides** — the workbook `scenarios` sheet and a
   `--scenarios file.yaml` share one resolution path; every
   `<sheet>.<key>` dotted target (plus the documented aliases and the
   `balancing` / `capex_multiplier` specials) is reachable, and an
   unknown target raises before any solver time is spent.

## Input workbook reference

The canonical workbook is `inputs/input.xlsx`.  Every sheet's first row
is the house header accent — white bold text on a navy `#1F3864` fill
with a thin `#BFBFBF` bottom border, frozen so it stays visible while
scrolling, plus AutoFit column widths.  Every workbook the tool *writes*
shares this exact style (one styler in `pvbess_opt/io_style.py`), so
inputs and outputs look identical.

### `timeseries`

15-minute series of `timestamp`, `pv_kwh`, optionally `load_kwh`
(required for `self_consumption`, ignored for `merchant`),
`dam_price_eur_per_mwh`, and the nine optional per-product balancing
price columns (`fcr_capacity_price_eur_per_mwh`,
`afrr_up_capacity_price_eur_per_mwh`,
`afrr_up_activation_price_eur_per_mwh`, etc.).  `pv_kwh` is the **single**
PV column: fill it to source PV from the timeseries, or leave it empty and
set a location on the `pv` sheet to source it from PVGIS instead.

### `project`

Project-level scalars including `mode`
(`self_consumption` | `merchant`), `project_lifecycle_years`,
`project_start_year`, `p_grid_export_max_kw`,
`retail_tariff_eur_per_mwh`, `allow_bess_grid_charging`,
`grid_cap_includes_load`, `unavailability_pct`, and the site-wide lump
sums (`site_capex_eur`, `site_devex_eur`).  Two presentation knobs also
live here: `currency_format` (`auto` | `millions` | `raw`, the axis /
label currency scaling) and `show_titles` (render plot titles; off by
default).  The balancing master switch (`balancing_enabled`) lives on
the `balancing` sheet.

### `pv`

`pv_source` (`auto` | `file` | `pvgis`), the PVGIS location / geometry
(`latitude`, `longitude`, `tilt`, `azimuth`, `losses_pct`,
`weather_year`, `timeseries_path`), `pv_nameplate_kwp`, and
the degradation coefficients.  The `pv_kwh` column is consumed verbatim
(absolute kWh per step); `pv_nameplate_kwp` is metadata for per-kW
CAPEX / OPEX and the sizing sweep.  `auto` uses the `pv_kwh` column when it is
filled and otherwise fetches the profile from the location — so a single
input file covers both "bring your own PV series" and "just give me a
location".

### `bess`

`bess_power_kw` (symmetric charge / discharge limit),
`bess_capacity_kwh`, one-way efficiencies, SOC bounds,
`max_cycles_per_day`, `capex_bess_eur_per_kwh` (full installed BESS
CAPEX per kWh of nameplate energy capacity; DEVEX and OPEX stay per
kW of the power block), calendar and per-cycle fade coefficients,
the replacement policy (`bess_replacement_year`: N = scheduled year,
blank or `auto` = replace when SOH first reaches `bess_eol_soh_pct`
with the CAPEX charged in the cashflow, 0 = never), the replacement
cost, and the `bess_wear_cost_eur_per_mwh` dispatch shadow price.

### `economics`

Discount rate, OPEX inflation, per-stream revenue indexation
(`retail_inflation_pct`, `dam_inflation_pct`), the energy-aggregator fee
(`aggregator_fee_pct_revenue`, on DAM + retail) and the optional,
separate balancing-aggregator / BSP fee
(`balancing_aggregator_fee_pct_revenue`, on gross balancing revenue;
default 0), LCOE / LCOS benchmark-band overrides, the five sensitivity-tornado
deltas (CAPEX / OPEX / revenue / discount-rate / PPA-price), the
debt layer (`gearing_pct`, `debt_interest_rate_pct`,
`debt_tenor_years`, `debt_repayment`), and grid-emissions
intensity for the optional 24/7-CFE accounting.  Per-asset CAPEX /
DEVEX / OPEX live on the `pv` and `bess` sheets; the site-wide lump
sums on `project`.

### `simulation`

Master uncertainty switch, per-source enable flags, log-normal noise
parameters, plot-scope flags
(`plot_daily_scope` / `plot_monthly_scope` / `plot_yearly_scope`
∈ `none | year1_only | all`), uncertainty diagnostics flag.

### `max_injection_profile`

Hour-of-day cap profile (24 rows) optionally with one column per
calendar month, expressing the share of `p_grid_export_max_kw`
available for export.  **Default 100 %** — no curtailment; users opt
in to curtailment by editing the sheet.  If the sheet is missing the
loader falls back to a flat 100 % and logs INFO.  Two optional
per-source sheets, `max_injection_profile_pv` and
`max_injection_profile_bess`, share the identical schema and impose a
sub-cap on the PV and BESS export legs respectively (the combined cap
still binds); omit them for a single shared cap.

### `balancing`

34 keys covering the master switch (`balancing_enabled`), per-product
capacity shares of `bess_power_kw` (`fcr_capacity_share_pct`,
`afrr_up_capacity_share_pct`, `afrr_dn_capacity_share_pct`,
`mfrr_up_capacity_share_pct`, `mfrr_dn_capacity_share_pct`),
acceptance and activation probabilities, fallback capacity and
activation prices, the FCR sustained-duration requirement, the SOC
safety buffer, a balancing-revenue inflation rate, and the Monte Carlo
price sigmas, scenario count (`bm_mc_scenarios`) and seed.  See
[`docs/balancing_market_design.md`](docs/balancing_market_design.md)
for the design deep-dive.

### `ppa`

Pay-as-produced PPA contract on a share of the PV export, mirroring the
`balancing` master-switch pattern: `ppa_enabled`, `ppa_structure`
(`pay_as_produced`; `baseload` reserved), `ppa_settlement`
(`physical` | `cfd`), `ppa_price_eur_per_mwh`, `ppa_volume_share_pct`,
`ppa_term_years`, `ppa_inflation_pct`.  Ships **disabled** — outputs
are bit-identical to a pre-PPA build until the switch is set.  See
[`docs/ppa_design.md`](docs/ppa_design.md) for the design note
(structures, settlements, dispatch treatment, fee and LCOE scope).

### `sizing`

Optional capacity-sweep grid, columnar (one column per axis —
`pv_nameplate_kwp`, `bess_power_kw`, and either `bess_capacity_kwh` or
`bess_duration_hours` — one value per row), gated by an `enabled`
TRUE / FALSE toggle in the first data row.  Ships **disabled** with a
worked example; set `enabled` to `TRUE` to sweep the Cartesian product of
the axes, rank an efficient frontier by NPV, and emit `sizing.xlsx` plus
the frontier / break-even plots.  A YAML / JSON config expresses the same
sweep as a `sizing:` block.

### `scenarios`

Optional batch comparison, tidy / long (one override per row, grouped by
`name`; blank `name` cells inherit the row above), gated by an `enabled`
TRUE / FALSE toggle in the first data row.  Ships **disabled** with a
worked example.  Each row's `target` is a dotted path (`project.mode`,
`bess.power_kw`) or a bare special (`balancing`, `capex_multiplier`), and
`inherits` clones another scenario.  Set `enabled` to `TRUE` to run every
named variant in one pass and emit a styled `scenario_comparison.xlsx`
plus comparison plots.  `--scenarios file.yaml` is the config equivalent.
The `sizing` and `scenarios` sheets are mutually exclusive.

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
                     degradation (+ debt_schedule / emissions /
                     rolling-horizon sheets when enabled)
04_financial_plots/  revenue stack, BESS waterfall/by-month/split,
                     balancing reservation + MC, lifetime cycles,
                     cumulative + monthly cashflow, payback, NPV/IRR
                     tornados, NPV waterfall, LCOE/LCOS, SOH, and the
                     energy Sankey + 24/7-CFE duration curve when
                     emissions accounting is on (full list below)
05_energy_plots/     daily / monthly / yearly dispatch views per year
06_uncertainty_plots/ forecast band, seasonal boxplot, DAM heatmap, diagnostics
```

### KPIs

`compute_kpis` returns a flat dict with the headline year-1 figures
plus per-product balancing breakdowns and nine canonical revenue
aggregates used by the financial plots:

* `revenue_pv_dam_eur`         — PV → DAM exports (under a physical
  PPA: the uncovered share only).
* `revenue_pv_ppa_eur`         — the PPA contract leg on the covered
  share of PV export (`0.0` without an active contract).
* `revenue_bess_dam_eur`       — BESS-DAM arbitrage net of grid
  charging.
* `revenue_self_consumption_eur` — load coverage from PV-direct and
  BESS-discharge; `0.0` in merchant mode.
* `revenue_bess_fcr_eur`       — FCR capacity revenue.
* `revenue_bess_afrr_up_eur`   — aFRR-up capacity + activation.
* `revenue_bess_afrr_dn_eur`   — aFRR-dn capacity + activation.
* `revenue_bess_mfrr_up_eur`   — mFRR-up capacity + activation.
* `revenue_bess_mfrr_dn_eur`   — mFRR-dn capacity + activation.

### Lifetime projection

`build_lifetime_dispatch` and `aggregate_lifetime_to_yearly` produce
the per-step / per-year frame consumed by `compute_financial_kpis`
to derive NPV, IRR, ROI, BCR, LCOE, LCOS, and payback year.

### Monte Carlo

`pvbess_opt.rolling_horizon.monte_carlo_rolling` samples log-normal
forecast noise on DAM price, PV, and load beyond the commit horizon and
re-optimises a rolling-window dispatch per seed, evaluating realised
KPIs against the noise-free actuals.  Output is one row per seed
(`profit_total_eur`, grid import/export, curtailment, cycles, and the
`foresight_gap_pct` against the perfect-foresight benchmark); the
pipeline reports its P10 / P50 / P90.  Seed KPIs share the headline-KPI
scope — the same unavailability derate and the same year-close SOC
condition as the benchmark — so `foresight_gap_pct` is non-negative up
to solver tolerance and the perfect-foresight marker bounds the
distribution from above (see `pvbess_opt/conventions.md`).  Balancing capacity / activation
prices are perturbed separately by
`pvbess_opt.rolling_horizon.monte_carlo_balancing`.

### PDF report

Generated under `results/<run>/04_financial_plots/`:

* Yearly revenue stack (PV-load, BESS-load, PV-DAM, BESS-DAM, the PPA
  contract leg when a contract is on, 5 balancing products, aggregator
  fee, grid-charging cost) with net line.
* BESS revenue waterfall — single chart stepping from BESS-DAM through
  every balancing product to the total BESS revenue.
* BESS revenue capacity-vs-activation split — grouped bar per
  product.
* BESS revenue by month — 12 stacked bars of BESS-DAM + 5 balancing
  products per calendar month.
* Lifetime cycles per operating year.
* Cumulative cashflow + payback marker.
* Monthly cashflow Year 1 (CAPEX / DEVEX events booked in month 12, so
  the monthly and yearly DCFs agree).
* NPV / IRR tornado plots (drivers: CAPEX, OPEX, revenue, discount
  rate — NPV only — and PPA price when a contract is on).
* NPV waterfall.
* LCOE / LCOS summary with Lazard 2024 benchmark band.
* Rolling-horizon Monte Carlo distribution.
* Balancing reservation profile + Monte Carlo distribution per
  product.
* Energy Sankey (PV / BESS / grid / load flows) and the 24/7 carbon-free
  energy duration curve — emitted only when emissions accounting is on
  (`grid_co2_intensity_kg_per_mwh > 0`).

Energy plots under `results/<run>/05_energy_plots/`: daily / monthly / yearly
supply, surplus, combined, dispatch, SOC, and revenue, plus the
merchant trio when the mode is `merchant`.

## Results gallery

Real output from two runs on the shipped `inputs/input.xlsx`
(PV 15 MWp, BESS 15 MW / 60 MWh, 20-year horizon, 7 % discount,
retail 120 EUR/MWh).  Regenerate with
`python scripts/export_readme_figures.py` (renders the PDF report figures
as PNG through the same styler — `set_figure_format`).

### Merchant + balancing (`--mode merchant`, `balancing_enabled = TRUE`)

PV + BESS dispatching to the day-ahead market with FCR / aFRR / mFRR
participation stacked on the battery.

![Merchant yearly revenue stack](docs/assets/merchant_revenue_stack.png)

*Yearly revenue stack — PV-DAM and BESS-DAM exports plus the five
balancing products, net of the energy-aggregator fee, grid-charging cost
and the optional balancing-aggregator (BSP / route-to-market) fee (shown
here at a representative 10 % of balancing revenue).*

![BESS revenue waterfall](docs/assets/merchant_bess_revenue_waterfall.png)

*BESS revenue waterfall — stepping from DAM arbitrage through each
balancing product, then down by the balancing-aggregator (BSP) fee, to the
total battery revenue.*

![LCOS benchmark band](docs/assets/merchant_lcos_band.png)

*Levelised cost of storage against the Lazard 2024 LCOS benchmark band.*

![Merchant cumulative cashflow](docs/assets/merchant_cumulative_cashflow.png)

*Cumulative discounted cashflow over the project life, with the payback
marker (or none, when the discounted cashflow never crosses zero).*

### Self-consumption (`--mode self_consumption`)

Behind-the-meter PV + BESS serving a co-located load at the retail
tariff, exporting only the surplus to the DAM — no balancing.

![Self-consumption daily dispatch and SOC](docs/assets/self_consumption_daily_dispatch_soc.png)

*A representative day: the hard PV→load priority, BESS charge / discharge,
grid import / export, and the battery state of charge.*

![Self-consumption yearly revenue stack](docs/assets/self_consumption_revenue_stack.png)

*Yearly revenue stack — retail-valued self-consumption (avoided cost)
plus the DAM surplus-export leg, net of the aggregator fee.*

## Methodology & conventions

The dispatch MILP is solved **once** for a representative Year 1; Years
2..N are derived analytically (the Gridcog / Aurora / HOMER "fast mode"
recipe).  The conventions that keep every sheet, KPI, and plot in
lockstep:

* **Year convention** — Year 0 carries CAPEX + DEVEX only at calendar
  `project_start_year - 1`; Year 1 is the first operating year with
  degradation factor 1.0.  Escalation indices use `(1 + i)^(y-1)`;
  discounting uses `1/(1+r)^y` (end-of-year), refined to end-of-month
  `1/(1+r)^((y-1)+m/12)` on the monthly sheet.
* **Investment outlays** — `initial_investment_eur` is the Year-0
  outlay (per-asset CAPEX + DEVEX + site lump sums) and matches the
  Year-0 bar in the plots; `total_capex_eur` / `total_capex_devex_eur`
  are lifecycle totals that also include the scheduled BESS replacement
  CAPEX.  `roi_pct` = operating net cashflow (Years 1..N) over
  `|initial_investment_eur|`.
* **Degradation** — PV: Year-2 LID then linear; BESS: multiplicative
  calendar fade minus additive cycle fade, optional scheduled
  replacement (fade reset + replacement CAPEX in the same year).  One
  implementation (`lifetime._bess_factor`) drives the cashflow, the
  lifetime scaling, the SOH diagnostic, and the fade decomposition; a
  cross-module test sweep keeps them numerically identical.
* **Availability** — `unavailability_pct` is applied once, post-solve,
  to the Year-1 energy / revenue KPIs and the lifetime aggregates.
* **Aggregator fee** — a non-negative deduction applied once to the
  gross DAM + retail revenue only; balancing revenue (TSO-settled) and
  PPA revenue (bilateral offtake) never carry it.
* **PPA stream** — the contract leg is its own cashflow column with
  its own indexation; after `ppa_term_years` the covered volume's DAM
  value reverts into the DAM stream (where the fee applies), and a
  disabled contract leaves every output bit-identical to a pre-PPA
  build.
* **LCOE / LCOS** — Lazard-style: per-asset CAPEX / DEVEX / OPEX (plus
  discounted BESS replacement) over discounted delivered MWh.  Site-wide
  lump sums, balancing revenue, and PPA revenue are excluded by
  convention.  `capex_bess_eur_per_kwh` is the full installed cost per
  kWh of nameplate energy capacity, so the LCOS numerator is directly
  comparable to the Lazard band.
* **Battery wear cost** — an optional €/MWh shadow price that shapes
  dispatch only; it is never added to the cashflow, so degradation is
  not double-counted with the replacement CAPEX.

Limitations: dispatch is optimised for a *given* size (no capacity
search beyond the optional sweep); years 2..N are scaled, not
re-solved; the regulatory model targets the Greek self-consumption and
merchant regimes; balancing participation is expected-value in the MILP
with Monte Carlo realisation ex-post.

## Documentation map

The mathematical specification lives in the domain design documents
under [`docs/`](docs/README.md) — one template, one shared notation
table, every numbered equation mapped to its implementing symbol:

* [`docs/self_consumption_design.md`](docs/self_consumption_design.md) —
  the self-consumption MILP: variables, objective, every hard
  constraint, the nine audit invariants (machine-checked against the
  built model by `tests/test_logic_spec_conformance.py`).
* [`docs/merchant_design.md`](docs/merchant_design.md) — the merchant
  regime: pinning constraints, cap semantics, the merchant objective.
* [`docs/balancing_market_design.md`](docs/balancing_market_design.md) —
  FCR / aFRR / mFRR reservations, expected-value MILP terms, the six
  balancing invariants, and the verification appendix.
* [`docs/ppa_design.md`](docs/ppa_design.md) — the PPA engine:
  settlements, the `(1-s)·DAM + s·strike` dispatch price, term and
  reversion.
* [`docs/economics_design.md`](docs/economics_design.md) — year
  conventions, the nine revenue aggregates, fee clamp, degradation
  factors, debt, NPV/IRR/payback, LCOE/LCOS.
* [`docs/uncertainty_design.md`](docs/uncertainty_design.md) —
  rolling-horizon MC, the foresight gap, balancing MC, sensitivity
  drivers.

The Sphinx manual (`make -C docs html`) carries the user's guide
(installation, workbook reference, outputs, CLI) and links to the
design docs as the authoritative formulation; cross-module lockstep
rules live in [`pvbess_opt/conventions.md`](pvbess_opt/conventions.md).

## Citing

If this tool contributes to academic work, please cite it as:

```
Konstantellos, L. (2026). PV & BESS Optimizer (v0.9.0): MILP dispatch
and project-finance pipeline for co-located PV + battery systems.
https://github.com/lamproskonstantellos/pv-bess-optimizer
```

BibTeX:

```bibtex
@software{konstantellos_pv_bess_optimizer_2026,
  author  = {Konstantellos, Lampros},
  title   = {{PV} \& {BESS} Optimizer: {MILP} dispatch and
             project-finance pipeline for co-located {PV} + battery
             systems},
  version = {0.9.0},
  year    = {2026},
  url     = {https://github.com/lamproskonstantellos/pv-bess-optimizer}
}
```

A [`CITATION.cff`](CITATION.cff) ships at the repository root, so
GitHub's "Cite this repository" button serves the same metadata.

## Development

### Solver

HiGHS via `highspy` is the default.  Gurobi and CBC are picked up
automatically if installed.

### Running tests

```bash
pip install -r requirements/dev.txt
pytest                       # default fast lane (the full fast-lane suite)
pytest -m slow               # opt-in real-scale workbook suite (minutes wall-clock)
```

### Code style

`ruff check pvbess_opt tests scripts` is the project lint pass.
The project rule set is `F,E,I,B,UP,ARG,RUF` with `RUF001/002/003`
ignored (the codebase uses Unicode intentionally: `→` in energy-flow
labels, `€` in prices, Greek-letter docstring math).

## Quality

The test suite is the executable specification; see
[`docs/audit_test_index.md`](docs/audit_test_index.md) for how `tests/`
is organized and how to run the fast and slow lanes.
