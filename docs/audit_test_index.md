# Phase 3 Test-Suite Audit — `tests/`

One-line summary plus verdict per file. 60 test files reviewed. Investigation prioritised stale config keys (`p_charge_max` / `e_cap` as decision var / pre-mode references / flat uncertainty params), brittle KPI key-count assertions, smoke-only tests, and duplicates.

## Per-file verdicts

* `test_annotation_safety.py` — Verifies `apply_universal_margins` padding semantics and that `plot_revenue_stack_yearly` floors at zero for non-negative bars. **KEEP**
* `test_asset_modes.py` — PV-only / BESS-only / hybrid mode pinning via `derive_asset_capacities` and `run_scenario`, plus project-mode label round-trip. **KEEP**
* `test_balancing_invariants.py` — Six balancing-market dispatch invariants (INV-B1..INV-B6) including the off-state preserving the prior 9 invariants. **KEEP**
* `test_balancing_io.py` — Workbook loader / writer round-trip for the `balancing` sheet, share-sum validation, settlement-minutes mismatch, gating when disabled. **KEEP**
* `test_balancing_lifetime_cashflow.py` — Per-year capacity and activation balancing revenue scale by `bess_factor × (1+bm_inflation)^(y-1)`; Year 0 is zero. **KEEP** (Phase-1 add)
* `test_balancing_mc.py` — Monte-Carlo balancing helper quantile ordering, reproducibility, SOC-constrained fraction bounds, empty dict when disabled. **KEEP**
* `test_balancing_mc_coupling.py` — Regression for the coupled-draw fix in `realise_balancing_scenario` (SOC pass and revenue pass consume the same Bernoulli draws). **KEEP** (Phase-1 add)
* `test_balancing_module.py` — Unit tests for `pvbess_opt.balancing` constants, dataclass, share / probability getters, synthetic timeseries reproducibility and shape. **KEEP**
* `test_balancing_optimization.py` — MILP-level integration: off-state matches baseline, on-state adds `bm_reservation_*` columns, per-direction power budget and SOC headroom hold, FCR symmetry. **KEEP**
* `test_bess_degradation_cycle.py` — Cycle-fade additivity, baseline KPI fixture comparison, reconciliation invariant, old workbook fallback for `bess_degradation_pct_per_cycle`. **KEEP**
* `test_bess_only_output_frame.py` — BESS-only solve zeros phantom PV in the output frame and passes energy-balance and invariants. **KEEP**
* `test_bess_spec.py` — Symmetric `bess_power_kw`, `bess_capacity_kwh` parameter (not decision var), `e_cap_mwh` KPI sourced from capacity, two-tuple `run_scenario` return. **KEEP**
* `test_bess_utilization.py` — Confirms the BESS-utilisation diagnostics block is present and that the solver actually cycles the battery when PV surplus exists. **KEEP**
* `test_color_registry.py` — Asserts `COLORS` / `MERCHANT_COLORS` have unique mappings, energy-counterpart parity, and no inline hex literals in plotting modules. **KEEP**
* `test_combined_with_soc.py` — Render checks for daily combined+SOC plots in both modes: PDF emission, BESS-absent twinx collapse, SOC overlay axis layout, grid-charge legend. **KEEP**
* `test_cumulative_payback_dedup.py` — `plot_cumulative_cashflow` draws no payback markers; `plot_payback` draws them; `main.py` uses the renamed filename. **KEEP**
* `test_dispatch_invariant_hardening.py` — Unrounded `invariant_4` stays below 1e-4 on the full year, NaN-fill warning carries the location, and time-limit-no-incumbent raises. **KEEP**
* `test_economic_model_acceptance.py` — Five economic-model acceptance invariants plus a HiGHS-gated uncertainty round-trip producing the 16-row `rolling_horizon_compare_mc` sheet. **KEEP**
* `test_economics.py` — `calculate_irr`, `derive_asset_capacities`, `build_yearly_cashflow`, monthly aggregation, and lowercase KPI keys for the multi-year economics module. **KEEP**
* `test_economics_retail_dam_split.py` — Retail-only vs DAM-only inflation flows; zero DAM inflation flattens export revenue; retail+DAM sum invariant. **KEEP**
* `test_economics_v08.py` — DEVEX folds into Year 0, unavailability derate, aggregator-fee scaling, yoy revenue monotonicity, baseline reproducibility. **KEEP**
* `test_financial_kpis.py` — Hand-checked LCOE / LCOS, capacity factor, lifetime cycles, revenue breakdown keys, and plot helper smoke renders. **KEEP**
* `test_financial_label_consistency.py` — `FINANCIAL_LABELS` cover + canonical legend ordering, `Net cash-flow` label, year-annotated payback prefix match, IEEE charcoal colour. **KEEP**
* `test_grep_audits.py` — Source-level grep audits (no inline bbox text / white marker edge / italic prose / inline hex / date-format mismatches / lifecycle imports legend helper). **KEEP**
* `test_grid_export_unlimited.py` — Optional/unlimited `p_grid_export_max_kw` via empty cell / disable-token strings, finite Big-M substitution, zero curtailment, finite-cap pass-through parity. **KEEP**
* `test_input_workbook_smoke.py` — End-to-end `main.py` smoke run on the case-study workbook plus headline-KPI pin on the full year. **KEEP**
* `test_input_workbook_style.py` — Style of the shipped `input.xlsx`: no amber fills anywhere, bold `F2F2F2` header row, data rows carry no per-cell fill. **KEEP**
* `test_inputs_uncertainty.py` — `_lognormal_band` collapse / ordering and PDF smoke for the three input-uncertainty plots. **KEEP**
* `test_io.py` — Sheet-defaults keys, `_parse_bool` / `_flat_dict_from_sheet`, timestep detection, mode validation, max-injection-profile schema. **KEEP**
* `test_io_v08_schema.py` — Pins the exact key sets of each sheet (project / pv / bess / economics / simulation), round-trip warning silence, misplaced-key routing. **KEEP**
* `test_irr_tornado_dumbbell.py` — `plot_irr_tornado` dumbbell PDF rendering, single-driver path, discount-rate row drop, empty / zero-spread placeholders. **KEEP**
* `test_kpis.py` — KPI keys are lowercase, canonical keys present, energy-balance residuals under tolerance, merchant zeroes load KPIs. **KEEP**
* `test_kpis_financials_contract.py` — `derive_monthly_cashflow` / `build_lifetime_dispatch` / `aggregate_lifetime_to_yearly` ordering guards plus diagnostics flattening in `kpis_year1`. **KEEP**
* `test_lcoe_lcos_summary.py` — Separate `plot_lcoe_summary` and `plot_lcos_summary` PDFs, PV-only / BESS-only N/A fallbacks, benchmark constants, legend contents. **KEEP**
* `test_lifetime.py` — Multi-year lifetime dispatch: pv_factor invariant, cashflow vs lifetime BESS-revenue reconcile, replacement reset, Feb-29 rollover, unavailability symmetry. **KEEP**
* `test_max_injection_default_is_no_curtailment.py` — `DEFAULT_MAX_INJECTION_PCT_HOURLY = 100`, default profile -> flat 1.0 fraction, shipped workbook is no-curtailment. **KEEP**
* `test_max_injection_profile.py` — Hourly / monthly profile helper, workbook round-trip, HiGHS-gated end-to-end cap enforcement, hour-of-day interval parser. **KEEP**
* `test_merchant_plots.py` — Daily / monthly / yearly merchant trio rendering plus dispatcher branching on `params['mode']`. **KEEP**
* `test_no_historical_version_strings.py` — Scans the repository for forbidden version strings, phase/round/bug annotations, and pre-v0.8 markers. **KEEP**
* `test_npv_waterfall.py` — `plot_npv_waterfall` legend has six canonical entries, no in-axis DEVEX/CAPEX text annotations. **KEEP**
* `test_optimization.py` — `derive_tight_big_m` tightness, dispatch invariants in both modes, terminal-SOC override, initial-SOC kWh override, PV priority and no-simultaneity rules. **KEEP**
* `test_plot_bess_revenue.py` — Canonical 8-key revenue aggregates, double-counting invariant across DAM + balancing products, BESS revenue plot smoke renders. **KEEP**
* `test_plot_scopes.py` — `_scope_active_for_year` truth table, `_ALLOWED_VALUES` cover `none`/`year1_only`/`all`, `main.py` drops the obsolete `plot_daily_year1` token. **KEEP**
* `test_plotting_sensitivity.py` — `_format_driver_value` cases, tornado driver-value annotations, base-line legend entry, label-overlap geometry, minimal-frame fallback. **KEEP**
* `test_plotting_uncertainty.py` — DD-MM-YYYY date format, `upper right` legend pin, and PDF smoke for the four uncertainty diagnostic plots. **KEEP**
* `test_plotting_universality.py` — Enumeration test: every public `plot_*` function obeys universality rules (no white marker edge, no inline hex, no raw bbox-text, `apply_universal_margins` called). **KEEP**
* `test_pv_loader.py` — Eleven PV-loader contracts: rescaling pass-through, `pv_kwh_override`, partial-NaN rejection, implausible specific-production warning. **KEEP**
* `test_realscale_all_combos.py` — Parametrised energy-balance + 9-invariant coverage across the six mode x asset combinations (fastlane + `slow` full-year). **KEEP**
* `test_resample_timeseries.py` — `scripts.resample_timeseries._resample_column` energy summing / splitting and price averaging / forward-fill. **KEEP**
* `test_revenue_stack_line_colour.py` — `net_revenue_line` colour registered and high-contrast, stack components sum to the net line, aggregator-fee bar suppressed at zero. **KEEP**
* `test_rolling_horizon.py` — Forecast-noise sign-aware semantics, hours-to-steps helper, RH dispatch SOC continuity and invariants, Monte-Carlo reproducibility, merchant parity. **KEEP**
* `test_rolling_horizon_realscale.py` — Single-seed real-scale guard (~89 s baseline) with profit envelope and wall-clock budget. **KEEP**
* `test_sensitivity.py` — IRR / NPV sensitivity variable sets, lowercase columns, CAPEX delta sign on NPV. **KEEP**
* `test_site_lump_sum_costs.py` — Site CAPEX / DEVEX defaults, folding into Year 0, NPV / IRR sensitivity, LCOE / LCOS unchanged, round-trip and sensitivity scaling. **KEEP**
* `test_soc_axis_range.py` — Daily / monthly / yearly SOC plot dual-axis layout: left 0-100 with 10 % ticks, right 0-capacity with 11 ticks, right axis has no gridlines. **KEEP**
* `test_soc_no_markers.py` — Monthly / yearly SOC aggregate lines draw with no point markers; daily SOC still renders its raw 15-min trace. **KEEP**
* `test_soc_plot_aggregation.py` — Merchant overlay masks night zeros; monthly / yearly SOC aggregate `soc_pct` directly; x-axis extends through the last bin. **KEEP**
* `test_tornado_labels.py` — Tornado endpoint driver-value labels match the scenario at each axis position; labels stay outside the dots and y-axis spine; no overlap for tight-spread rows. **KEEP**
* `test_uncertainty_config.py` — Eleven uncertainty defaults, per-source enable flags on `add_forecast_noise`, foresight-gap zero when all sources off, source-set plot helpers. **KEEP**
* `test_v0_leftover_audit.py` — Forbidden / required token grep audit, required-file presence, legacy-warning silence on `inputs/input.xlsx`, version-badge sanity. **KEEP**
* `test_version_badge_consistency.py` — README shields.io version badge equals `pvbess_opt.__version__`. **KEEP**
* `test_workbook_io.py` — Per-sheet typed contract (types + ranges), grid-export limit lives on `project`, full typed-dict round-trip with float tolerance. **KEEP**
* `test_year0_convention.py` — `build_yearly_cashflow` row count, Year-0 = start-1 / Year-1 = start mapping, `capex_year` KPI, lifetime first-calendar alignment, payback-marker mapping. **KEEP**

