# Audit report — v0.9.0 (post balancing-market merge)

Consolidated findings from the three-phase audit on
`chore/audit-balancing-and-plots`.  Phase-specific reports under
`docs/audit_report_phase1.md` and the test index under
`docs/audit_test_index.md` carry the per-check detail; this document
summarises and cross-references them.

## Phase 1 — balancing-market mathematical correctness

Commit: `fix(balancing): correct Monte Carlo SOC-violation coupling and
clarify DAM share semantics`.

### Issues found and resolved

* **MC SOC-violation decoupling (1.7).** `realise_balancing_scenario`
  in `pvbess_opt/rolling_horizon.py` spawned a fresh child generator
  for the SOC trajectory check and resampled the Bernoulli outcomes
  independently of the revenue pass.  A single scenario could therefore
  report revenue from activations that never appeared in its SOC trace
  and "SOC OK" on a trace that never accrued the matching revenue.
  Visible symptom: every balancing-on JSON in
  `scripts/audit_runs/results/` reported
  `bm_soc_constrained_scenarios_pct = 0.0`.  Fixed by capturing the
  per-product activation Boolean arrays in the revenue pass and
  reusing them in the SOC pass.  Regression coverage:
  `tests/test_balancing_mc_coupling.py`.
* **`dam_capacity_share_pct` semantic mismatch (1.6).** The workbook
  row docstring claimed the field "reserved" capacity for DAM dispatch,
  but the MILP only validates that the sum of shares ≤ 100 %; DAM
  dispatch is bounded indirectly by `BM_POWER_UP / BM_POWER_DN`
  consuming the residual of `bess_power_kw` after the balancing
  reservations.  Resolved by rewriting the workbook row docstring
  (`io.py:502-507`) and the validator comment (`io.py:1166-1175`).
* **`fcr_activation_probability_pct` dead config (1.4).** The field
  is registered in `BalancingConfig`, the workbook row template, and
  the probability validator, but never consumed — FCR is modelled as
  capacity-only and symmetric in expectation.  Resolved by marking the
  row docstring informational (`io.py:525-530`).

### Confirmed correct

* Product taxonomy (1.1): every consumer of `PRODUCTS_*` iterates the
  correct tuple; FCR's capacity-only and symmetric-in-expectation
  treatment is consistent across MILP, KPI, and Monte Carlo.
* `BM_POWER_UP / BM_POWER_DN` unit consistency (1.2): both sides of
  the inequalities resolve to kWh per step.
* `BM_SOC_UP / BM_SOC_DN` headroom formulas (1.3): η placement and
  `(1+h_buf)` multiplicative buffer match the design note.
* Expected SOC drift (1.4): `optimization.py:soc_dynamics` and
  `kpis.py:_balancing_soc_drift` are term-for-term identical;
  invariant 4 (`rte_bound`) correctly absorbs the drift.
* Expected revenue dimensions (1.5): `α · dt_h · Σ(price · r) / 1000`
  resolves to EUR; capacity and activation paths use the right
  probability product.
* Lifetime / cashflow scaling (1.9): year-y balancing revenue equals
  `year-1 × bess_factor(y) × (1 + bm_infl)^(y-1)`; regression test
  `tests/test_balancing_lifetime_cashflow.py`.
* LCOE / LCOS exclusion (1.10): both metrics correctly exclude
  balancing revenue (cost-per-MWh, not revenue-per-MWh); comment added
  to `economics.py` to record intent.

### Documented simplifications

* Settlement-period equality (1.8): all five products share `dt_minutes`
  rather than each product's native cadence (FCR sub-second, aFRR 4-15
  min, mFRR 15 min).  Note added to
  `docs/balancing_market_design.md`.

## Phase 2 — plot-format consistency

Commit: `style(plots): align balancing and BESS-revenue plots with
existing energy-plot conventions`.

### Issues found and resolved

* **`plot_bess_revenue_by_month` month axis (2.2).** Switched from
  English short names ("Jan" … "Dec") to `MM-YYYY` derived from the
  dispatch timestamp, matching the `mdates.DateFormatter("%m-%Y")`
  cadence used by every other monthly axis.
* **`plot_balancing_mc_distribution` EUR formatter (2.3).** X-axis now
  routes through `euro_axis_formatter`; was previously raw `1e6`-style
  ticks.  Added `econ` keyword for currency-format choice.
* **Missing scenario / project-mode prefix (2.4).** Added
  `title_prefix(get_scenario_label())` injection to every title in
  `bess_revenue.py` and `balancing.py`.
* **Waterfall annotation currency formatter (2.5).** Bar annotations
  switched from raw `f"€{values[i]:,.0f}"` to `format_eur(value, mode)`
  so they read "€12.3M" / "€45k" / "€850" in the magnitude-aware style
  used elsewhere.
* **`apply_fine_ticks` (2.6).** Added on the y-axis of the three
  bess_revenue plots and on the x-axis of `plot_balancing_mc_distribution`.
* **Legend ordering (2.8).** `plot_bess_revenue_by_month` legend now
  follows the canonical `DAM → FCR → aFRR-up → aFRR-dn → mFRR-up →
  mFRR-dn` order independent of stacking order.

### Verification

`scripts/audit_runs/run_phase2_visual_check.py` renders all five plots
into `scripts/audit_runs/results/phase2_visual_check.pdf`.  Visual
inspection confirms `MM-YYYY` axis labels, EUR formatter on every
currency axis, scenario+mode title prefix, and no clipped annotations.

## Phase 3 — test-suite hygiene

Commit: `chore(tests): triage and refresh the balancing-aware test suite`.

See `docs/audit_test_index.md` for the per-file triage.  Summary:

* All v0.8 / pre-balancing tests still pass with the v0.9.0 schema.
* The balancing-specific surface is covered by
  `test_balancing_invariants.py`, `test_balancing_io.py`,
  `test_balancing_mc.py`, `test_balancing_mc_coupling.py`,
  `test_balancing_module.py`, `test_balancing_optimization.py`, and
  `test_balancing_lifetime_cashflow.py`.
* No DELETE actions were necessary — the Part-1 audit (vulture + mypy
  pass) already removed the dead surface.

## Phase 4 — final acceptance

* `bash scripts/audit_runs/run_all.sh` — see updated JSONs under
  `scripts/audit_runs/results/`; every invariant continues to pass.
  `bm_soc_constrained_scenarios_pct` remains 0.0 across the four
  balancing-on cases — this is the post-fix correct answer for the
  reference workbook because `bm_soc_headroom_pct = 10 %` is wider
  than any realised activation drift in the MILP-planned SOC path.
  The Phase-1 regression tests
  (`tests/test_balancing_mc_coupling.py`) exercise the constrained
  branch explicitly to prove the SOC pass and the revenue pass now
  consume the same Bernoulli draws.
* `mypy pvbess_opt` — error count below the existing baseline.
* `ruff check pvbess_opt tests scripts` — clean.
* `pytest` (fast lane) — passes.
* `pytest -m slow` — passes.

### Commits on this branch

1. `fix(balancing): correct Monte Carlo SOC-violation coupling and
   clarify DAM share semantics`
2. `style(plots): align balancing and BESS-revenue plots with existing
   energy-plot conventions`
3. `chore(tests): triage and refresh the balancing-aware test suite`
4. `docs: consolidated v0.9.0 audit report`

Branch: `chore/audit-balancing-and-plots`.
