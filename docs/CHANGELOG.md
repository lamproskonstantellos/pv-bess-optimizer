# Changelog

This file tracks only the most recent release.  The repository no
longer carries internal version history — past migration notes and
breaking-change diffs have been folded into the present-tense
descriptions across the codebase and Sphinx docs.

## 0.8.4 — current

Plot-polish release covering three areas:

* **Tornado endpoint labels** — `_annotate_dumbbell_endpoints` in
  `pvbess_opt/plotting/financial.py` places each endpoint label
  outside the corresponding dot (left label right-aligned 8 pt to
  the left of the leftmost dot; right label left-aligned 8 pt to
  the right of the rightmost dot).  The dumbbell-plot x-axis
  padding bumps from 8 % to 18 % so the outward labels never clip
  the y-axis spine or the right frame.  Solves the centre-overlap
  collision on tight-range rows.
* **Universal axes margin rule** — a new
  `apply_universal_margins(ax)` helper in
  `pvbess_opt/plotting/style.py` pads every plot's `xlim`/`ylim`
  by 2 % / 5 % so data, annotations, and legend boxes never touch
  the frame.  Wired into every public plotting function across the
  six plotting modules; plots with fixed x-domains
  (daily / monthly / yearly resolution, tornado, heatmap, LCOE/LCOS
  summary) pass `skip_x=True` or opt out via a `margins: delegated`
  docstring marker.  NPV-waterfall total annotation moves to axes
  `(0.98, 0.98)` so the bbox always sits in the top-right corner
  clear of the cumulative-NPV line.
* **Evergreen codebase** — every reference to past pre-release
  version identifiers has been stripped from source files,
  docstrings, comments, log messages, Sphinx docs and the README.
  Historical changelog files under `docs/` are removed.
  `tests/test_no_historical_version_strings.py` scans the
  repository for any reintroduction.

Verification log:

* 525 tests pass under `pytest tests/` (including the three new
  parametrized suites: `test_tornado_labels`'s
  `endpoint_labels_outside_dots` /
  `do_not_overlap_y_axis_spine` /
  `short_range_row_labels_dont_collide`,
  `test_annotation_safety`'s
  `test_apply_universal_margins_pads_both_axes` /
  `skip_x_leaves_x_alone` /
  `npv_total_annotation_has_breathing_room`,
  `test_plotting_universality`'s
  `test_apply_universal_margins_called[...]` over every public plot
  function, plus `test_no_historical_version_strings` over the seven
  pre-release-version regex patterns).
* Audits 1–7 grep clean across the plotting package.
* `python main.py inputs/input.xlsx --solver highs` renders the
  full financial-plot set under
  `results/<run>/04_financial_plots/` (cumulative, waterfall,
  payback, tornados, monthly cashflow, lifecycle cycles, revenue
  stack, LCOE/LCOS summary) plus the daily / monthly / yearly
  energy plots without error.
