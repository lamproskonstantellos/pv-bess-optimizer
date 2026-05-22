# Changelog

This file describes the current release only.  Migration notes and
breaking-change diffs are folded into the present-tense descriptions
across the codebase and the Sphinx docs.

## Current release

PV + BESS sizing-as-input optimizer with a multi-year project-finance
pipeline and rolling-horizon Monte Carlo for uncertainty analysis.

### Capabilities

- Three asset modes (hybrid PV+BESS, PV-only, BESS-only) and two
  regulatory regimes (`vnb`, `merchant`), read literally from the
  seven-sheet workbook.
- Mixed-integer linear dispatch with nine audit invariants, exact in
  every mode; `--strict` turns invariant violations into errors.
- Split-revenue cash-flow projection: retail and DAM streams degraded on
  their own PV / BESS capacity factors and indexed by separate inflation
  rates, with an explicit aggregator-fee component.
- Cost model: per-kWp PV and per-kW BESS CAPEX/DEVEX, a site-wide
  lump-sum CAPEX and DEVEX figure for items that are not naturally
  per-asset (substation, grid upgrades, interconnection works,
  environmental studies), OPEX with escalation, and BESS replacement.
- Cycle-aware BESS degradation: multiplicative calendar fade plus an
  optional linear cycle-fade term (`bess_degradation_pct_per_cycle`; set
  to 0 for calendar-only mode).
- Financial KPIs: NPV, IRR, ROI, BCR, simple and discounted payback, and
  LCOE (PV-only) / LCOS (BESS-only) against the Lazard 2024 benchmark
  bands.  Site-wide lump-sum costs are excluded from LCOE/LCOS by the
  Lazard convention.
- One-at-a-time tornado sensitivity over CAPEX (including DEVEX and the
  site lump sum), OPEX, revenue and discount rate.
- Rolling-horizon dispatch with imperfect foresight and Monte Carlo over
  forecast scenarios, with P10 / P50 / P90 distribution and
  forecast-calibration diagnostics.
- IEEE-styled PDF plots and a multi-sheet results workbook.

### Compatibility

Old workbooks load unchanged.  Numerical output is identical when the
optional inputs are left at their defaults (site lump sums 0, cycle-fade
coefficient 0, no aggregator fee, no unavailability derate).  The
`curtailment_profile` sheet and the `revenue_inflation_pct` key are still
accepted with a deprecation warning for one release.
