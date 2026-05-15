# Changelog

This file tracks only the most recent release.  The repository no
longer carries internal version history — past migration notes and
breaking-change diffs have been folded into the present-tense
descriptions across the codebase and Sphinx docs.

## 0.8.7 — current

Corner value annotations (NPV total, lifetime cycle total) now
anchor at upper-right with deterministic frame expansion when
needed.  If data would overlap the annotation, the y-axis upper
limit is computed exactly (measured pixel overlap converted to a
data-coordinate Δy) and snapped to the next clean tick value via
matplotlib's standard tick locator.  The annotation home stays
upper-right; the frame grows to a round number; tick labels remain
clean.  A figure-level fallback covers pathological cases.

Public surface:

* New `pvbess_opt.plotting.style.anchor_corner_value(ax, *, text,
  loc="upper right", fontsize=8, borderaxespad=0.5)`.  Always
  anchors at upper-right by default; if the trial placement
  overlaps any data artist (or the legend), measures the pixel
  overlap, converts it to a data-coordinate delta, and snaps the
  new ymax to the next "nice" tick value via
  `matplotlib.ticker.MaxNLocator` with the standard step family
  (1, 2, 2.5, 5, 10) so y-axis tick labels stay aligned with the
  rest of the codebase's tick aesthetics.
* `plot_npv_waterfall` and `plot_lifetime_cycles` switch from the
  v4-era `apply_universal_margins(ax, y_frac=HEADROOM_Y_FRAC)` +
  `annotate_value_safe(..., transform=ax.transAxes, ...)` combo to
  the new helper.  Strict order of operations: data → legend →
  `apply_universal_margins(ax)` → `anchor_corner_value(ax, ...)`.
* The `HEADROOM_Y_FRAC` constant in `style.py` is removed (no
  callers remain).

Verification log:

* 453 tests pass under
  `pytest -q --ignore=tests/test_bess_utilization.py
  --ignore=tests/test_rolling_horizon.py --ignore=tests/test_kpis.py
  --ignore=tests/test_optimization.py
  --ignore=tests/test_asset_modes.py --ignore=tests/test_bess_spec.py
  --ignore=tests/test_curtailment_profile.py
  --ignore=tests/test_plot_scopes.py
  --ignore=tests/test_uncertainty_config.py
  --ignore=tests/test_merchant_plots.py` (the ignored suites
  require either the `pyomo` Python package or an external HiGHS /
  CBC solver binary, neither of which is available in this
  environment).
* Coverage includes the new v5 anchor-corner suite in
  `test_annotation_safety`
  (`test_anchor_corner_value_snaps_to_nice_tick_when_expanding`,
  `test_anchor_corner_value_no_expansion_when_corner_already_clear`,
  `test_anchor_corner_value_expansion_is_single_shot`,
  `test_anchor_corner_value_lands_in_upper_right_quadrant`,
  `test_npv_waterfall_zero_overlap_and_clean_ticks`,
  `test_lifetime_cycles_zero_overlap_and_clean_ticks`) and the
  refreshed `test_npv_total_annotation_inside_frame` finder in
  `test_npv_waterfall_redesign` that locates the AnchoredText
  artist and verifies upper-right quadrant placement inside the
  axes spines.
* `test_plotting_universality.test_all_plotting_functions_registered`
  remains green; the registry is unchanged.
* Audits 1–7 grep clean across the plotting package.
