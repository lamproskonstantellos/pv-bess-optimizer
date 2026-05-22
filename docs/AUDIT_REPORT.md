# End-to-end audit report

This report is the historical record of the legacy-reference cleanup and
the site-wide lump-sum CAPEX/DEVEX feature integration.  It is the **one**
file in the repository that intentionally retains the old version /
phase / round / bug tokens, because it documents what was removed.  Every
other surface is rewritten in present-tense terms.

The audit covers three areas:

1. Legacy references in user-facing surfaces (docstrings, comments,
   workbook notes, README, docs).
2. Test inventory (active / obsolete / duplicate / regression-pin).
3. Bugs and inconsistencies surfaced while reading the code.

---

## Legacy references

Classification key:

* **REMOVE** — purely historical context, no current value.
* **REWRITE** — explains current behaviour but in past-tense /
  release-comparison terms; restate in plain present tense.
* **KEEP** — genuine migration shim with a documented one-release
  deprecation contract.  These stay until the next schema-breaking
  release.

### `pvbess_opt/` source

| file:line | snippet | classification | proposed rewrite |
|---|---|---|---|
| io.py:26 | "fall back to a constant 73 % and log INFO. The legacy schema (`curtailment_profile`…)" | KEEP | Migration shim — keep the `curtailment_profile` → `max_injection_profile` description. |
| io.py:204-236 | `_LEGACY_RENAMED` / `_LEGACY_REMOVED` constants + "no longer supported" hints | KEEP | Documented one-release deprecation contract. |
| io.py:366-369 | `bess_degradation_pct_per_cycle` note: "…recover pre-v0.8.8 calendar-only behavior." | REWRITE | "Set to 0 to disable cycle aging (calendar-only mode)." |
| io.py:726-740 | legacy-key WARNING log strings | KEEP | Warning path for the deprecation contract. |
| io.py:758-768, 858 | `curtailment_profile` shim parser + "legacy integer format" | KEEP | Migration shim + integer `hour_of_day` parser. |
| io.py:1084-1090 | "Old workbook (pre-v0.8.8): default the cycle-fade…", "pre-v0.8.8 behavior" log | REWRITE | "Older workbooks that omit the key default it to 0.0 (calendar-only mode)." Drop version. |
| io.py:1112-1126 | `curtailment_profile` DeprecationWarning | KEEP | Migration shim. |
| economics.py:280-290 | "legacy single-rate behaviour", "legacy single-curve behaviour", "legacy fallback" | REWRITE | Describe the fallback: revenue with no per-stream breakdown is degraded on the PV factor and indexed at the retail rate. |
| economics.py:805 | "BESS capacity-fade decomposition at the final year (v0.8.8)" | REMOVE | Drop the `(v0.8.8)` tag. |
| optimization.py:402 | "PV-only / BESS-only / hybrid asset support — see Phase 3 of the" (dangling comment) | REWRITE/REMOVE | "Pin all flows for an absent asset to zero." (also fixes the truncated sentence — see Bugs). |
| optimization.py:786-787 | "…so it is no longer a decision variable and no longer returned." | REWRITE | "`e_cap` is a parameter pinned to `params['bess_capacity_kwh']`; it is not a decision variable and is not returned." |
| kpis.py:291-292 | "In v0.8 `e_cap` is no longer a decision variable…" | REWRITE | "`e_cap` is not a decision variable — the BESS energy capacity is pinned to `params['bess_capacity_kwh']`." |
| lifetime.py:118-120 | "Backward compatible: …result equals the pre-v0.8.8 calendar-only behaviour exactly." | REWRITE | "With the cycle keyword-only parameters left at 0 the result is the multiplicative calendar fade alone." |
| rolling_horizon.py:239 | "# v0.8: BESS energy capacity is pinned to params['bess_capacity_kwh']…" | REWRITE | Drop the `v0.8:` tag, keep the description. |
| availability.py:1 | "Annual unavailability derate helpers (v0.8 Phase 4)." | REMOVE | "Annual unavailability derate helpers." |
| config.py:166 | "Financial-plot colour palette (v0.8 polish)" | REMOVE | "Financial-plot colour palette." |
| sensitivity.py:129 | "Scale CAPEX *and* DEVEX by the same factor (v0.8 folds DEVEX in)." | REWRITE | "Scale CAPEX and DEVEX by the same factor — the CAPEX driver represents the full Year-0 outlay (CAPEX + DEVEX + site lump sum)." |
| sensitivity.py:181 | "# v0.8: CAPEX driver folds in DEVEX (single-asset Year-0 outlay)." | REWRITE | "The CAPEX driver is the full Year-0 outlay: per-asset CAPEX + DEVEX + site lump sum." |
| plotting/financial.py:662 | "legacy frames without driver metadata" | REWRITE | "frames without driver metadata" (describe the no-op branch). |
| plotting/lifecycle.py:214 | "Round-3 universality rule forbids markeredgecolor='white' rings" | REWRITE | "The universality rule forbids `markeredgecolor='white'` rings; contrast comes from the charcoal colour." |
| plotting/lifecycle.py:449-454 | "Round-3 redesign:…", "Round-5 splits LCOE and LCOS…" | REWRITE | Describe current behaviour: every numeric value lives in the legend; LCOE and LCOS are separate PDFs. |
| plotting/monthly.py:320 | "so legacy callers keep working" | REWRITE | "so callers that omit `soc_pct` still render." |
| plotting/style.py:169 | "Universal value-annotation helper (round-3)" | REMOVE | "Universal value-annotation helper." |
| main.py:605 | "feed the same number into build_lifetime_dispatch (Bug #3 fix)." | REWRITE | Describe the symmetric cycle-counter contract; drop the bug number. |

