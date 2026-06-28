# Independent audit (v2) — verification, red-team & targeted features

Independent second pass over the PV & BESS Optimizer. The first
production-readiness pass (`docs/production_readiness_report.md`) merged 34
fixes; this pass does **not** repeat it. Its mandate is threefold:

1. **Distrust and verify** the prior pass — reproduce the gate from a clean
   environment, mutation-check the regression locks, and re-derive the core
   numbers from first principles with an independent calculation.
2. **Red-team** the software — attack the inputs, the solver path, the
   determinism guarantees and the test suite before production does.
3. **Deliver two confirmed scope changes** to Gridcog-style revenue-stacking
   best practice (recorded below).

Definition of done: the two scope changes are implemented, documented and
tested; the core results are independently reproduced; the inputs and tests
are attacked; and zero findings remain open at any severity (P0/P1/P2/P3),
proven by evidence.

Severity rubric (same as the prior pass): **P0** wrong numbers / crash /
corruption / silent-incorrect / security; **P1** documented feature broken,
broken validation, surface divergence, missing error handling; **P2**
wrong/misleading docs, drift, output inconsistency, missing edge handling;
**P3** polish.

---

## Phase 0 — decided scope (settled, not re-litigated)

Both scope changes below are **owner-decided**. They are recorded here and
implemented in Phase 2; they are not re-opened.

### Decision 1 — Balancing ⇄ mode = "Approach B, guarded"

FCR / aFRR / mFRR participation stays **valid in both** `self_consumption`
and `merchant` mode — opt-in via `balancing_enabled`, OFF by default. The
activation gate (`optimization._resolve_balancing_inputs`, keyed on
`balancing_enabled AND bess_present`) is kept; **no mode gate** is added.

Guardrail to add: when `balancing_enabled` is true **and** mode is
`self_consumption`, emit **one** clear INFO/WARNING at load/resolve time
noting that revenue-stacking balancing participation under self-consumption
in practice requires an aggregator/BSP route-to-market and TSO
prequalification, and that the user should verify their self-consumption
scheme permits market cumulation. The same caveat is documented in README,
`self_consumption_design.md` and `balancing_market_design.md`. The warning
(and the both-mode contract) is locked with a test.

### Decision 2 — Balancing-aggregator (BSP / route-to-market) fee

Today balancing revenue is modelled **fee-free** (settled with the TSO, not
through the energy aggregator). For behind-the-meter / smaller assets,
balancing participation is routed through an aggregator/BSP that keeps a
share, so fee-free balancing **overstates** returns. A new optional,
separate per-stream route-to-market fee is added (Gridcog-style):

* Input key `balancing_aggregator_fee_pct_revenue` on the `economics` sheet,
  range-validated `[0, 100]`, **default 0.0**, mirroring the existing
  `aggregator_fee_pct_revenue` across all three config surfaces.
* Applied as a non-negative deduction to **gross** balancing revenue
  (capacity + activation) per operating year, escalated consistently with
  the balancing-revenue inflation, **before** it enters the cashflow — its
  own negative `balancing_aggregator_fee_eur` column on the yearly /
  quarterly / monthly cashflow, mirroring `aggregator_fee_eur`.
* NPV / IRR / ROI / payback consume the **net** balancing revenue. PPA
  carries neither fee. LCOE/LCOS exclude balancing and its fee (unchanged
  convention).
* Default 0.0 ⇒ all existing outputs stay **bit-identical** and the prior
  suite stays green; the capability is opt-in and fully documented.

For the prior pass's deferred owner-sign-off items (version bump 0.9.0 vs
0.9.1; discount/inflation sign bounds left unbounded; sizing+scenarios
warn-vs-error), the prior decision is kept unless found incorrect.

---

## Phase 1 — verification of the prior pass

### Gate reproduction (clean environment)

Environment: Python 3.11.15. Resolved tool versions match the prior report
except `highspy` (1.14.0 → 1.15.0, minor; noted, not a finding).

| Gate | Command | Prior report | This pass |
|---|---|---|---|
| ruff | `python -m ruff check .` | PASS | PASS |
| mypy | `python -m mypy` | PASS (46 files) | PASS (46 files) |
| vulture | `python -m vulture` | PASS | PASS |
| fast lane | `python -m pytest tests/ -q` | 1377 passed | _(pending — see Phase 1 log)_ |
| slow lane | `python -m pytest tests/ -q -m slow` | 8 passed | _(pending)_ |
| docs html | `make -C docs html` | PASS | _(pending)_ |

### Prior-pass lock verification (mutation check)

Each P0/P1 prior fix (and a representative P2 sample) is verified by
reverting the fix in a scratch copy and confirming the **named** regression
test fails. A lock that still passes with the bug reintroduced is a P1
finding (rewrite to truly pin the invariant).

| Finding | Sev | Named lock | Reverts → test fails? | Verdict |
|---|---|---|---|---|
| F6 | P1 | `test_cost_keys_validated_on_real_workbook_sections` | _pending_ | _pending_ |
| F13 | P1 | `_validate_ppa_config` settlement check | _pending_ | _pending_ |
| F22 | P1 | `test_repo_hygiene` allow-list | _pending_ | _pending_ |
| F32 | P1 | `test_script_runs_standalone_without_install` | _pending_ | _pending_ |
| F29 | P2 | `test_breakeven_duplicate_capacities_no_divide_by_zero` | _pending_ | _pending_ |
| F4 | P2 | `test_gearing_*` | _pending_ | _pending_ |
| F14 | P2 | degradation non-negative checks | _pending_ | _pending_ |

---

## New findings (this pass)

_None recorded yet._

---

## Mutation-kill summary

_Pending Phase 4._

---

## Independent sign-off

_Pending Phase 7._
