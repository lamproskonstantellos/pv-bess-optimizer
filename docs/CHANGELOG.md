# Changelog

## 0.9.0 — Current state (unreleased)

Feature-complete pre-release.  No prior versions have shipped; no
compatibility surface is maintained.

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
