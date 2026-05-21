# Changelog

This file tracks all releases since v0.8.7.  Older migration notes and
breaking-change diffs have been folded into the present-tense
descriptions across the codebase and Sphinx docs.

## 0.8.10 — 2026-05-21

The v0.8.9 full-codebase audit remediation (findings F1–F12) plus a
consistency pass on the uncertainty-plot family.

### Fixed

- **F1 (P0)** — `model_to_dataframe` copied the raw input `pv_kwh`
  column into BESS-only output frames (`pv_nameplate_kwp == 0`) while
  the model pinned PV flows to zero, producing ~800–3200 kWh per-step
  energy-balance residuals, breaching `invariant_1` / `invariant_9`,
  surfacing phantom `pv_generation_mwh` KPIs, and crashing `--strict`.
  PV flows are now zeroed in the output frame to match the model.
- **F2** — `build_yearly_cashflow` degraded all revenue (including
  BESS-origin streams) on `pv_factor`; BESS-origin revenue now degrades
  on `bess_factor`, reconciling the cashflow and lifetime sheets.
- **F5** — dispatch invariants are computed on unrounded model values,
  so the sum-based `invariant_4` no longer accumulates round(4) error
  across 35,040 rows and trips `--strict`.
- **F6** — NaN gaps in the input timeseries now emit a warning
  (column, count, first timestamp) before ffill/bfill.
- **F9** — the solver-status guard requires a feasible incumbent before
  accepting a `maxTimeLimit` / `maxIterations` termination.

### Changed

- **F3** — dependency floors bumped to the tested majors with upper
  bounds added (`pandas<4`, `numpy<3`, `matplotlib<4`, `pyomo<7`,
  `highspy<2`, `python-dateutil<3`, `openpyxl<4`, `Sphinx<10`).
- **F4** — upfront `[mc-runtime-estimate]` Monte-Carlo runtime warning
  and per-window rolling-horizon progress logging.

### Tooling / docs / tests

- **F8** — `ruff check` added as a CI lint gate ahead of pytest; two
  unused imports removed.
- **F7** — parametrized real-scale 9-invariant test across all six
  mode × asset combinations.
- **F10 / F11 / F12** — sheet-name and cross-reference fixes, output
  layout listings updated with `06_uncertainty_plots/`, malformed
  `inputs.rst` table repaired, and a bundle of P3 nits.
- **Plots** — the `06_uncertainty_plots/` family standardised on
  `DD-MM-YYYY` date ticks and `upper right` legends, with four new
  diagnostic plots (coverage-by-horizon, PIT histogram, CRPS timeline,
  residual Q-Q) behind the `uncertainty_diagnostics_enabled` flag.

## 0.8.9 — 2026-05-20

### Breaking changes

- Workbook schema: the `curtailment_profile` sheet is renamed to
  `max_injection_profile`, the `curtailment_pct` column to
  `max_injection_pct`, and the values are flipped from "share to
  curtail" to "share allowed to inject" (so the case-study workbook
  ships 73 in place of the old 27).  This matches the convention used
  by PyPSA (`p_max_pu`), PLEXOS (`Rating Factor × Max Capacity`),
  Gridcog (share of grid connection capacity), and PVsyst
  (`PNom grid`).  The loader still accepts the legacy schema with a
  `DeprecationWarning` for one release; the curtailed MWh continues to
  appear in outputs (`pv_curtail_kwh`, `pv_energy_curtailed_mwh`).

### Fixed

- Rolling-horizon `window_hours` and `commit_hours` are now real hours
  on sub-hourly cadences.  On the 15-minute production workbook a
  documented 48-hour window previously executed as a 12-hour window;
  any previously reported foresight gap should be recomputed.
- Revenue stack plot now renders the aggregator fee as an explicit
  negative component, removing the unexplained gap between the stack
  top and the net-revenue line at the default 10 % fee.
- Unavailability derate is applied symmetrically across the
  yearly-cashflow and lifetime-dispatch paths, eliminating a silent
  ~0.4 % cycle-count drift across the 20-year horizon when
  `unavailability_pct > 0`.
- `build_lifetime_dispatch` no longer rolls Feb-29 timestamps forward
  to Mar-1 in non-leap target years (uses
  `dateutil.relativedelta(years=N)` when the input contains Feb-29).

