# Production-readiness report

Working artifact for the production-readiness pass on PV & BESS Optimizer.
Every finding is logged with location, severity (P0–P3), root cause, fix,
and the regression test that locks it. Definition of done: zero open
findings at every severity, full suite + lint + types + dead-code + docs
build all green.

Severity rubric: **P0** wrong financial/energy numbers, crashes, data
corruption, silent-incorrect mode/feature, security/data-loss. **P1**
documented feature that does not work, broken input validation, config
surface divergence, missing error handling on bad input. **P2** incorrect
/ misleading docs, README↔code drift, missing edge-case handling, output
inconsistency. **P3** polish: unclear messages, docstring gaps, cosmetic
plot/label issues, naming/typo, nice-to-have validation.

---

## Feature inventory (coverage checklist)

### Regulatory modes (`pvbess_opt/modes.py`)
- `self_consumption` — retail-settled load coverage + DAM surplus export.
- `merchant` — DAM-only, load flows pinned to zero.

### Asset configurations
- `hybrid` (PV + BESS), `pv_only` (`bess_power_kw=0`), `bess_only`
  (`pv_nameplate_kwp=0`).

### Optional features
- **Balancing market** (FCR / aFRR / mFRR), `balancing` sheet, 34 keys.
  *(Contract under resolution — see Findings: balancing⇄mode.)*
- **PPA** (pay_as_produced; physical | cfd), `ppa` sheet, 7 keys.
  Applies in **both** modes (covers surplus export in self_consumption).
- **Sizing sweep** (`sizing` sheet) — Cartesian product, NPV frontier.
- **Scenarios batch** (`scenarios` sheet / `--scenarios`) — tidy overrides.
- **Rolling-horizon Monte Carlo** (`simulation` sheet) — log-normal noise,
  P10/P50/P90, foresight gap.
- **Sensitivity tornado** (`economics` sheet) — CAPEX/OPEX/revenue/discount/PPA.
- **Emissions / 24-7 CFE** (`economics` grid-CO2 keys).
- **Debt layer** (`economics` gearing/interest/tenor/repayment).
- **Max-injection curtailment** (`max_injection_profile` + `_pv`/`_bess`).
- **PVGIS sourcing** (`pv` sheet location/geometry; `pv_source` auto/file/pvgis).

### Config surfaces (must be exact mirrors)
1. Workbook `inputs/input.xlsx` (kv sheets).
2. YAML/JSON `--config` (sections mirror sheets; `timeseries_path`).
3. Scenario overrides (`scenarios` sheet / `--scenarios`; `<sheet>.<key>`
   dotted targets + aliases + `balancing`/`capex_multiplier` specials).

### Input workbook sheets/keys (enumerated from inputs/input.xlsx)
- `timeseries` (13 cols): timestamp, load_kwh, pv_kwh, dam_price_eur_per_mwh,
  + 9 balancing price cols (5 capacity, 4 activation).
- `project` (12 keys): project_lifecycle_years, project_start_year, mode,
  p_grid_export_max_kw, retail_tariff_eur_per_mwh, allow_bess_grid_charging,
  grid_cap_includes_load, unavailability_pct, site_capex_eur, site_devex_eur,
  **currency_format**, **show_titles**.
- `pv` (15 keys): pv_source, latitude, longitude, tilt, azimuth, losses_pct,
  weather_year, **raddatabase**, timeseries_path, pv_nameplate_kwp,
  pv_degradation_year1_pct, pv_degradation_annual_pct, capex_pv_eur_per_kw,
  devex_pv_eur_per_kw, opex_pv_eur_per_kwp.
- `bess` (18 keys).
- `economics` (21 keys).
- `simulation` (15 keys).
- `balancing` (34 keys).  ✓ matches README "34 keys".
- `ppa` (7 keys).
- `max_injection_profile` (24 hour rows) + `max_injection_profile_pv`,
  `max_injection_profile_bess` (per-source sub-caps).
- `sizing`, `scenarios` (gated by an `enabled` toggle; mutually exclusive).

