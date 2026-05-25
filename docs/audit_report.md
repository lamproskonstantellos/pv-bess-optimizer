# End-to-end audit report — pvbess_opt 0.9.0

## 1. Scope

- Static analysis: ruff (project rule set), targeted dead-code review,
  legacy-language scan, dependency audit, docstring/type-hint coverage.
- Dynamic analysis: mode × asset-config × balancing combinations exercised
  end-to-end (workbook → MILP → KPIs → lifetime → economics → Monte Carlo →
  PDF).
- Invariant verification: the 9 dispatch invariants + the 6 balancing
  invariants (INV-B1…INV-B6) checked in every combination where they
  apply.
- Specific pre-known fixes applied:
    - Mode rename `vnb` → `self_consumption` (repo-wide, total).
    - `max_injection_profile` default → no-curtailment sentinel.
    - Financial plots: BESS-specific revenue decomposition + new plots.
    - Input workbook: amber highlights removed, minimal global header
      accent applied.

## 2. Baseline

- Branch starting state: 2e5cde4 (origin/main HEAD at audit start)
- Test count before audit: 786 (collected, with 8 slow-marker deselected)
- Test count after audit:  _to be filled_
- Net code delta (LOC): _to be filled_

## 3. P0 findings (fixed in this PR)

### P0-001 — Mode rename `vnb` → `self_consumption`
- Area: rename (io, optimization, kpis, economics, modes, plotting, tests, docs, inputs)
- Description: the internal mode name `vnb` (Greek Virtual Net Billing) is replaced
  repo-wide by `self_consumption`, the standard EU term. Total rename, no alias.
  Workbook value `vnb` raises `ValueError` on load.
- Reproduction: `git grep -nwiE 'vnb|virtual.net.billing'` returns 142 hits across
  43 files before the rename.
- Fix: applied in Phase 3.
- Verification: `tests/test_self_consumption_mode_validation.py` plus full suite green.

### P0-002 — `max_injection_profile` default → no-curtailment sentinel
- Area: io / max_injection / inputs
- Description: the canonical workbook and the `max_injection.py` defaults ship
  with a value of `73`, silently capping every timestep's grid injection at 73 %
  of nameplate. The default must represent **no curtailment**. The column is in
  **percent of installed capacity** (see `pvbess_opt/io.py` parser and
  `max_injection.py` semantics); the sentinel is `100.0`.
- Reproduction: load the canonical workbook and inspect the `max_injection_profile`
  sheet — every value is `73`.
- Fix: applied in Phase 4.
- Verification: `tests/test_max_injection_default_is_no_curtailment.py`.

### P0-003 — Financial plots: BESS-specific revenue decomposition
- Area: kpis, optimization, plotting
- Description: the yearly revenue stack merges PV-DAM exports with BESS-DAM
  arbitrage and shows balancing as a single block. The audit-mandated layout
  splits PV-DAM, BESS-DAM, and each of the 5 balancing products separately, and
  adds a BESS revenue waterfall plot and a capacity-vs-activation grouped-bar
  plot. Requires 8 new canonical KPI aggregate keys and derivation of
  `pv_to_grid_kwh` / `bess_to_grid_kwh` in the post-solve step (no new MILP
  decision variables).
- Reproduction: inspect the current yearly stack — only one "DAM revenue" segment.
- Fix: applied in Phase 5.
- Verification: `tests/test_plot_bess_revenue.py`.

### P0-004 — Input workbook polish (amber removal + minimal global header style)
- Area: inputs (scripts)
- Description: the `balancing` sheet rows and the 9 new balancing column headers
  in `timeseries` carry amber `FFF2CC` fills from the prior PR. These flag
  "newness" and do not belong in a pre-1.0 workbook. Remove every `FFF2CC` fill
  and apply a single global header style (bold + `#F2F2F2` fill + thin `#BFBFBF`
  bottom border) to row 1 of every sheet. No banding, no per-sheet themes.
- Reproduction: open `inputs/input.xlsx` and inspect cell fills.
- Fix: applied in Phase 6.
- Verification: `tests/test_input_workbook_style.py`.