## Summary

* `KEEP`: 60
* `UPDATE`: 0
* `MERGE-INTO`: 0
* `DELETE`: 0

## Recommendations

The suite is in very good shape after the Phase-1 dead-code / mypy pass. No file currently asserts an exact KPI key count (`len(kpis) == N`), references the removed `p_charge_max_kw` / `p_dis_max_kw` / `p_charge_kw` / `p_dis_kw` keys outside the leftover-audit allowlist, treats `e_cap` as a decision variable, depends on flat uncertainty params, or runs as pure smoke. The few minor opportunities below are optional polish, not hygiene blockers.

1. `tests/test_kpis.py::test_compute_kpis_contains_canonical_lowercase_keys` only spot-checks seven keys. If you want stronger coverage of the v0.9 surface, consider extending the canonical-key list with one representative balancing aggregate (e.g. `bm_total_balancing_revenue_eur` and `revenue_bess_fcr_eur`) so the contract test catches accidental removal of the new BM block.
2. `tests/test_io_v08_schema.py::test_project_sheet_keys` and the four sibling exact-set asserts will need to be updated alongside any future v0.9.1 sheet-key additions; that is the intended brittleness, but flag it in the v0.9 changelog so reviewers know to update the four `expected = { ... }` literals when keys move.
3. `tests/test_dispatch_invariant_hardening.py::test_invariant4_unrounded_full_year_bess_only` is marked `slow` and runs `bess_only` + `self_consumption` + `allow_bess_grid_charging=True`. The same combination is also covered (lighter) by `test_realscale_all_combos.py`. Both are valuable; consider an explicit cross-reference comment so future cleanups do not assume them redundant.
4. `tests/test_no_historical_version_strings.py` and `tests/test_v0_leftover_audit.py` have heavy overlap on the version-string / phase-annotation patterns. They scan different surfaces (FORBIDDEN regex list vs literal-token list) so each catches things the other misses; keep both, but consider centralising the shared token list in a fixture if the two diverge further.
5. The `test_balancing_*` files share a `_balancing_on(params, **overrides)` helper that is copy-pasted three times. A small `tests/_balancing_helpers.py` would deduplicate without changing behaviour — purely a maintenance suggestion.
