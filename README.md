# PV & BESS Optimizer

[![license](https://img.shields.io/badge/license-All%20Rights%20Reserved-red)](LICENSE)
[![version](https://img.shields.io/badge/version-0.9.0-blue)](pvbess_opt/__init__.py)
[![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](pyproject.toml)
[![ci](https://github.com/lamproskonstantellos/pv-bess-optimizer/actions/workflows/ci.yml/badge.svg)](https://github.com/lamproskonstantellos/pv-bess-optimizer/actions/workflows/ci.yml)

## What it does

Mixed-integer linear programming model for co-located PV + BESS dispatch
at 15-minute resolution, with a multi-year project-finance pipeline,
stochastic balancing-market participation, and rolling-horizon Monte
Carlo for uncertainty analysis.

Two regulatory regimes are supported:

* `self_consumption` — co-located load with self-consumption priority.
  Load balance enforces a hard PV→load priority, surplus-only export
  through a binary-free slack, and no simultaneous grid I/O via a tight
  big-M.  Self-consumption is settled at the retail tariff; surplus is
  settled at the day-ahead market price.
* `merchant` — utility-scale dispatch with **no co-located load**.  PV
  and BESS dispatch entirely to the DAM, optionally augmented with
  stochastic balancing-market participation (FCR / aFRR / mFRR).

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
(`self_consumption` | `merchant`),
`p_grid_export_max_kw`, `retail_tariff_eur_per_mwh`,
`allow_bess_grid_charging`, `grid_cap_includes_load`,
`unavailability_pct`, and the site-wide lump sums (`site_capex_eur`,
`site_devex_eur`).  The balancing master switch (`balancing_enabled`)
lives on the `balancing` sheet.

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
`bess_capacity_kwh`, round-trip efficiencies, SOC bounds,
`max_cycles_per_day`, cycle-fade coefficient, replacement year.

### `economics`

Discount rate, OPEX / inflation rates per stream
(`retail_inflation_pct`, `dam_inflation_pct`), CAPEX and DEVEX per
asset and per site, sensitivity-tornado deltas, aggregator fee.

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
loader falls back to a flat 100 % and logs INFO.

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
02_dispatch/         dispatch_hourly.xlsx (one sheet per calendar year)
03_results.xlsx      kpis_year1 | kpis_monthly_year1 | dispatch_year1 |
                     cashflow_yearly | cashflow_quarterly | cashflow_monthly |
                     financial_kpis | sensitivity_analysis |
                     lifetime_dispatch_yearly | economic_assumptions |
                     degradation (+ debt_schedule / emissions /
                     rolling-horizon sheets when enabled)
04_financial_plots/  cashflow, payback, tornados, waterfall, LCOE/LCOS, SOH
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

* Yearly revenue stack (PV-load, BESS-load, PV-DAM, BESS-DAM, 5
  balancing products, aggregator fee, grid-charging cost) with net
  line.
* BESS revenue waterfall — single chart stepping from BESS-DAM through
  every balancing product to the total BESS revenue.
* BESS revenue capacity-vs-activation split — grouped bar per
  product.
* BESS revenue by month — 12 stacked bars of BESS-DAM + 5 balancing
  products per calendar month.
* Lifetime cycles per operating year.
* Cumulative cashflow + payback marker.
* Monthly cashflow Year 1.
* NPV / IRR tornado plots.
* NPV waterfall.
* LCOE / LCOS summary with Lazard 2024 benchmark band.
* Rolling-horizon Monte Carlo distribution.
* Balancing reservation profile + Monte Carlo distribution per
  product.

Energy plots under `results/<run>/05_energy_plots/`: daily / monthly / yearly
supply, surplus, combined, dispatch, SOC, and revenue, plus the
merchant trio when the mode is `merchant`.

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
* **Aggregator fee** — a non-negative deduction on gross DAM + retail
  revenue (never on balancing revenue), applied once in the cashflow.
* **LCOE / LCOS** — Lazard-style: per-asset CAPEX / DEVEX / OPEX (plus
  discounted BESS replacement) over discounted delivered MWh.  Site-wide
  lump sums and balancing revenue are excluded by convention.  For an
  LCOS comparable to the Lazard band, supply `capex_bess_eur_per_kw` as
  the *full installed* cost (duration_h × EUR/kWh) — a power-block-only
  figure understates LCOS against that band.
* **Battery wear cost** — an optional €/MWh shadow price that shapes
  dispatch only; it is never added to the cashflow, so degradation is
  not double-counted with the replacement CAPEX.

Limitations: dispatch is optimised for a *given* size (no capacity
search beyond the optional sweep); years 2..N are scaled, not
re-solved; the regulatory model targets the Greek self-consumption and
merchant regimes; balancing participation is expected-value in the MILP
with Monte Carlo realisation ex-post.

## Citing

If this tool contributes to academic work, please cite it as:

```
Konstantellos, L. (2026). PV & BESS Optimizer (v0.9.0): MILP dispatch
and project-finance pipeline for co-located PV + battery systems.
https://github.com/lamproskonstantellos/pv-bess-optimizer
```

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
