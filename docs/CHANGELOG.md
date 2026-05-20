# Changelog

This file tracks only the most recent release.  The repository no
longer carries internal version history — past migration notes and
breaking-change diffs have been folded into the present-tense
descriptions across the codebase and Sphinx docs.

## Unreleased

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
