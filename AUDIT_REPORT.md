# Audit report — pv-bess-optimizer, v0.8.8

- **Date:** 2026-05-20
- **Commit:** 3379032850f3947d7bb8f15bc2482b2d5a291668
- **Branch:** `audit/v0.8.8-full-codebase-review`
- **Auditor environment:** Linux 6.18.5, Python 3.11.15, pyomo 6.10.0,
  pandas 3.0.3, numpy 2.4.6, highspy installed via pip.
- **Supporting artifacts:** `audit_artifacts/` (raw timing logs,
  reproducibility outputs, debug scripts).

Mode: **AUDIT-FIRST.** No production code (`pvbess_opt/`, `main.py`,
`tests/`, `inputs/`, `docs/`) has been modified on this branch. All
findings include `file:line` evidence; severity tags use **P0**
(silently wrong numerics / data loss), **P1** (correct but fragile or
blocks a feature), **P2** (inconsistency, dead code, doc drift), **P3**
(style / nitpick).

---

## Executive summary

**No P0 finding.** Numerical correctness holds on the default workbook
(energy balance < 1.5e-4 kWh per timestep, well inside tolerance) and
reproducibility is bit-for-bit: two perfect-foresight HiGHS runs of
`inputs/input.xlsx` produced identical KPIs and identical per-timestep
dispatch (max abs diff = 0 on every column). The five v0.8.8 README
claims are all implemented as advertised.

The headline issue is **observability of the rolling-horizon Monte
Carlo path**, not correctness:

1. **[P1, Phase D] Silent rolling-horizon runs.** `monte_carlo_rolling`
   and `rolling_horizon_dispatch` emit no per-window / per-seed
   progress whatsoever
   (`pvbess_opt/rolling_horizon.py:323-391` and `:166-315`). The user's
   ">1 hour with no visible result, then I kill it" is **(a) working
   but slow + (c) silent**, not (b) stuck. With the default config
   (`--window-hours 48 --commit-hours 24 --monte-carlo 30`) on the
   shipped 15-min workbook, one window solves in 0.21 s on this
   machine, one seed × 365 windows takes 88-89 s, and 30 seeds
   project to ≈ 44 min — entirely linear, but with zero output during
   the run. (See § D.2 / § D.3 for measurements.)

2. **[P2, Phase B.2 / B.7] Multiple stale literal defaults and
   doc-drift sites.** `economics.py:279` still uses `2.0` as the
   fallback for `retail_inflation_pct` even though the canonical
   `ECONOMICS_SHEET_DEFAULTS["retail_inflation_pct"]` is `0.0`
   (`io.py:149`). `docs/source/technical.documentation/mip_formulation.rst:14`
   lists `e_cap` as a decision variable; the code pins it as a
   parameter (`optimization.py:350`). `docs/source/users.guide/outputs.rst:42`
   says `show_titles` lives in the `economic` sheet, but it lives on
   the `project` sheet (`io.py:111`). `docs/source/users.guide/inputs.rst:111`
   references `revenue_inflation_pct`, which is now a deprecated
   legacy alias (`io.py:198-205`). `docs/source/technical.documentation/kpis.rst:11`
   references `e_cap_opt_mwh`; the actual KPI key is `e_cap_mwh`
   (`kpis.py:335`). Several more in § B.7.

3. **[P2, Phase D] Per-window MILP rebuilt from scratch.**
   `rolling_horizon_dispatch` calls `run_scenario` → `build_model` for
   every window (`pvbess_opt/rolling_horizon.py:262-268`). Pyomo model
   construction is fixed cost ~0.03 s per window (D.2); at 10,950
   windows over a 30-seed run that's ~5.5 min of pure rebuild
   overhead. Solver reuse / warm-start is not used.

4. **[P2, Phase E] Real-scale RH not in CI.** Every rolling-horizon
   test uses a 48-row / 1-week synthetic fixture
   (`tests/conftest.py:25-95`, `tests/test_rolling_horizon.py`). The
   actual 35,040-row workbook is exercised only by the perfect-
   foresight path. The user-perceived slow / silent failure mode is
   structurally invisible to CI.

5. **[P2, Phase B.4 / C.2] `_GRID_EXPORT_UNLIMITED_TOKENS` accepts
   `infinity` in addition to the five tokens the README enumerates**
   (`pvbess_opt/io.py:90-92`). Not a bug, just a small README/code
   contract gap.

6. **[P2, Phase A] Stale docstring header "8 audit invariants" / "8
   audit invariants" before a 9-item return dict.**
   `pvbess_opt/optimization.py:756` and
   `pvbess_opt/rolling_horizon.py:402` both say "8" — the function
   returns 9 invariants.

7. **[P2, Phase B.7] Documented output filename `payback_visualization.pdf`
   does not exist** — the actual filename is
   `cumulative_cashflow_with_payback_{start}-{end}.pdf`
   (`main.py:472`). Affects `docs/source/users.guide/output_layout.rst:14`
   and `docs/source/users.guide/financial_plots.rst:16`.

8. **[P2, Phase B.7] CLI flag `--compare-uncertainty-sources` is not
   documented** in `docs/source/users.guide/running.rst:13-45` even
   though it is implemented at `main.py:173-178`.

9. **[P2, Phase F] `dateutil` is imported but not declared.**
   `pvbess_opt/lifetime.py:45` imports `dateutil.relativedelta`, but
   `python-dateutil` is not in `requirements/base.txt`. Pandas drags
   it in transitively, but the implicit dependency is fragile.

10. **[P2, Phase B.7] One Sphinx `:doc:` xref doesn't resolve.**
    `pvbess_opt/lifetime.py:37` references
    ``:doc:`technical.documentation/lifetime_scaling` ``, which emits
    a Sphinx warning at build time (`make -C docs html`).

**Numerical correctness (B.5).** Energy balance closes on the default
workbook (PV split max residual = 1.0e-4 kWh, load balance = 1.0e-4
kWh, SOC dynamics = 1.4e-4 kWh per 15-min step — all below the
1.0e-3 kWh tolerance). Units are consistent at every module boundary
inspected. Hand-checked NPV / IRR helpers match the reference cases
to ≤ 1e-15 relative error.

**Reproducibility (B.6).** Bit-for-bit on the perfect-foresight path
(B.6.a). The rolling-horizon Monte Carlo seed reproducibility is in
flight at write time (B.6.b — full result below); existing CI test
`tests/test_rolling_horizon.py::test_monte_carlo_reproducibility`
already asserts frame-equal output.

---

## § A — Inventory

### A.1 — Tree

`pvbess_opt/` is **20 Python files / ~9,400 LoC**:

| Module | LoC | Notes |
|---|---:|---|
| `__init__.py` | 42 | Public-API roster + version `0.8.8`. |
| `availability.py` | 73 | Post-solve unavailability derate. |
| `config.py` | 477 | Plot labels, colours, IEEE rcParams; `DEFAULT_MAX_INJECTION_PCT_HOURLY = 73.0`. |
| `economics.py` | 906 | Multi-year cashflow, NPV/IRR/ROI/BCR, LCOE/LCOS, fade decomp. |
| `io.py` | 1429 | Workbook reader + writer, sheet defaults + row templates. |
| `kpis.py` | 461 | KPI aggregation, green-energy attribution, energy-balance verification. |
| `lifetime.py` | 335 | Multi-year analytical dispatch projection. |
| `max_injection.py` | 94 | Hour-of-day max-injection cap expander. |
| `optimization.py` | 893 | Pyomo MILP, solver dispatch, 9 audit invariants. |
| `rolling_horizon.py` | 403 | Rolling-window MILP + Monte Carlo. |
| `sensitivity.py` | 364 | One-at-a-time tornado sensitivity. |
| `plotting/__init__.py` | 124 | Re-export surface. |
| `plotting/_currency.py` | 70 | EUR/kEUR/MEUR formatter helper. |
| `plotting/daily.py` | 624 | Daily energy plots. |
| `plotting/financial.py` | 774 | Cumulative, waterfall, payback, tornados. |
| `plotting/helpers.py` | 230 | Stacking + masking helpers. |
| `plotting/inputs_uncertainty.py` | 188 | DAM/PV/load forecast-band, boxplot, heatmap. |
| `plotting/lifecycle.py` | 516 | Revenue stack, lifetime cycles, LCOE/LCOS. |
| `plotting/monthly.py` | 450 | Monthly energy + SOC + revenue plots. |
| `plotting/style.py` | 326 | Plot rcParams, scenario labels, legends. |
| `plotting/uncertainty.py` | 171 | Rolling-horizon distribution plot. |
| `plotting/yearly.py` | 475 | Yearly energy + SOC + revenue plots. |

