# Changelog

## 1.0.0 (2026-07-06)

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

- Rolling-horizon window solves are decoupled from the benchmark's
  requested `mip_gap`: they floor their gap at `1e-3` (never tighter,
  even when the benchmark is solved to `1e-5` for a publication) and
  cap their per-solve time.  A 48 h window finds its near-optimal
  incumbent in under a second but can then spend minutes merely
  *proving* a tight gap that does not change the committed schedule,
  and an ensemble runs thousands of window solves; since each window is
  re-priced against the noise-free actuals only the schedule matters,
  not the proof.  For default runs (benchmark gap `1e-3`) the floor is
  a no-op and windows are unchanged.
- The run records the benchmark's PROVEN optimality gap, not just the
  requested one: `--mip-gap` is a target that competes with
  `--time-limit`, and on a hard year-scale grid-charging MILP the time
  limit usually binds first, so the solver stops looser than requested
  (e.g. asking for `1e-5` but proving `5e-4`).  A new
  `pf_benchmark_gap_achieved` KPI captures the relative gap the solver
  actually proved (`|bound − incumbent| / |incumbent|`, matching the
  solver's own printed gap), alongside the existing
  `pf_benchmark_mip_gap` (requested).  `SUMMARY.md` gains a
  "Rolling-horizon foresight" section rendering both plus the foresight
  percentiles.  The achieved gap is the number a publication should
  quote as the benchmark's certified optimality; it threads off the
  solved dispatch frame's metadata, so no public return signature
  changed.  The `[milp-solve] done` log line now also reports it.
- Gurobi solves carry a memory-safety default (`NodefileStart` 8 GB
  with node files under the system temp dir): a branch-and-bound tree
  that outgrows RAM spills to disk instead of the OS killing the
  process mid-run.  Node files are transparent to the search — below
  the threshold the parameter is dormant, above it only node storage
  moves to disk — so results are identical either way.
- Solver resolution is provenance-safe: requesting a solver that is
  not available (e.g. `--solver gurobi` without `gurobipy` or a
  licence) stops the run with an error listing the installed
  alternatives, instead of silently falling back to HiGHS/CBC.  The
  solver identity is part of the results' provenance (run log,
  SUMMARY.md, any solver statement in a publication), so it is never
  substituted quietly.
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
  the shared project-window year ticks.
- Rolling-horizon distribution plot: a degenerate ensemble (every seed
  on the same profit, e.g. a PV-only plant) renders a dedicated layout
  with a readable window, whole-euro ticks and a collapsed legend
  instead of a single full-height bar with sub-euro tick labels.
- Publication-ready figure text: legend entries carry series names
  only (no computed values; the numbers live in SUMMARY.md and the
  results workbook), explanatory annotation boxes are removed, and the
  placeholder messages use parentheses instead of em dashes.
- The BESS revenue waterfall and by-month views carry the battery's
  exact shares of both route-to-market fees (flat percentages of the
  gross DAM export and gross balancing revenue), so their totals are
  net of the same deductions the cashflow applies.
- The energy-flow diagram is rebuilt as a layered Year-1 Sankey using
  the canonical flow colours and renders for every run in both modes
  under the energy plots (it previously required emissions inputs and
  used a monochrome layout).
- The energy aggregator fee renders as "Energy aggregator fee" in the
  revenue stack, waterfall and by-month legends, symmetric with
  "Balancing aggregator fee" so the two per-stream route-to-market
  deductions read unambiguously side by side.
- Every per-year figure (cumulative cashflow, yearly bars, NPV
  waterfall, payback, revenue stack, lifetime cycles, lifetime
  summary, SOH) labels EVERY project year, rotated and right-anchored
  like the month and date axes, so no tick lands outside the project
  window and the reader never interpolates between sparse ticks.
  Each plot's window hugs its own data: the cashflow views open at
  Year 0 (the CAPEX year), the operational views (SOH, revenue stack,
  cycles, lifetime summary) open at Year 1 with no empty Year-0 slot.
- The cumulative discounted cash-flow curve draws solid like its
  undiscounted companion, distinguished by colour only (dashes remain
  reserved for reference lines and markers).
- The financial monthly figures (Year-1 monthly cashflow, BESS revenue
  by month, seasonal boxplot) all take the house MM-YYYY month labels
  of the energy plots' month-of-year axes (rotated, right-anchored),
  through one shared month-axis helper.
- One legend placement for the whole report: every legend hangs
  BELOW its axes, horizontally centered (the energy plots' long-held
  convention, now universal across the financial, uncertainty and
  diagnostic figures).  The in-plot legend headroom system is removed,
  so the full canvas belongs to the data; the below placement is
  measured: the column count narrows until the legend fits the
  figure width and the legend drops until it clears the x tick labels
  and the axis label.
