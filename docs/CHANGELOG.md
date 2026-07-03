# Changelog

## 1.0.0 (2026-07-03)

Production release.

### Changed (breaking)

- BESS CAPEX is priced per kWh of nameplate energy capacity:
  `capex_bess_eur_per_kw` is renamed to `capex_bess_eur_per_kwh`
  (default 250, full installed cost, Lazard band 215-315 EUR/kWh) and
  multiplies `bess_capacity_kwh` instead of `bess_power_kw` in the
  Year-0 CAPEX and the LCOS numerator; the replacement charge inherits
  the basis. `devex_bess_eur_per_kw` and `opex_bess_eur_per_kw` stay on
  the power basis. Workbooks and configs carrying the legacy key are
  rejected with a conversion message (value_per_kwh = value_per_kw x
  bess_power_kw / bess_capacity_kwh); `scripts/polish_input_workbook.py`
  migrates them automatically.
- `bess_replacement_year` has three-way semantics resolved identically
  across the cashflow, the LCOS numerator, the lifetime projection and
  the degradation report: a positive integer N schedules the
  replacement in year N (the SOH threshold is ignored); a blank cell or
  `auto` replaces in the first project year state-of-health falls to
  `bess_eol_soh_pct`, with the replacement CAPEX charged in the
  cashflow; 0 never replaces. A blank cell no longer collapses silently
  into 0. Only the first threshold crossing is charged; a second
  crossing logs a prominent warning. `SUMMARY.md` and the
  economic_assumptions sheet report the effective year and its source.
- New default assumptions: `efficiency_charge` and
  `efficiency_discharge` move from 0.97 to 0.95 (round-trip 0.9025) and
  `bess_wear_cost_eur_per_mwh` from 0 to 10 (a dispatch shadow price,
  never charged in the cashflow).

### Fixed

