# Changelog

This file tracks only the most recent release.  The repository no
longer carries internal version history — past migration notes and
breaking-change diffs have been folded into the present-tense
descriptions across the codebase and Sphinx docs.

## 0.8.5 — current

Plot-polish release covering four areas:

* **Baseline-aware universal margins** — `apply_universal_margins`
  in `pvbess_opt/plotting/style.py` is now baseline-aware: bar /
  stacked-bar plots whose data starts at 0 keep their y-min floored
  at the data baseline (so bars sit flush against the €0 axis line),
  while plots whose data crosses zero still get symmetric top/bottom
  padding.  Bar plots also keep the leftmost bar at the left frame
  edge on the x-axis.  A new `HEADROOM_Y_FRAC` (0.12) constant lets
  plots with corner annotations opt in to extra breathing room.
* **Merchant combined energy plots** — new
  `plot_daily_combined_merchant`, `plot_monthly_combined_merchant`,
  and `plot_yearly_combined_merchant` give merchant runs a one-shot
  view of every flow at each resolution: PV→BESS / PV→Grid /
  PV→Curtailment / BESS→Grid / Import→BESS stacked together, with
  the PV generation line overlaid as the natural ceiling.  A new
  `"PV generation"` label (`#FFB300`) is registered in the energy
  palette.  Rendered from inside the `is_merchant` branch of the
  dispatcher in `main.py`.
* **NPV and lifetime-cycles annotation headroom** —
  `plot_npv_waterfall` and `plot_lifetime_cycles` opt in to
  `HEADROOM_Y_FRAC` so the "NPV = €X.XM" and "Total: N cycles" boxes
  sit with at least 5 % axes-fraction whitespace above the topmost
  data point.
* **Separate LCOE / LCOS summary PDFs** — `plot_lcoe_lcos_summary`
  is removed; `plot_lcoe_summary` and `plot_lcos_summary` each emit
  their own single-row PDF (`lcoe_summary.pdf`, `lcos_summary.pdf`)
  reusing the same `_draw_benchmark_row` renderer.  The rotated
  bold y-axis label is dropped — panel context is now implicit from
  the filename and legend entries.

Verification log:

* 508 tests pass under
  `pytest -q --deselect tests/test_bess_utilization.py --deselect
  tests/test_rolling_horizon.py --deselect tests/test_kpis.py
  --deselect tests/test_optimization.py` (the deselected suites
  require an external HiGHS / CBC solver binary that is unavailable
  in this environment).  Coverage includes the new
  `test_annotation_safety` suites
  (`test_apply_universal_margins_pads_top_for_non_negative_data`,
  `test_apply_universal_margins_pads_both_for_signed_data`,
  `test_apply_universal_margins_bar_plot_x_tight_left`,
  `test_bar_plot_revenue_stack_floors_at_zero`,
  `test_npv_total_annotation_has_full_breathing_room`,
  `test_lifetime_cycles_total_has_breathing_room`), the new
  merchant combined-plot suites in `test_merchant_plots`
  (`test_plot_daily_combined_merchant_renders`,
  `test_plot_monthly_combined_merchant_renders`,
  `test_plot_yearly_combined_merchant_renders`,
  `test_dispatcher_renders_merchant_combined`), and the rewritten
  `test_lcoe_lcos_redesign` suite covering
  `test_plot_lcoe_summary_renders`,
  `test_plot_lcos_summary_renders`,
  `test_lcoe_lcos_summary_function_is_gone`, and the
  no-y-axis-label invariants.
* `test_plotting_universality.test_all_plotting_functions_registered`
  passes with the three new merchant-combined and two new
  LCOE / LCOS entries in the registry.
* Audits 1–7 grep clean across the plotting package.