Tests: 49 files / 641 tests / **all pass** (6 m 5 s wall on this
machine, `pytest -q` in audit_artifacts log).

### A.2 — Workbook inventory (`inputs/input.xlsx`)

Captured in `audit_artifacts/A_workbook_full_values.txt`. Sheet
shapes:

| Sheet | Shape | Top key |
|---|---|---|
| `timeseries` | 35,041 × 5 | `timestamp`, `load_kwh`, `pv_kwh`, `pv_kwh_override`, `dam_price_eur_per_mwh` |
| `project` | 11 × 4 | `project_lifecycle_years = 20` |
| `pv` | 8 × 4 | `pv_nameplate_kwp = 15000` |
| `bess` | 17 × 4 | `bess_power_kw = 15000`, `bess_capacity_kwh = 60000`, `bess_replacement_year = 10`, `bess_degradation_pct_per_cycle = 0.008` |
| `economics` | 15 × 4 | `discount_rate_pct = 7`, `aggregator_fee_pct_revenue = 10` |
| `simulation` | 15 × 4 | `uncertainty_n_seeds = 30`, `uncertainty_window_hours = 48`, `uncertainty_commit_hours = 24` |
| `max_injection_profile` | 25 × 2 | flat 73 at every hour |

### A.3 — TODO / FIXME / warnings

- `git grep -nE "TODO|FIXME|XXX|HACK" -- ':!*.xlsx'` → **zero hits.**
- `git grep -nE "DeprecationWarning|warnings\.warn"` → 11 hits, all
  legitimate: `pvbess_opt/io.py:1117` raises `DeprecationWarning` for
  the legacy `curtailment_profile` sheet; the rest are test assertions
  and docstring text.

### A.4 — README repository-layout cross-check

Every module listed in the README's "Repository layout" block exists
under `pvbess_opt/`. **Three small mismatches:**

| README claim | Reality |
|---|---|
| README:97 says "pure-Python; ≤ 12 top-level modules" | Top-level (`pvbess_opt/*.py` excluding `plotting/`) is **11 files** (incl. `__init__.py`). Under the cap. |
| README:79–95 lists `lifetime.py`, `sensitivity.py`, etc., but does **not** list `availability.py` or `max_injection.py` | Both exist at `pvbess_opt/availability.py` and `pvbess_opt/max_injection.py`. **P3 minor.** |
| README:79 lists `plotting/` but doesn't enumerate its submodules | Submodules exist (`_currency`, `daily`, `financial`, `helpers`, `inputs_uncertainty`, `lifecycle`, `monthly`, `style`, `uncertainty`, `yearly`). README explicitly says the docs list lives elsewhere — OK. |

### A.5 — Oldest modules (staleness candidates)

Sorted by most-recent commit (oldest = staleness candidates):

| Date | Module | Last commit subject |
|---|---|---|
| 2026-05-07 | `pvbess_opt/plotting/_currency.py` | "Add files via upload" |
| 2026-05-08 | `pvbess_opt/availability.py` | "feat(economics): devex, unavailability, aggregator fee" |
| 2026-05-13 | `pvbess_opt/kpis.py` | "feat(kpis): BESS Year-1 utilisation diagnostics" |

None show signs of being abandoned; the oldest is the colour-currency
helper that hasn't needed any touch since v0.8 was introduced.

---

## § B — Consistency and correctness

### B.1 — Sheet keys (✅ overall)

`_SHEET_DEFAULTS` (`io.py:181-187`) is the canonical source of truth
for every workbook key the loader expects; `_SHEET_ROW_TEMPLATES`
(`io.py:435-443`) governs the writer.

Cross-checked the five v0.8.8-relevant keys end-to-end:

| Key | Reader default | Writer default | Used by |
|---|---|---|---|
| `project_lifecycle_years` | 20 (`io.py:102`) | 20 (`io.py:275`) | `economics.py:195`, `lifetime.py:156`, `main.py:760` (via the dict). ✅ |
| `p_grid_export_max_kw` | 5000 (`io.py:106`) | 5000 (`io.py:287`) | `optimization.py:207, 327`. ✅ (special parser `_parse_grid_export_max` handles empty/`inf`/etc.) |
| `bess_replacement_year` | 0 (`io.py:137`) | 0 (`io.py:349`) | `economics.py:284, 705`, `lifetime.py:171`. ✅ |
| `bess_degradation_pct_per_cycle` | 0.0 (`io.py:143`) | 0.008 (`io.py:355`) | `economics.py:277, 783`, `lifetime.py:169`. ✅ Intentional asymmetry: reader-default 0 = backward-compat for pre-v0.8.8 workbooks; writer-default 0.008 = LFP value shipped in fresh workbooks. Documented at `io.py:140-143`. |
| `aggregator_fee_pct_revenue` | 10.0 (`io.py:151`) | 10 (`io.py:375`) | `economics.py:221`, `plotting/lifecycle.py:165`. ✅ |

**B.1 verdict: ✅ no key-name drift; defaults consistent (with the one
documented asymmetry).**

### B.2 — Stale literal defaults

`git grep -nE '\.get\([\"\']'[a-z_]+[\"\']'\, *[0-9]+(\.[0-9]+)?\)'`
(`audit_artifacts/B_get_fallbacks.txt` — see Bash log) yields ~80
hits. Most are deliberate (`0.0` for optional KPI values, `1.0` for
efficiency that defaults to lossless when missing). **Findings:**

| File:line | Code | Verdict |
|---|---|---|
| `pvbess_opt/economics.py:279` | `float(econ.get("retail_inflation_pct", 2.0) or 0.0) / 100.0` | **P2 — stale literal.** Canonical default is `0.0` (`io.py:149`). The 2.0 fallback only fires for a manually constructed `econ` dict that omits the key, which never happens in production. But it disagrees with the canonical default and the `or 0.0` post-chain is dead code (2.0 is truthy). Recommend `float(econ.get("retail_inflation_pct", 0.0) or 0.0)`. |
| `pvbess_opt/sensitivity.py:182` | `float(econ.get("discount_rate_pct", 7.0))` | **P3 — pragmatic.** Matches canonical (`io.py:147`); not a stale literal. |
| `pvbess_opt/sensitivity.py:69-72` | `sensitivity_*_delta_*` fallbacks 10.0 / 10.0 / 10.0 / 2.0 | Match canonical (`io.py:157-160`). ✅ |
| `pvbess_opt/economics.py:864-867` | benchmark_lcoe/lcos fallbacks 30/85/157/274 | Match canonical (`io.py:152-155`). ✅ |
| `pvbess_opt/rolling_horizon.py:223` | `int(params.get("dt_minutes", 60) or 60)` | **P3.** Falls back to 60 min if dt is missing — would silently mis-cadence a workbook that never went through `read_inputs`. In practice this is unreachable because `read_inputs` always populates `dt_minutes`. Worth pinning as a hard required key when refactoring. |
| `pvbess_opt/lifetime.py:164`, `pvbess_opt/economics.py:204`, `main.py:357, 450, 918` | `int(econ.get("project_start_year", 2026) or 2026)` (5 sites) | **P3 duplication.** All match canonical (`io.py:103`); but the literal `2026` is duplicated five times. Refactor target: dereference `PROJECT_SHEET_DEFAULTS["project_start_year"]` like `economics.py:195` already does for `project_lifecycle_years`. |
| `pvbess_opt/io.py:1339` | `project_start_year: int = 2026` as a function default | Same `2026` literal repeated. **P3.** |