- Rolling horizon: SOC carry-over across fully committed windows now
  includes the expected balancing-activation drift (shared helper
  mirroring the model's terminal SOC expression) and is clamped into
  the SOC envelope; the year-close SOC target is relaxed by a heavily
  penalised shortfall variable so a physically unreachable target
  (winter year end, surplus-only charging) no longer aborts the run;
  non-divisible window/cadence combinations raise instead of silently
  truncating the horizon; a runtime guard warns (or errors under
  `--strict`) when any Monte Carlo seed beats the perfect-foresight
  bound beyond solver tolerance.
- Lifetime projection: `grid_export_total_kwh` is rebuilt from its
  scaled components so the identity export_total = pv_to_grid +
  bess_dis_grid holds in every projected year when the PV and BESS fade
  curves diverge.
- Cycle KPIs: `bess_equivalent_cycles_total` and
  `bess_equivalent_cycles_per_day` derate with unavailability alongside
  `bess_total_discharge_mwh`, so headline cycles reconcile with
  `bess_lifetime_cycles / years`.
- SOH trajectory plot: fixed 0-100 percentage axis with headroom and
  integer year ticks.
- Rolling-horizon distribution plot: a degenerate ensemble (every seed
  on the same profit, e.g. a PV-only plant) renders a dedicated layout
  with a readable window, whole-euro ticks, a collapsed legend and an
  explanatory annotation instead of a single full-height bar with
  sub-euro tick labels.

### Added

- Validation of the self-consumption foresight gap: at mip_gap 1e-5 the
  5-seed median gap is 0.464 % against a 2,849,785 EUR perfect-foresight
  benchmark; the sigma-zero run isolates a 0.324 % horizon-truncation
  component, documented in the rolling-horizon guide.
- Cost-accounting invariant tests: the battery wear cost enters the
  optimization objective only, the replacement CAPEX books exactly once
  in the effective replacement year of the cashflow, and no KPI
  subtracts wear again.
- A Sphinx docs job in CI (warnings are errors).
- `--strict` also promotes energy-balance residuals above the energy
  tolerance to hard errors.

### Removed

- The one-off audit and readiness process documents; audit-flavored
  test files are renamed to behavior names. The README and every
  shipped guide are rewritten as plain, factual product documentation,
  and the gallery figures are regenerated for the new economics.

## 0.9.0 (2026-06-27)

First public release.  No prior versions have shipped; no
compatibility surface is maintained.

### Added (revenue-stacking economics, independent audit)

- **Balancing-aggregator (BSP) fee.** New optional
  `balancing_aggregator_fee_pct_revenue` key on the `economics` sheet
  (range `[0, 100]`, **default 0.0**): a separate route-to-market fee on
  **gross** balancing revenue for assets that participate through an
  aggregator/BSP that keeps a share.  It mirrors the energy
  `aggregator_fee_pct_revenue` across all three config surfaces, surfaces
  as a signed `balancing_aggregator_fee_eur` column on the yearly /
  quarterly / monthly cashflow, is folded into NPV / IRR / ROI / payback
  (the DCF consumes NET balancing revenue), is shown as its own deduction
  on the yearly revenue stack and steps the BESS revenue waterfall total
  down, and is excluded from LCOE/LCOS.  Gross balancing KPIs are
  unchanged; the fee and net are exposed as
  `lifetime_bm_aggregator_fee_total_eur` /
  `lifetime_bm_revenue_net_total_eur`.  The default 0.0 keeps every
  existing output bit-identical.  PPA still carries neither fee.
- **Self-consumption balancing guardrail.** A single load/resolve-time
  warning is emitted when balancing-market participation runs under
  `self_consumption` with a BESS present, noting that revenue stacking in
  practice needs aggregator/BSP routing and TSO prequalification.
  Balancing remains valid in **both** modes (opt-in, off by default); no
  mode gate is introduced.
- **Fee range validation.** Both revenue-fee percentages
  (`aggregator_fee_pct_revenue` and the new
  `balancing_aggregator_fee_pct_revenue`) are now range-checked to
  `[0, 100]` and rejected loudly when out of range, instead of the energy
  fee being silently clamped.  This is consistent with `gearing_pct`.

### Production-readiness hardening

- **Input validation.** `validate_workbook_params` now reads the PV/BESS
  CAPEX/OPEX keys from the sheets they actually live on (they were read
  from `economics`, so a negative CAPEX was silently accepted and flipped
  the Year-0 outflow to an inflow), and its non-negative coverage extends
  to DEVEX, the site lump sums, the degradation / cycle-fade percentages,
  the replacement year and cost, and the grid-CO2 intensity; `gearing_pct`
  and `grid_co2_annual_decline_pct` are range-checked to `[0, 100]`.
- **Fail loud on bad enums.** An invalid value for any known enum (not
  just `mode`) now raises instead of silently falling back to the default;
  an unknown `ppa_settlement` is rejected; the YAML/JSON config surface
  validates and normalises through the same per-key parser as the
  workbook.
- **CLI / scenarios.** `--mode` is now applied to `--scenarios` batches
  (it was ignored); `--config` shadowing a positional workbook and
  `--scenarios` overriding an enabled `sizing` sheet now warn.
- **Domain fixes.** Widened the IRR bisection bracket to `-0.999` to match
  the spec; guarded the oversizing break-even against duplicate-capacity
  divide-by-zero; `build_model` raises when a self-consumption run is
  missing `load_kwh`.
- **Tooling / docs.** `scripts/polish_input_workbook.py` runs as the
  documented standalone command (added the repo-root `sys.path`
  bootstrap); the design docs' KPI-key names, worked-example IRR and
  sensitivity-frame columns were corrected to match the code; the README
  documents balancing as a both-mode opt-in feature, the per-source
  max-injection sheets, the presentation knobs and the emissions plots.
- **PNG figure export** for the README result gallery.

### Changed (financial reporting consistency)

- New `initial_investment_eur` KPI: the Year-0 CAPEX + DEVEX outlay
  (matching the Year-0 bar in the plots).  The lifecycle
  `total_capex_eur` / `total_capex_devex_eur` are now documented as
  replacement-inclusive.
- `roi_pct` switched to the standard total-return form: operating net
  cashflow (Years 1..N) over `|initial_investment_eur|`.  Previously
  the denominator was Year-0 CAPEX alone, excluding DEVEX.  Reported
  ROI values change accordingly.
- The NPV/IRR tornado CAPEX driver value now reports the Year-0 outlay
  (the perturbation still scales the replacement CAPEX row by the same
  factor), so the bar-end EUR labels agree with the other charts.
- `plot_yearly_cashflow_bars` / `plot_npv_waterfall` stack every
  negative segment cumulatively; previously a BESS replacement year
  painted the CAPEX bar over the OPEX bar, hiding it.
- EUR axis ticks escalate decimal precision automatically on narrow
  axes; the rolling-horizon Monte-Carlo profit histogram no longer
  renders every tick as the same rounded label.

### Fixed (pre-publication audit)

- Monthly / quarterly cashflow discounting now uses the end-of-month
  convention `t = (y - 1) + m/12`; previously every month was
  discounted 11/12 of a year too late, so the monthly DCF summed below
  the yearly DCF.
- `pv_production_mwh` on the monthly / quarterly cashflow sheets is now
  availability-derated, reconciling with `kpis_year1` and the
  `lifetime_dispatch_yearly` sheet.
- `read_economic_params` now merges the `balancing` sheet, so the
  workbook's `bm_inflation_pct` (default 2 %/yr) actually indexes the
  balancing revenue lines in the lifetime cashflow (it was silently 0).
- The advertised `00_summary/SUMMARY.md` digest is now written on every
  run.
- Sequence-valued KPI entries are no longer crammed into single cells
  of the `kpis_year1` / `financial_kpis` sheets; the per-year balancing
  revenue lives in `cashflow_yearly['balancing_revenue_eur']`.
- The `[LCOE/LCOS audit]` log line is emitted once per run instead of
  once per sensitivity perturbation.
- Documentation corrections: package docstring no longer claims the PV
  column is rescaled to the nameplate; README points at
  `choose_solver` / `monte_carlo_rolling`; the economics guide states
  the per-origin (PV vs BESS) revenue degradation split; the BESS CAPEX
  note spells out the full-installed-cost basis required for
  Lazard-comparable LCOS.

### Capabilities

- Co-optimised dispatch of PV + BESS in two modes
  (`self_consumption` and `merchant`) and three asset configurations
  (`hybrid`, `pv_only`, `bess_only`).
- PV input from a column or a location.  The `timeseries` sheet has a
  single `pv_kwh` column and the `pv` sheet carries `pv_source`
  (`auto` | `file` | `pvgis`) plus the PVGIS coordinates / geometry
  (`latitude`, `longitude`, `tilt`, `azimuth`, `losses_pct`,
  `weather_year`, `timeseries_path`).  Fill `pv_kwh` to use it verbatim
  (absolute kWh per step; `pv_nameplate_kwp` is metadata), or clear it and
  set `latitude` / `longitude` to fetch the profile from PVGIS.  One
  presence-aware rule resolves the
  source for the Excel workbook and a YAML / JSON config alike; the
  legacy `pv_kwh_override` column is deprecated but still read as a
  fallback when `pv_kwh` is empty.
- Stochastic balancing market participation across FCR, aFRR, and
  mFRR with per-product capacity reservation, expected-value MILP,
  and Monte Carlo realisation.
- Pay-as-produced PPA contract engine on a configurable share of PV
  export: physical (sleeved) or two-way-CfD settlement, the
  PPA-adjusted dispatch price `(1-s)·DAM + s·strike`, contract-term
  cutoff with post-term reversion of the covered DAM value, its own
  inflation index, a dedicated `ppa` workbook sheet, a `PPA price`
  tornado driver, and bit-identical disabled runs.
- Project finance pipeline: lifetime cashflow, NPV, IRR, ROI, BCR,
  LCOE, LCOS, payback.  Inflation indexation per revenue / cost
  stream.  Optional debt/equity leverage layer (gearing, annuity or
  linear amortisation, equity IRR, minimum DSCR, debt schedule
  sheet).
- Grid-emissions accounting and 24/7 CFE scoring from a grid CO2
  intensity (scalar with annual decline, or a per-step timeseries
  column); off by default.
- Optional per-source max-injection sub-caps
  (`max_injection_profile_pv` / `max_injection_profile_bess` sheets)
  on top of the combined hour-of-day injection cap.
- IEEE-styled PDF reporting with full plotting suite, including
  per-product balancing revenue breakdowns (yearly stack, per-month
  BESS revenue, BESS revenue waterfall, capacity-vs-activation split).
- Rolling-horizon Monte Carlo with log-normal forecast noise on DAM,
  PV, load, and balancing prices.
- One-at-a-time sensitivity tornado.
- Capacity sizing sweep with efficient frontier, marginal value of
  storage and oversizing break-even, driven from the workbook's
  columnar `sizing` sheet (gated by an `enabled` toggle) or a `sizing:`
  config block.
- Batch scenario comparison (inheritance, per-section overrides, CAPEX
  multiplier, balancing on/off) driven from the workbook's tidy
  `scenarios` sheet (gated by an `enabled` toggle) or a `--scenarios`
  YAML / JSON file.  Unknown override targets fail fast with a
  did-you-mean hint; the three configuration surfaces (workbook,
  YAML/JSON config, scenario dotted targets) are regression-locked
  mirrors of one another.
- Publication-grade domain design documents under `docs/`
  (`self_consumption_design.md`, `merchant_design.md`,
  `balancing_market_design.md`, `ppa_design.md`,
  `economics_design.md`, `uncertainty_design.md`) sharing one
  template and one notation table (`docs/README.md`), every numbered
  equation mapped to its implementing symbol.

### Solver

- Pyomo + HiGHS (default).
