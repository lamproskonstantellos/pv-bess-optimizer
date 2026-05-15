# Changelog

This file tracks only the most recent release.  The repository no
longer carries internal version history — past migration notes and
breaking-change diffs have been folded into the present-tense
descriptions across the codebase and Sphinx docs.

## 0.8.7 — current

Final polish.  Removed corner value annotations (NPV total, cycle
total) since the values are already readable from the y-axis.
Added `apply_fine_ticks` helper for denser tick density on currency
and energy axes; applied across all financial and lifecycle plots.
Moved SOC plot legends below the axes for consistency with other
energy plots.  Audited the colour registry: every label now maps to
a unique colour, fixing the daily-revenue PV→Grid / BESS→Grid
colour clash.  Added `test_color_registry.py` to enforce uniqueness
and ban inline hex colours in plotting modules.
