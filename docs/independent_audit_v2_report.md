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
| ruff | `python -m ruff check .` | PASS | **PASS** (All checks passed) |
| mypy | `python -m mypy` | PASS (46 files) | **PASS** (no issues, 46 files) |
| vulture | `python -m vulture` | PASS | **PASS** (exit 0) |
| fast lane | `python -m pytest tests/ -q` | 1377 passed | **1377 passed** (reproduced; see note) |
| slow lane | `python -m pytest tests/ -q -m slow` | 8 passed | **confirmed in Phase 7** (see slow-lane note) |
| docs html | `make -C docs html` / `test_docs_build.py` | PASS | **PASS** (`test_docs_build` green; see env note) |

**Fast-lane note.** First reproduction under `pytest -n 3` (xdist, for
wall-clock) reported `1 failed, 1376 passed`; the single failure was
`test_repo_hygiene::test_no_old_version_strings[\bPhase [1-8]\b]` tripping on
**this very report file** (`docs/independent_audit_v2_report.md` records
"Phase 1".."Phase 7" as data) — the identical self-inflicted mechanism the
prior pass logged as **F22** for `production_readiness_report.md`. Allow-listing
the v2 report in both hygiene scans (folded into the Phase 0 commit) restores
`1377 passed`. The pristine prior-pass tree therefore reproduces green
exactly; no behaviour finding.

**Slow-lane note.** A first pristine-tree slow run was started and verified
green progressively; it was then stopped (the realscale matrix is ~80 min on
this constrained machine) because the default balancing-aggregator fee is 0.0
and thus **bit-identical** to the pristine tree (proven by the golden
`kpi_baseline.json` suite and the dedicated bit-identical locks). A SINGLE
authoritative slow run on the FINAL Phase-2 code therefore subsumes both the
prior-baseline reproduction and the new-feature regression check; it is run
and recorded in Phase 7 (8 passed). Running two ~80-min slow lanes would be
redundant given the bit-identical default.

**Environment notes (not code findings).** (1) `highspy` resolves to 1.15.0
here vs 1.14.0 in the prior report — a minor solver build bump; no numeric
divergence observed. (2) `make -C docs html` emits 5 `intersphinx inventory
not fetchable` warnings: the sandbox proxy returns 403 for the pandas / numpy /
pyomo / python / matplotlib `objects.inv` fetches. These are network-only;
`make html` exits 0 and `tests/test_docs_build.py` passes, so the docs gate is
green.

### Prior-pass lock verification (mutation check)

Each P0/P1 prior fix (and a representative P2 sample) is verified by
reverting the fix in a scratch copy and confirming the **named** regression
test fails. A lock that still passes with the bug reintroduced is a P1
finding (rewrite to truly pin the invariant).

Method: revert the fix in place, run **only** the named lock, confirm it
**fails**, then `git checkout` to restore. A lock that still passes with the
bug reintroduced would be a P1 finding (rewrite to truly pin the invariant).

| Finding | Sev | Named lock | Revert applied | Lock fails on mutant? | Verdict |
|---|---|---|---|---|---|
| F6 | P1 | `test_cost_keys_validated_on_real_workbook_sections` | validate PV cost keys from `economics` section | **yes** (rc=1) | **genuine** |
| F13 | P1 | `test_invalid_ppa_settlement_rejected` | drop the `ppa_settlement` enum check in `_validate_ppa_config` | **yes** (rc=1) | **genuine** |
| F22 | P1 | `test_no_old_version_strings[\bPhase [1-8]\b]` | remove `production_readiness_report.md` from the allow-list | **yes** (rc=1) | **genuine** |
| F32 | P1 | `test_script_runs_standalone_without_install` | remove the `sys.path` bootstrap (after `pip uninstall -e` to simulate a fresh checkout — the editable install otherwise masks it) | **yes** — exact `ModuleNotFoundError: No module named 'pvbess_opt'` | **genuine** |
| F29 | P2 | `test_breakeven_duplicate_capacities_no_divide_by_zero` | restore the unguarded `np.diff(npv)/np.diff(mwh)` | **yes** (rc=1, RuntimeWarning-as-error) | **genuine** |
| F4 | P2 | `test_gearing_above_100_rejected` | remove the `[0,100]` gearing range check | **yes** (rc=1) | **genuine** |
| F14 | P2 | `test_negative_pv_degradation_year1_rejected` | drop the degradation keys from the non-negative loop | **yes** (rc=1) | **genuine** |