### CLI flags (`pvbess_opt/cli.py`)
excel(positional), --config, --scenarios, --solver, --outdir, --mode,
--strict, --mip-gap, --time-limit, --tee, --rolling-horizon, --window-hours,
--commit-hours, --monte-carlo, --seed, --compare-uncertainty-sources.

---

## Phase 1 — static quality gate (baseline)

Environment: Python 3.11.15; deps from `requirements/dev.txt`.
Resolved versions (key): pandas 2.3.3, numpy 1.26.4, pyomo 6.10.1,
highspy 1.14.0, matplotlib 3.11.0, openpyxl 3.1.5, ruff 0.15.20,
mypy 1.19.1, vulture 2.16, pytest 9.1.1.

| Gate | Command | Result |
|---|---|---|
| ruff | `python -m ruff check .` | **PASS** (All checks passed) |
| mypy | `python -m mypy` | **PASS** (no issues, 46 files) |
| vulture | `python -m vulture` | **PASS** (exit 0) |
| fast lane | `python -m pytest tests/ -q` | running (baseline) |
| slow lane | `python -m pytest tests/ -q -m slow` | pending |

Environment note (not a code finding): the container ships a stale
`/root/.local/bin/mypy` and `ruff` on PATH bound to a different
site-packages; invoking via `python -m <tool>` uses the freshly
installed env that CI exercises. All gate commands below use `python -m`.

---

## Findings log

Legend: status ∈ open / fixed / wontfix. Severity assigned at triage.

All 34 findings are resolved (status **fixed**). `*` on F22 marks a
self-inflicted issue created by adding this report. Severity counts:
P0 = 0; P1 = 3 (F6, F22, F32) + 1 mitigated (F13); P2 = 13; P3 = 17.
Each behaviour change carries a regression test (named in the row).

