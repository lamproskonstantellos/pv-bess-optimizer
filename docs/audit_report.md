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

For each:
- ID: P0-NNN
- Area: io | optimization | balancing | kpis | economics | lifetime |
        rolling_horizon | plotting | tests | docs | inputs | rename | other
- Description (one short paragraph).
- Reproduction (commands or test case).
- Fix (commit SHA when applied).
- Verification (which test now covers it).

_Populated incrementally across phases._

## 4. P1 backlog (documented, NOT fixed)

_Populated during static and dynamic discovery phases._

## 5. P2 backlog (documented, NOT fixed)

_Populated during static and dynamic discovery phases._

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