- The energy-flow diagram's curtailment sink takes the canonical
  "Curtailed PV" label of the energy plots, and its single-asset
  regimes are locked by tests: a PV-only project draws no battery
  column or losses sink, a BESS-only project no PV column or
  curtailment sink.
- Legends with up to four entries sit on one row (matching the
  LCOE / LCOS strips) in both legend helpers, so small legends read
  flat everywhere.
- One font size per text role across every figure, locked by a static
  test: ticks, axis labels and titles from the IEEE preset, legends at
  7 pt, in-plot annotations and node labels at 7 pt, empty-input
  placeholders at 10 pt.
- Every figure saves at its exact declared canvas (7 in wide across
  the report, legend included) instead of a per-figure tight crop, so
  figures placed side by side scale identically and their fonts read
  the same apparent size; the sweep asserts the legend sits fully
  inside the canvas.
- The BESS revenue waterfall omits zero-value product steps (a
  no-balancing run previously showed five flat EUR-0 steps), exactly
  as it omits zero fees.
- The README gallery carries the SAME figure set for both business
  models (energy flow, representative-day dispatch, revenue stack,
  BESS waterfall, monthly and cumulative cashflow, NPV waterfall and
  tornado, LCOE / LCOS bands, SOH, and a rolling-horizon foresight
  distribution from an 8-seed Monte Carlo run of each scenario), so
  the two modes compare figure by figure; a Read the Docs
  configuration (.readthedocs.yaml) builds the Sphinx docs from
  docs/source/conf.py.
- The measured legend system pins the y-view across its tick prunes: a
  locator tick emitted below the visible minimum could re-expand the
  autoscaled view AFTER the legend was measured clear, shifting the
  bars under the legend for some project start dates.  A
  cross-start-date sweep test now renders every per-year and monthly
  figure over a grid of start years and horizon lengths, asserting the
  shared tick grid, the data-hugging windows, the MM-YYYY month labels
  and the measured legend clearance for any project window.

### Changed (case-study re-parameterisation)

- The shipped ``inputs/input.xlsx`` case study moves to a 2-hour
  battery: ``bess_capacity_kwh`` 60,000 to 30,000 (15 MW / 30 MWh),
  ``capex_bess_eur_per_kwh`` 250 to 200, and the replacement switches
  from the fixed year 10 to the SOH-triggered ``auto`` semantics with
  ``bess_eol_soh_pct`` 70 (the pack crosses the threshold in project
  year 11 under the default calendar + cycle fade).  The schema
  defaults are unchanged; only the case-study values move.  The
  gallery figures, the workbook-pinned smoke KPIs and every doc
  quoting the case study are regenerated/updated accordingly.

### Changed (citation and license polish)

- The citation strings (README plain + BibTeX, CITATION.cff title)
  read "co-located PV and battery systems" instead of "PV + battery";
  CITATION.cff carries a `license-url` pointing at the shipped
  LICENSE.
- The LICENSE adds an explicit limited academic-evaluation permission
  (viewing and executing unmodified copies solely for non-commercial
  peer review and the verification or reproduction of published
  results, citation requested) while every other right stays reserved;
  the README gains a License section and the docs license page mirrors
  the summary.

### Changed (single-panel uncertainty figures)

- The multi-panel uncertainty figures are split into one figure per
  source so the whole report shares a single canvas and styling
  contract: `inputs_forecast_band`, `inputs_seasonal_boxplot`,
  `pit_histogram`, `crps_timeline` and `residual_qq` now write
  `_dam` / `_pv` / `_load` variants (the `load` variant only when the
  timeseries carries `load_kwh`), each on the standard 7x4 canvas
  with its own x label, full tick styling and the house
  below-the-axes legend.  `coverage_by_horizon` and
  `dam_intraday_heatmap` were already single-panel and are unchanged.
  The per-source writers return the list of written paths.

### Changed (overlay-line contrast and naming)

- The line palette is a two-colour system, identical in every figure:
  charcoal for undiscounted / net series (the net lines overlaid on
  the monthly / yearly cashflow bars and the NPV waterfall — where
  blue read poorly against the saturated stacks — plus the cumulative
  undiscounted curve and the simple-payback marker) and the house
  blue for their discounted companions (cumulative discounted curve,
  discounted-payback marker).  The separate purple `discounted` and
  indigo `net_discounted` palette keys are retired.