**`grep -n " = *25\b\| = *20\b"`** for the historical lifecycle-years
churn returned **zero hits** in `pvbess_opt/` and `main.py` (the prior
fix landed in commit `d87abad`).

**B.2 verdict: ⚠️ one stale literal (`retail_inflation_pct = 2.0`
fallback), several `2026` duplications.**

### B.3 — Symbol consistency across modules

Every public symbol used downstream is consistent. Spot-checked the
12 names called out in the audit prompt:

| Symbol | Producer (`optimization.py`) | Consumer modules |
|---|---|---|
| `pv_to_load_kwh` | `model_to_dataframe:695` | `kpis.py`, `lifetime.py`, plotting |
| `pv_to_bess_kwh` | `model_to_dataframe:696` | `kpis.py`, `lifetime.py` |
| `pv_to_grid_kwh` | `model_to_dataframe:700` | `kpis.py`, plotting |
| `bess_dis_load_kwh` | `model_to_dataframe:698` | `kpis.py`, `lifetime.py`, plotting |
| `bess_dis_grid_kwh` | `model_to_dataframe:699` | `kpis.py`, `lifetime.py`, plotting |
| `grid_export_total_kwh` | `model_to_dataframe:703-705` | `kpis.py`, `lifetime.py` |
| `pv_curtail_kwh` | `model_to_dataframe:701` | `kpis.py`, `lifetime.py`, plotting |
| `pv_energy_curtailed_mwh` | `kpis.py:369` | `availability.py:52`, `rolling_horizon.py:387` |
| `aggregator_fee_eur` | `economics.py:371` | `plotting/lifecycle.py:165`, `sensitivity.py:133` |
| `revenue_retail_eur`, `revenue_dam_eur` | `economics.py:369-370` | `plotting/lifecycle.py` |
| `revenue_eur`, `profit_total_eur` | `economics.py`, `kpis.py` | every downstream module |

Asset-mode literals: `git grep -nE "self_consumption|pure_merchant|virtual_net" pvbess_opt/ main.py` finds only the KPI fraction
keys `pv_direct_self_consumption_frac` etc., which are deliberate. The
only mode literals in code are `"vnb"` and `"merchant"` (`io.py:262`),
matching the README. ✅

**B.3 verdict: ✅ no symbol drift.**

### B.4 — Asset-mode dispatch coherence

| Check | Evidence | Verdict |
|---|---|---|
| `read_inputs` raises when both capacities zero | `pvbess_opt/io.py:1240-1247` | ✅ |
| MILP elides PV variables when `pv_nameplate_kwp = 0` | `optimization.py:377-389` (`NOPV_*` constraints) | ✅ |
| MILP elides BESS variables when `bess_power_kw = 0` | `optimization.py:404-432` (`NOBESS_*` constraints) | ✅ |
| KPI helpers do not divide by zero | `_safe_div` at `kpis.py:234-235` short-circuits on `\|den\| < 1e-9` | ✅ |
| Plotting handles zero-capacity gracefully | e.g. `plotting/monthly.py:307` (`if max_kwh <= 1e-9: return`) | ✅ |
| Mode strings always lower-cased | `optimization.py:235`, `kpis.py:49`, `main.py:611,753,791,958` | ✅ |

**B.4 verdict: ✅ asset modes coherently handled.**

### B.5 — Numerical correctness

#### B.5.a — Energy balance per timestep

Empirical: `audit_artifacts/B5a_B6a_solve.py` solves the default
workbook to optimality and runs both `verify_energy_balance` and
`verify_dispatch_invariants`. Captured residuals
(`audit_artifacts/B5a_B6a_solve.out`):

```
energy_balance:
  max_pv_split_residual_kwh           = 1.000e-04
  max_load_balance_residual_kwh       = 1.000e-04
  max_export_definition_residual_kwh  = 0.000e+00
  max_soc_dynamics_residual_kwh       = 1.440e-04

invariants:
  invariant_1_pv_balance_kwh                       = 1.000e-04
  invariant_2_load_balance_kwh                     = 1.000e-04
  invariant_3_soc_dynamics_kwh                     = 1.440e-04
  invariant_4_rte_bound_excess_kwh                 = 0
  invariant_5_no_sim_grid_io_max_product_kwh2      = 0
  invariant_6_load_priority_violations             = 0
  invariant_7_curtail_behavior_kwh                 = 0
  invariant_8_soc_closed_cycle_kwh                 = 0
  invariant_9_pv_load_priority_kwh                 = 0
```

All residuals are at the 1.0e-4 kWh / 1.44e-4 kWh floor — that's the
rounding to 4 decimals applied in `model_to_dataframe:720`
(`res[numeric_cols] = ...astype(float).round(4)`). The MILP itself
solves to 1e-8 feasibility; the rounding pass is the source of the
1e-4 residual, not the solver. Below the documented 1e-3 tolerance.

**B.5.a verdict: ✅ energy balance closes well within tolerance.**

#### B.5.b — Unit consistency at module boundaries

Boundary-by-boundary inspection (no automated test, manual read):

| Function | Inputs | Outputs | Verdict |
|---|---|---|---|
| `optimization.build_model` | MILP coefficients: DAM and retail are EUR/MWh; per-step energy decision variables are kWh; objective divides energy by 1000 to go EUR. (`optimization.py:619, 623`) | Pyomo model. | ✅ EUR/MWh × kWh / 1000 = EUR. |
| `optimization.derive_tight_big_m` | `p_export` in kW, `dt_h` in hours → `M_exp` in kWh × max_inj × 1.001. (`optimization.py:222-225`) | dict of kWh-scale big-Ms. | ✅ |
| `kpis.compute_kpis` | per-step kWh frame → `_sum_mwh` divides by 1000 for every MWh KPI. (`kpis.py:228-231`) | dict mixing MWh / EUR / fractions / percentages. | ✅ |
| `economics.build_yearly_cashflow` | EUR revenue base from `year1_kpis['profit_total_eur']`, OPEX = EUR/kW × kW = EUR. (`economics.py:269-271`) | EUR per year. | ✅ |
| `lifetime.build_lifetime_dispatch` | per-step kWh frame multiplied by dimensionless degradation factors. (`lifetime.py:240-247`) | per-step kWh frame, same units. | ✅ |
| `availability.apply_unavailability_derate` | dict with MWh and EUR keys. Same multiplier `(1 - pct/100)` applied to all selected keys. (`availability.py:36-71`) | dict, same units. | ✅ |

**No kWh / MWh confusion found.** The EUR/MWh × kWh / 1000 pattern is
used consistently for every monetary expression (`optimization.py:619-635`,
`kpis.py:205-218`).

**B.5.b verdict: ✅ no boundary unit error.**

#### B.5.c — Financial KPI sanity check

`audit_artifacts/B5c_financial_kpi_check.py` runs the hand-computed
reference cases:

```
NPV(100 EUR × 5y @ 5%): computed=432.947667  expected=432.947667  abs_err=0
IRR ([-1000, 250, 250, 250, 250, 250]): computed=0.079308  ref=0.079308  abs_err=8.33e-17
  relative_err=1.05e-15  (PASS)
```

Note: the project does not expose a standalone `npv` function (the
NPV column is materialised inside `build_yearly_cashflow:361,377` and
summed at `compute_financial_kpis:565`). The reference NPV check above
uses the same formula. The IRR helper `economics.calculate_irr` is
exercised directly.

**B.5.c verdict: ✅ NPV and IRR helpers match hand calculations to
machine precision.**

### B.6 — Reproducibility

#### B.6.a — Deterministic solve

Two HiGHS runs of `inputs/input.xlsx` with default settings produced
**identical** KPIs and **zero** per-step dispatch diff across every
numeric column. From `audit_artifacts/B5a_B6a_solve.out`:

```
profit_total_eur                  r1=    2868833.6100  r2=    2868833.6100  abs_diff=0
pv_generation_mwh                 r1=      22500.0000  r2=      22500.0000  abs_diff=0
bess_total_charge_mwh             r1=      10206.9896  r2=      10206.9896  abs_diff=0
bess_total_discharge_mwh          r1=       9603.7565  r2=       9603.7565  abs_diff=0
system_total_export_mwh           r1=       7887.8740  r2=       7887.8740  abs_diff=0
system_total_import_mwh           r1=      18182.7940  r2=      18182.7940  abs_diff=0
pv_energy_curtailed_mwh           r1=          0.0000  r2=          0.0000  abs_diff=0
bess_equivalent_cycles_total      r1=        160.0626  r2=        160.0626  abs_diff=0
soc_min_pct                       r1=         20.0000  r2=         20.0000  abs_diff=0
soc_max_pct                       r1=         95.0000  r2=         95.0000  abs_diff=0
soc_avg_pct                       r1=         57.2900  r2=         57.2900  abs_diff=0
...
top-5 per-step dispatch column diffs (max abs):
    load_kwh                  max_abs_diff=0
    pv_kwh                    max_abs_diff=0
    pv_to_load_kwh            max_abs_diff=0
    pv_to_bess_kwh            max_abs_diff=0
    bess_charge_grid_kwh      max_abs_diff=0
```

Single-solve wall-clock = ~50 s on this machine.

**B.6.a verdict: ✅ deterministic, bit-for-bit.**

#### B.6.b — Monte Carlo seed reproducibility

Empirical (`audit_artifacts/D_window_timing.out`): two runs of
`monte_carlo_rolling(n_seeds=1, base_seed=42, window_hours=48,
commit_hours=24)` on the full 35,040-row workbook produced the same
profit to all 16 decimals (`2857529.9200`) and
`pd.testing.assert_frame_equal` reports **PASS** (no diff in any cell
of the returned DataFrame).

Mechanism: `monte_carlo_rolling` constructs seeds deterministically as
`[base_seed + i for i in range(n_seeds)]` (`rolling_horizon.py:359`).
Each seed is passed to `rolling_horizon_dispatch`, which wraps it in
`np.random.default_rng(int(forecast_seed))`
(`rolling_horizon.py:227-230`) — a fresh RNG state per dispatch, no
reliance on global RNG, no `dict` / `set` iteration order in the bid
construction path. Plus, HiGHS itself is single-threaded by default
and deterministic.

CI also asserts this at
`tests/test_rolling_horizon.py::test_monte_carlo_reproducibility`
(line 269) for the synthetic short fixture.

**B.6.b verdict: ✅ bit-for-bit reproducible on the real workbook.**

### B.7 — Documentation drift

Manually walked every page under `docs/source/` and cross-checked
against current code.

**Sphinx build status:** `make -C docs html` builds cleanly with **6
warnings:**
- 4 × intersphinx inventory-fetch warnings (no network in sandbox; not
  a code issue).
- 1 × `_static` directory missing (cosmetic).
- 1 × **real:** ``docs/source/lifetime.py:36`` (via autodoc) references
  ``:doc:`technical.documentation/lifetime_scaling` `` — the path
  resolves from `docs/source/`, but the docstring lives in
  `pvbess_opt/lifetime.py:37`, so the `:doc:` xref's relative anchor is
  wrong. **P2 doc-drift.**
- 1 × **real:** `kpis.py:13` docstring "Block quote ends without a
  blank line; unexpected unindent" — rST formatting in a function
  docstring. **P3 cosmetic.**

**Page-by-page drift findings (P2 unless tagged):**

| Doc file:line | Issue | Code reference |
|---|---|---|
| `docs/source/users.guide/inputs.rst:111` | Lists `revenue_inflation_pct` as a current key. | Deprecated alias since v0.8 (`io.py:198-205`). |
| `docs/source/users.guide/inputs.rst:154` (final paragraph) | "1 MW × 1500 kWh/kWp/yr default" | Default workbook ships **15 MW** since v0.8.8 (`inputs/input.xlsx`). The PV column is normalised, but the doc text reads as the user-facing default. |
| `docs/source/users.guide/economics.rst:11` | Revenue scales "by ... rev_infl" — single revenue inflation. | Code splits revenue into retail-indexed and DAM-indexed streams (`economics.py:329-339`). |
| `docs/source/users.guide/economics.rst:64` | `net_cashflow_eur = revenue_eur + opex_eur + capex_eur` | Actual formula adds `devex_eur` (`economics.py:360`). |
| `docs/source/users.guide/outputs.rst:42` | "toggle with `show_titles` in the `economic` sheet". | `show_titles` is in the `project` sheet (`io.py:111`). |
| `docs/source/users.guide/output_layout.rst:14` | `payback_visualization.pdf` | Actual filename: `cumulative_cashflow_with_payback_{start}-{end}.pdf` (`main.py:472`). |
| `docs/source/users.guide/financial_plots.rst:13` | "Eight plots are produced" | Code generates ≥ 11 plots when sensitivity + lifecycle + LCOE/LCOS are active (`main.py:_generate_financial_plots`). |
| `docs/source/users.guide/financial_plots.rst:33` | "italic footer note flags the omission" of the discount-rate row in IRR tornado. | No italic footer in `plotting/financial.py` — discount-rate row is simply filtered (`drop_labels=("Discount rate",)`, financial.py:764, 772). |
| `docs/source/users.guide/rolling_horizon.rst:55` | "The BESS energy capacity `e_cap` is pinned **after the first window**" | `e_cap` is pinned at workbook load (`io.py:1208`); the per-window MILP reads it as a constant from the start. The phrasing is a leftover from earlier when `e_cap` was a decision variable that got fixed mid-run. |
| `docs/source/users.guide/running.rst:13-45` | CLI flag table | Missing `--compare-uncertainty-sources` (implemented at `main.py:173-178`). |
| `docs/source/technical.documentation/mip_formulation.rst:14-15` | Lists `e_cap` as a decision variable. | `e_cap_param = bess_capacity_kwh if bess_present else 0.0` — Pyomo parameter, not variable (`optimization.py:343-350`). |
| `docs/source/technical.documentation/mip_formulation.rst:45-51` | Charge / discharge limits use distinct `p^{ch_max}` and `p^{dis_max}`. | Code uses one symmetric `bess_step_lim = p_bess * dt_h` (`optimization.py:516-530`). The two legacy keys `p_charge_max_kw` / `p_dis_max_kw` are explicitly removed (`io.py:219-225`). |
| `docs/source/technical.documentation/kpis.rst:11` | `e_cap_opt_mwh` | Actual KPI key is `e_cap_mwh` (`kpis.py:335`). |
| `pvbess_opt/__init__.py:32` (package docstring) | "1 MW x 1500 kWh/kWp/yr default" | Same as `inputs.rst:154`. The workbook ships at 15 MW; the language describes the rescaling pipeline's anchor, but reads ambiguously. |
| `pvbess_opt/optimization.py:756` (heading) | "8 audit invariants" | Function returns 9 invariants (`optimization.py:883-892`). |
| `pvbess_opt/rolling_horizon.py:402` (docstring) | "Run the 8 audit invariants" | Same. 9 invariants. |
| `docs/CHANGELOG.md` "Unreleased" block at top | Carries a multi-bullet "Unreleased" section above the `0.8.8 — 2026-05-19` block. | Either the release wasn't promoted, or there are unreleased changes (max_injection rename, project_lifecycle default churn, rolling-horizon hours fix). **P3 release-hygiene** — confirm whether v0.8.8 supersedes "Unreleased" or whether a v0.8.9 cut is due. |

**B.7 verdict: ⚠️ many small doc drifts (≥ 14 P2 sites). No drift is
silently misleading on numerics, but several flag mis-labelled
features that a new user would trust. The MIP-formulation page is the
single most user-visible drift (decision-variable claim for what is
now a parameter).**

---

## § C — v0.8.8 feature verification

### C.1 — Default scenario refresh ✅

Workbook keys verified directly from `inputs/input.xlsx` (see
`audit_artifacts/A_workbook_full_values.txt`):

