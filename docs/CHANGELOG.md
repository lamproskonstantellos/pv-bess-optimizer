# Changelog

This file tracks only the most recent release.  The repository no
longer carries internal version history — past migration notes and
breaking-change diffs have been folded into the present-tense
descriptions across the codebase and Sphinx docs.

## Unreleased

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