### Changed

- `PROJECT_SHEET_DEFAULTS["project_lifecycle_years"]` default is now
  20 (was 25), matching the documented horizon.
- Default max-injection percentage constant lives in
  `pvbess_opt/config.py::DEFAULT_MAX_INJECTION_PCT_HOURLY = 73.0`.
- `aggregate_lifetime_to_yearly` now uses symmetric reindex across
  every per-year MWh aggregation path (pathological inputs without
  `pv_kwh` get 0.0 instead of NaN).
- Module file `pvbess_opt/curtailment.py` is renamed to
  `pvbess_opt/max_injection.py`; the public helper becomes
  `build_per_step_max_injection_frac`.

### Removed

- `params["curtailment_frac"]`: computed but never read in production
  (the loader always populates the per-step profile).

### Added (v0.8.8 audit follow-up)

- `python-dateutil` declared as a direct requirement in
  `requirements/base.txt`.  Pandas drags it in transitively, but the
  implicit dependency is fragile and would break the day pandas drops
  or vendors `dateutil`.
- Per-seed progress logging with running ETA in `monte_carlo_rolling`.
  Long-running ensembles now show live progress instead of sitting
  silent for ~44 min on the shipped workbook with the default 30
  seeds.  Log handlers are flushed after every line so the output
  shows up immediately on long-running runs.
- Real-scale CI test `tests/test_rolling_horizon_realscale.py`
  exercises the full 35 040-row default workbook through one seed of
  `rolling_horizon_dispatch` with a wall-clock budget that catches
  >3-5x per-window regressions.  Marked `@pytest.mark.slow` and gated
  on the workbook existing.

### Fixed (v0.8.8 audit follow-up)

- `economics.build_yearly_cashflow` no longer falls back to
  `retail_inflation_pct = 2.0` when the key is missing; the fallback
  is now `0.0`, matching the canonical
  `ECONOMICS_SHEET_DEFAULTS["retail_inflation_pct"]`.  The `or 0.0`
  post-chain is reachable again as intended.
- `add_forecast_noise` now clips noisy PV at the instantaneous
  nameplate proxy (`pv.max()`).  Tail samples of the multiplicative
  noise could previously exceed the panel's instantaneous capability;
  the MILP was already curtailing the over-cap fraction, but
  downstream consumers reading the noisy forecast from the workbook
  saw implausible values.  Numerical impact on the MILP is zero by
  construction.

### Changed (v0.8.8 audit follow-up)

- Five sites that duplicated the literal `2026` for `project_start_year`
  (`pvbess_opt/economics.py`, `pvbess_opt/lifetime.py`, three sites in
  `main.py`, and `pvbess_opt/io.py::write_dispatch_artifacts`) now
  dereference `PROJECT_SHEET_DEFAULTS["project_start_year"]`.
- `tests/conftest.py::_short_params` updated to canonical
  `retail_tariff_eur_per_mwh = 120.0` (was a stale `132.0` from
  pre-v0.8.8 fixtures).

### Documentation (v0.8.8 audit follow-up)

- Twenty doc-drift sites swept across `docs/source/`, three code
  docstrings, and the `README.md`.  Highlights:
  - `mip_formulation.rst`: `e_cap` correctly identified as a fixed
    parameter pinned to `bess_capacity_kwh` rather than a decision
    variable; charge / discharge power section rewritten to reflect
    the single symmetric `bess_step_lim = bess_power_kw * dt`.
  - `output_layout.rst` and `financial_plots.rst`: the payback PDF is
    `cumulative_cashflow_with_payback_{start}-{end}.pdf`, not the
    non-existent `payback_visualization.pdf`.
  - `outputs.rst`: `show_titles` documented as a `project`-sheet key.
  - `running.rst`: CLI flag table now lists
    `--compare-uncertainty-sources`.
  - `kpis.rst`: `e_cap_mwh` (was `e_cap_opt_mwh`).
  - `economics.rst`: split-revenue model (retail + DAM streams) and
    `devex_eur` included in the `net_cashflow_eur` formula.
  - `inputs.rst`: drop the `1 MW x 1500 kWh/kWp/yr default` language
    that predates the v0.8.8 shipped workbook; remove
    `revenue_inflation_pct` from the current-keys list (deprecated
    alias).
  - `rolling_horizon.rst`: `e_cap` is pinned at workbook load, not
    "after the first window".
  - `pvbess_opt/__init__.py`: package docstring no longer claims a
    "1 MW x 1500 kWh/kWp/yr default".
  - `pvbess_opt/optimization.py`, `pvbess_opt/rolling_horizon.py`:
    docstrings say "9 audit invariants" (the code returns 9, not 8).
  - `pvbess_opt/lifetime.py`: unresolvable `:doc:` xref replaced with
    a plain path reference.
  - `pvbess_opt/kpis.py`: top docstring rewritten as bulleted literal
    blocks, removing the `Block quote ends without a blank line` rST
    warning.
  - `README.md`: `infinity` token added to the unlimited-grid-export
    enumeration so it matches the implementation set
    `_GRID_EXPORT_UNLIMITED_TOKENS`.