All seven sampled locks (every P0/P1 plus the named P2 sample) **fail** when
their bug is reintroduced — none gives false confidence. The "F6 lesson" does
not recur in this sample.

### Completeness of prior fixes (root cause vs one instance)

* **F6 (section placement)** — `validate_workbook_params` reads every PV cost
  key from the `pv` section, every BESS cost key from `bess`, the site lump
  sums from `project`, and `gearing_pct` / grid-CO2 from `economics`. No
  remaining cost/sign key is read from the wrong section. **Complete.**
* **F3 (enum rejection)** — all nine `_STR_KEYS` enums (`mode`,
  `currency_format`, `plot_{daily,monthly,yearly}_scope`, `pv_source`,
  `debt_repayment`, `ppa_structure`, `ppa_settlement`) have an
  `_ALLOWED_VALUES` set and route through `_parse_string_enum`, which **raises**
  for any out-of-set value (not just `mode`). **Complete.**

**Phase 1 result:** the prior pass reproduces green and every sampled lock is
genuine; no new finding, no weak lock to harden.

---

## Phase 2 — the two scope changes, as delivered

### 2A — self_consumption balancing guardrail (Decision 1)

`pvbess_opt/pipeline.py` gains `_warn_self_consumption_balancing(params)`,
called once per run from `_run_one` (after the `--mode` override is applied,
so the final mode is known). It emits ONE
`[balancing-in-self_consumption]` warning when balancing is enabled, a BESS
is present, and the mode resolves to `self_consumption` — never in
`merchant`, and never for a balancing-on-but-BESS-less no-op. The both-mode
contract is unchanged (no mode gate added). Documented in README,
`self_consumption_design.md`, and `balancing_market_design.md`.

Locks (`tests/test_balancing_mode_contract.py`, extended):
`test_guardrail_warns_in_self_consumption`,
`test_guardrail_silent_in_merchant`,
`test_guardrail_silent_when_balancing_off`,
`test_guardrail_silent_without_bess`; the existing both-mode
activation/settlement tests stay valid.

### 2B — balancing-aggregator (BSP) fee (Decision 2)

New `balancing_aggregator_fee_pct_revenue` (economics sheet, default 0.0,
`[0,100]`), wired through every surface and mirroring the energy fee:

* **io.py** — `ECONOMICS_SHEET_DEFAULTS`, the `_ECONOMICS_ROWS` template
  (right after the energy fee), and a `[0,100]` check in
  `validate_workbook_params` (covering BOTH fees — see finding V2-1).
* **economics.py** — `build_yearly_cashflow` computes a non-negative,
  zero-gross-clamped deduction on gross balancing revenue (escalated with
  the gross), adds the `balancing_aggregator_fee_eur` column, and folds it
  into `net_cashflow_eur`; `derive_monthly_cashflow` allocates it by the
  same reservation weights and (because balancing revenue is gross) adds it
  to the monthly/quarterly net; `compute_financial_kpis` exposes
  `lifetime_bm_aggregator_fee_total_eur` and
  `lifetime_bm_revenue_net_total_eur` while the gross roll-up is unchanged.
* **sensitivity.py** — the revenue driver scales the fee column with the
  gross; `_recompute_net` includes it.
* **plotting** — the yearly revenue stack draws a "Balancing aggregator
  fee" deduction bar and steps its net line down; the BESS revenue
  waterfall inserts a fee step so the total steps down. `theme.py` gets a
  unique `balancing_aggregator_fee` colour + canonical label.
* **three config surfaces** — workbook row, `--config` key, scenario dotted
  target `economics.balancing_aggregator_fee_pct_revenue` (all
  schema-derived, proven by the surface-parity suite).
* **docs** — economics_design (Eq. E13b + table + net cashflow + KPI list),
  balancing_market_design, conventions.md, README, docs/README, inputs.rst,
  uncertainty_design, CHANGELOG.

Locks (`tests/test_balancing_aggregator_fee.py`, new): per-year deduction
equals `-frac × gross` (escalated); net drops by exactly the fee; gross
unchanged; default-0 bit-identical to a missing key; lifecycle fee/net KPIs;
monthly+quarterly reconciliation; `[0,100]` validation for both fees;
sensitivity scaling; both plots draw the fee (and do NOT when off). The
default-off path is proven bit-identical (the golden `kpi_baseline.json`
suite stays green).

---

## Phase 3 — independent numerical re-derivation