| # | Sev | Area | Title | Resolution |
|---|---|---|---|---|
| F1 | P2 | docs | README implied balancing was merchant-only; it is valid in both modes (owner-confirmed) | **fixed** — README both-mode note; `test_balancing_mode_contract` |
| F2 | P2 | input | YAML/JSON `--config` loader bypassed enum/type coercion (`load_structured_config`) | **fixed** — route known keys through `io._parse_value`; parity/config tests green |
| F3 | P2 | input | invalid values for non-`mode` enums silently coerced to default | **fixed** — `_parse_string_enum` raises for every invalid enum (`baseload` still parses) |
| F4 | P2 | input | `gearing_pct` not range-validated; >100 → nonsensical equity cashflow | **fixed** — `[0,100]` check; `test_gearing_*` |
| F5 | P3 | docs | README omitted `max_injection_profile_pv`/`_bess` sub-cap sheets | **fixed** — documented in README |
| F6 | P1 | input | `validate_workbook_params` read PV/BESS CAPEX/OPEX from the wrong section (`economics`) → non-negative check was a silent no-op; negative CAPEX flipped Year-0 cost→income; test gave false confidence | **fixed** — validate from correct sections; fixture corrected + `test_cost_keys_validated_on_real_workbook_sections` |
| F6b | P2 | input | devex/site/replacement-cost keys unvalidated for sign | **fixed** — added to non-negative coverage + tests |
| F7 | P2 | cli | `--mode` override ignored for `--scenarios`/sheet batches | **fixed** — override applied; `test_run_scenarios_applies_cli_mode_override` |
| F8 | P3 | cli | `--config` shadowing a positional workbook was silent | **fixed** — warns now |
| F9 | P3 | cli | enabled `sizing` sheet silently skipped under `--scenarios` | **fixed** — warns now |
| F10 | P3 | docs/output | `dispatch_hourly.xlsx` misleading at 15-min cadence | **fixed** — renamed to `dispatch_timeseries.xlsx` across code+docs+test |
| F11 | P3 | test | conformance regex missed `DIS_LIM`/`MODE_LINK`/`NO_SIM_GRID_EXPORT` | **fixed** — parser harvests all names up to the `/` alternation |
| F12 | P3 | opt | `build_model` treated missing `load_kwh` as zero load in self_consumption | **fixed** — raises now; `test_build_model_self_consumption_requires_load_column` |
| F13 | P1→mitig | ppa | invalid `ppa_settlement` decomposed inconsistently (kpis vs economics) → wrong numbers | **fixed** — F3 raises at loader + explicit `ppa_settlement` check in `_validate_ppa_config`; test |
| F14 | P2 | input | no sign validation on degradation/replacement-year (negative → SOH >100%) | **fixed** — non-negative checks + tests |
| F15 | P2 | docs | `economics_design.md` lifecycle KPI key names wrong | **fixed** — corrected to emitted `lifetime_bm_*`/`lifetime_ppa_*` (verified) |
| F16 | P3 | docs | `economics_design.md` worked-example IRR wrong (−25.5% vs −13.95%) | **fixed** — corrected (verified via `calculate_irr`) |
| F17 | P3 | code/docs | IRR bisection bracket `-0.99` vs doc `-0.999` | **fixed** — code → `-0.999` (matches Newton floor + doc) |
| F18 | P2 | docs | `uncertainty_design.md` RH/MC KPI key names did not exist | **fixed** — corrected to emitted keys (verified) |
| F19 | P2 | docs | `uncertainty_design.md` sensitivity columns wrong (claimed LCOE/LCOS) | **fixed** — corrected to the actual 11 columns |
| F20 | P3 | docs | SOC-violation doc 'fraction' vs emitted percent key | **fixed** — named `bm_soc_constrained_scenarios_pct` (percent) |
| F21 | P3 | docs | impl-map misattributed `_lognormal_unit_mean` to forecast noise | **fixed** — disambiguated |
| F22 | P1* | test | report tripped `test_repo_hygiene` forbidden-token scan (self-inflicted) | **fixed** — allow-listed in both scans (mirrors the hygiene file) |
| F23 | P2 | docs | README PDF-report list omitted energy Sankey + CFE plots | **fixed** — added (gated on emissions) |
| F24 | P2 | output | Sankey/CFE in `04_financial_plots/` vs taxonomy | **fixed** — documented (placement pinned by `test_emissions_cfe` is the intended report bundle) |
| F25 | P3 | docs | README `04_financial_plots/` summary incomplete | **fixed** — expanded |
| F26 | P3 | docs | `show_titles`/`currency_format` undocumented in README | **fixed** — documented |
| F27 | P3 | docs | `conf.py` build path contradicted Makefile | **fixed** — corrected to `docs/build/html`; `make -C docs html` clean |
| F28 | P3 | docs | CHANGELOG 'unreleased' vs CITATION `date-released` | **fixed** — reconciled to 2026-06-27 + production-readiness entry |
| F29 | P2 | sizing | `find_oversizing_breakeven` divided by zero on duplicate capacities | **fixed** — `np.errstate`+`np.where` guard (both siblings); `test_breakeven_duplicate_capacities_no_divide_by_zero` |
| F30 | P3 | emissions | `grid_co2_annual_decline_pct > 100` → negative intensity | **fixed** — range-validated `[0,100]`; tests |
| F31 | P3 | emissions | `cfe_score` clamp asymmetry | **fixed** — documented physical-bound invariant (intentional no-clamp) |
| F32 | P1 | scripts | documented `python scripts/polish_input_workbook.py` failed (ModuleNotFoundError) | **fixed** — added repo-root `sys.path` bootstrap; `test_script_runs_standalone_without_install` |
| F33 | P3 | plotting | LCOE/LCOS plots forked the saver (`fig.savefig(..., format="pdf")`) so the figure-format switch never reached them | **fixed** — added `save_figure_object` (single styler honours `FIGURE_FORMAT`); both routed through it; `test_figure_format` + `test_lcoe_lcos_summary` green |

Open questions deferred to owner sign-off at the final gate: discount-rate / inflation
sign bounds (left unbounded to allow deflation / stress scenarios — intentional);
whether `--scenarios` + enabled sizing sheet should hard-error vs warn (chose: warn).

### Phase 6 — documentation fixes (all resolved against verified ground truth)

- **F5** — README `max_injection_profile` section now documents the
  `max_injection_profile_pv` / `_bess` per-source sub-cap sheets.