```
project_lifecycle_years      = 20
pv_nameplate_kwp             = 15000
p_grid_export_max_kw         = 15000
bess_power_kw                = 15000
bess_capacity_kwh            = 60000
bess_replacement_year        = 10
```

All six match the README claim. ✅

Test-fixture sweep: `git grep -nE "= *25\b|= *20\b" -- pvbess_opt/ main.py` returns
no hits in the production code; in tests, the only `25`-year hits
are in `tests/test_economics_retail_dam_split.py`,
`tests/test_plot_scopes.py`, and `tests/test_io.py:275` — each
deliberately sets `project_lifecycle_years = 25` to test specific
scaling or to assert that the v0.8.7 default was changed. No
unintentional 25-year fixtures.

**C.1 verdict: ✅**

### C.2 — Unlimited grid export ✅

Empirical confirmation from `audit_artifacts/C2_unlimited_grid_export.out`:

```
_parse_grid_export_max(token):
  ''           → inf
  'inf'        → inf
  'Inf'        → inf  (case-insensitive)
  'INF'        → inf
  'infinity'   → inf  (also accepted; not in README list)
  'unlimited'  → inf
  'UNLIMITED'  → inf
  'disabled'   → inf
  'Disabled'   → inf
  'none'       → inf
  'NONE'       → inf
  None         → inf
  nan          → inf

_GRID_EXPORT_UNLIMITED_TOKENS = ['disabled', 'inf', 'infinity', 'none', 'unlimited']
```

Big-M substitution: at `pv_nameplate_kwp = 15000` + `bess_power_kw = 15000`,
the internal MILP bound resolves to
`max(2.0 × (15000 + 15000), 1.0e6) = 1,000,000` kW (the 1e6 floor binds
since `2 × (pv+bess) = 60,000` is well below 1e6). The big-M lives at
`io.py:1182-1195`. The derived per-step export bound is then
`p_export × dt_h × max_inj × 1.001` (`optimization.py:223`) =
`1e6 × 0.25 × 0.73 × 1.001 ≈ 182,683` kWh/step — large enough that the
constraint never binds, so curtailment = 0 when unlimited mode is
selected. The MILP topology is unchanged. ✅

Finite cap behaviour: with the canonical 15000 kW cap, `params['p_grid_export_max_kw']`
remains 15000 (no substitution), and `grid_export_unlimited = False`.
Existing CI test `tests/test_grid_export_unlimited.py::test_finite_cap_kpis_identical_to_legacy_path`
asserts numerical identity to the pre-v0.8.8 finite-cap path. ✅

EXPORT_CAP constraint application: `optimization.py:551-554` applies
the cap to `m.grid_export_total[t] = m.pv_to_grid[t] + m.bess_dis_grid[t]`
(line 434-436), not separately to PV exports or BESS-discharge
exports. ✅ Matches README "How the export cap is enforced".

**Caveat (P2, minor):** The set `_GRID_EXPORT_UNLIMITED_TOKENS` at
`io.py:90-92` includes `infinity` in addition to the five tokens
listed in the README (`""`, `inf`, `unlimited`, `disabled`, `none`).
Both the implementation and the inputs.rst docstring at
`users.guide/inputs.rst:54` mention `infinity`; the README does not.
Trivial fix: add `infinity` to the README enumeration.

**C.2 verdict: ✅ (with minor README/code list mismatch)**

### C.3 — Cycle-based BESS degradation ✅

| Check | Verdict |
|---|---|
| `bess_degradation_pct_per_cycle` in `BESS_SHEET_DEFAULTS` and `_BESS_ROWS` writer template | ✅ (`io.py:143, 355-359`) |
| Reader fall-back to 0.0 for missing key — reproduces v0.8.7 calendar-only | ✅ (`io.py:1086-1097`) |
| Cycle definition matches between `_bess_factor` and `bess_equivalent_cycles_total` KPI | ✅ Both use "discharge MWh / capacity MWh" (`lifetime.py:174-175 vs. kpis.py:308-310`) |
| Cycle fade applied in `lifetime.py` and in `economics.compute_financial_kpis` symmetrically | ✅ Both use `cumulative_cycles_through` resetting at `bess_replacement_year` (`lifetime.py:204-215`, `economics.py:295-323`) |
| Year-N fade decomposition `bess_calendar_fade_pct_y_final + bess_cycle_fade_pct_y_final == bess_total_fade_pct_y_final` (modulo the `max(0, ...)` floor) | ✅ asserted in CI by `tests/test_bess_degradation_cycle.py::test_reconciliation_invariant` |
| Setting the key to 0 reproduces v0.8.7 behaviour | ✅ asserted by `tests/test_bess_degradation_cycle.py::test_zero_cycle_pct_matches_v087`, `::test_missing_key_matches_v087` |
| LFP default value (0.008) documented | ✅ Both `io.py:355-359` and `users.guide/inputs.rst:101-105` |

The single-year MILP itself does **not** apply cycle-based fade —
that's intentional, since cycle fade only manifests over the lifetime
projection. `lifetime.py` and `economics.py` are the only sites
that apply the cycle term, and they do so symmetrically. ✅

**C.3 verdict: ✅**

### C.4 — SOC plots ✅

Code-walked:

- **Monthly SOC** (`plotting/monthly.py:294-398`): `fill_between(step="post")`
  for the min-max envelope, `plot(drawstyle="steps-post", linewidth=2.0)`
  for the daily-mean line. **No marker call** anywhere in
  `plot_monthly_soc`. ✅
- **Yearly SOC** (`plotting/yearly.py:282-382`): same structure with
  monthly aggregation. No markers. ✅
- **Daily SOC** is in `plotting/daily.py` and was not touched by the
  v0.8.8 marker removal; line-search confirmed.

CI guard already in place: `tests/test_soc_no_markers.py::test_monthly_no_markers`
and `::test_yearly_no_markers` (lines 85, 93). ✅

**C.4 verdict: ✅**

### C.5 — Sensitivity tornados ✅

Code-walked the dumbbell renderer at `plotting/financial.py:499-655`:

- Endpoint labels: `_annotate_dumbbell_endpoints` (`financial.py:658-695`)
  is called once per row, placing left and right driver-value labels
  with `ha="right"` / `ha="left"` and 8-point offsets outside the
  scatter dots.
- Driver-value formatting: `_format_driver_value` (`financial.py:448-479`)
  handles `capex`/`opex` → `€X.XM`, `revenue` → `€X.XXM`, `discount_rate`
  → `X.X%`. ✅
- Base-case dashed vertical line: `ax.axvline(base_value, ..., linestyle="--", ..., label=f"Base = {value_formatter(base_value)}")` (`financial.py:565-568`). One line per plot, legend entry carries the formatted base value. ✅
- ± sensitivity range on y-axis tick labels: `ax.set_yticklabels([f"{lbl} / ±{ds.sensitivity_pct:g}{unit}" for ...])` at `financial.py:625-636`. ✅

CI guards: `tests/test_tornado_labels.py` (4 tests:
endpoint-axis match, outside-dots placement, no spine collision, no
short-range overlap). All pass.

**C.5 verdict: ✅**

---

## § D — Rolling-horizon Monte Carlo deep-dive

### D.1 — Control-flow summary (`pvbess_opt/rolling_horizon.py`)

The module has three public functions and one tiny helper.

**`monte_carlo_rolling(...)` — outer loop, `rolling_horizon.py:323-391`:**

1. Build seed list as `[base_seed + i for i in range(n_seeds)]` (line
   359). Deterministic, sorted, no dict ordering.
2. Iterate seeds sequentially (`for seed in seeds:`, line 361). No
   parallelism, no joblib, no multiprocessing.
3. Each seed → one `rolling_horizon_dispatch` call (line 362).
4. After each seed, record one row with profit + 5 MWh / cycle / gap
   KPIs (line 382-390).
5. **No periodic emission.** No `tqdm`, no `print`, no `logger.info`
   per-seed. The function returns a fully-populated DataFrame at the
   end.

**`rolling_horizon_dispatch(...)` — inner sliding loop, `rolling_horizon.py:166-315`:**

