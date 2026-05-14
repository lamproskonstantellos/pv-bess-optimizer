# PV & BESS Optimizer

[![license](https://img.shields.io/badge/license-All%20Rights%20Reserved-red)](LICENSE)
[![version](https://img.shields.io/badge/version-0.8.1-blue)](pvbess_opt/__init__.py)
[![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](pyproject.toml)
[![ci](https://github.com/lamproskonstantellos/pv-bess-optimizer/actions/workflows/ci.yml/badge.svg)](https://github.com/lamproskonstantellos/pv-bess-optimizer/actions/workflows/ci.yml)

Mixed-integer linear programming model for PV + BESS sizing and 15-minute
dispatch, with a multi-year project-finance pipeline and rolling-horizon
Monte Carlo for uncertainty analysis.

Two regulatory regimes and three asset modes are supported:

* `vnb` — co-located load with self-consumption priority.  Load
  balance enforces a hard PV→load priority, surplus-only export
  through a binary-free slack, and no simultaneous grid I/O via a
  tight big-M.  Self-consumption is settled at the retail tariff;
  surplus is settled per applicable settlement rules at the
  day-ahead market price.
* `merchant` — pure utility-scale dispatch with **no co-located load**.
  PV and BESS dispatch entirely to the day-ahead market.  The hourly
  static curtailment cap on grid-bound flows still applies and is
  supplied by the user through the `curtailment_profile` sheet, in
  line with the applicable grid-connection regulations.

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
│   └── input.xlsx                # Seven-sheet workbook (v0.8 schema)
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
    ├── economics.py              # Cashflow + NPV/IRR/ROI/BCR + DEVEX + fee
    ├── availability.py           # Post-solve unavailability derate (v0.8)
    ├── curtailment.py            # Hour-of-day cap profile expander (v0.8)
    ├── lifetime.py               # Multi-year analytical hourly dispatch projection
    ├── sensitivity.py            # One-at-a-time tornado sensitivity
    ├── rolling_horizon.py        # Rolling-horizon dispatch + Monte Carlo
    └── plotting/                 # IEEE-styled PDFs (energy + financial + uncertainty)
```

The package keeps a flat module layout (≤ 12 top-level modules; see
`CONTRIBUTING.md`).  Once the count crosses 12 we will subpackage by
responsibility (`solve/`, `finance/`, `uncertainty/`, `plotting/`).

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

## Workbook schema (v0.8)

Seven themed sheets:

* **`timeseries`** — per-step data: `timestamp`, `load_kwh` (required
  for `vnb`, optional for `merchant`), `pv_kwh`,
  `dam_price_eur_per_mwh`, optional `retail_price_eur_per_mwh`.
  Case-study fixture is 35 040 rows at 15-minute cadence (one full
  year); the MILP timestep is auto-detected.
* **`project`** — high-level run config:
  `project_lifecycle_years`, `project_start_year`, `mode`,
  `settlement_minutes`, `p_grid_export_max_kw` (project-wide grid
  export limit applied to the combined PV + BESS flow),
  `retail_tariff_eur_per_mwh`, `allow_bess_grid_charging`,
  `unavailability_pct` (user-configurable post-solve derate),
  `currency_format`, `show_titles`.
* **`pv`** — `pv_nameplate_kwp`, `specific_production_kwh_per_kwp`,
  `pv_degradation_year1_pct`, `pv_degradation_annual_pct`,
  `capex_pv_eur_per_kw`, `devex_pv_eur_per_kw`, `opex_pv_eur_per_kwp`.
* **`bess`** — `bess_power_kw` (symmetric charge / discharge limit),
  `bess_capacity_kwh` (pinned), efficiency / SOC bounds / cycle cap,
  `capex_bess_eur_per_kw`, `devex_bess_eur_per_kw`,
  `opex_bess_eur_per_kw`, `bess_replacement_year`,
  `bess_replacement_cost_pct`, `bess_degradation_annual_pct`.
* **`economics`** — `discount_rate_pct`, `opex_inflation_pct`,
  `revenue_inflation_pct`, `aggregator_fee_pct_revenue`
  (user-configurable fraction of gross revenue retained by the
  aggregator), `sensitivity_*` (5 keys).
* **`simulation`** — `uncertainty_*` (11 keys), `plot_daily_scope` /
  `plot_monthly_scope` / `plot_yearly_scope` ∈
  `none | year1_only | all`.
* **`curtailment_profile`** — user-configurable hourly cap profile.
  24 hourly rows × 1 col (`curtailment_pct`) for a constant-by-month
  cap, or 24 rows × 12 cols (`curtailment_pct_jan` …
  `curtailment_pct_dec`) for a per-month hour-of-day cap.  Missing
  sheet ⇒ the loader falls back to a flat default.

### How the export cap is enforced

The per-step grid-export cap is computed as
`p_grid_export_max_kw × dt_h × (1 − curtailment_fraction)` and
applied to the **combined** PV + BESS export flow
(`grid_export_total[t] = pv_to_grid[t] + bess_dis_grid[t]`), not
separately to PV exports or BESS-discharge exports.
`p_grid_export_max_kw` is the nameplate grid-connection limit;
`curtailment_profile` is the per-hour regulatory derate that scales
the nameplate down for that step.  The same cap applies in both
`vnb` and `merchant` modes.

Setting `pv_nameplate_kwp = 0` makes the project BESS-only;
`bess_power_kw = 0` makes it PV-only; both > 0 ⇒ hybrid.  Setting both
to zero raises `ValueError` from `read_inputs`.

See `docs/source/users.guide/inputs.rst` for the full reference,
`docs/v0.8_changelog.md` for the v0.7 → v0.8 migration notes, and
`docs/technical.documentation/uncertainty_modelling.md` plus
`docs/technical.documentation/asset_modes.md`.

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

## What's new in v0.8

* **Seven-sheet workbook schema (breaking)** — `project` and
  `economic` split into seven themed sheets: `project`, `pv`, `bess`,
  `economics`, `simulation`, `curtailment_profile`, plus the existing
  `timeseries` sheet.
* **BESS spec rationalisation** — `battery_hours`, `p_charge_max_kw`
  and `p_dis_max_kw` are dropped.  `bess_power_kw` is the symmetric
  charge / discharge limit and `bess_capacity_kwh` pins the energy
  capacity (industry standard for sizing-as-input projects).  `e_cap`
  is no longer a decision variable; `run_scenario` returns
  `(res, resolved_solver_name)`.
* **Hourly curtailment cap profile** — the old scalar
  `curtailment_pct` becomes a 24-row hour-of-day profile, optionally
  with one column per calendar month.  Missing sheet ⇒ the loader
  falls back to a flat default.
* **DEVEX (NEW)** — per-asset `devex_pv_eur_per_kw` and
  `devex_bess_eur_per_kw` replace the v0.7 `capex_licenses_eur_per_kw`.
  Paid in Year 0 alongside CAPEX, surfaces as a `devex_eur` column on
  `cashflow_yearly` and as `total_devex_eur` /
  `total_capex_devex_eur` financial KPIs.
* **Unavailability (NEW)** — `unavailability_pct` is a
  user-configurable post-solve derate applied to PV generation, BESS
  discharge, and revenue.  Implemented in `pvbess_opt.availability`.
* **Aggregator fee (NEW)** — `aggregator_fee_pct_revenue` is a
  user-configurable fraction that reduces gross revenue and shows up
  as a signed `aggregator_fee_eur` column on `cashflow_yearly`.
* **Plot redesigns** — IRR tornado switches to a dumbbell layout for
  unambiguous endpoint labels; LCOE/LCOS summary becomes a single
  panel with the project sensitivity range overlaid on the Lazard
  2024 industry benchmark band.

## What's new in v0.8.1 (patch round)

* **Retail / DAM inflation split** — `revenue_inflation_pct` is
  replaced by `retail_inflation_pct` (default 2 %, PPA / VNB load
  coverage) and `dam_inflation_pct` (default 0 %, wholesale exports).
  Industry tools (Lazard / Aurora / Gridcog) do not apply CPI to
  wholesale prices.  Legacy workbooks still load with a WARNING and
  the value mapped to `retail_inflation_pct`.
* **LCOE audit fix** — LCOE numerator no longer includes BESS CAPEX /
  DEVEX / OPEX.  Per IEA / IRENA / NREL ATB convention LCOE is
  PV-only; LCOS is BESS-only.  Benchmark bands updated to Lazard 2024
  EUR-equivalents (LCOE 30–85 EUR/MWh; LCOS 157–274 EUR/MWh) and
  overridable per project via four `benchmark_*` workbook keys.
* **`run_log.txt` LCOE/LCOS audit** — single INFO line records the
  computed LCOE / LCOS next to the benchmark bands.
* **NPV waterfall** matches `yearly_cashflow_bars` morphology
  (five-entry legend, no in-axis CAPEX/DEVEX captions, padded
  y-axis).
* **Cumulative / payback dedup** —
  `cumulative_cashflow_<start>-<end>.pdf` is now pure cumulative
  trajectory; payback markers live in
  `cumulative_cashflow_with_payback_<start>-<end>.pdf`.
* **LCOS annotation placement** — when the project bar undershoots
  the Lazard band the summary annotation now lifts above the bar
  rather than landing inside the grey benchmark band.
* **Net-revenue line contrast** — Net revenue / Real-EUR net lines
  in `revenue_stack_yearly` switch from dark purple to magenta
  (`FINANCIAL_COLORS["net_revenue_line"]`) so they stay visible
  over the dark blue BESS-export stack.

See `docs/v0.8_changelog.md` for the full breaking-changes contract
and the five-line v0.7 → v0.8 migration summary.

## Documentation

The Sphinx site under `docs/` covers:

* `users.guide/` — install, inputs, running, economics, financial plots,
  sensitivity, rolling horizon
* `technical.documentation/` — MILP formulation, regulatory framework
  (per applicable grid-connection regulations), KPIs, energy balance,
  lifetime scaling
* `api/` — full API reference (autodoc)

Build locally:

```bash
pip install -r requirements/docs.txt
make -C docs html         # output: docs/build/html/index.html
```

## License

All Rights Reserved — see [LICENSE](LICENSE).