## 0.8.8 — 2026-05-19

Backward compatible throughout: old workbooks load unchanged, and the
numerical output is identical to v0.8.7 whenever the new features are
left disabled (the default-0 / no-cap settings).

- Optional / unlimited grid export: `p_grid_export_max_kw` may be left
  empty or set to `inf` / `unlimited` / `disabled` to remove the export
  cap.  A finite Big-M is substituted internally so the MILP topology
  is unchanged and the result stays solver-agnostic; curtailment driven
  by the cap then becomes zero.  A finite positive cap behaves exactly
  as before.
- Cycle-based BESS degradation: a new `bess` sheet key
  `bess_degradation_pct_per_cycle` (LFP default 0.008 %) adds a linear
  cycle-fade term on top of the unchanged multiplicative calendar fade.
  `compute_financial_kpis` reports the year-N calendar / cycle / total
  fade split.  Set the key to 0 — or omit it on an older workbook — to
  recover pre-v0.8.8 calendar-only behaviour exactly.
- SOC plots: the monthly and yearly SOC figures drop the misleading
  point markers.  They keep the stepped mean line (now slightly
  heavier) and the min→max fill envelope; markers on a daily / monthly
  aggregate misread as instantaneous SOC.  The daily SOC plot — genuine
  15-minute point-in-time data — is unchanged.
- Sensitivity tornados: the IRR and NPV tornado plots annotate each bar
  end with the absolute driver value that produced it (CAPEX / OPEX /
  revenue in EUR, discount rate as a percentage) and fold the ±
  sensitivity range into each y-axis label.  The base-case dashed line
  is unchanged.
- Tornado endpoint labels carry the driver value only — the metric is
  read off the x-axis — and the base appears once, via the dashed line
  and its legend entry (`Base = 15.9%` / `Base = €9.0M`).
- Default scenario: `inputs/input.xlsx` ships a 15 MW system
  (`pv_nameplate_kwp`, `p_grid_export_max_kw`, `bess_power_kw` = 15000;
  `bess_capacity_kwh` = 60000; `bess_replacement_year` = 10) over a
  20-year `project_lifecycle_years`.
- Add `daily_combined_with_soc` plot (VNB + merchant) — daily energy
  stacks with an SOC (%) overlay on the right axis.
- Drop `data/pv_shape_15min.csv` and `scripts/build_input_xlsx.py`.
  The shipped `inputs/input.xlsx` is the canonical PV source.
- New optional `pv_kwh_override` column on the `timeseries` sheet —
  user-supplied 15-min PV bypasses the rescaling pipeline.
- Workbook defaults: `retail_tariff_eur_per_mwh = 120.0` (was 132.0),
  `retail_inflation_pct = 0.0` (was 2.0).  The retail tariff and its
  indexation are user-supplied knobs; the codebase no longer
  recommends specific values.

## 0.8.7

Final polish.  Removed corner value annotations (NPV total, cycle
total) since the values are already readable from the y-axis.
Added `apply_fine_ticks` helper for denser tick density on currency
and energy axes; applied across all financial and lifecycle plots.
Moved SOC plot legends below the axes for consistency with other
energy plots.  Audited the colour registry: every label now maps to
a unique colour, fixing the daily-revenue PV→Grid / BESS→Grid
colour clash.  Added `test_color_registry.py` to enforce uniqueness
and ban inline hex colours in plotting modules.