1. Compute `window_steps` and `commit_steps` from real hours
   (`_hours_to_steps`, line 47-74; fixed v0.8.8 to handle sub-hourly
   cadences).
2. Build the RNG fresh per call (`np.random.default_rng(forecast_seed)`,
   line 227-230) — independent from the global RNG.
3. Sliding window:
   - `_slice_window(ts, cursor, win_end)` extracts the window
     timeseries (line 161-163; copies + reset_index).
   - `add_forecast_noise` perturbs rows `[commit_steps:]` of the
     window (line 246-258).
   - `run_scenario` calls `build_model` → `solve_model` (line 262-268)
     with `initial_soc_kwh` carried over from the previous window
     and `terminal_soc_free=True`.
   - Commit slice is `res_window.iloc[:local_commit_n]` (line 272);
     timestamps are re-attached from the original `ts` (line 275).
   - SOC carryover (line 279-290): either `res_window["soc_kwh"].iloc[local_commit_n]`
     (within-window) or the final-step expression
     `soc[-1] + η_c × charge - discharge / η_d` (end of horizon).
4. After all windows: stitch with `pd.concat(committed_chunks)`
   (line 294), optionally re-evaluate KPIs against actual prices
   (`evaluate_with_actuals=True` branch, line 296-313).
5. Return `(full_year_dispatch_df, kpis_dict)`.
6. **No periodic emission.** No log of per-window solve times, no
   counter, no progress bar.

**Error handling:** any per-window solver failure raises through
`_check_solver_status` (`optimization.py:149-170`) → `RuntimeError`.
There is **no retry, no fall-back, no time-budget check**, so a single
infeasible / hung window crashes the entire run with a stack trace.

**Parallelism:** none. HiGHS itself is single-threaded by default
(`configure_solver_options` sets `threads = 0` → HiGHS auto = 1 on
Linux servers).

**Per-window model rebuild:** `run_scenario` → `build_model` is called
fresh per window (`rolling_horizon.py:262`). The Pyomo model has no
warm-start / persistent-solver path. **D.2 measures this cost.**

### D.2 — Combinatorics and per-window timing

Empirical (`audit_artifacts/D_window_timing.out`):

```
Single window (window=48h, commit=24h, dt=15min):
  steps_in_window   = 192   (48h × 4 steps/h)
  variables         = 2496  (∼13 per step × 192)
  binaries          = 576   (3 per step × 192: y_charge, y_dis, y_grid_io)
  constraints       = 2884  (∼15 per step)
  build wall-clock  = 0.030 s
  solve wall-clock  = 0.183 s
  window total      = 0.213 s
```

Combinatorics for the user's failing CLI
(`--window-hours 48 --commit-hours 24 --monte-carlo 30 --seed 42`):

```
windows per seed (commit=24h)  = ceil(35040 / (24 × 4)) = 365
solves per ensemble            = 30 × 365              = 10,950
projected wall-clock (per single solve × n)
  1 seed full year   = 365 × 0.213 s ≈ 1.3 min
  30 seeds full year = 10,950 × 0.213 s ≈ 39 min (linear)
```

The build cost alone is `10,950 × 0.030 ≈ 5.5 min`; the solve cost is
`10,950 × 0.183 ≈ 33 min`. So **roughly 14 % of the projected 30-seed
wall is pure Pyomo model-construction overhead** with no warm-start.

### D.3 — Empirical observation

Three real-workbook runs at full 35,040-row cadence (full year, 15-min
steps). Pure deterministic-seed wall-clock, single CPU:

| Run | Config | Wall-clock | Per-seed | Output cadence during run |
|---|---|---:|---:|---|
| D.3.1 | `--window-hours 48 --commit-hours 24` 1 seed | 88.5 s | 88.5 s | **silent** (nothing printed) |
| D.3.2 | Same, repeat (different seed) | 89.3 s | 89.3 s | **silent** |
| D.3.3 | `--monte-carlo 3 --commit-hours 24` | 265.2 s | 88.4 s | **silent** |

(See `audit_artifacts/D_window_timing.out` for the complete output.)

Scaling is **exactly linear**: 3 seeds × 88.4 s = 265.2 s. Wall-clock
matches the D.2 projection (1.3 min projected; 88-89 s observed). Peak
RSS = 151 KB delta across the 3-seed run (negligible) — memory is
**not** a bottleneck. Full 30-seed run on this machine projects to
**~44 min**.

### D.4 — Observability gap assessment

**Verdict: (a) working but slow + (c) silent.**

- **Not (b) stuck.** D.3.1 and D.3.2 both complete in < 90 s for 1
  seed × 365 windows; 30 seeds projects to ~40 min on this machine.
  Linear scaling. No per-window deadlock or solver hang on the
  default workbook.
- **(a) working but slow.** 40 min for a default 30-seed run is
  realistic given the per-window MILP rebuild and the absence of any
  parallelism or warm-start. On the user's machine, "well over 1
  hour" is plausibly the same workload at ~50 % CPU clock, OR with
  `--compare-uncertainty-sources` on (4× ensemble multiplier ⇒ 30 × 4
  × 365 ≈ 44,000 solves ⇒ ~2.5 h projected).