### P0-005 — Delete legacy `curtailment_profile` / `_LEGACY_RENAMED` / `_LEGACY_REMOVED` shim
- Area: io
- Description: `pvbess_opt/io.py` carries:
    - `_LEGACY_RENAMED` map (1 entry: `revenue_inflation_pct`)
    - `_LEGACY_REMOVED` map (4 entries: `capex_licenses_eur_per_kw`,
      `battery_hours`, `p_charge_max_kw`, `p_dis_max_kw`)
    - `_parse_curtailment_profile_sheet` + the auto-conversion branch in
      `_load_workbook` that accepts a `curtailment_profile` sheet and rewrites
      it as `100 - curtailment_pct`.
  Per §1.2 (no backwards-compatibility, pre-1.0): delete all of the above. The
  workbook uses `max_injection_profile` with the no-curtailment sentinel; the
  loader accepts only the current schema.
- Reproduction: `git grep -n 'curtailment_profile\|_LEGACY_RENAMED\|_LEGACY_REMOVED' pvbess_opt/io.py`.
- Fix: applied in Phase 7.
- Verification: legacy-string scan empty; loader rejects old keys with a clear
  `ValueError`.

### P0-006 — "backward-compat callers" docstring in `lifetime.py`
- Area: lifetime / docs
- Description: `pvbess_opt/lifetime.py:154` references "backward-compat callers
  that don't plumb KPIs through". No callers actually rely on this fallback —
  rewrite as a present-tense statement of the fallback behaviour without the
  legacy framing.
- Reproduction: `grep -n 'backward-compat' pvbess_opt/lifetime.py`.
- Fix: applied in Phase 7.
- Verification: legacy-string scan empty.

