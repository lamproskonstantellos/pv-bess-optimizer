# Uncertainty modelling — rolling-horizon Monte Carlo

The annual MILP solved by `pvbess_opt.optimization.run_scenario` is a
**perfect-foresight** model: it sees every hour's DAM price, PV
output, and load and produces an upper bound on achievable profit.
Real operators never have that visibility.  The package supplies a
workbook-driven rolling-horizon Monte Carlo so the foresight gap is
quantifiable.

## Sources of uncertainty

Three independent log-normal noise streams are applied to the input
timeseries beyond a configurable commit horizon.  The defaults are
literature-anchored:

| Variable      | Sigma (default) | Source                                       |
| ------------- | --------------- | -------------------------------------------- |
| DAM price     | 0.20 (MAPE)     | ENTSO-E D+1 benchmark for volatile markets   |
| PV generation | 0.12 (RMSE)     | NREL day-ahead PV forecast study             |
| Load          | 0.05 (MAPE)     | Predictable customer benchmark               |

The noise is multiplicative log-normal with `mu = -sigma^2 / 2`
applied to the log of the absolute value, which yields a unit-mean
multiplier in linear space.  Negative DAM prices are sign-aware: the
sign is restored after the absolute value is perturbed, so a
negative-price hour stays negative.

`pvbess_opt.rolling_horizon.add_forecast_noise` exposes per-source
``enable_dam`` / ``enable_pv`` / ``enable_load`` flags.  Disabling a
source clamps its sigma to 0 internally — the column is left exactly
as in the input.

## Workbook configuration (the `# uncertainty` group)

Eleven keys on the economic sheet drive the rolling-horizon engine:

| Key | Default | Notes |
| --- | ------- | ----- |
| `uncertainty_enabled`         | `FALSE` | Master on / off switch. |
| `uncertainty_compare_sources` | `FALSE` | Run 4 ensembles per source. |
| `uncertainty_n_seeds`         | `30`    | Monte Carlo seeds per ensemble. |
| `uncertainty_window_hours`    | `48`    | Rolling window length. |
| `uncertainty_commit_hours`    | `24`    | Commit slice. |
| `uncertainty_dam_enabled`     | `TRUE`  | Apply DAM noise. |
| `uncertainty_pv_enabled`      | `TRUE`  | Apply PV noise. |
| `uncertainty_load_enabled`    | `TRUE`  | Apply load noise (forced FALSE in merchant mode). |
| `uncertainty_sigma_dam`       | `0.20`  | Log-normal sigma, DAM. |
| `uncertainty_sigma_pv`        | `0.12`  | Log-normal sigma, PV. |
| `uncertainty_sigma_load`      | `0.05`  | Log-normal sigma, load. |

The CLI flags `--rolling-horizon`, `--monte-carlo`, `--window-hours`,
`--commit-hours`, `--compare-uncertainty-sources` are overrides:
omitted on the command line, the workbook value applies.

## The four-source comparison workflow

Setting `uncertainty_compare_sources = TRUE` runs four
`monte_carlo_rolling` passes with hard-coded
`(enable_dam, enable_pv, enable_load)` flags:

| `source_set` | DAM | PV | Load |
|--------------|-----|----|------|
| `dam`        | T   | F  | F    |
| `pv`         | F   | T  | F    |
| `load`       | F   | F  | T    |
| `all`        | T   | T  | T    |

Each ensemble produces `uncertainty_n_seeds` Monte Carlo realisations.
The four DataFrames are concatenated with a `source_set` column and
written to `03_results.xlsx → rolling_horizon_compare_mc`.  Four new
KPI keys land on `03_results.xlsx → financial_kpis`:

* `foresight_gap_pct_p50_dam`
* `foresight_gap_pct_p50_pv`
* `foresight_gap_pct_p50_load`
* `foresight_gap_pct_p50_all`

Two new plots are emitted:

* `04_financial_plots/rolling_horizon_distribution_compare.pdf` —
  one tinted histogram per source set, colours from a colour-blind-
  friendly palette.
* `06_uncertainty_plots/rolling_horizon_foresight_gap_comparison.pdf`
  — horizontal box-plot per source set, sorted by median.

In merchant mode the load is pinned to zero, so
`uncertainty_load_enabled` is forced FALSE internally and an INFO
message is logged at run start.  The `load` ensemble in the compare
mode then degenerates to a noiseless run for that source.

## When to use what

* **Single ensemble** (`uncertainty_compare_sources = FALSE`) — fastest
  diagnostic of overall foresight gap.  Use the per-source enable
  flags to silence sources that are not realistic for the asset
  (e.g. `uncertainty_load_enabled = FALSE` for a customer with
  perfectly forecastable load).
* **Compare-sources mode** — slower (4× the seeds) but the only way
  to allocate the foresight gap to its drivers.  Useful when a
  client asks "is most of my profit risk price risk or generation
  risk?"

## Implementation notes

* The rolling-horizon dispatcher pins the BESS energy capacity to the
  first window's solution so subsequent windows operate against the
  same physical asset.
* SOC carryover happens at the end of each committed slice — there is
  no closed-cycle constraint within a window.
* KPIs are re-evaluated against the original (noise-free) timeseries
  by default (``evaluate_with_actuals=True``).  This reflects realised
  performance rather than what the solver thought it was getting.
* Reproducibility: identical `--seed` produces identical Monte Carlo
  DataFrames across runs.  The four compare-source ensembles share
  the same base seed so noise realisations are aligned across source
  sets.