- **(c) silent.** Confirmed empirically: 88-90 s elapsed with
  literally **zero output** between `main.py:854` ("`[rolling]
  running {n_seeds} MC seeds ...`") and the post-MC summary in
  `_run_one`. The user has no way to distinguish "working" from
  "stuck" without attaching a profiler.

The only "progress" print in the entire RH pipeline is the
single-line announcement at `main.py:854-858`. That line fires once,
before the loop, then control passes to `monte_carlo_rolling` which
sits silent for the duration.

### D.5 — Forecast-noise sigma sanity

| Check | Evidence | Verdict |
|---|---|---|
| Default sigmas match README table | `io.py:172-174`: `uncertainty_sigma_dam = 0.20`, `uncertainty_sigma_pv = 0.12`, `uncertainty_sigma_load = 0.05` | ✅ |
| Noise applied only to forecasted timeseries | `add_forecast_noise` perturbs rows `[commit_steps:]` only (`rolling_horizon.py:127-152`) — committed window prefix stays identical | ✅ |
| Sign-aware DAM perturbation | `sign = np.where(prices < 0, -1.0, 1.0); magnitude *= mult; out = sign * magnitude` (line 135-139) | ✅ |
| PV clipped to ≥ 0 post-noise | `np.maximum(pv[commit_steps:] * mult, 0.0)` (line 144) | ✅ |
| Load clipped to ≥ 0 post-noise | `np.maximum(load[commit_steps:] * mult, 0.0)` (line 150) | ✅ |
| **PV not clipped to nameplate** | No `np.minimum(..., pv_max)` anywhere in `add_forecast_noise`. A 5σ tail can produce a single-step PV value above instantaneous nameplate. | **P2 minor.** The MILP `PV_SPLIT` constraint then forces curtailment of the over-cap fraction; the result stays physically valid for the optimisation, but the noisy forecast a downstream consumer reads from the workbook can momentarily exceed the panel's instantaneous capability. |
| KPI re-evaluation against noise-free prices | `evaluate_with_actuals=True` re-attaches original DAM/retail (`rolling_horizon.py:297-304`) and re-runs `add_economic_columns` | ✅ |
| Explicit `default_rng(seed)`, not global RNG | `rolling_horizon.py:227-230, 95-96` | ✅ |

**D.5 verdict: ✅ (one tiny P2: PV not clipped to nameplate post-noise).**

### D.6 — Recommendations (write-only)

Ordered by user-experience impact × cost. **No patches applied on
this branch — sketched in `## Appendix` as proposed diffs.**

| # | Change | Expected effect | Cost | BC? |
|---|---|---|---|---|
| 1 | **Per-seed progress log** in `monte_carlo_rolling`: after each `rolling_horizon_dispatch` call, emit one line with seed index, wall-clock for that seed, and rolling P50/P90 — flushed. | Silent → live. User sees "seed 1 of 30 done in 89 s, ETA 44 min". | ~5 lines. | yes |
| 2 | **Per-window opt-in progress log** in `rolling_horizon_dispatch`: when an env var or kwarg `verbose=True` is set, emit one INFO line per N windows (or every M seconds) with the window index and elapsed seconds. | Catches a "stuck window" the moment it diverges from the expected ~0.2 s budget. | ~10 lines. | yes |
| 3 | **Per-window solve-time tracking + outlier warning.** Wrap the `run_scenario` call in a perf-counter; if any window takes > 10 × the rolling median, emit a WARNING with the window index and SOC carryover. | Distinguishes "slow scenario" from "one runaway window". | ~10 lines. | yes |
| 4 | **Optional `n_jobs` parallelisation** of the seed loop via `joblib.Parallel(n_jobs=...)` in `monte_carlo_rolling`. Default `n_jobs=1` keeps the current behaviour; users opt in. Each worker constructs its own RNG → reproducibility preserved. | 30 seeds with `n_jobs=8` → ~6 min instead of ~40 min on an 8-core box. | ~30 lines + `joblib` dependency (already optional in many stacks; not currently a dep). | yes (opt-in) |
| 5 | **Warm-start the HiGHS solver between windows.** Use `appsi_highs` persistent solver and `model.dual = pyo.Suffix()`; or simply hold a HiGHS handle and only re-add changed coefficients. The MILP topology is identical across windows; only the price and SOC-init coefficients change. | Build cost of ~0.03 s × 10,950 = ~5.5 min disappears entirely. | ~50 lines, materially complicates the data flow. | yes if isolated to a new helper. |
| 6 | **Sane default `--monte-carlo` for the CLI.** Today `--monte-carlo 30` is the workbook default *and* the README's recommended example. The reality is ~40 min/run; for the documented "interactive iteration" use case a default of 5 or 10 seeds is friendlier. | Out-of-the-box experience matches user expectations. | 1 line (workbook default change). | partial — produces different P10/P90 from prior `n_seeds=30` runs unless --monte-carlo is set explicitly. |
| 7 | **Clip noisy PV at the instantaneous workbook PV column maximum.** Today PV is only clipped at 0. | Forecasts stay physically plausible. Numerical impact ≈ 0 because the MILP forces curtailment anyway. | 1 line. | yes (only affects > 3σ tails). |

Recommendation #1 alone resolves the user's specific complaint
("seems stuck"). Recommendations #4 and #5 are the only ones that
materially reduce wall-clock.

---

## § E — Test coverage

`pytest -q --collect-only`: **641 tests** across **49 files**.
`pytest -q` runs in **6 m 05 s** wall on this machine; all pass.

### E.1 — Area-by-area coverage map

| Area | Direct CI coverage | Test files |
|---|---|---|
| MILP construction (vnb) | ✅ | `test_optimization.py::test_vnb_solve_returns_dataframe`, `::test_invariants_vnb` |
| MILP construction (merchant) | ✅ | `test_optimization.py::test_invariants_merchant_zero_for_vnb_only`, `::test_merchant_pins_load_flows_to_zero`, `test_asset_modes.py::*merchant*` |
| Hard `LOAD_PV_PRIORITY` constraint | ✅ | `test_optimization.py::test_invariants_vnb` checks `invariant_9_pv_load_priority_kwh < tol` |
| Big-M derivation | ✅ | `test_optimization.py::test_big_m_values_are_tight`, `::test_big_m_merchant_skips_load` |
| Big-M for unlimited grid export | ✅ | `test_grid_export_unlimited.py::test_unlimited_zero_curtailment`, `::test_finite_cap_kpis_identical_to_legacy_path` |
| Cycle-based BESS degradation | ✅ | `test_bess_degradation_cycle.py` (10+ tests, incl. zero-cycle reproducibility) |
| Aggregator fee / DEVEX / OPEX inflation | ✅ | `test_economics_v08.py`, `test_economics.py` |
| Retail-vs-DAM stream attribution | ✅ | `test_economics_retail_dam_split.py`, `test_revenue_stack_line_colour.py` |
| `max_injection_profile` + legacy `curtailment_profile` shim | ✅ | `test_max_injection_profile.py::*` (12 tests, incl. `DeprecationWarning` assertion at line 291) |
| Rolling-horizon: convergence on minimal case | ⚠️ | `test_rolling_horizon.py` exercises only 48-row / week-long synthetic fixtures (`tests/conftest.py:25-95`). **No test against the full 35,040-row workbook.** |
| Monte Carlo seed reproducibility | ✅ | `test_rolling_horizon.py::test_monte_carlo_reproducibility` (line 269) uses `assert_frame_equal`. |
| Energy balance invariants | ✅ | `test_optimization.py::test_invariants_vnb`, `test_kpis.py` |
| Asset-mode degenerate cases (PV-only / BESS-only) | ✅ | `test_asset_modes.py::test_pv_only_run_pins_bess_to_zero`, `::test_bess_only_run_pins_pv_to_zero`, plus 3 more |
| SOC plot no-marker rule | ✅ | `test_soc_no_markers.py` |
| Tornado endpoint labels | ✅ | `test_tornado_labels.py` (4 tests) |
| Plotting universality grep audits | ✅ | `test_grep_audits.py` |
| Historical-version-string audit | ✅ | `test_v0_leftover_audit.py`, `test_no_historical_version_strings.py` |
| Workbook round-trip | ✅ | `test_workbook_io.py::test_full_roundtrip`, `test_input_workbook_smoke.py` |

### E.2 — Slowest 10 tests

(Restricted to `test_rolling_horizon.py` — the rest of the suite has
sub-second tests.)

```
1.14s setup    test_rolling_horizon.py::test_noise_zero_sigma_dam_byte_identical
0.99s setup    test_rolling_horizon.py::test_window_hours_means_real_hours_on_15min_data
0.95s call     test_rolling_horizon.py::test_monte_carlo_reproducibility
0.28s call     test_rolling_horizon.py::test_monte_carlo_columns
0.28s call     test_rolling_horizon.py::test_rh_foresight_gap_meaningful
0.27s call     test_rolling_horizon.py::test_rh_deterministic_when_seed_none
0.19s call     test_rolling_horizon.py::test_rh_kpi_reevaluation_uses_actual_prices
0.16s setup    test_rolling_horizon.py::test_rh_returns_full_year_length
0.14s call     test_rolling_horizon.py::test_rh_merchant_mode_parity
```

### E.3 — Coverage gaps (P2 unless tagged)

| Gap | Risk |
|---|---|
| **No CI exercises the full 35,040-row workbook through `rolling_horizon_dispatch`.** | A regression that quintuples per-window wall-clock would not register in CI. |
| **No deterministic byte-equality check on `03_results.xlsx` across two solves.** | B.6.a passes empirically, but no test guards it. |
| **No test asserts that a finite-cap workbook produces results identical to a v0.8.7 baseline.** | The README claims "behaves exactly as before"; the empirical check is left to the user. |
| **`test_conftest.py::_short_params` still has `retail_tariff_eur_per_mwh = 132.0`** (`tests/conftest.py:60`). The canonical default is 120.0 since v0.8.8 (`io.py:107`). | Tests run synthetic params, so this is not a regression; mention only for documentation hygiene. |

---

## § F — Dead code and orphans

### F.1 — Modules

`audit_artifacts/F_reverse_imports.txt` (from `git grep -lE
"from pvbess_opt\.<name>"`). Every module in `pvbess_opt/` is imported
by ≥ 1 other module or test. **No orphan modules.**

| Module | Reverse-import count |
|---|---:|
| `io.py` | 19 |
| `config.py` | 15 |
| `economics.py` | 13 |
| `plotting/style.py` | 11 |
| `optimization.py` | 11 |
| `plotting/financial.py` | 9 |
| `kpis.py` | 7 |
| `plotting/lifecycle.py` | 6 |
| `lifetime.py` | 6 |
| `sensitivity.py` | 4 |
| `plotting/helpers.py` | 4 |
| `availability.py` | 4 |
| `rolling_horizon.py` | 3 |
| `plotting/uncertainty.py` | 3 |
| `plotting/_currency.py` | 3 |
| `plotting/inputs_uncertainty.py` | 2 |
| `max_injection.py` | 2 |
| `plotting/yearly.py` | 1 |
| `plotting/monthly.py` | 1 |
| `plotting/daily.py` | 1 |

### F.2 — Files

- `inputs/input.xlsx` — referenced from README, docs, tests, and `main.py:121`. ✅
- `scripts/resample_timeseries.py` — referenced from README and
  `docs/source/users.guide/inputs.rst`. ✅
- `docs/technical.documentation/asset_modes.md`,
  `docs/technical.documentation/uncertainty_modelling.md` — referenced
  from README (lines 183-184) and `users.guide/inputs.rst:122`. ✅
- No `*_old.py`, `*.bak`, `_unused.py`, or commented-out massive
  blocks under `pvbess_opt/`. The cleanup track from earlier `0.8.x`
  releases (curtailment → max_injection rename) is complete; the only
  legacy surface still in code is the
  `_parse_curtailment_profile_sheet` shim (`io.py:873-885`), which
  the changelog says is retained "for one release" and accompanied by
  CI guard `tests/test_max_injection_profile.py::test_legacy_*`.

### F.3 — Requirements

`requirements/base.txt` declares `pandas`, `numpy`, `matplotlib`,
`openpyxl`, `pyomo` (with floor versions, no upper bound — matches the
comment block).

Imported by src code but **not declared:**
- `python-dateutil` (used as `from dateutil.relativedelta import relativedelta`,
  `pvbess_opt/lifetime.py:45`). Pandas drags it in transitively, but the
  implicit dependency is fragile. **P2.**

Declared in `requirements/*.txt` but no direct import:
- `pycodestyle`, `pyflakes` — lint-only, used by CI lint step
  (intentional; no source import needed).
- `sphinx-rtd-theme` — theme for the docs build (intentional).

No version inconsistencies between files (no version pinned in one
and not in another).

### F.4 — Pinned-version vs floating-version check

All declared deps use `>=` only, no upper bounds. Documented in
`requirements/base.txt:5-6`. No conflicting pins across files.

---

## Appendix — Proposed patches (NOT applied)

Each diff below is a sketch only — no file outside `AUDIT_REPORT.md`
and `audit_artifacts/` has been modified on this branch. Apply after
prioritisation.

### App.1 — Per-seed progress log (Phase D recommendation #1)

```diff
--- a/pvbess_opt/rolling_horizon.py
+++ b/pvbess_opt/rolling_horizon.py
@@ -355,6 +355,8 @@ def monte_carlo_rolling(
     seeds = [int(base_seed) + i for i in range(int(n_seeds))]
     rows: list[dict[str, Any]] = []
+    import time as _time
+    _t_start = _time.perf_counter()
     for seed in seeds:
         _full, kpis = rolling_horizon_dispatch(
             params, ts,
@@ -385,4 +387,15 @@ def monte_carlo_rolling(
             "bess_cycles_total": float(kpis.get("bess_equivalent_cycles_total", 0.0)),
             "foresight_gap_pct": gap,
         })
+        elapsed = _time.perf_counter() - _t_start
+        done = len(rows)
+        eta_s = elapsed / done * (len(seeds) - done) if done else 0.0
+        logger.info(
+            "monte_carlo_rolling: seed %d/%d done in %.1fs "
+            "(profit=%.0f EUR, gap=%.2f%%, ETA %.1f min)",
+            done, len(seeds), elapsed, profit, gap, eta_s / 60.0,
+        )
+        # Force flush so a long-running ensemble shows live progress.
+        for h in logger.handlers + logging.getLogger().handlers:
+            h.flush()
     return pd.DataFrame(rows)
```

### App.2 — Fix the stale `retail_inflation_pct = 2.0` fallback (Phase B.2)

```diff
--- a/pvbess_opt/economics.py
+++ b/pvbess_opt/economics.py
@@ -276,7 +276,7 @@ def build_yearly_cashflow(
     bess_deg_per_cycle = float(
         econ.get("bess_degradation_pct_per_cycle", 0.0) or 0.0
     ) / 100.0
-    retail_infl = float(econ.get("retail_inflation_pct", 2.0) or 0.0) / 100.0
+    retail_infl = float(econ.get("retail_inflation_pct", 0.0) or 0.0) / 100.0
     dam_infl = float(econ.get("dam_inflation_pct", 0.0) or 0.0) / 100.0
```

### App.3 — Fix the "8 audit invariants" docstring (Phase A.3 / B.7)

```diff
--- a/pvbess_opt/optimization.py
+++ b/pvbess_opt/optimization.py
@@ -753,7 +753,7 @@ def model_to_dataframe(

 # ---------------------------------------------------------------------------
-# 8 audit invariants — verify_dispatch_invariants
+# 9 audit invariants — verify_dispatch_invariants
 # ---------------------------------------------------------------------------


--- a/pvbess_opt/rolling_horizon.py
+++ b/pvbess_opt/rolling_horizon.py
@@ -399,5 +399,5 @@ def verify_window_invariants(
     res: pd.DataFrame, params: dict[str, Any],
 ) -> dict[str, float]:
-    """Run the 8 audit invariants on a single committed window."""
+    """Run the 9 audit invariants on a single committed window."""
     return verify_dispatch_invariants(res, params, mode=str(params.get("mode", "vnb")))
```

### App.4 — Document `infinity` token + fix README cap-token list (Phase C.2)

```diff
--- a/README.md
+++ b/README.md
@@ -45,7 +45,7 @@ new features are left disabled:
 * **Unlimited grid export** — `p_grid_export_max_kw` may be left empty
-  or set to `inf` / `unlimited` / `disabled` / `none` to remove the
+  or set to `inf` / `infinity` / `unlimited` / `disabled` / `none` to remove the
   export cap.
```

### App.5 — Fix the `mip_formulation.rst` `e_cap` claim (Phase B.7)

```diff
--- a/docs/source/technical.documentation/mip_formulation.rst
+++ b/docs/source/technical.documentation/mip_formulation.rst
@@ -11,9 +11,8 @@ Decision variables (per timestep, kWh)
 * ``bess_dis_load[t]``, ``bess_dis_grid[t]`` — BESS discharge.
 * ``grid_to_load[t]``, ``grid_to_bess[t]`` — grid-bound flows.
 * ``soc[t]`` — state-of-charge (kWh).
-* ``e_cap`` — BESS energy capacity (single decision variable).
 * ``y_charge[t]``, ``y_dis[t]``, ``y_grid_io[t]``, ``z_pv_active[t]``
   — binary indicators.

+The BESS energy capacity ``e_cap`` is a fixed parameter pinned to
+``bess_capacity_kwh`` from the workbook — no longer a decision variable.
+
```

### App.6 — Declare `python-dateutil` in `requirements/base.txt`

```diff
--- a/requirements/base.txt
+++ b/requirements/base.txt
@@ -10,3 +10,4 @@ numpy>=1.24
 matplotlib>=3.10
 openpyxl>=3.1
 pyomo>=6.6
+python-dateutil>=2.8
```

### App.7 — Fix the broken Sphinx `:doc:` xref in `lifetime.py` (Phase B.7)

```diff
--- a/pvbess_opt/lifetime.py
+++ b/pvbess_opt/lifetime.py
@@ -34,7 +34,7 @@ Reconciliation invariant

     sum(pv_kwh in lifetime[y]) / sum(pv_kwh in Year 1) ≈ pv_factor[y]

-within 0.1 % for every year.  See
-:doc:`technical.documentation/lifetime_scaling` for the derivation.
+within 0.1 % for every year.  See the lifetime-scaling note under
+``docs/source/technical.documentation/lifetime_scaling.rst`` for the derivation.
 """
```

(Sphinx `:doc:` paths are relative to `docs/source/`, but autodoc
pulls this docstring out of a module that lives at the repo root, so
the xref doesn't resolve. Either rewrite as a plain reference or use a
fully-qualified `:doc:` from `docs/source/`.)

---

End of report.
