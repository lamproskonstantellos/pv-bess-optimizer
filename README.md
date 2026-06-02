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

## Installation

```bash
git clone https://github.com/lamproskonstantellos/pv-bess-optimizer.git
cd pv-bess-optimizer
pip install -e .[dev]
```

HiGHS is the default solver (`pip install highspy`).  Gurobi and CBC
work too — the solver search order is set in
`pvbess_opt.optimization._pick_solver`.

## Quickstart

```bash
python main.py inputs/input.xlsx --output-dir out/
```

The runner reads the workbook, solves the MILP, computes KPIs and the
multi-year cashflow, runs the rolling-horizon Monte Carlo (when
enabled in the `simulation` sheet), exercises the sensitivity tornado
(when enabled in the `economics` sheet), and writes:

* a multi-sheet results workbook,
* the IEEE-styled PDF report under `out/03_financial_plots/`,
* the energy plots under `out/02_energy_plots/`,
* uncertainty diagnostics under `out/06_uncertainty_plots/`.

Override the workbook value at the CLI:

```bash
python main.py inputs/input.xlsx --mode merchant --output-dir out/merchant
```

## Input workbook reference

The canonical workbook is `inputs/input.xlsx`.  Every sheet's first
row is the global header accent (bold + light grey fill + thin bottom
border); no other styling is applied.

### `timeseries`

15-minute series of `timestamp`, `pv_kwh`, optionally `load_kwh`
(required for `self_consumption`, ignored for `merchant`),
`dam_price_eur_per_mwh`, and the nine optional per-product balancing
price columns (`fcr_capacity_price_eur_per_mwh`,
`afrr_up_capacity_price_eur_per_mwh`,
`afrr_up_activation_price_eur_per_mwh`, etc.).

### `project`

Project-level scalars including `mode`
(`self_consumption` | `merchant`), `settlement_minutes`,
`p_grid_export_max_kw`, `retail_tariff_eur_per_mwh`,
`allow_bess_grid_charging`, `unavailability_pct`, and
`balancing_enabled`.

### `pv`

`pv_nameplate_kwp`, `specific_production_kwh_per_kwp` (used for the
PV column rescale), and the degradation coefficients.

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

33 keys covering the master switch (`balancing_enabled`), per-product
capacity shares of `bess_power_kw` (`fcr_capacity_share_pct`,
`afrr_up_capacity_share_pct`, `afrr_dn_capacity_share_pct`,
`mfrr_up_capacity_share_pct`, `mfrr_dn_capacity_share_pct`),
acceptance and activation probabilities, fallback capacity and
activation prices, the FCR sustained-duration requirement, the SOC
safety buffer, a balancing-revenue inflation rate, and the two Monte
Carlo seeds.  See
[`docs/balancing_market_design.md`](docs/balancing_market_design.md)
for the design deep-dive.

## Output reference

### KPIs

`compute_kpis` returns a flat dict with the headline year-1 figures
plus per-product balancing breakdowns and eight canonical revenue
aggregates used by the financial plots:

* `revenue_pv_dam_eur`         — PV → DAM exports.
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

`pvbess_opt.rolling_horizon.run_rolling_horizon_mc` samples the
log-normal forecast noise on DAM, PV, load, and balancing prices and
re-optimises a rolling-window dispatch per sample.  Output is a
distribution of headline KPIs (P10 / P50 / P90 of `profit_total_eur`,
`npv_eur`, etc.) plus the per-scenario realised dispatch summary.

### PDF report

Generated under `out/03_financial_plots/`:

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

Energy plots under `out/02_energy_plots/`: daily / monthly / yearly
supply, surplus, combined, dispatch, SOC, and revenue, plus the
merchant trio when the mode is `merchant`.

## Development

### Solver

HiGHS via `highspy` is the default.  Gurobi and CBC are picked up
automatically if installed.

### Running tests

```bash
pip install -e .[dev]
pytest                       # default fast lane (the full fast-lane suite)
pytest -m slow               # opt-in real-scale workbook suite (minutes wall-clock)
```

### Code style

`ruff check pvbess_opt tests scripts` is the project lint pass.
The project rule set is `F,E,I,B,UP,ARG,RUF` with `RUF001/002/003`
ignored (the codebase uses Unicode intentionally: `→` in energy-flow
labels, `€` in prices, Greek-letter docstring math).

## Quality

The test-suite audit, with per-file verdicts and the verification
tests behind each fix, is indexed in
[`docs/audit_test_index.md`](docs/audit_test_index.md).
