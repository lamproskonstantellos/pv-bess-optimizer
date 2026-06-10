# Changelog

## 0.9.0 — Current state (unreleased)

Feature-complete pre-release.  No prior versions have shipped; no
compatibility surface is maintained.

### Changed (financial reporting consistency)

- New `initial_investment_eur` KPI: the Year-0 CAPEX + DEVEX outlay
  (matching the Year-0 bar in the plots).  The lifecycle
  `total_capex_eur` / `total_capex_devex_eur` are now documented as
  replacement-inclusive.
- `roi_pct` switched to the standard total-return form: operating net
  cashflow (Years 1..N) over `|initial_investment_eur|` — previously
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
- Project finance pipeline: lifetime cashflow, NPV, IRR, ROI, BCR,
  LCOE, LCOS, payback.  Inflation indexation per revenue / cost
  stream.
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
  YAML / JSON file.

### Solver

- Pyomo + HiGHS (default).