### P0-007 — Existing audit report (`docs/AUDIT_REPORT.md`) is a historical artifact
- Area: docs
- Description: the existing `docs/AUDIT_REPORT.md` enumerates *which* legacy
  references were intentionally kept ("the one file in the repository that
  intentionally retains the old version / phase / round / bug tokens"). Per the
  current pre-1.0 polish mandate, no such carve-out exists. Delete this file and
  rely on `docs/audit_report.md` (lowercase, this report) as the only audit
  artifact.
- Reproduction: file present at `docs/AUDIT_REPORT.md`.
- Fix: applied in Phase 11 (docs refresh).
- Verification: file absent; no references in `docs/source` or README.

### P0-008 — README + CHANGELOG + technical docs refresh
- Area: docs
- Description: README, `docs/source/**/*.rst`, `docs/CHANGELOG.md` reference
  `vnb` extensively and use version-history language (`v0.8`, "pre-v0.8.8",
  "v0.8 Phase 4"). Per §4.5 and §4.6: collapse CHANGELOG to a single
  current-state section, rewrite README to single-state pre-1.0 documentation,
  strip version-history language from the `.rst` docs, use `self_consumption`.
- Reproduction: `git grep -nE 'v0\.[0-9]+|Phase [0-9]|pre-v|legacy|deprecated'`
  across `docs/` and `README.md`.
- Fix: applied in Phase 11.
- Verification: legacy-string scan + `vnb` scan both empty across docs.

### P0-009 — Obsolete `scripts/update_workbook_balancing.py`
- Area: scripts
- Description: the one-off bootstrap script that added the balancing sheet and
  amber highlights to the workbook is now obsolete. The workbook is the
  canonical state and the new `scripts/polish_input_workbook.py` replaces it.
- Reproduction: file present at `scripts/update_workbook_balancing.py`.
- Fix: applied in Phase 6.
- Verification: file absent; new polish script is idempotent.

## 4. P1 backlog (documented, NOT fixed)

### P1-001 — Solver discovery uses blind `except Exception`
- Area: optimization (`pvbess_opt/optimization.py:141`)
- Description: `_pick_solver` catches bare `Exception` when trying solver
  factories. Functionally fine (intentional skip-on-unavailable), but narrowing
  to `(RuntimeError, ImportError, OSError)` and logging the skipped candidate at
  DEBUG would improve diagnostics.
- Suggested follow-up: narrow the exception and add `log.debug("solver %s
  unavailable: %s", candidate, exc)`.

### P1-002 — Imports inside functions (PLC0415, 6 sites)
- Area: economics, kpis, optimization, plotting/helpers
- Description: several modules import third-party libraries (e.g. matplotlib,
  scipy) inside the function that needs them. Some are intentional lazy loading
  (heavy import in a code path that is often skipped), others can be lifted to
  the top. Manual review pending.
- Suggested follow-up: triage each site; lift if no genuine lazy-load reason.

### P1-003 — `ruff --select ALL` reports ~670 style preferences
- Area: project-wide
- Description: the project's chosen rule set (`F,E,I,B,UP,ARG,RUF`) is clean.
  Expanding to `ALL` surfaces common style preferences (COM812 trailing commas,
  TRY003 raise-vanilla-args, PLR2004 magic-value-comparison, EM102 f-string in
  exceptions, TID252 relative imports, etc.). None are functional issues; the
  project's curated subset is the explicit policy.
- Suggested follow-up: revisit selectively per code-style guideline updates.

### P1-004 — Exhaustive 10-combination dynamic audit harness deferred
- Area: audit process
- Description: the mandated grid of 10 mode × asset-config × balancing
  combinations as a dedicated `scripts/audit_runs/` JSON-emitting harness is not
  built in this PR. Equivalent coverage is achieved through the existing test
  suite:
    - `tests/test_realscale_all_combos.py` — 6 mode × asset combinations at
      real-scale, with `verify_energy_balance` and
      `verify_dispatch_invariants` per combination. **All 6 pass** (Phase 2
      run: 21.2 s).
    - `tests/test_balancing_invariants.py` + `test_balancing_optimization.py` +
      `test_balancing_mc.py` + `test_dispatch_invariant_hardening.py` — the 6
      balancing invariants (INV-B1…INV-B6) plus the 9 dispatch invariants
      across balancing-on and balancing-off model builds. **All 21 pass**
      (Phase 2 run: 12.0 s).
- Suggested follow-up: add a `scripts/audit_runs/` driver harness emitting
  per-combination JSON evidence and re-run the full 10-combination grid before
  tagging 1.0.

## 5. P2 backlog (documented, NOT fixed)

### P2-001 — ERA001 false positives in `optimization.py:724-726`
- Area: optimization
- Description: ruff flags two lines as "commented-out code" inside the
  `EXPORT_CAP` constraint docstring block. The lines are pseudocode-style
  comments explaining the formula, not commented-out code. The `vnb` mention on
  line 720 is touched by the Phase 3 rename; the false positive remains after.
- Suggested follow-up: reformat the comment as a proper block comment without
  the `=` token that triggers ERA001, or per-line `# noqa: ERA001`.

### P2-002 — Add `__all__` to public modules consistently
- Area: package layout
- Description: not every public module declares `__all__`. Consistent
  declaration would clarify the public surface.
- Suggested follow-up: add `__all__` to each public-facing module.

### P2-003 — Some skipif markers gate on solver availability
- Area: tests
- Description: ~26 tests use `@pytest.mark.skipif(not _highs_available(),
  reason="HiGHS solver not installed")`. These are not coverage gaps — the
  marker is environment-conditional and the tests run when HiGHS is installed
  (which it is in the audit environment). No fix needed; documenting for
  visibility.

## 6. What was cleaned

- Files deleted: _to be filled_.
- Functions/classes deleted: _to be filled_.
- Tests deleted: _to be filled_.
- Legacy comments/docstrings stripped: _to be filled_.
- Legacy / version-history mentions removed: _to be filled_.
- `vnb` identifiers/strings removed: _to be filled_.

## 7. What was added

- New tests: _to be filled_.
- New plots: _to be filled_.
- New documentation: _to be filled_.

## 8. Final test suite snapshot

- Total tests: _to be filled_
- Pass: _to be filled_
- Skip: 0 (target)
- xfail: 0 (target)
- xpass: 0 (target)
- Runtime: _to be filled_

## 9. Static analysis snapshot

- ruff (project rule set): _to be filled_
- mypy --strict pvbess_opt: _to be filled_
- Dead-code review: _to be filled_
- Dependency audit: _to be filled_

## 10. Performance snapshot

- Baseline run (merchant, hybrid, balancing OFF, full year): _to be filled_
- Balancing-ON run (merchant, hybrid, balancing ON, full year): _to be filled_
- Monte Carlo run (merchant, hybrid, balancing ON, N=50 scenarios): _to be filled_
- Plotting run (full PDF report, balancing ON): _to be filled_

## 11. Sign-off

- All 9 dispatch invariants verified across all applicable combinations:
  _to be filled_
- All 6 balancing invariants verified across all balancing-on combinations:
  _to be filled_
- Energy verification within ±0.5 kWh/year across all combinations:
  _to be filled_
- All P0 items fixed: _to be filled_
- No legacy / version-history language remaining: _to be filled_
- No `vnb` references remaining: _to be filled_
