# Changelog

This file describes the current release only.  Migration notes and
breaking-change diffs are folded into the present-tense descriptions
across the codebase and the Sphinx docs.

## Current release

PV + BESS sizing-as-input optimizer with a multi-year project-finance
pipeline and rolling-horizon Monte Carlo for uncertainty analysis.

### Capabilities

- Three asset modes (hybrid PV+BESS, PV-only, BESS-only) and two
  regulatory regimes (`self_consumption`, `merchant`), read literally from the
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
coefficient 0, no aggregator fee, no unavailability derate, balancing
sheet disabled).  The `curtailment_profile` sheet and the
`revenue_inflation_pct` key are still accepted with a deprecation
warning for one release.

### 0.9.0 — balancing market participation

- Stochastic co-optimisation of the European balancing markets
  (FCR / aFRR / mFRR) alongside the existing DAM dispatch.
- New `balancing` workbook sheet with 33 keys covering the master
  switch, per-product capacity shares of `bess_power_kw`, acceptance
  and activation probabilities, fallback capacity and activation
  prices, the FCR sustained-duration requirement, the SOC safety
  buffer, a balancing-revenue inflation rate, the two Monte Carlo
  price sigmas and the default seed.
- Nine optional per-step price columns on the `timeseries` sheet
  (five capacity prices for every product, four activation prices for
  the aFRR / mFRR products; FCR is capacity-only). Missing columns
  fall back to the scalar defaults from the balancing sheet with a
  single warning per column.
- New `pvbess_opt/balancing.py` module hosting the dataclass-based
  configuration, the per-step price container, the per-product
  accessor helpers, and a reproducible synthetic price generator.
- MILP extension in `pvbess_opt/optimization.py`: per-product
  reservation variables, per-direction power-budget constraints with
  FCR counting in both directions, SOC-headroom constraints sized to
  one settlement period for aFRR / mFRR and to the FCR-specific
  duration for FCR, expected-activation drift in the SOC recursion,
  and expected capacity and activation revenue in the objective.
- New KPIs covering per-product capacity and activation revenue,
  aggregate totals, the two expected activation energies, the revenue
  share against DAM, and per-product average reservation in kW.
- Lifetime cashflow integrates the balancing revenue split into
  capacity and activation lines, degrades both on the BESS capacity-
  fade curve, indexes them by `bm_inflation_pct`, and exposes the
  lifecycle totals plus a per-year list through the financial KPI
  dict. LCOE / LCOS denominators are unchanged — balancing revenue is
  treated as a cash offset, not as delivered energy, following the
  Lazard convention.
- Monte Carlo realisation in `pvbess_opt/rolling_horizon.py` with
  per-product Bernoulli draws for acceptance and activation, plus
  log-normal price noise, returning P10 / P50 / P90 of balancing
  revenue and a per-product breakdown with reproducible seeds.
- Two new IEEE-styled plots in `pvbess_opt/plotting/balancing.py`:
  a 24-hour average reservation profile and a Monte Carlo revenue
  histogram with P10 / P50 / P90 lines.
- Six new dispatch invariants (INV-B1 through INV-B6) added to the
  test suite, plus 37 new tests across five files covering the
  workbook loader, the data-model helpers, the MILP integration,
  the invariants, and the Monte Carlo realisation.

The feature is fully opt-in. With `balancing_enabled = FALSE` (the
default) the MILP topology, the KPI dictionary, the lifetime cashflow,
the Monte Carlo output, and the PDF report are bit-identical to the
0.8.x release; the existing nine dispatch invariants continue to hold.