- The NPV waterfall's cumulative line is labelled `Cumulative
  discounted cash-flow` — it is the identical series (cumsum of the
  discounted net) the cumulative-cashflow figure plots under that
  name, so the two figures now share one label; the redundant
  `Cumulative NPV` label is retired from the palette registry.

### Changed (edge-to-edge axis windows)

- Line and time plots across the financial and uncertainty families
  span their x axis edge to edge, matching the energy plots: the
  cumulative cashflow, payback, SOH trajectory and lifetime summary
  drop the half-slot side gaps of the shared year axis (bar charts
  keep half a slot so the first/last bar bodies are not clipped, now
  0.5 instead of 0.75), the month axis tightens to half a slot per
  side, the balancing reservation profile pins its exact 0-23 hour
  window, the forecast band and CRPS timelines start and end on their
  first/last timestamps, the coverage-by-horizon axis spans the commit
  window exactly, and the PIT histogram pins the probability axis to
  0-1.  The uncertainty family's date ticks rotate right-anchored like
  the energy plots' date axes (they were horizontal and could crowd on
  year-long timelines).

### Fixed (perfect-foresight benchmark re-tightening)

- The rolling-horizon foresight gap can no longer read negative merely
  because the annual benchmark stopped at its ``mip_gap``: when any
  Monte Carlo realisation's profit exceeds the perfect-foresight
  incumbent (the realisation is PF-feasible, so this is solver slack,
  not model error), the pipeline re-solves the benchmark at 10x
  tighter gaps (down to 1e-6) until it is the best case, recomputes
  the ``foresight_gap_pct`` column and its percentiles against the
  final benchmark, and uses the re-tightened solution for every
  downstream artifact.  The gap of the solve that produced the final
  benchmark is recorded as the new ``pf_benchmark_mip_gap`` KPI and
  each re-solve is logged in ``run_log.txt``.  A re-solve is accepted
  only if it improves the incumbent: when the time limit terminates
  the search, a deterministic solver returns the identical incumbent
  at any requested gap, so the guard keeps the previous benchmark
  after one unimproved probe (logging that the time limit binds and
  advising a higher ``--time-limit`` or a faster solver) instead of
  burning the limit on every rung of the escalation ladder; a
  float-rounding artifact that could repeat the floor-gap solve twice
  is also fixed.

### Fixed (final pre-release audit)

- Regulatory framing is fully neutral: remaining country-specific
  regime references and a leftover country-specific percentage in the
  max-injection docstrings are removed; the injection cap is
  documented everywhere as a plain user input (a value of X means X %
  allowed injection, equivalently 100 - X % curtailment).
- The two README foresight-distribution figures are produced by the
  gallery export script itself (8-seed rolling-horizon Monte Carlo
  runs through the pipeline), so they carry the IEEE preset like every
  other gallery figure; the script also applies the preset explicitly
  and defaults to the documented mip-gap 0.002.
- BESS revenue waterfall: the battery's energy-aggregator-fee share is
  computed on the cashflow's fee base (exports net of grid-charging
  expense) instead of gross exports, and an all-zero DAM step is
  omitted like every other zero step.
- Yearly cashflow bars, the NPV waterfall and the Year-1 monthly
  cashflow stack the balancing revenue (gross), the balancing
  aggregator fee and the PPA leg when those streams carry value, so
  the bars sum to the overlaid net line in every frame.
- Figure conventions: rendered figure strings use hyphens instead of
  em dashes (locked by a static source rule), the IEEE preset pins
  `figure.titlesize` 9 and marker size 3, the two emissions-plot
  placeholders route through the canonical 10 pt placeholder helper,
  and the daily combined + SOC legend follows the house measured-fit
  placement rule.
- Docs synchronized with the shipped workbook and pipeline: the wear
  cost default reads 10 in the objectives page, `raddatabase` and the
  balancing price columns are documented, the results-folder reference
  lists every artifact a default run writes, and the cumulative
  cashflow guide matches the solid colour-distinguished curves.  The
  workbook-schema parity tests now cover the balancing and ppa sheets
  and pin every kv sheet of the shipped workbook to the schema.
- Monthly cashflow allocation falls back to a flat 1/12 split for a
  year whose Year-1 revenue (or OPEX) base is zero, keeping the
  monthly-sums-to-yearly reconciliation exact in degenerate regimes;
  the no-breakdown cashflow fallback carves the fee-free PPA leg out
  of the fee base.
- Packaging hygiene: the wheel ships only the `pvbess_opt` packages
  (no top-level `main` module) and `.gitignore` covers `dist/` and
  root `build/`.

### Added

- Validation of the self-consumption foresight gap (measured on the
  4-hour, 15 MW / 60 MWh configuration of the case study): at
  mip_gap 1e-5 the 5-seed median gap is 0.464 % against a
  2,849,785 EUR perfect-foresight benchmark; the sigma-zero run
  isolates a 0.324 % horizon-truncation component, documented in the
  rolling-horizon guide.
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