### `docs/` + `README.md` + `CONTRIBUTING.md`

| file:line | snippet | classification | proposed rewrite |
|---|---|---|---|
| README.md:33-57 | "## What's new in v0.8.10" section with F1–F12 bullets | REMOVE | Replace with a present-tense "Capabilities" overview. |
| README.md:4 | version badge `version-0.8.10` | KEEP | Dynamic via `__version__`; bump in version-bump phase. |
| docs/CHANGELOG.md (whole) | per-version history back to 0.8.7 with F1–F12, pre-v0.8.8 | REMOVE | Replace with a single forward-looking current-release feature list. |
| docs/source/users.guide/inputs.rst:5-115 | "(v0.8)", "NEW in v0.8", "recover pre-v0.8.8 calendar-only behaviour" | REWRITE | Drop version tags; describe keys in present tense. |
| docs/source/users.guide/economics.rst:43 | "recovers the pre-v0.8.8 calendar-only behaviour exactly" | REWRITE | "disables the cycle-fade term (calendar-only mode)." |
| docs/source/technical.documentation/mip_formulation.rst:62 | "explicitly removed in v0.8 (see `REMOVED_BESS_KEYS`…)" | REWRITE | "not part of the schema (see `_LEGACY_REMOVED` in `pvbess_opt/io.py`)." |
| docs/source/technical.documentation/lifetime_scaling.rst:54 | "`bess_factor` reduces to the pre-v0.8.8 calendar-only formula" | REWRITE | "with the cycle coefficient at 0, `bess_factor` is the calendar-only formula." |

---

## Test inventory

Classification key: **ACTIVE** (current public surface), **OBSOLETE**
(removed/renamed surface), **DUPLICATE** (≥80 % overlap; identify the
canonical), **REGRESSION-PIN** (valuable assertion, bug-number name to
rewrite).

