# PV & BESS Optimizer

[![license](https://img.shields.io/badge/license-All%20Rights%20Reserved-red)](LICENSE)
[![version](https://img.shields.io/badge/version-0.7.0-blue)](pvbess_opt/__init__.py)
[![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](pyproject.toml)
[![ci](https://github.com/lamproskonstantellos/pv-bess-optimizer/actions/workflows/ci.yml/badge.svg)](https://github.com/lamproskonstantellos/pv-bess-optimizer/actions/workflows/ci.yml)

Mixed-integer linear programming model for PV + BESS sizing and 15-minute
dispatch, with a multi-year project-finance pipeline and rolling-horizon
Monte Carlo for uncertainty analysis.

Two regulatory regimes and three asset modes are supported:

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

The asset mode is read literally from the workbook in v0.6 — set
`pv_nameplate_kwp = 0` for a BESS-only project, `bess_power_kw = 0`
for a PV-only project, both > 0 for a hybrid PV+BESS project.

The codebase is pure Python and runs on **Linux, macOS, and Windows** with
Python ≥ 3.11.  All plots are exported as IEEE-styled PDFs.

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

## Workbook schema (v0.6)

Three sheets:

* **`timeseries`** — per-step data with lowercase snake_case column
  names: `timestamp`, `load_kwh` (required for `vnb`, optional for
  `merchant`), `pv_kwh`, `dam_price_eur_per_mwh`, optional
  `retail_price_eur_per_mwh`.  The case-study workbook ships at
  15-minute cadence (35 040 rows for one year) per
  MD YPEN/DAPEEK/93976/2772/2024; the MILP timestep is auto-detected.
* **`project`** — three logical groups (separator rows allowed):
    * `# system_sizing` — `pv_nameplate_kwp`, `bess_power_kw`,
      `bess_capacity_kwh`, `battery_hours`, `p_charge_max_kw`,
      `p_dis_max_kw`, `p_grid_export_max_kw`.
    * `# bess_operation` — `efficiency_charge`, `efficiency_discharge`,
      `soc_min_frac`, `soc_max_frac`, `initial_soc_frac`,
      `terminal_soc_equal`, `max_cycles_per_day`.
    * `# regulatory` — `mode`, `retail_tariff_eur_per_mwh`,
      `curtailment_pct`, `allow_bess_grid_charging`, `settlement_minutes`.
* **`economic`** — eight logical groups: `# horizon`, `# capex`,
  `# opex`, `# degradation_replacement`, `# sensitivity`,
  `# uncertainty` (new), `# output`.  The output group's plot scope
  flags share the same vocabulary in v0.6:
  `plot_daily_scope` / `plot_monthly_scope` / `plot_yearly_scope` ∈
  `none | year1_only | all`.

See `docs/source/users.guide/inputs.rst` for the full reference,
`docs/v0.6_changelog.md` for the v0.5 → v0.6 migration notes, and
`docs/technical.documentation/uncertainty_modelling.md` plus
`docs/technical.documentation/asset_modes.md` for the new groups.

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

## What's new in v0.6

* **Three asset modes** — set `pv_nameplate_kwp = 0` for BESS-only,
  `bess_power_kw = 0` for PV-only, both > 0 for hybrid.  Setting both
  to zero raises `ValueError` from `read_inputs`.
* **Year-0 / Year-1 calendar split** — a 20-year run with
  `project_start_year = 2026` produces 21 yearly cashflow rows: Year 0
  (CAPEX) at calendar 2025; Years 1..20 at 2026..2045.  The new
  `capex_year` financial KPI surfaces 2025 explicitly.
* **Workbook-driven uncertainty** — the new `# uncertainty` group on
  the economic sheet drives rolling-horizon Monte Carlo.  Set
  `uncertainty_compare_sources = TRUE` to run four ensembles
  (DAM-only, PV-only, Load-only, All-combined) and emit a foresight-
  gap comparison plot plus four P50 KPIs.
* **Unified plot scopes** — `plot_daily_scope`, `plot_monthly_scope`,
  `plot_yearly_scope` all accept the same `none | year1_only | all`
  vocabulary.  Selecting `all` for the daily resolution warns about
  the ~9 000 PDFs a 25-year run would emit.
* **Merchant-mode plots** — dispatch / SOC / revenue trio per
  resolution replaces the empty supply / surplus / combined plots
  that the load-pinned merchant runs used to produce.
* **New financial KPIs** — `lcoe_eur_per_mwh`, `lcos_eur_per_mwh`,
  `pv_capacity_factor`, `bess_lifetime_cycles`, plus five Year-1
  revenue-breakdown keys.  Three new lifecycle plots:
  `revenue_stack_yearly`, `lifetime_cycles`, `lcoe_lcos_summary`.

See `docs/v0.6_changelog.md` for the full list of breaking changes
and the five-line v0.5 → v0.6 migration summary.

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