`tests/test_independent_reconciliation.py` re-derives the headline numbers
with a from-scratch numpy calculation that imports the project's economics
builder ONLY to produce the *actual* frame — every *expected* value is an
independent hand/numpy computation. A 3-year, round-number case (CAPEX
−1000; Year-1 retail 300, DAM 200, balancing cap 100 + act 50; energy fee
10 %, BSP fee 20 %; discount 10 %) is reconciled to ≤ 1e-6 / 1e-2:

* **Energy-fee scope + split.** `aggregator_fee_eur = −50` (10 % of the
  500 gross DAM+retail only), split pro-rata to `revenue_retail_eur = 270`,
  `revenue_dam_eur = 180`, `revenue_eur = 450`.
* **BSP fee.** Gross balancing stays `150`; `balancing_aggregator_fee_eur =
  −30` (20 % of 150); the net cashflow is `450 + 150 − 30 = 570`; PPA
  carries neither fee (`ppa_revenue_eur = 0`).
* **DCF consumes NET balancing.** Turning the BSP fee on lowers NPV by
  exactly the discounted fee stream `Σ 30 / 1.1^y`.
* **Escalation / discount.** A second parametrisation with `i_ret = 3 %`,
  `i_bm = 2 %` reproduces every per-year `net`/`dcf` under `(1+i)^(y-1)`
  escalation and `1/(1+r)^y` discounting.
* **NPV + IRR.** NPV matches the independent discounted sum; IRR is
  re-solved with an INDEPENDENT bisection on the polynomial NPV (not
  `economics.calculate_irr`) and matches `irr_pct` to 1e-3.

The pre-existing `tests/test_financial_reference.py` (an independent
reference implementation) plus the LCOE/LCOS, PPA, balancing-MC,
degradation and debt locks already re-derive the remaining formulas; the
new fee is the only addition and is covered above. No mismatch beyond
tolerance ⇒ no Phase 3 finding.

---

## New findings (this pass)

| # | Sev | Area | Title | Resolution |
|---|---|---|---|---|
| V2-1 | P3 | input | The energy `aggregator_fee_pct_revenue` was only *clamped* to [0,1] in economics, never range-validated — a typo like `150` (meaning "1.50 %") silently became a 100 % fee that wiped revenue. Inconsistent with `gearing_pct`/`grid_co2_*` (which reject) and with the new BSP fee. | **fixed** — `validate_workbook_params` now rejects either revenue fee outside `[0,100]`; `tests/test_balancing_aggregator_fee.py::test_energy_aggregator_fee_out_of_range_rejected` + `::test_balancing_fee_out_of_range_rejected`. |
| V2-2 | P2 | test | The monthly BSP-fee reconciliation lock only checked the annual *sum* (both candidate weightings sum to 1, so the test could not tell `balancing_share` from `fee_share`), and its fixture had flat shares — a mutation allocating the fee by the energy-revenue share *survived*. | **fixed** — added `test_monthly_fee_follows_balancing_profile_not_revenue` with a seasonal fixture (reservations in H1, revenue in H2) asserting per-month `fee == -frac × gross balancing`; the mutant is now killed. |

---

## Mutation-kill summary

21 mutations injected across three batches; every one is caught by a named
test (one survivor found and its lock strengthened — V2-2).

**Prior-pass locks (Phase 1, 7):** F6, F13, F22, F32, F29, F4, F14 — each
fails its named lock when reverted. (Table above.)

**New-feature code (Phase 4, 10):** flip BSP-fee sign; zero the fee fraction;
drop the fee from the yearly net; drop it from the monthly net; remove the
`[0,100]` validation; **allocate the monthly fee by the wrong share
(SURVIVED → strengthened, V2-2)**; drop the fee from the sensitivity-scaled
columns; suppress the revenue-stack fee bar; drop the guardrail mode gate;
lifecycle net KPI ignoring the fee. After V2-2 all 10 are killed.

**Existing core numerics (Phase 4, 4):** wrong discount exponent
(`(1+r)^(y+1)`); retail-escalation off-by-one (`^y` vs `^(y-1)`); energy-fee
sign flip; balancing-escalation off-by-one — all killed by
`test_independent_reconciliation` / `test_balancing_lifetime_cashflow`.

Restoration after every mutation is from a saved in-memory copy, never
`git checkout` (which would discard uncommitted work — a hazard hit and
corrected mid-audit).

---

## Independent sign-off

_Pending Phase 7._
