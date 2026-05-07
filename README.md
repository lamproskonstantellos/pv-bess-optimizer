# PV & BESS Optimizer

[![ci](https://github.com/lamproskonstantellos/pv-bess-optimizer/actions/workflows/ci.yml/badge.svg)](https://github.com/lamproskonstantellos/pv-bess-optimizer/actions/workflows/ci.yml)
[![version](https://img.shields.io/badge/version-0.5.0-blue)](#)
[![python](https://img.shields.io/badge/python-3.9%20%7C%203.11%20%7C%203.12-blue)](#)
[![license](https://img.shields.io/badge/license-All%20Rights%20Reserved-red)](LICENSE)

Mixed-integer linear programming model for PV + BESS sizing and 15-minute
dispatch, with a multi-year project-finance pipeline and rolling-horizon
Monte Carlo for uncertainty analysis.

Two regulatory regimes are supported:

* `vnb` — Greek Virtual Net Billing with co-located load.  Load
  balance, hard PV→load priority (Section 2 of the spec), surplus-only
  export (Section 5, binary-free slack), no simultaneous grid I/O
  (tight big-M), retail tariff for self-consumption, DAM for export.
* `merchant` — pure utility-scale dispatch with **no co-located load**.
  PV and BESS dispatch entirely to the day-ahead market.  The hard
  static curtailment cap on grid-bound flows still applies (regulatory
  grid-connection limit per
  [MD YPEN/DAPEEK/53563/1556/2023](https://www.et.gr/), 27 % distribution-
  connected, 28 % transmission-connected).

The codebase is pure Python and runs on **Linux, macOS, and Windows** with
Python ≥ 3.9.  All plots are exported as IEEE-styled PDFs.

## Repository layout

```
pv-bess-optimizer/
├── main.py                       # CLI entry point
├── LICENSE                       # All Rights Reserved
├── requirements.txt
├── requirements/
│   ├── base.txt                  # pandas, numpy, matplotlib, openpyxl, pyomo
│   ├── solvers.txt               # HiGHS via highspy (Gurobi/CBC notes)
│   ├── dev.txt                   # Linters + pytest + base + solvers
│   └── docs.txt                  # Sphinx + RTD theme
├── inputs/
│   └── input.xlsx                # Three-sheet workbook (timeseries + project + economic)
├── scripts/
│   ├── build_input_xlsx.py       # Regenerate inputs/input.xlsx
│   └── resample_timeseries.py    # Mixed-resolution timeseries harmoniser
├── results/                      # Run outputs (gitignored)
├── docs/                         # Sphinx documentation source
├── tests/                        # pytest unit + smoke tests
└── pvbess_opt/                   # Library code
    ├── config.py                 # Plot labels, colours, IEEE rcParams
    ├── io.py                     # Excel I/O, output workbook, layout
    ├── optimization.py           # Pyomo MILP, solver dispatch, 9 audit invariants
    ├── kpis.py                   # KPIs, green attribution, energy verification
    ├── economics.py              # Cashflow + NPV/IRR/ROI/BCR
    ├── lifetime.py               # Multi-year analytical hourly dispatch projection
    ├── sensitivity.py            # One-at-a-time tornado sensitivity
    ├── rolling_horizon.py        # Rolling-horizon dispatch + Monte Carlo
    └── plotting/                 # IEEE-styled PDFs (energy + financial + uncertainty)
```

The package keeps a flat module layout (≤ 10 top-level modules).  Once the
count crosses 12 we will subpackage by responsibility (`solve/`, `finance/`,
`uncertainty/`, `plotting/`).  See `CONTRIBUTING.md`.

## Quick start

```bash
pip install -r requirements/dev.txt           # base + solvers + linters + pytest
python scripts/build_input_xlsx.py            # regenerate inputs/input.xlsx
python main.py inputs/input.xlsx --solver highs
```

A run produces, under `results/<input>_<scenario>_<timestamp>/`:

```
00_summary/        SUMMARY.md, run_log.txt
01_inputs/         input_snapshot.xlsx, assumptions_summary.txt
02_dispatch/       dispatch_hourly.xlsx (one sheet per calendar year)
03_results.xlsx    KPIs, cashflows, financial KPIs, sensitivity, rolling-horizon MC
04_financial_plots/ cumulative, waterfall, payback, tornados, rolling_horizon_distribution
05_energy_plots/<calendar_year>/{daily,monthly,yearly}/...
                   lifetime_summary_<start>-<end>.pdf
06_uncertainty_plots/ inputs_forecast_band, inputs_seasonal_boxplot, dam_intraday_heatmap
```

## Workbook schema

Three sheets:

* **`timeseries`** — per-step data with lowercase snake_case column
  names: `timestamp`, `load_kwh` (required for `vnb`, optional for
  `merchant`), `pv_kwh`, `dam_price_eur_per_mwh`, optional
  `retail_price_eur_per_mwh`.  The case-study workbook ships at
  15-minute cadence (35 040 rows for one year) per
  MD YPEN/DAPEEK/93976/2772/2024; the MILP timestep is auto-detected.
* **`project`** — physical system + regulatory framework + optimisation
  behaviour, in three logical groups (separator rows allowed).  Keys
  include `efficiency_charge`, `p_charge_max_kw`, `p_grid_export_max_kw`,
  `mode`, `retail_tariff_eur_per_mwh`, `curtailment_pct`,
  `allow_bess_grid_charging`, `solver_mip_gap`.
* **`economic`** — project finance + plot preferences, in six logical
  groups.  Keys include `project_lifecycle_years`, `discount_rate_pct`,
  `capex_pv_eur_per_kw`, `opex_bess_eur_per_kw`, sensitivity deltas,
  plot scopes.

See `docs/source/users.guide/inputs.rst` for the full reference.

## CLI

```bash
# Single perfect-foresight solve
python main.py inputs/input.xlsx --solver highs

# Override mode (workbook says vnb; force merchant)
python main.py inputs/input.xlsx --mode merchant --solver highs

# Rolling-horizon with Monte Carlo (imperfect foresight + 30 seeds)
python main.py inputs/input.xlsx \
    --rolling-horizon \
    --window-hours 48 \
    --commit-hours 24 \
    --monte-carlo 30 \
    --seed 42 \
    --solver highs

# Strict mode: dispatch-invariant violations error out
python main.py inputs/input.xlsx --strict --solver highs
```

The `--strict` flag turns the nine dispatch invariants from warnings into
errors.  See `docs/source/technical.documentation/mip_formulation.rst` for
the invariant set.

## Objective

Single objective: **profit maximisation**.  Under Greek VNB economics
retail (132 EUR/MWh) > DAM avg (~100 EUR/MWh) in > 99 % of hours, so the
profit objective produces the same dispatch as a "green" objective in
this market.  Self-consumption is no longer emergent: the hard
`LOAD_PV_PRIORITY` constraint pins
`pv_to_load[t] == min(pv[t], load[t])` exactly (Section 2 of the spec).
In `merchant` mode there is no load to "be green about" in the first
place.  See `docs/source/technical.documentation/objectives.rst` for the
full reasoning.

## Rolling horizon (uncertainty)

A single annual MILP with full visibility into every hour's DAM price,
PV output, and load is a **perfect-foresight** model — it produces an
upper bound on achievable profit, not a realistic operating result.

`pvbess_opt.rolling_horizon` adds:

* sliding-window MILP with imperfect foresight beyond the commit horizon;
* SOC carryover across windows (no closed-cycle constraint within a window);
* KPI re-evaluation against the original (noise-free) timeseries;
* Monte Carlo over forecast scenarios with reproducible seeds;
* P10 / P50 / P90 distribution + foresight-gap reporting.

Forecast-noise sigmas (defensible from literature):

| Variable      | sigma        | Source                                       |
| ------------- | ------------ | -------------------------------------------- |
| DAM price     | 0.20 (MAPE)  | ENTSO-E D+1 benchmark                        |
| PV generation | 0.12 (RMSE)  | NREL day-ahead PV forecast study             |
| Load          | 0.05 (MAPE)  | Predictable-customer benchmark               |

See `docs/source/users.guide/rolling_horizon.rst` for the full guide.

## Documentation

The Sphinx site under `docs/` covers:

* `users.guide/` — install, inputs, running, economics, financial plots,
  sensitivity, rolling horizon
* `technical.documentation/` — MILP formulation, regulatory framework
  (MD YPEN/DAPEEK/53563/1556/2023), KPIs, energy balance, lifetime
  scaling
* `api/` — full API reference (autodoc)

Build locally:

```bash
pip install -r requirements/docs.txt
make -C docs html         # output: docs/build/html/index.html
```

## License

All Rights Reserved — see [LICENSE](LICENSE).
