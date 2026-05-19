# Changelog

This file tracks only the most recent release.  The repository no
longer carries internal version history — past migration notes and
breaking-change diffs have been folded into the present-tense
descriptions across the codebase and Sphinx docs.

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
- SOC plots: the monthly and yearly SOC figures now render each
  aggregate period as a vertical range bar (min→max) with a horizontal
  mean tick, instead of a stepped line with point markers.  The daily
  SOC plot is unchanged.
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