| test file / id | classification | action |
|---|---|---|
| test_no_historical_version_strings.py | ACTIVE | Keep; expand FORBIDDEN list with new banned patterns (Phase N, Round-N, Bug #, F1–F12, pre-v0.8, v0.8 polish, post-DEVEX, post-refactor, pre-refactor). |
| test_v0_leftover_audit.py | ACTIVE | Keep; add `site_capex_eur` / `site_devex_eur` to REQUIRED_TOKENS. Rename docstring tokens stay present-tense. |
| test_grep_audits.py:1,74,92 | ACTIVE | Rewrite "round-3 universality addendum" → "universality rules". |
| test_lifetime.py:85,109 (`test_f2_…`) | REGRESSION-PIN | Rename `test_f2_cashflow_lifetime_bess_revenue_ratio_reconcile` → `test_cashflow_and_lifetime_bess_revenue_reconcile`; drop "F2". |
| test_lifetime.py:247 (Bug #6c) | REGRESSION-PIN | Docstring already describes the invariant; keep name `test_feb29_lifetime_does_not_roll_over_in_non_leap_target_years`-style; drop "Bug #6c". |
| test_lifetime.py:281,326 (Bug #3) | REGRESSION-PIN | Rewrite docstrings to the symmetric cycle-counter contract; drop "Bug #3". |
| test_revenue_stack_line_colour.py:135 (Bug #2) | REGRESSION-PIN | Rewrite docstring; drop "Bug #2". |
| test_rolling_horizon.py:90 (Bug #1) | REGRESSION-PIN | Rewrite comment to "window/commit hours are real hours on sub-hourly cadences"; drop "Bug #1". |
| test_bess_only_output_frame.py:1 (F1) | REGRESSION-PIN | Rewrite module docstring; drop "F1". |
| test_realscale_all_combos.py:1,7 (F7/F1) | ACTIVE/REGRESSION-PIN | Rewrite docstring to describe the 9-invariant coverage across mode×asset; drop F-tags. |
| test_phase3_hardening.py (F5/F6/F9) | REGRESSION-PIN | Rename file → test_dispatch_invariant_hardening.py; rename `test_f5_*`, `test_f6_*`, `test_f9_*`; rewrite comments. |
| test_io.py:42,276 (post-refactor, Bug #4) | REGRESSION-PIN | Rewrite comments; drop tags. |
| test_year0_convention.py:1,187 (Phase 2, v06) | ACTIVE | Rewrite docstring; rename `test_plot_payback_marker_axis_v06` → `…_axis`. |
| test_economics.py:93 (`_v06_convention`) | ACTIVE | Rename `test_calendar_year_v06_convention` → `test_calendar_year_convention`. |
| test_economics_v08.py:1 ("Post-DEVEX") | ACTIVE | Rewrite module docstring → "DEVEX / availability / aggregator-fee economics tests."; keep tests. |
| test_economics_retail_dam_split.py | ACTIVE | Keep; rewrite version-y docstring lines. |
| test_irr_tornado_redesign.py:1 (Phase 5) | ACTIVE | Rename file → test_irr_tornado_dumbbell.py; rewrite docstring. |
| test_uncertainty_config.py:1 (Phase 4) | ACTIVE | Rewrite docstring; drop "Phase 4". |
| test_merchant_plots.py:1,191 (Phase 6, Round-5) | ACTIVE | Rewrite docstrings; drop tags. |
| test_plotting_sensitivity.py:1,91 (Phase 5, v0.8.8, v0.8.7-style) | ACTIVE | Rewrite docstrings; covers render + format + range + base + geometry. |
| test_tornado_labels.py | ACTIVE | Closer inspection: overlap with test_plotting_sensitivity.py is well under 80 %. This file uniquely covers the *semantic* correctness of which scenario's driver value maps to which endpoint (the inverted-ordering swap), plus y-axis-spine clipping. Retained and cleaned (rewrite docstrings, drop the "Round-4" comment) rather than deleted — deleting would lose the axis-position-matching coverage. |
| test_economic_model_acceptance.py:1 (Phase 7) | ACTIVE | Rewrite docstring; add a `site_capex_eur=100_000` parametrized variant. |
| test_plotting_uncertainty.py:1 (Phase 8) | ACTIVE | Rewrite docstring; drop "Phase 8". |
| test_plot_scopes.py:1 (Phase 5) | ACTIVE | Rewrite docstring; drop "Phase 5". |
| test_bess_degradation_cycle.py:1,8 (Phase 3, v0.8.8, v0.8.10) | ACTIVE | Rewrite docstring; rename `test_zero_cycle_pct_matches_v087` / `test_missing_key_matches_v087` → `…_matches_calendar_only_baseline`. |
| test_asset_modes.py:1 (Phase 3) | ACTIVE | Rewrite docstring; drop "Phase 3". |
| test_plotting_universality.py:2 (round-3) | ACTIVE | Rewrite docstring; drop "round-3". |
| test_max_injection_profile.py:148,299 (v08) | ACTIVE | Rewrite comment; rename `test_loader_parses_v08_interval_string_hour_of_day` → `…_parses_interval_string_hour_of_day`. |
| test_grid_export_unlimited.py:1,27,135 (Phase 2, F401, pre-v0.8.8) | ACTIVE | Rewrite docstring; replace `import highspy  # noqa: F401` with `importlib.util.find_spec` (removes the `\bF[0-9]+\b` match); rewrite the pre-v0.8.8 comment. |
| test_input_workbook_smoke.py:121 (pre-refactor) | ACTIVE | Rewrite docstring to "headline year-1 KPIs pinned to the baseline"; add lump-sum=0.0 load check. |
| test_lcoe_lcos_redesign.py:3,80 (Round-5) | ACTIVE | Rename file → test_lcoe_lcos_summary.py; rewrite docstring. |
| test_financial_kpis_v06.py | ACTIVE | Rename file → test_financial_kpis.py; it is the canonical LCOE/LCOS/capacity-factor/cycles suite. (REQUIRED_FILES entry updated accordingly.) |
| test_npv_waterfall_redesign.py | ACTIVE | Rename file → test_npv_waterfall.py; rewrite docstring. |
| test_phase5_plot_polish.py:1 ("Phase-5 plot-polish") | ACTIVE | Rename file → test_soc_plot_aggregation.py; rewrite docstring. |
| test_io_v08_schema.py:160,172 (`_v08`) | ACTIVE | Keep file name (schema-version neutral `v08` token only in func names); rename `test_round_trip_v08` → `test_round_trip`. Extend project contract with site keys. |
| test_workbook_io.py:1 ("regression tests") | ACTIVE | Keep; "regression" word is generic, retain. Add site-key round-trip. |
| test_rolling_horizon_realscale.py | ACTIVE | Keep; "perf regression" wording is generic. |
| all other test files | ACTIVE | No legacy tokens; no change. |

No test exercises a surface that no longer exists, so there are **no
strictly OBSOLETE tests** — the redesign/phase suites all still pin
current behaviour and are reclassified ACTIVE-with-rename.  No test
file is a true ≥80 % DUPLICATE either: the tornado-label suites
(`test_tornado_labels.py` and `test_plotting_sensitivity.py`) were
examined closely and found to cover complementary aspects (semantic
which-value-where correctness vs. rendering geometry), so both are
retained and cleaned rather than consolidated.  No test files are
deleted; the cleanup is renames, docstring rewrites, and file renames
for the phase/redesign-named modules.

---

## Bugs and inconsistencies found

1. **`pvbess_opt/optimization.py:402` — truncated comment.** The comment
   reads `# PV-only / BESS-only / hybrid asset support — see Phase 3 of the`
   and the sentence is never finished; the following line begins an
   unrelated comment (`# Pin all flows for an absent asset to zero.`).
   This is a documentation defect (incomplete sentence) rather than a
   logic error.  Fixed as part of the Phase 2 rewrite, not a separate
   Phase 4 bug.

No genuine logic bugs (sign errors, off-by-one cycle counters, KPI key
spelling drift between modules, plot-label drift vs `FINANCIAL_LABELS`,
unguarded dereferences, or schema-default vs row-template mismatches)
were surfaced by the audit:

* Cycle-counter accumulators in `economics.build_yearly_cashflow` and
  `compute_financial_kpis` share the same `discharge_mwh / capacity_mwh`
  convention and both reset at `bess_replacement_year`.
* CAPEX/DEVEX signs are consistently negative in Year 0; LCOE/LCOS read
  per-asset CAPEX directly (not the cashflow column), so they are
  isolated by construction.
* Every `_SHEET_DEFAULTS` key has a matching `_SHEET_ROW_TEMPLATES`
  entry and vice versa (verified by the workbook round-trip tests).
* `read_inputs` raises when both `pv_nameplate_kwp` and `bess_power_kw`
  are zero; no downstream code dereferences an absent asset.

Therefore **Phase 4 has no separate logic-bug commits** — the single
documentation defect above is folded into the Phase 2 present-tense
rewrite.
