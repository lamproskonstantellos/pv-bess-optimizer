# Changelog

## 0.9.0 (2026-06-27)

First production release.  No prior versions have shipped; no
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
