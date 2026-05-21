# pv-bess-optimizer — Full Codebase Audit (v0.8.9)

## Metadata

- **Audited commit:** `ba166377a0aa769f45c1e5bbef63d278eb5164f4` (the `chore/v0.8.9-audit-fixes` merge, PR #25 — this *is* the released v0.8.9 tree; `__version__ = "0.8.9"`).
- **Audit branch:** `audit/v0.8.9-full-codebase-review`, based off the current `HEAD` above.
  - Note: the `main` branch is **stale** — it points at `d69576a` (PR #18), a full version behind the v0.8.9 code. The audit was deliberately based off the v0.8.9 HEAD (confirmed with the project owner) so it reflects the released codebase, not pre-v0.8.9 `main`.
- **Date:** 2026-05-21. **Auditor:** Lampros Konstantellos.
- **Machine:** Intel(R) Xeon(R) @ 2.80 GHz, 4 vCPU, ~15.7 GiB RAM, Linux 6.18.5.
- **Toolchain:** Python 3.11.15; HiGHS (`highspy`) 1.14.0; Pyomo 6.10.0; pandas 3.0.3; numpy 2.4.6; matplotlib 3.10.9; openpyxl 3.1.5; python-dateutil 2.9.0.post0; Sphinx 9.0.4; ruff 0.15.13.
- **Baseline (green):**
  - `python -m pytest tests/ -q` → **642 passed in 547.32 s** (9 m 07 s), exit 0.
  - Fast lane `-m "not slow"` not separately completed; it is ≈ full minus the single slow real-scale RH test (which measured **126.5 s** here), i.e. ≈ 420 s. Almost every other test runs a real solve, so the fast lane is not materially faster than the full suite.
  - `ruff check .` → **2 errors** (F401 unused import) — see F8/F13. `ruff format --check .` → not enforced (67/75 files would reformat). The project's *declared* linters are `pyflakes` + `pycodestyle` (`requirements/dev.txt`), not ruff.
  - `make -C docs html` (Sphinx 9.0.4) → **build succeeded, 7 warnings**. 6 are sandbox-only (intersphinx SSL/403 to pyomo/numpy/pandas/matplotlib/python, and a missing `_static` dir); **1 is real and pre-existing**: `docs/source/users.guide/inputs.rst:31: ERROR: Malformed table` (F11).

## Executive Summary

Findings (severity tag + one line):

1. **F1 — P0** — In BESS-only runs (`pv_nameplate_kwp = 0`) with a populated `pv_kwh` timeseries column, the output frame copies the raw `pv_kwh` while the model pins all PV flows to 0 → per-step energy-balance violation (≈800–3200 kWh), `invariant_1` & `invariant_9` breach, phantom `pv_generation_mwh`, and `--strict` (or `verify_energy_balance(raise=True)`) crashes.
2. **F2 — P1** — `build_yearly_cashflow` degrades **all** revenue (incl. BESS-derived) on the **PV** curve (`pv_factor`), while the lifetime path degrades BESS revenue on `bess_factor`. The same output workbook then carries two contradictory BESS revenue series; headline NPV/IRR for BESS-only is misstated (~2% NPV on a pure BESS-only case; grows for BESS-dominant/low-discount projects).
3. **F3 — P2** — Dependency floors are `>=` only; the installed pandas **3.0.3** (floor `>=2.0`) and numpy **2.4.6** (floor `>=1.24`) are a full major ahead of the floor — a fresh/CI install resolves to majors the floor never validated, and a future major auto-upgrades unguarded.
4. **F4 — P2** — The default Monte-Carlo ensemble (`uncertainty_n_seeds = 30`) takes **~63 min**; compare-sources (4×30) **~253 min (~4.2 h)**. Progress is logged only per seed (~2 min granularity) with no upfront runtime warning (unlike the plot fan-out, which does warn).
5. **F5 — P2** — `model_to_dataframe` rounds to 4 dp *before* invariant checks; the sum-based `invariant_4` accumulates rounding to **0.0022 kWh > 1e-3** on high-throughput runs → false `--strict` failure risk.
6. **F6 — P2** — Timeseries numeric NaNs are silently `ffill`/`bfill`'d with no log/warning; a corrupt data block is masked.
7. **F7 — P2** — Real-scale test coverage gap: only **vnb hybrid** is exercised at full 35,040-row scale; merchant and PV-only/BESS-only run only at 1-day scale, and the BESS-only tests assert flows-are-zero but never energy balance / invariants — which is exactly why F1 went undetected.
8. **F8 — P2** — CI installs `pyflakes`/`pycodestyle` but never runs them; there is no lint gate, so the 2 F401 unused imports slip through.
9. **F9 — P2** — `_check_solver_status` treats `maxTimeLimit`/`maxIterations` as acceptable without confirming a feasible incumbent was loaded; a time-limited solve with no incumbent returns instead of raising actionably.
10. **F10 — P2** — Documentation/sheet-name drift: docs and docstrings reference an `economic` sheet (real name: `economics`) and attribute `show_titles` / plot-scope flags to the wrong sheet.
11. **F11 — P2** — `inputs.rst:31` malformed-table ERROR (renders broken); output-layout listings omit `06_uncertainty_plots/` in three places; broken `:data:` xref `DEPRECATED_ECONOMICS_KEYS`; stale test reference `test_lifetime_dispatch.py`.
12. **F12 — P3** — Assorted nits (2 unused imports; `_currency.py` "EUR" vs `€`; README "v0.8.8" headline; CONTRIBUTING "10 modules"; `monte_carlo_rolling(n_seeds=0)` column-less frame; merchant lacks an (economically non-binding) no-sim-grid-IO constraint; economics docstring sign convention omits DEVEX; PV noise clips to window-max not nameplate; utilization diagnostics not derated; non-lowercased `mode` passed to the invariant checker).

**Counts: P0 = 1, P1 = 1, P2 = 9, P3 = 1 (a consolidated bundle of ~12 nits).**

**Release-readiness verdict: NOT release-ready until F1 is fixed.** F1 is a true correctness defect on a first-class, documented, "tested" asset mode (BESS-only): it violates the energy balance and two dispatch invariants and crashes under `--strict`. The fix is a one-line change in `model_to_dataframe` (Appendix 1). Everything else is P1/P2/P3 and can follow in a remediation pass. The numerical core is otherwise sound: across the four clean mode×asset combinations the per-step energy-balance residual is ≤ 1.4e-4 kWh (machine-precision for PV-only), all nine invariants hold, dispatch is bit-for-bit reproducible across identical solves, and the default vnb-hybrid headline KPIs reproduce the pinned baseline exactly (derated profit 2,840,145 EUR).

---

## A. Numerical correctness

**What/how checked.** Full-year (35,040-step) perfect-foresight solves on the default workbook for all six mode×asset combinations; `kpis.verify_energy_balance` and `optimization.verify_dispatch_invariants` (all 9) on each; two identical vnb-hybrid solves compared bit-for-bit; cashflow sign convention traced end-to-end; degenerate inputs exercised (zero/huge export cap, unavailability 0 vs 5 %, leap-year Feb-29, single-window RH, `n_seeds` 0/1, zero-sigma noise). Solves at `mip_gap=0.01` for the battery (invariants/balance are gap-independent — they assert feasibility, not optimality) and `mip_gap=0.001` for timing.

**Energy balance — holds.** Worst per-step residual across the four clean combos = **1.44e-4 kWh** (vnb hybrid; rounding-dominated, see F5) — three orders below `ENERGY_TOLERANCE = 1e-3` (`kpis.py:41`). PV-only combos are machine-precise (4.5e-13). The SOC continuity equation in the `kpis.py` docstring (lines 14-18) matches the code (`kpis.py:89-92`) and the model (`optimization.py:468-473`). The four balances (PV split, load balance, export definition, SOC dynamics) all verified.

**Dispatch invariants — hold for every clean combo; breach only under F1.** Per-combo (full year, gap 0.01):

| mode / asset | eb max (kWh) | inv1 | inv3 | inv4 | inv9 | discharge MWh | profit EUR |
|---|---|---|---|---|---|---|---|
| vnb hybrid | 1.44e-4 | 1e-4 | 1.44e-4 | 0 | 0 | 9603.76 | 2,868,834 |
| vnb pv_only | 4.5e-13 | 4.5e-13 | 0 | 0 | 0 | 0 | 2,019,409 |
| **vnb bess_only** | **3202.93** | **3202.93** | 1.37e-4 | **0.00217** | **1181.71** | 21,818.41 | 1,806,277 |
| merchant hybrid | 1.41e-4 | 1e-4 | 1.41e-4 | 0 | 0 (n/a) | 14,220.80 | 2,699,786 |
| merchant pv_only | 4.5e-13 | 2.3e-13 | 0 | 0 | 0 (n/a) | 0 | 1,343,617 |
| **merchant bess_only** | **3202.93** | **3202.93** | 8.6e-5 | 0 | 0 (n/a) | 21,361.01 | 1,657,219 |

The bess_only breaches are **F1** (phantom `pv_kwh`); the BESS *dispatch* is valid — only PV reporting is broken. `invariant_4 = 0.00217 > 1e-3` on vnb bess_only is **F5** (rounding).

**Reproducibility — holds.** Two identical vnb-hybrid full-year solves: dispatch **bit-identical** (`max_abs_diff = 0`), KPIs identical — despite `threads=0` (auto/multi-thread). The reproducibility concern from `threads=0` is therefore not realized in practice (HiGHS is deterministic here). `add_forecast_noise` zero-sigma path is byte-identical (both `enable_*=False` and `sigma=0`).

**RTE / SOC — holds.** Empirical RTE = 0.9409 = `efficiency_charge × efficiency_discharge` (0.97×0.97) on closed-cycle runs, matching `bess_roundtrip_eff_theoretical`. `invariant_4` (discharge ≤ RTE bound) is algebraically implied by `invariant_3` and holds (modulo F5 rounding).

**Sign conventions — hold.** Traced: `capex_eur`, `devex_eur`, `opex_eur` stored negative; `revenue_eur` positive; `net_cashflow_eur = revenue + opex + capex + devex` (`economics.py:363`). NPV = Σ discounted CF; IRR via Newton+bisection with all-positive/all-negative guard (`economics.py:88`); payback via linear interpolation (`economics.py:888-909`). The module docstring's sign-convention note (`economics.py:40`) omits the `devex` term that the code adds — see F12.

**Findings here:** F1 (P0), F5 (P2). All other numerical checks verified to hold.

## B. Modes & asset coverage

All six `mode ∈ {vnb, merchant} × asset ∈ {hybrid, pv_only, bess_only}` combos **load, solve, and produce KPIs** (table in §A). Mode-specific constraints fire correctly:

- **vnb** enforces `LOAD_BAL` (`optimization.py:449`), hard `LOAD_PV_PRIORITY` → `pv_to_load = min(pv, load)` (`:462`, inv9=0 on clean combos), surplus-only-export slack (`:565-577`), and no-simultaneous-grid-IO via `y_grid_io` (`:579-593`, inv5=0).
- **merchant** pins `pv_to_load`/`bess_dis_load`/`grid_to_load` to 0 (`:365-373`), omits load balance/priority and no-sim — confirmed import=0 on merchant hybrid (no load, grid-charge off) and inv2/inv6/inv9 = 0 (n/a).
- **pv_only** pins all BESS vars to 0 (`:404-432`); **bess_only** pins all PV vars to 0 (`:377-389`). Both verified (flows = 0).
- Export cap fires: merchant pv_only curtails **219.48 MWh** when PV exceeds the 73 % injection cap (inv7=0 — curtail only when the cap binds).

**Findings here:** the BESS-only *output/KPI* path is broken (F1) even though the *dispatch* is correct in both modes.

## C. Rolling horizon & Monte Carlo

- **Real-hours semantics — holds (past bug fixed).** `_hours_to_steps` (`rolling_horizon.py:69`) computes `hours*60//dt_minutes`; a 48 h window = 192 steps at 15-min cadence (verified). The CHANGELOG documents the prior 12 h-vs-48 h bug as fixed in 0.8.9.
- **Commit-horizon correctness — holds.** Noise is applied only beyond `commit_steps` (`:128-156`); the committed slice `[0:commit_steps]` is the actual (un-noised) data, so committed decisions use actuals and the look-ahead tail is noisy — correct imperfect-foresight design. SOC carryover is exact, incl. the end-of-horizon post-final-step derivation (`:284-295`).
- **DAM sign-aware noise — holds** (`:136-140`): noise on `|price|`, sign restored. **PV clip:** to the per-*window* max, not nameplate (`:146-149`) — the function has no access to nameplate, so this is an acceptable proxy (F12).
- **Reproducibility — holds.** `monte_carlo_rolling` seeds are `base_seed + i` (`:364`); each seed uses `default_rng(seed)`. Deterministic given the (verified) solver determinism.
- **Progress/ETA — holds but coarse.** Per-seed logging flushes all handlers (`:400-407`); ETA math `elapsed/done*(N-done)` is correct. But there is **no per-window** progress (a single seed = ~365 silent windows ≈ 2 min) and no upfront duration warning — see F4.
- **Single-window RH, `n_seeds` 0/1 — work** (F12 notes the column-less empty frame at `n_seeds=0`, which `main.py` never triggers).

**Findings here:** F4 (P2), plus F12 nits.

## D. Economics & lifetime

- **Retail/DAM split + dual inflation — wired correctly** for the *PV* portion (`economics.py:240-250, 332-341`): retail revenue indexed by `retail_inflation_pct`, DAM by `dam_inflation_pct`, grid-charge cost tracks DAM. Reconciliation guard at `:253-262`.
- **F2 (P1): BESS revenue uses the wrong degradation curve.** `build_yearly_cashflow` scales **all** revenue by `pv_factor` (`:332-341`), but `lifetime.py` correctly scales BESS revenue columns by `bess_factor` (`lifetime.py:75-83, 248-251`). Demonstrated on a synthetic BESS-only project: `cashflow_yearly` year-20 revenue ratio = **0.883** (= `pv_factor`), while `lifetime_dispatch_yearly` year-20 `revenue_eur_total` ratio = **0.758** (= `bess_factor` incl. cycle term). The two sheets in `03_results.xlsx` disagree by ~16 % in the final year. Headline NPV (which uses `build_yearly_cashflow`) is overstated **2.1 %** for that pure BESS-only case. The comment at `economics.py:328-331` / `lifetime.py:72-74` admits this is a known "future enhancement."
- **BESS calendar+cycle fade — sums correctly.** `_bess_factor = max(0, (1-d_annual)^years_since − d_per_cycle·cum_cycles)` (`lifetime.py:95-128`); the year-final decomposition (`economics.py:772-815`) splits calendar + cycle and they sum to total when the floor is inactive.
- **Lifetime scaling reconciliation — holds** (per `tests/test_lifetime.py`): `Σ pv_kwh[y] / Σ pv_kwh[Y1] ≈ pv_factor[y]`.
- **Single-source defaults — holds.** `project_start_year` / `project_lifecycle_years` dereference `PROJECT_SHEET_DEFAULTS` everywhere (`economics.py:195-207`, `main.py`, `lifetime.py:157-168`). No stray literal defaults found in the financial path.
- **Replacement / DEVEX / aggregator fee — correct.** Replacement at `bess_replacement_year` (workbook ships 10) resets the cycle counter and applies `capex_bess·repl_cost_pct` (`economics.py:317, 345-346`); the calendar factor resets at replacement (`lifetime.py:122-126`, verified — year-10 `bess_capacity_factor` jumps back to 1.0). Aggregator fee deducted from gross and split across streams (`:343-362`). Unavailability derate applied **once** per path (verified trace: `kpis` dict via `apply_unavailability_derate`, `lifetime_yearly` via `main._build_financials:578-587`).

**Findings here:** F2 (P1).

## E. I/O & workbook parsing

- **Robust where it counts.** Missing required sheet → `ValueError` (`io.py:1073-1078`); irregular timestep → `ValueError` with a remediation hint (`:923-930`); both-assets-zero → `ValueError` (`:1240-1247`); deprecated `revenue_inflation_pct` → auto-mapped to `retail_inflation_pct` with WARNING (`:198-205, 736-751`); removed keys (`capex_licenses_eur_per_kw`, `battery_hours`, `p_charge_max_kw`, `p_dis_max_kw`) → WARNING + ignored (`:209-227, 752-757`); wrong-sheet keys flagged (`:759-764`).
- **Unlimited export — safe.** Empty/`inf`/`unlimited`/`disabled`/`none` → `float('inf')` (`:90-92, 685-721`), then `_typed_to_flat` substitutes a **finite** big-M = `max(2·(pv+bess), 1e6)` before the MILP (`:1182-1196`). Verified: a `p_grid_export_max_kw = 1e7` solve produces no NaN and zero curtailment. (Note: `read_workbook`/`read_economic_params` keep `inf` in the typed dict; `econ['p_grid_export_max_kw']` is never used in a numeric computation downstream, so no `inf` leak — verified.)
- **PV resolution — correct.** `pv_kwh_override` all-null → rescale path; all-filled → verbatim; partial-NaN → `ValueError` (`:997-1065`). The shipped workbook's override is fully empty → rescale ×15 to hit `15000 kWp × 1500` = 22.5 GWh (verified).
- **Output layout** (`make_run_layout:1283-1302`): `00_summary, 01_inputs, 02_dispatch, 04_financial_plots, 05_energy_plots, 06_uncertainty_plots`; `03_results.xlsx` is a root-level file. All consumed correctly by `main.py`.
- **F6 (P2):** `_normalise_timeseries` `ffill().bfill()` on `load_kwh`/`pv_kwh`/`dam_price`/`retail_price` (`:907-909`) silently fills NaN with **no log** — a corrupt block is masked rather than surfaced.

**Findings here:** F6 (P2). Parsing is otherwise solid.

## F. Dependencies & supply chain

- **No used-but-undeclared dependency.** Runtime third-party set = numpy, pandas, matplotlib, pyomo, `dateutil` (`lifetime.py:46`), plus openpyxl (engine string) and highspy (Pyomo backend). All declared in `requirements/base.txt` + `solvers.txt`. **`python-dateutil` is now correctly declared** (`base.txt:13`) — the prior-audit "undeclared dateutil" class of issue is resolved. `scipy` is not imported anywhere (correctly absent).
- **No declared-but-unused** runtime dependency.
- **F3 (P2): open upper bounds.** All floors are `>=` (intentional, `base.txt:5-6`). Installed pandas **3.0.3** (floor `>=2.0`) and numpy **2.4.6** (floor `>=1.24`) are a full major beyond the floor. The suite is green on these majors today, so this is fragility, not breakage: a `pip install` resolves to the latest major (works now), but the floor also permits the old major (pandas 2 / numpy 1) which is *not* tested, and a future pandas 4 / numpy 3 would auto-upgrade unguarded. matplotlib/openpyxl/pyomo/highspy/dateutil are same-major near-floor (negligible).

**Findings here:** F3 (P2).

## G. Test suite quality

- **642 tests, all green**, deterministic (verified). The `slow` marker is registered (`conftest.py:25-29`) and gates one test; the real-scale RH budget (445 s = 5× the prior ~89 s; here 126.5 s) is sane.
- **F7 (P2): real-scale coverage is vnb-hybrid-only.** `test_input_workbook_smoke.py:119-190` runs the full-year PF solve on the 35,040-row workbook and pins exact KPIs (pv_gen 22275, discharge 9507.72, profit 2,840,145.28, NPV 8,975,262.78, IRR 15.9272) — but only for **vnb hybrid**. `test_rolling_horizon_realscale.py` covers **vnb-hybrid RH** (slow). Merchant and PV-only/BESS-only are only smoke-tested at 1-day (`test_main_*_short_horizon`, 96 steps). Crucially, the BESS-only tests (`test_asset_modes.py:166-178, 200-215`) assert only that PV *flows* are zero — they never call `verify_energy_balance` or `verify_dispatch_invariants` on a BESS-only frame, which is precisely why **F1** was never caught.
- **F8 (P2): no lint gate.** `requirements/dev.txt` declares `pyflakes`/`pycodestyle`, but `.github/workflows/ci.yml` only runs `pytest` — neither linter is invoked. The 2 F401 unused imports (F13) confirm nothing enforces them.
- Assertions are generally tight (the headline KPI pin is to ±1 EUR). The plotting "grep audits" (`test_grep_audits.py`) are a clever source-level hygiene gate. No flaky/xfail/loose-envelope tests of concern were found.

**Findings here:** F7, F8 (P2).

## H. Documentation drift

Full table (doc file:line | claim | code reality | sev):

| doc file:line | claim | reality (code) | sev |
|---|---|---|---|
| `docs/source/users.guide/inputs.rst:31` | timeseries table | **Sphinx ERROR: malformed table** — first column rule (`====...`, 26 chars) narrower than `` ``retail_price_eur_per_mwh`` `` (28). Renders broken. | P2 |
| `main.py:15` (docstring) | "`show_titles` in the `economic` sheet" | `show_titles` ∈ `PROJECT_SHEET_DEFAULTS` (`project`); no `economic` sheet (it's `economics`). | P2 |
| `main.py:17` (docstring) | "Plot-scope flags in `economic`" | `plot_*_scope` ∈ `SIMULATION_SHEET_DEFAULTS` (`simulation`). | P2 |
| `main.py:3-11`, `output_layout.rst:7-24`, `running.rst:54-63` | output layout `00..05` | omit `06_uncertainty_plots/` (created at `io.py:1289`, written by `_generate_uncertainty_plots`). | P2 |
| `CONTRIBUTING.md:71-72` | "`show_titles` in the `economic` sheet" | `project` sheet. | P2 |
| `docs/source/users.guide/economics.rst:4` | "The `economic` sheet" | sheet is `economics`. | P2 |
| `docs/source/users.guide/inputs.rst:113` | `:data:\`pvbess_opt.io.DEPRECATED_ECONOMICS_KEYS\`` | symbol does not exist; the map is `_LEGACY_RENAMED` (`io.py:198`). Broken xref. | P2 |
| `pvbess_opt/lifetime.py:32` (docstring) | "`test_lifetime_dispatch.py` asserts:" | file does not exist; real file `tests/test_lifetime.py`. | P3 |
| `docs/technical.documentation/uncertainty_modelling.md:35` | "Eleven keys on the **economic** sheet" | 11 `uncertainty_*` keys are on the `simulation` sheet (counts/defaults correct). | P3 |
| `README.md:33` | "## What's new in **v0.8.8**" | current release is 0.8.9. | P3 |
| `CONTRIBUTING.md:10` | "Current modules (10 + plotting)" | lists 11 incl. `__init__`. | P3 |
| `docs/CHANGELOG.md:1-6` | "tracks only the most recent release" | also contains 0.8.8 / 0.8.7 sections. | P3 |
| `docs/source/users.guide/running.rst:33-36` | `--window-hours`/`--commit-hours` "(default 48/24)" | argparse default `None` (sentinel "use workbook"); 48/24 are the *workbook* defaults. | P3 |
| `pvbess_opt/plotting/_currency.py:27-32` (docstring) | examples "EUR 12.3M" | code emits the `€` glyph (`:19`). | P3 |

**Verified correct (no drift):** CLI flag table (`running.rst:13-47`) matches `parse_args` exactly (14 flags, all defaults); invariant count is **9/nine** everywhere (`mip_formulation.rst`, README, `optimization.py:40,767`, `rolling_horizon.py:419`); all KPI keys cited in `kpis.rst`/`economics.rst` exist; 7-sheet schema; economic defaults (CAPEX 525/200, deg 2.5/0.55/2.0, discount 7 %, cycle-fade 0.008). No stale version strings beyond the legitimate "pre-v0.8.8" context.

**Findings here:** F10, F11 (P2); the rest fold into F12 (P3).

## I. Performance

Timing (this machine, default workbook, single-threaded notwithstanding `threads=0`):

| Measurement | Result |
|---|---|
| Perfect-foresight full-year solve (gap 0.001) | **57.4 s** |
| One RH window (192 steps, `terminal_soc_free`) | 0.25 s |
| One full RH seed (~365 windows, 48 h/24 h) | **126.5 s** (profit 2,860,962 EUR) |
| Projected 30-seed MC (linear) | **~63 min** |
| Projected 4×30 compare-sources | **~253 min (~4.2 h)** |
| vnb **bess_only** PF full-year (gap 0.01) | **677.7 s** — hardest MILP (no-sim + slack + load-priority binaries with full grid arbitrage); merchant bess_only was only 76.6 s |

- The PF solve (57 s) is fine. The MC ensemble is the performance concern (**F4**): per-window solves are cheap (~0.35 s) but a seed is ~2 min and the default 30-seed run is ~1 h with only per-seed progress; compare-sources is ~4 h. No quadratic/accidental-recompute hot path was found — each window rebuilds the model from scratch (~0.35 s), so warm-starting is not worth the complexity at this per-window cost.
- vnb BESS-only's 677 s solve is an intrinsic-difficulty observation (informational), not a defect — it completes well within the 1800 s default time limit.

**Findings here:** F4 (P2).

## J. Robustness & security

- **No dangerous sinks.** No `eval`/`exec`/`os.system`/`subprocess`/`pickle`/`shell=True` anywhere in `pvbess_opt/` or `main.py`. Workbook paths go through `pathlib`; openpyxl reads data cells only. There is no network, no deserialization of untrusted data, no user-controlled format strings reaching a dangerous sink. As a numerical CLI tool, the attack surface is minimal — verified clean.
- **F9 (P2): solver-status handling.** `_check_solver_status` (`optimization.py:159-166`) returns on `maxTimeLimit`/`maxIterations` regardless of whether a feasible incumbent exists. If a solve hits the time limit with no incumbent, `model_to_dataframe`'s `pyo.value(...)` calls would then surface as an opaque error (or stale values) rather than an actionable "no feasible solution within time limit." Recommend checking `result.solver.termination_condition` together with solution availability.
- **Boundaries are mostly actionable.** Workbook read raises with clear messages; `main()` wraps the run in try/except and returns non-zero (`main.py:1004-1009`). The one silent boundary is F6 (NaN fill).

**Findings here:** F9 (P2).

---

## Findings detail

### F1 — P0 — BESS-only output frame keeps phantom `pv_kwh` (energy-balance + invariant breach, `--strict` crash)

- **Location:** `pvbess_opt/optimization.py:694` (output) vs `:286, 303-307` (model). Reachable end-to-end via `io.read_inputs` for any `pv_nameplate_kwp = 0` workbook whose `timeseries::pv_kwh` column is non-zero (the schema *requires* a `pv_kwh` column, and nothing zeroes it when PV is "absent").
- **Evidence (production path, `read_inputs` on a `pv_nameplate_kwp=0` workbook with a populated pv column):**
  ```
  res['pv_kwh'].sum()  = 24437.5 kWh   <- copied from input (model_to_dataframe:694)
  sum(pv_to_* flows)   = 0.000000 kWh  <- model pinned to 0 (build_model:303-307)
  verify_energy_balance: max_pv_split_residual_kwh = 800   (> 1e-3 -> VIOLATION)
  verify_dispatch_invariants: invariant_1_pv_balance_kwh = 800,
                              invariant_9_pv_load_priority_kwh = 800   (BREACH)
  compute_kpis: pv_generation_mwh = 24.4375   (phantom PV for a BESS-only project)
  --strict: RAISES AssertionError (invariant_1=800, invariant_9=800)
  verify_energy_balance(raise_on_failure=True): RAISES
  ```
  On the full default workbook scaled to BESS-only the residual is ≈3203 kWh / inv1 3202.9 / inv9 1181.7 (§A table). Confirmed in **both** vnb and merchant modes.
- **Impact:** A first-class, documented, "tested" asset mode produces (a) an energy-balance violation, (b) two breached dispatch invariants, (c) phantom `pv_generation_mwh` / self-consumption / curtailment KPIs that a user would read and trust, and (d) a hard crash under `--strict`. The test suite misses it because BESS-only tests assert only that PV *flows* are zero (F7). Root cause: `model_to_dataframe` copies the raw input `pv_kwh` instead of the PV the model actually used (which `build_model:303-307` overrides to 0 when `pv_nameplate_kwp ≤ 0`).
- **Recommendation:** mirror the model's PV override in the output frame.
  ```diff
  --- a/pvbess_opt/optimization.py
  +++ b/pvbess_opt/optimization.py
  @@ def model_to_dataframe(model, ts, params):
  +    pv_present = float(params.get("pv_nameplate_kwp", 0.0) or 0.0) > 0.0
       res["load_kwh"] = [
           float(ts.loc[t, "load_kwh"]) if "load_kwh" in ts.columns else 0.0
           for t in time_index
       ]
  -    res["pv_kwh"] = [float(ts.loc[t, "pv_kwh"]) for t in time_index]
  +    res["pv_kwh"] = [
  +        float(ts.loc[t, "pv_kwh"]) if pv_present else 0.0
  +        for t in time_index
  +    ]
  ```
  Add a regression test that runs `verify_energy_balance(raise_on_failure=True)` and `verify_dispatch_invariants` on a BESS-only frame built from a non-zero `pv_kwh` column.

### F2 — P1 — BESS-derived revenue degraded on the PV curve (`build_yearly_cashflow`)

- **Location:** `pvbess_opt/economics.py:332-341` (vs the correct per-stream split in `pvbess_opt/lifetime.py:75-83, 248-251`).
- **Evidence (synthetic BESS-only, default degradation params):** `cashflow_yearly` revenue ratio tracks `pv_factor` exactly (year 5 = 0.95900 = pv_factor; year 20 = 0.88286 = pv_factor), never `bess_factor` (year 20 bess = 0.81707). The lifetime aggregate (`aggregate_lifetime_to_yearly`) `revenue_eur_total` tracks `bess_factor` (year 20 ratio 0.75796). Same `03_results.xlsx`, two BESS revenue series ~16 % apart in the final year. NPV via `build_yearly_cashflow` overstated **2.1 %** vs a `bess_factor`-scaled recompute on the pure BESS-only case.
- **Impact:** Headline NPV/IRR for BESS-only (and the BESS share of any hybrid) are computed with the wrong (too-gentle) degradation; the two output sheets contradict each other. Effect is small for PV-dominant hybrids (<1 %) but material and confusing for BESS-only / BESS-dominant portfolios, especially at low discount rates / long horizons. Acknowledged in-code as a "future enhancement" (`economics.py:328-331`).
- **Recommendation:** split the Year-1 revenue base into PV-origin and BESS-origin streams and scale each by its own factor (reuse the `bess_factor` already computed in the loop). Patch sketch in Appendix 2.

### F3 — P2 — Unpinned dependency upper bounds (pandas 3 / numpy 2 a major beyond the floors)

- **Location:** `requirements/base.txt:8-9` (`pandas>=2.0`, `numpy>=1.24`).
- **Evidence:** installed pandas 3.0.3, numpy 2.4.6 — one major above the floors; suite green on them.
- **Impact:** lockfile-free `>=` floors mean (a) a fresh/CI install resolves to the latest major (works today), (b) the floor still permits the *old* untested major, and (c) a future pandas 4 / numpy 3 auto-upgrades with no guard. The prior `python-dateutil` undeclared-dep issue is fixed; this is the remaining supply-chain risk.
- **Recommendation:** add tested upper bounds, e.g. `pandas>=2.2,<4`, `numpy>=1.26,<3` (and equivalently for matplotlib/pyomo/sphinx), or commit a CI constraints/lock file. Bump the floors to the majors actually tested.

### F4 — P2 — Default Monte-Carlo ensemble ~1 h (compare-sources ~4 h), coarse progress, no upfront warning

- **Location:** `pvbess_opt/rolling_horizon.py:328-408`; `main.py:819-889`; defaults `uncertainty_n_seeds=30` (`io.py:166`).
- **Evidence:** one seed = 126.5 s here → 30 seeds ≈ 63 min; 4×30 compare ≈ 253 min. Progress logs only per seed; the first ~2 min (one seed of ~365 windows) is silent. The plot fan-out warns upfront (`main.py:765-773`) but the MC path does not.
- **Impact:** a user who flips `uncertainty_enabled=True` (or `--rolling-horizon`) gets a multi-hour run that can look hung for the first couple of minutes.
- **Recommendation:** emit an upfront WARNING with the projected wall-clock when MC is enabled (mirror the `plot_daily_scope='all'` warning), and add per-window progress (e.g. every N windows) inside `rolling_horizon_dispatch`.

### F5 — P2 — `round(4)` before invariant checks → false `--strict` `invariant_4` failure on high-throughput runs

- **Location:** `pvbess_opt/optimization.py:719-720` (round) vs `:822-834` (sum-based `invariant_4`); strict gate `main.py:622-637`.
- **Evidence:** vnb bess_only (21,818 MWh discharge) → `invariant_4 = 0.00217 kWh > 1e-3`; `invariant_3 = 1.37e-4` (just above the 1e-4 rounding floor). The MILP solution is feasible to solver tolerance (~1e-6); the residual is `round(4)` accumulated over 35,040 rows.
- **Impact:** `--strict` would falsely abort an otherwise-valid high-throughput run.
- **Recommendation:** compute invariants on the unrounded model values (verify before the `round(4)`), or scale the strict tolerance by throughput, or round only the persisted frame and keep a full-precision copy for verification.

### F6 — P2 — Silent NaN fill in the timeseries

- **Location:** `pvbess_opt/io.py:907-909`.
- **Evidence:** `ts[col].astype(float).ffill().bfill()` for `load_kwh`/`pv_kwh`/`dam_price`/`retail_price` with no log.
- **Impact:** a corrupt/missing block (e.g. a day of absent DAM prices) is silently interpolated; the user has no signal that their input had gaps.
- **Recommendation:** count NaNs per column before filling and `logger.warning` when any are filled (with count + first index).

### F7 — P2 — Real-scale coverage is vnb-hybrid only; BESS-only tests don't verify balance/invariants

- **Location:** `tests/test_input_workbook_smoke.py:119-190` (vnb hybrid PF, real scale), `tests/test_rolling_horizon_realscale.py` (vnb hybrid RH, slow), `tests/test_asset_modes.py:166-178, 200-215` (BESS-only, flows-only assertions).
- **Evidence:** merchant and PV-only/BESS-only are exercised only at 1-day scale; no test calls `verify_energy_balance`/`verify_dispatch_invariants` on a BESS-only frame — the gap that hid F1.
- **Impact:** correctness regressions on merchant / asset-mode / real-scale paths can land green.
- **Recommendation:** add a parametrized real-scale (or at least multi-day) test over all six combos that asserts energy balance + all 9 invariants; this single test catches F1 and F5.

### F8 — P2 — CI installs linters but never runs them

- **Location:** `.github/workflows/ci.yml` (only `pytest`); `requirements/dev.txt` declares `pyflakes`/`pycodestyle`.
- **Evidence:** 2 live F401 unused imports (F13) prove no lint gate.
- **Recommendation:** add a `ruff check` (or `pyflakes`) CI step; fix the 2 imports.

### F9 — P2 — Solver time-limit accepted without confirming a feasible incumbent

- **Location:** `pvbess_opt/optimization.py:159-170`.
- **Evidence:** `maxTimeLimit`/`maxIterations` short-circuit to `return` regardless of solution availability.
- **Impact:** a time-limited solve with no incumbent yields an opaque downstream failure instead of an actionable message.
- **Recommendation:** require a loaded/feasible solution (e.g. check `result.solver.status` / solution count) before accepting a soft-limit termination; otherwise raise a clear "no feasible solution within time limit."

### F10 — P2 — Sheet-name / wrong-sheet documentation drift

- **Location & evidence:** see the §H table — `main.py:15,17`, `CONTRIBUTING.md:71-72`, `economics.rst:4` say `economic` (real: `economics`); `show_titles` and plot-scope flags attributed to the wrong sheet.
- **Impact:** a user looking for `show_titles`/scope flags or the `economic` sheet is sent to the wrong place.
- **Recommendation:** s/`economic` sheet/`economics` sheet/; attribute `show_titles` → `project`, plot-scope → `simulation`.

### F11 — P2 — Malformed table + missing-dir layout drift + broken xrefs

- **Location & evidence:** `inputs.rst:31` malformed-table Sphinx ERROR (first-column rule too narrow for `` ``retail_price_eur_per_mwh`` ``); `06_uncertainty_plots/` omitted from `main.py:3-11`, `output_layout.rst:7-24`, `running.rst:54-63`; `inputs.rst:113` broken `:data:\`...DEPRECATED_ECONOMICS_KEYS\``; `lifetime.py:32` stale `test_lifetime_dispatch.py`.
- **Recommendation:** widen the `inputs.rst` column rule; add the `06_uncertainty_plots/` line in all three layout listings; fix the xref to `_LEGACY_RENAMED`; update the test reference to `tests/test_lifetime.py`.

### F12 — P3 — Consolidated nits

- `pvbess_opt/plotting/lifecycle.py:41` — unused import `annotate_value_safe` (ruff F401). `tests/test_pv_loader.py:27` — unused import `_resolve_pv_column` (F401).
- `pvbess_opt/plotting/_currency.py:27-32` — docstring examples say "EUR 12.3M" but code emits `€` (`:19`).
- `README.md:33` — "What's new in v0.8.8" (release is 0.8.9); `docs/CHANGELOG.md:1-6` "only the most recent release" but contains 0.8.8/0.8.7; `CONTRIBUTING.md:10` "10 modules" then lists 11; `running.rst:33-36` window/commit "default 48/24" (argparse default is `None`, 48/24 are the workbook defaults).
- `pvbess_opt/rolling_horizon.py:364` — `monte_carlo_rolling(n_seeds=0)` returns a column-less empty DataFrame (KeyError if a caller reads `foresight_gap_pct`); `main.py` avoids it via a separate branch.
- `pvbess_opt/optimization.py` — merchant mode omits the no-simultaneous-grid-IO constraint (vnb-only at `:579-593`); with grid-charging on it is unconstrained but economically non-binding (verified: never occurs).
- `pvbess_opt/economics.py:40` — module-docstring sign convention `net_cashflow = revenue + opex + capex` omits the `devex` term the code adds (`:363`).
- `pvbess_opt/rolling_horizon.py:146-149` — PV noise clips to the per-window max, not nameplate (function lacks nameplate; acceptable proxy).
- `pvbess_opt/availability.py:68-70` — `bess_utilization_diagnostics` (a nested dict) is not derated, unlike the headline MWh keys (informational only).
- `main.py:797`, `rolling_horizon.py:420` — `verify_dispatch_invariants(..., mode=str(params.get("mode","vnb")))` passes `mode` without `.lower()`; safe because the loader normalizes `mode` to lowercase (`io.py` `_parse_string_enum`) and the `--mode` choices are lowercase, but inconsistent with the lowercasing done everywhere else.

---

## Appendices

### Appendix 1 — F1 patch (ready to apply)

```diff
--- a/pvbess_opt/optimization.py
+++ b/pvbess_opt/optimization.py
@@ def model_to_dataframe(model, ts, params):
+    pv_present = float(params.get("pv_nameplate_kwp", 0.0) or 0.0) > 0.0
     res["load_kwh"] = [
         float(ts.loc[t, "load_kwh"]) if "load_kwh" in ts.columns else 0.0
         for t in time_index
     ]
-    res["pv_kwh"] = [float(ts.loc[t, "pv_kwh"]) for t in time_index]
+    res["pv_kwh"] = [
+        float(ts.loc[t, "pv_kwh"]) if pv_present else 0.0
+        for t in time_index
+    ]
```

### Appendix 2 — F2 patch sketch (per-stream BESS revenue degradation)

```diff
--- a/pvbess_opt/economics.py
+++ b/pvbess_opt/economics.py
@@ build_yearly_cashflow: split the Year-1 revenue base into PV- and BESS-origin
+    # PV-origin vs BESS-origin Year-1 revenue (mirrors lifetime.py columns).
+    rev1_retail_pv   = float(year1_kpis.get("profit_load_from_pv_eur", 0.0) or 0.0)
+    rev1_retail_bess = float(year1_kpis.get("profit_load_from_bess_eur", 0.0) or 0.0)
+    rev1_dam_pv      = float(year1_kpis.get("profit_export_from_pv_eur", 0.0) or 0.0)
+    rev1_dam_bess    = float(year1_kpis.get("profit_export_from_bess_eur", 0.0) or 0.0) \
+                       - float(year1_kpis.get("expense_charge_bess_grid_eur", 0.0) or 0.0)
@@ inside the y-loop (bess_factor `bess_factor` is already computed):
-            revenue_retail_y = revenue_1_retail * pv_factor * (1.0 + retail_infl) ** (y - 1)
-            revenue_dam_y    = revenue_1_dam    * pv_factor * (1.0 + dam_infl)    ** (y - 1)
+            revenue_retail_y = (rev1_retail_pv * pv_factor + rev1_retail_bess * bess_factor) \
+                               * (1.0 + retail_infl) ** (y - 1)
+            revenue_dam_y    = (rev1_dam_pv * pv_factor + rev1_dam_bess * bess_factor) \
+                               * (1.0 + dam_infl) ** (y - 1)
```
(Fall back to the existing `pv_factor`-only behaviour when `year1_kpis` carries no per-stream breakdown, as the current `_has_breakdown` guard already does.)

### Appendix 3 — How to reproduce the empirical evidence

All evidence was produced with throwaway scripts under `/tmp` (not committed). To reproduce: load `inputs/input.xlsx` via `read_inputs`; for each combo build a `params` variant (`pv_only`: `bess_power_kw=bess_capacity_kwh=0`; `bess_only`: `pv_nameplate_kwp=0`, `allow_bess_grid_charging=True`); `run_scenario(..., mip_gap=0.01)`; then `verify_energy_balance`, `verify_dispatch_invariants`, `compute_kpis`. F1: build a workbook with `pv_nameplate_kwp=0` and a non-zero `pv_kwh` column, `read_inputs`, solve, and inspect `res['pv_kwh'].sum()` vs the (zero) PV flows. Timing: `run_scenario(gap=0.001)` and `rolling_horizon_dispatch(forecast_seed=42)` on the full workbook.

---

## Severity → suggested phase mapping

A remediation PR can be generated directly from this table (mirrors the v0.8.8→v0.8.9 follow-up flow).

| Phase | Findings | Theme | Risk if deferred |
|---|---|---|---|
| **Phase 1 — correctness (must-fix)** | **F1** | Zero phantom `pv_kwh` in BESS-only output frame (Appendix 1) + regression test (also closes F7's blind spot) | Wrong KPIs + `--strict` crash on a shipped mode |
| **Phase 2 — financial fidelity** | F2 | Per-stream BESS revenue degradation (Appendix 2) | Misstated NPV/IRR for BESS-only; contradictory output sheets |
| **Phase 3 — robustness hardening** | F5, F6, F9 | Invariant check on unrounded values; warn on NaN fill; require feasible incumbent on time-limit | Latent false-strict aborts, masked bad input, opaque solver failures |
| **Phase 4 — supply chain & CI** | F3, F8 | Add tested dependency upper bounds; add a lint gate; fix the 2 F401s | Future major auto-upgrade breakage; unguarded style drift |
| **Phase 5 — test coverage** | F7 | Parametrized real-scale energy-balance + 9-invariant test across all 6 combos | Mode/asset regressions land green |
| **Phase 6 — performance/UX** | F4 | Upfront MC runtime warning + per-window progress | Multi-hour runs that look hung |
| **Phase 7 — docs** | F10, F11, F12 | Sheet names, layout `06_`, malformed table, xrefs, version headline, nits | Users misled to wrong sheets / broken doc table |