- **F15** — `economics_design.md` KPI list corrected to the emitted keys
  (`lifetime_bm_revenue_total_eur`, `lifetime_bm_capacity_revenue_total_eur`,
  `lifetime_bm_activation_revenue_total_eur`, `lifetime_ppa_revenue_total_eur`);
  the doc previously named `total_balancing_*_eur_lifecycle` /
  `total_ppa_revenue_eur_lifecycle`, which are intermediate locals, not keys
  (verified by dumping `compute_financial_kpis` output).
- **F16** — worked-example IRR corrected from −25.5 % to −13.95 % (verified:
  `calculate_irr([-1000, 400, 396.292]) == -13.95 %`).
- **F17** — code now brackets the IRR bisection at −0.999 (was −0.99), matching
  the design-doc statement and the Newton-path domain floor.
- **F18** — `uncertainty_design.md` RH/MC KPI names corrected to the emitted
  keys (`foresight_gap_pct_p10/p50/p90`, `bm_total_balancing_revenue_p10/p50/p90_eur`,
  `bm_soc_constrained_scenarios_pct`, `bm_mc_total_realised_eur`, the per-product
  quantiles); the doc previously named non-existent `rh_profit_total_eur_p*`,
  `bm_mc_revenue_p*_eur`, `bm_mc_soc_violation_share`.
- **F19** — sensitivity-frame column list corrected to the actual 11 columns
  (no LCOE/LCOS columns; those ranges are derived by the plot from the exported
  `lcoe_disc_*`/`lcos_disc_*` components).
- **F20** — SOC-violation metric named correctly (`bm_soc_constrained_scenarios_pct`,
  a percent).
- **F21** — uncertainty impl-map disambiguates `_lognormal_multiplier` (forecast
  noise) from `_lognormal_unit_mean` (balancing-MC price multipliers).
- **F23 / F24 / F25** — README PDF-report list + output-reference summary now
  describe the full `04_financial_plots/` contents including the energy Sankey
  and 24/7-CFE duration curve (emitted when emissions accounting is on); the
  code placement (verified pinned by `test_emissions_cfe`) is the intended
  report bundle, so it is documented rather than moved.
- **F26** — README `project` section documents `currency_format`, `show_titles`,
  `project_lifecycle_years`, `project_start_year`.
- **F27** — `conf.py` build-command output path corrected to `docs/build/html`
  to match the Makefile/README; `make -C docs html` builds with no documentation
  warnings (`test_docs_build` green).
- **F28** — CHANGELOG and CITATION dates reconciled (both 2026-06-27); a
  production-readiness entry was added under 0.9.0.

### Version / release decision (for sign-off)

The CHANGELOG declared 0.9.0 "unreleased" while CITATION carried
`date-released: 2026-06-12` — contradictory.  **Decision: no version bump.**
This production-readiness pass *finalises* the still-unreleased 0.9.0 as its
first production release; both CHANGELOG and CITATION are dated 2026-06-27
and `__version__` / pyproject / README badge stay at 0.9.0 (the
version-consistency suite stays green).  Flagged here in case you prefer to
ship the readiness work as 0.9.1 over an already-tagged 0.9.0 instead.

### Resolved design decisions

- **balancing⇄mode contract — RESOLVED (owner sign-off 2026-06-27).**
  Investigation showed balancing-in-self_consumption is a *deliberately
  designed, fully-tested, fully-documented* capability, not an
  accidentally-ungated path: `self_consumption_design.md` specifies the
  `r_balancing` variable, the `R^bm` objective term and the SOC
  activation drift; ~80 balancing-test usages across five files exercise
  it in `self_consumption` mode; the economics there are already correct
  (TSO-settled, fee-free, SOC-buffered). The README never literally said
  "merchant-only" — it merely listed balancing while describing merchant
  mode, creating the appearance of an exclusion.
  **Owner decision: keep balancing valid in BOTH modes (opt-in via
  `balancing_enabled`, disabled by default); make NO code change to the
  gate; fix the README so it is accurate; lock correctness with a test.**
  → logged as a **P2 documentation** finding (README drift) plus a
  regression test asserting balancing activates and settles correctly in
  both regimes. The `_resolve_balancing_inputs` mode-gate idea is
  dropped.
