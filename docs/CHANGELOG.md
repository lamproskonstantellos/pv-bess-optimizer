# Changelog

## 1.0.0 (2026-07-06)

Production release.

### Fixed (pre-release docs sync)

- Stale cross-document counts and equation ranges brought in line with
  the shipped surface: the `balancing` sheet is 36 keys (was quoted as
  34 in the design doc's Inputs section and the user guide), the
  design doc's Inputs table gains the `bm_block_hours` /
  `bm_merit_order_enabled` rows (B9/B10, previously documented only in
  their own sections), the `ppa` sheet is 14 keys (nine `ppa_*` plus
  five `support_*`), the economics sensitivity block is 6 delta keys
  (the tax-rate driver joined the original five), the MIP-formulation
  summary page cites S1-S36 / B1-B10 and gains the intraday-extension
  paragraph (I1-I5) next to the balancing one, and the
  uncertainty-modelling page cites U1-U12 with the imbalance /
  VaR-CVaR / two-stage extensions.

### Changed (README)

- The README now covers the full opt-in surface: the intraday venue
  (What-it-does layer, `intraday` sheet reference, gallery scenario),
  the sliding-FiP / two-way CfD support engine and
  guarantees-of-origin revenue in the fiscal-landscape paragraph, the
  dispatch/asset levers paragraph (grid import limit, curtailment
  compensation, cycle caps, overbuild / augmentation, mid-life
  re-solve), the imbalance / VaR-CVaR / two-stage Monte Carlo
  extensions, the missing workbook keys on the `project` / `bess` /
  `economics` / `simulation` / `ppa` / `balancing` sheet sections, and
  the previously undocumented `trajectories` sheet.  The results
  gallery gains a third scenario — merchant + intraday venue — with
  the DA-vs-IDA price duration curves, the intraday net position and
  the revenue stack carrying the intraday bands, rendered by
  `scripts/export_readme_figures.py` from an illustrative intraday
  deck derived from the shipped day-ahead deck (documented in the
  script and the captions).

### Added (intraday figures)

- Two IEEE-styled venue figures in `04_financial_plots/`, emitted only
  when the two-stage re-dispatch ran (default figure set unchanged):
  `da_ida_price_duration.pdf` — DAM vs IDA price duration curves, each
  sorted descending over the share of time (`Day-ahead price`
  `#1E88E5`, `Intraday price` `#8E24AA`) — and `intraday_position.pdf`
  — the per-step intraday net position (sells positive, buys negative)
  as a step line (`Intraday net position` `#00897B`).  Both follow the
  house figure contract (7x4 in canvas, registered labels, legend
  below the axes, `empty_placeholder` gating).

### Added (two-stage intraday Monte Carlo)

- The intraday venue inside the rolling-horizon Monte Carlo
  (Eq. U12): Stage-1 windows commit day-ahead dispatch under noisy
  forecasts, then a SINGLE annual Stage-2 pass per seed re-dispatches
  the stitched committed schedule against the actual intraday prices
  — one extra solve per seed, honouring the soft year-close SOC pin
  and lifting the cycle caps to the committed day's throughput where
  window seams exceed them (the re-dispatch never ADDS cycling beyond
  the operational cap).  The Stage-2 timeseries carries the actual
  prices on the COMMITTED physical envelope; residual volume error
  stays the imbalance settlement's domain (now mutually exclusive
  with the venue, replacing the interim uncertainty gate).
- Simulation keys `uncertainty_ida_enabled` (default TRUE) and
  `uncertainty_sigma_ida` (default 0.15, below the DAM's): sign-aware
  log-normal noise on `ida_price_eur_per_mwh`, drawn from a SPAWNED
  child generator so every pre-existing seed's DAM/PV/load
  multipliers stay bit-identical; the flag is forced off when the
  venue is off.
- The foresight benchmark becomes the TWO-STAGE perfect-foresight
  profit on intraday runs (threaded through `pipeline._run_one`,
  including the benchmark-retightening guard, which now re-runs the
  Stage-2 pass on each tighter incumbent); `monte_carlo_rolling`
  appends an `id_net_revenue_eur` per-seed column on two-stage
  ensembles only (the imbalance conditional-column pattern).

### Added (intraday revenue stream)

- The intraday margin as a first-class cashflow stream (Eqs. E58/E59):
  `intraday_revenue_eur` (Year-1 gross spread margin, per-origin fade
  on the sell-volume split, indexed by `id_inflation_pct`) and
  `intraday_fee_eur` (flat venue rate on the per-origin fading traded
  volume), both folded into `net_cashflow_eur`, reconciled exactly on
  the monthly/quarterly frames (Year-1 margin and traded-volume
  shapes from the Stage-2 dispatch), rolled up to two SUMMARY-optional
  lifetime totals and excluded from LCOE/LCOS per the market-fee
  convention.
- Fee applicability matrix (Eq. I6): the energy-aggregator ad-valorem
  fee does NOT charge the intraday margin (the venue fee already
  prices the intermediation — the balancing/E13b precedent,
  superseding the pre-merge design note); the route-to-market volume
  bases follow the Stage-2 frame automatically; the optimizer revenue
  share DOES charge the BESS-origin intraday margin in both variants
  (still zero-clamped), which also joins the E25a netting base and is
  zeroed in 'zeroed' toll years.
- Surface wiring: the sensitivity net-component list gains both
  columns (the Revenue driver scales the margin, not the volume-based
  fee; the `_scale_revenue(cf, 1.0)` no-op stays exact); the
  availability and curtailment derates scale the seven `id_*` KPI
  keys; the cashflow figure families gain `Intraday revenue`
  (`#26C6DA`) and `Intraday fee` (`#E91E63`) bands drawn only when
  non-zero; the lifetime dispatch sheet scales the intraday trades
  per origin and rebuilds the settlement columns from the scaled
  flows.

### Added (two-stage intraday re-dispatch)

- The intraday auction (IDA) as a second wholesale venue via two-stage
  sequential re-dispatch (Eqs. I1-I5, `docs/intraday_design.md`):
  Stage 1 is the unchanged day-ahead solve; Stage 2 re-solves the same
  model with the committed day-ahead net position pinned as data
  (`pvbess_opt/intraday.py`: config resolver, position extractor,
  Stage-2 driver) and an intraday block added — per-origin IDA
  sells/buys linked to physical flows, a deviation cap as a fraction
  of the export cap, and a per-step complementarity binary excluding
  wash trades.  The objective adds the spread margin net of the venue
  fee, so the committed position settles day-ahead and only the
  deviation trades at the IDA price; the wear-cost term prices the
  incremental Stage-2 throughput automatically.
- `pipeline._run_one` re-solves after the deterministic Stage-1 run
  and the Stage-2 frame becomes the headline result (cycles,
  degradation, KPIs and the financial stack see the combined DA + ID
  operation); the Stage-1 profit is kept as
  `id_stage1_profit_total_eur`.  New Year-1 KPI keys
  (`id_net_revenue_eur`, `id_venue_fee_eur`, `id_sell_mwh`,
  `id_buy_mwh`, `id_traded_volume_mwh`, `id_sell_pv_mwh`,
  `id_sell_bess_mwh`) and per-step settlement columns
  (`id_revenue_eur`, `id_fee_eur`) appear only on intraday runs.
- `verify_dispatch_invariants` gains the INV-I1..INV-I4 family
  (position link, deviation cap, sell/buy overlap, origin split),
  reported as 0.0 when the venue is off; strict mode treats the
  overlap product with the kWh^2 tolerance of invariant 5.
- v1 scope gates at load time: merchant mode only, finite export cap,
  and mutual exclusivity with balancing, PPA/support schemes, the
  rolling-horizon Monte Carlo and the mid-life re-solve diagnostic.

### Added (intraday venue input surface)

- The optional `intraday` workbook sheet (5 keys, master-switch
  pattern like `balancing`/`ppa`; absent sheet or `id_enabled =
  FALSE` keeps every output bit-identical): `id_enabled`,
  `id_max_deviation_frac_of_cap` (validated in `[0, 1]`),
  `id_allow_purchases`, `id_fee_eur_per_mwh` (non-negative) and
  `id_inflation_pct`.  YAML configs, the JSON schema and scenario
  dotted-target overrides (`intraday.id_enabled`) inherit the sheet
  automatically; `scripts/polish_input_workbook.py` materialises it
  in existing workbooks.
- The `ida_price_eur_per_mwh` timeseries column (intraday auction
  price, Eq. I1): required when `id_enabled = TRUE` — deliberately no
  scalar fallback, a constant IDA price would silently produce zero
  spread — NaN-filled alongside the DAM price, deck-variant capable
  (`ida_price_eur_per_mwh__<deck>`), and registered in
  `rolling_horizon.PRICE_COLUMNS` so the Monte Carlo actuals-restore
  picks it up.  An INFO log notes the hour-averaging on hourly
  workbooks.
- `docs/intraday_design.md` — the design note owning the I equation
  namespace (I1 allocated; the registry row in
  `docs/economics_design.md` now points at it).

### Added (structural market-access fees)

- Two structural market-access fees, both default-off (results
  bit-identical when unset), modelled on European practice and
  controlled from the workbook `economics` sheet (YAML and scenario
  surfaces inherit automatically):
  - `route_to_market_fee_eur_per_mwh` — representation fee per MWh of
    grid-exported energy (Greek FoSE / last-resort FoSETeK under
    regulated charges, German Direktvermarktung; typical 0.5-5
    EUR/MWh).  Charged on sold energy only; the PPA-covered PV export
    share is exempt while a physical (sleeved) contract is in term;
    flat over the project life while the charged MWh fade on the
    per-origin degradation curves (Eq. E13c).
  - `optimizer_revenue_share_pct` — battery optimizer revenue share on
    the POSITIVE annual BESS wholesale trading margin (export minus
    grid charging), the merchant / floor+share structure of BESS
    optimizers (typical 10-25 %); never invoices a share of a loss
    (Eq. E13d).
  Both surface as signed cashflow columns
  (`route_to_market_fee_eur`, `optimizer_fee_eur`) folded into
  `net_cashflow_eur`, roll up to lifetime totals rendered in
  `SUMMARY.md` when non-zero, join every cashflow figure (revenue
  stack, yearly/monthly bars, NPV waterfall, BESS revenue waterfall
  and monthly view) as their own deduction bands drawn only when
  non-zero, and are excluded from LCOE/LCOS.  New Year-1 KPIs
  `pv_export_mwh` / `bess_export_mwh` carry the fee's export base
  (availability-derated with the totals they compose).  The loader
  warns when the legacy `aggregator_fee_pct_revenue` and the optimizer
  share are combined (double-charging the battery's wholesale stream).

### Added (per-year stream trajectories)

- Optional per-year escalation vectors per revenue/cost stream
  (Eq. E24/E24a), default-off and bit-identical when unset: a new
  `trajectories` workbook sheet (tidy: `enabled|stream|mode|year|value`)
  and equivalent YAML `trajectories:` block reshape `revenue_dam`
  (PV capture-rate decline; the CfD DAM leg, the post-term PPA
  reversion and the optimizer-share base ride the same series),
  `revenue_retail`, `balancing_capacity` / `balancing_activation`
  (ancillary-services price decay as the fleet saturates) and `opex`
  or the per-asset `opex_pv` / `opex_bess` split (post-warranty LTSA
  step, insurance) — `replace` substitutes the stream's flat
  `(1+i)^(y-1)` index (the loader warns when the matching
  `*_inflation_pct` is also non-zero), `overlay` multiplies on top.
  Vectors must cover every operating year and anchor at 1.0 in year 1
  (the Year-1 cashflow stays equal to the dispatch base).  The LCOE /
  LCOS discounted-OPEX numerators consume the identical series as the
  cashflow OPEX row, so metric and cashflow OPEX cannot diverge; the
  PPA strike and the route-to-market fee deliberately take no
  trajectory.  YAML scenario files can override trajectories per
  scenario (the Excel scenarios sheet cannot carry per-year vectors
  and says so).  The polish script adds the disabled sheet to
  workbooks that predate it.

### Added (multi-scenario price decks)

- Named price decks as first-class scenario inputs: `<column>__<deck>`
  variant columns on the base `timeseries` sheet (DAM, retail and the
  balancing price columns; double underscore reserved and validated)
  stay inert in a normal run and are selected per scenario with the
  bare `price_deck` target — the deck's columns are copied onto the
  canonical names before the per-scenario re-solve, so Low/Central/High
  fundamentals change the DISPATCH, not just the cashflow scaling.
  Partial decks fall back to base columns (INFO); a deck matching no
  variant column fails before any solver time; deck-resolved balancing
  columns win over the scalar fallback.  YAML configs may keep decks in
  external files via a top-level `price_decks:` mapping.  The
  comparison table/workbook and the comparison bars carry a deck
  column / `[deck]` tick suffix only when a deck is used (deck-free
  batches stay bit-identical).

### Added (contracted-BESS foundations)

- Two shared primitives for the upcoming contracted BESS revenue
  structures, result-neutral by construction: the contract phase-window
  indicator `economics._contract_phase` (Eq. E25; `year_to = 0` means
  end-of-life, Year 0 is never in a phase) and the informational yearly
  column `bess_market_revenue_eur` (Eq. E25a) — the battery's UNclamped
  wholesale trading margin plus balancing revenue net of the BSP fee,
  riding the DAM escalation series.  The column is the single netting
  base tolling / floor+share / clawback structures will read; it is not
  summed into `net_cashflow_eur`, has no monthly counterpart, and the
  Revenue tornado driver scales it price-proportionally.

### Added (PPA negative-price suspension clause)

- `ppa_negative_price_rule` — `none` (default, unchanged as-produced
  behaviour) or `suspend`: the contract pauses in every step with
  DAM < 0 (strict; Eqs. P6-P8).  Physical settlement stops paying the
  strike on the covered volume, which then faces spot; CfD suspends
  the difference leg while the market leg keeps selling; both still
  total identically per step.  The dispatch reacts — the effective PV
  export price collapses to the DAM in suspended steps, so the MILP
  curtails or charges the BESS instead of exporting covered PV at a
  loss (the standard clause of post-2024 European pay-as-produced
  terms and premium schemes).  With the clause on, the exact per-step
  covered export surfaces as the availability-derated KPI
  `ppa_fee_exempt_export_mwh` and the route-to-market exemption (E13c)
  uses it instead of the share-based approximation; without it every
  path is bit-identical to before.

### Added (charging-side grid fee)

- `grid_charging_fee_eur_per_mwh` / `grid_charging_fee_exempt` on the
  project sheet (default 0 / FALSE, bit-identical when unset): a
  regulated wedge on grid-charged BESS energy (network charges +
  levies; typical European range 10-30 EUR/MWh) that enters the MILP
  objective as a buy-price adder (Eq. E26) — thin arbitrage spreads
  flip sign with the wedge, so it must shape the dispatch, not just
  the cashflow — and projects over the lifecycle as its own signed
  `grid_charging_fee_eur` column (Eq. E27; flat rate, charged volume
  fading on the BESS curve).  The wedge actually paid surfaces per
  step and as an availability-derated KPI, is subtracted from
  `profit_total_eur` (KPI == objective, mip-gap-0 locked), allocates
  monthly on the Year-1 charging shape, rolls up to a lifetime total
  with a conditional SUMMARY row, joins every cashflow figure as its
  own "Grid-charging fee" deduction band, is excluded from LCOE/LCOS,
  and is not scaled by the Revenue tornado driver (regulated rate x
  volume).  The exemption switch keeps the exempt / non-exempt regime
  pair a one-cell scenario change; a latched warning flags a wedge
  that can never bind because grid charging is disallowed.

### Added (BESS tolling agreement)

- `bess_toll_eur_per_mw_year` + window / treatment / indexation keys
  on the economics sheet (default 0, bit-identical when off): a fixed
  EUR/MW/yr payment for BESS dispatch rights over a phase window
  (Eqs. E29/E29a).  Availability-conditioned, contractually indexed,
  no capacity-fade scaling (power-block basis).  Under the default
  `zeroed` merchant treatment every BESS-origin merchant stream (BESS
  DAM margin, both balancing legs and their BSP fee, the BESS
  route-to-market fee share, the optimizer share and the charging-side
  grid fee) is gated to zero in toll years — the toller keeps them;
  `retained` stacks the toll on top (warned).  Surfaces as a
  `toll_revenue_eur` cashflow column with exact flat-1/12 monthly
  allocation, a lifetime KPI + conditional SUMMARY row, a "Tolling
  revenue" figure band (teal, drawn only when non-zero), and is
  excluded from LCOE/LCOS and from Revenue-driver scaling (fixed
  contractual payment).  Stacking warnings: no-op toll (no BESS),
  `retained` double-monetisation, toll + optimizer-share overlap.

### Added (optimizer floor + share above floor)

- `optimizer_floor_enabled` + floor level / term window / margin-basis
  keys on the economics sheet (default off, bit-identical — the plain
  `optimizer_revenue_share_pct` becomes the E13d special case): the
  floor+share BESS-optimizer contract (Eqs. E30/E30a).  The optimizer
  guarantees an availability-scaled EUR/kW/yr floor and takes the
  share of the margin ABOVE it; shortfalls surface as a separate
  `optimizer_floor_topup_eur` column (>= 0, month-12 ex-post booking)
  so the fee column keeps its <= 0 sign contract.  The margin base is
  the E13d DAM margin or, under `dam_plus_balancing`, the full E25a
  base (share applies after the BSP fee — fees never compound).
  `sensitivity._scale_revenue` gains an optional econ parameter that
  recomputes the piecewise fee/top-up pair from the scaled margin base
  against the un-scaled floor, making the Revenue tornado exact at the
  floor kink (the None-default legacy path is unchanged and remains
  exact for the plain share).  Lifetime KPI + conditional SUMMARY row,
  'Optimizer floor top-up' band (teal 900), LCOE/LCOS invariant; a
  'zeroed' toll window overlapping the optimizer term warns (full
  floor top-up every overlap year).

### Added (state support with two-way clawback)

- `state_support_eur_per_mw_year` + window / threshold / share /
  indexation keys on the economics sheet (default 0, bit-identical
  when off): RRF-style fixed storage support with the TWO-WAY netting
  used by Greek storage-support auctions (Tameio Anakampsis / TAA
  reference; neutral mechanism) — Eqs. E31/E31a.  Availability-scaled
  support on the power block; the netting settles realised market
  revenue (the E25a base, plus capacity-market revenue when present)
  against an indexed threshold, both directions at the same share, no
  floor (net-repayment years are flagged once in the run log).  Two
  cashflow columns: `state_support_eur` (flat 1/12 monthly) and the
  signed `state_support_clawback_eur` (month-12 ex-post booking), both
  in the net, with SUMMARY-optional lifetime totals, 'State support' /
  'State-support netting' figure bands (amber / purple, the netting
  band element-wise signed), LCOE/LCOS invariance and an
  exactly-recomputed Revenue-tornado netting (scaled base vs un-scaled
  threshold - revenue-stabilising).  Stacking warning: support window
  overlapping a 'zeroed' toll (the netting tops up to the threshold
  every overlap year - two capacity payments for the same MW).

### Added (capacity-market payment)

- `capacity_market_eur_per_mw_year` + derating / window / indexation
  keys on the economics sheet (default 0, bit-identical when off): a
  capacity payment on the DERATED power block (Eq. E32) - the
  derating factor is the auction's published storage class factor
  (duration-based eligibility), the payment lands on derated MW by
  stated convention, availability-scaled with no capacity-fade
  scaling.  Counts toward the state-support netting base (Eq. E31a),
  computed before the clawback in the year loop (order locked by
  test) while the E25a base stays capacity-free.  Flat-1/12 monthly
  allocation, lifetime KPI + conditional SUMMARY row,
  'Capacity-market revenue' band (deep orange 800), LCOE/LCOS
  invariant, NOT scaled by the Revenue tornado driver (administered
  price).  Stacking warnings: overlap with a state-support window
  (cumulation) and with a 'zeroed' toll (the toller usually holds the
  capacity obligation).

### Changed (contract stacking-warning matrix + run-log audit)

- The per-feature contract stacking warnings (toll no-op / 'retained'
  double-monetisation / toll x optimizer share / toll x floor /
  toll x state support / capacity x support / capacity x toll) now
  live in one data-driven `io._CONTRACT_STACKING_RULES` table
  evaluated in a single validation pass over the parsed phase windows
  (`io._phase_windows_overlap`) - exact message strings preserved, and
  phase-disjoint configurations are locked silent by a parametrised
  matrix test (the toll x optimizer-share rule now honours the
  optimizer term window instead of firing whole-life).  A matrix row
  is reserved for the Phase-5 support-scheme x state-support
  cumulation rule.  `compute_financial_kpis` emits one
  `[contracted revenue]` INFO audit line (five lifetime totals) when
  any contracted structure is active; the design doc gains the
  per-structure conventions table and the stacking-interaction table,
  and the uncertainty design documents the contracted-revenue tornado
  damping (fixed streams unscaled; piecewise terms recomputed at
  their kinks).

### Added (revenue levy on gross market turnover)

- `revenue_levy_pct` on the economics sheet (default 0, bit-identical
  when off; validated in [0, 100]): a percentage levy on gross MARKET
  turnover (Eq. E33) - DAM export revenue gross of the aggregator
  fee, both balancing legs gross of the BSP fee, and the PPA contract
  leg (fees never compound; a turnover levy charges gross sales).
  The 3 % special RES turnover levy applied in Greece is the
  reference.  Retail/self-consumption savings, the contracted streams
  (E29-E32) and the imbalance settlement are excluded by
  construction; a negative total turnover (e.g. a deeply negative CfD
  difference leg) never yields a rebate (clamp).  Signed
  `revenue_levy_eur` column inside net_cashflow_eur (E15 amended),
  revenue-share monthly weights with exact yearly reconciliation,
  lifetime KPI + conditional SUMMARY row, 'Revenue levy' band (pink
  400) in all cashflow/revenue stacks, LCOE/LCOS invariance, and the
  Revenue tornado driver scales it exactly (uniform-scaling base
  preserves the clamp).  Being inside EBITDA it is automatically
  deductible from taxable income once the tax layer lands.

### Added (depreciation + corporate tax engine)

- `corporate_tax_rate_pct` + three straight-line lives +
  `tax_loss_carryforward_years` on the economics sheet (default rate
  0, bit-identical: every tax column is an exact zero and the
  post-tax family passes through value-identical to pre-tax): the
  pure post-processing tax layer `economics.apply_tax_layer`
  (Eqs. E34-E38), called at the end of build_yearly_cashflow so the
  frame always carries the columns.  Per-asset straight-line
  depreciation (PV, BESS incl. a replacement tranche in service the
  year AFTER its month-12 booking, site lump sums; N=0 = no claim;
  horizon truncation, no terminal write-off), taxable income =
  EBITDA - depreciation - E20 debt interest (the levy is deductible
  by construction), FIFO loss carry-forward (unlimited default,
  optional expiry window), TAX_y <= 0 always.  Post-tax columns
  discount at the single WACC (documented convention).  Monthly:
  month-12 tax booking with exact post-tax reconciliation.
  Sensitivity: perturbed frames DROP all tax-layer columns (nonlinear
  - stale-value guard); the pre-tax tornado is byte-identical with
  the layer on or off.  No default figures change (the post-tax net
  is a separate column family).  Locked by hand-computed schedules,
  a FIFO-expiry worked example and an independent levered reference.

### Added (sculpted debt repayment + average DSCR)

- `debt_repayment` gains the `sculpted` profile (Eqs. E40/E40a): debt
  service tracks CFADS at the constant implied DSCR - the lender
  profile in which coverage is level across the tenor instead of
  binding in one year.  CFADS = net_cashflow_eur (replacement CAPEX
  included, the per-year-DSCR numerator convention); a CFADS <= 0
  year pays nothing (interest not capitalised) and later years absorb
  it; any clamp residual sweeps into the final year's principal so
  the balance amortises to ~0 exactly.  `_leverage_kpis` now returns
  `avg_dscr` alongside `min_dscr` (equal under sculpting by
  construction) and levered runs gain the `avg_dscr` KPI - the only
  key addition for already-levered runs, called out here per the
  bit-identity contract (feature-OFF runs are unchanged).  SUMMARY.md
  gains a finite-gated leverage block (equity IRR, min/avg DSCR).

### Added (target-DSCR debt sizing)

- `debt_sizing_mode = target_dscr` (Eqs. E41-E43; default `manual` =
  unchanged gearing_pct convention, bit-identical): the debt amount
  is SIZED to hold `target_dscr` (default 1.30, validated >= 1.0) on
  the sizing-case CFADS, in closed form per repayment profile - the
  exact inverse of the amortization schedule, so replaying the sized
  debt reproduces the target to machine precision (annuity/linear
  bind at the minimum-CFADS year; sculpted holds the target level in
  every positive-CFADS year).  Capacity caps at the Year-0 outlay and
  gearing becomes an OUTPUT: new KPI family `debt_capacity_eur` /
  `sized_debt_eur` / `gearing_sized_pct` / `gearing_input_pct` /
  `target_dscr` / `dscr_target_met` / `binding_dscr_year` (all NaN in
  manual mode) plus a SUMMARY.md "Debt sizing" block.  Sizing
  resolves ONCE per run and the sized debt is FROZEN (debt is
  committed at financial close): sensitivity and uncertainty replays
  consume the committed amount, never a per-perturbation re-size; the
  corporate-tax layer re-applies so its interest deduction runs on
  the sized debt.  An unachievable target (a loss-making year inside
  the tenor under a level-service profile) completes the run
  all-equity with a neutral message, never an error; a non-zero
  `gearing_pct` in sizing mode warns that it is an input echo only.
  `debt_sizing_case` fixes the CFADS case (`base` implemented; `p90`
  / `low_price` reserved and rejected with guidance).  LCOE/LCOS and
  every default figure are untouched (financing stays excluded and
  `net_cashflow_eur` does not change).

### Added (P90 production lender case)

- `production_p90_factor_pct` (Eq. E44; default 100 = disabled,
  bit-identical): the lender convention of a downside resource year
  as a deterministic INTER-annual haircut on the PV-linked cashflow
  lines - retail/DAM gross recovered from the base frame and the
  aggregator fee rederived through its clamp (the sensitivity
  gross/net identity), PPA volume, route-to-market fee (export
  volume falls with production) and the E28 imbalance line; the
  balancing family, contracted BESS payments (toll / floor+share /
  state support / capacity market), grid-charging fee, levy, OPEX
  and CAPEX deliberately unscaled, each decision documented in the
  new `pvbess_opt/lender.py`.  Explicitly distinct from the
  forecast-noise Monte Carlo (intra-year dispatch realism) - scope
  split cross-referenced in both design docs; no re-dispatch
  (documented cashflow-level approximation, scenario-engine re-solve
  recorded as future work).  `lender_cases_enabled` (default FALSE)
  writes the case table - base and P90 rows with per-case min/avg
  DSCR, equity IRR, NPV and E41/E42 debt capacity, all on the run's
  frozen committed debt - to a `lender_cases` results sheet and a
  SUMMARY block (LCOE/LCOS deliberately excluded: Lazard cost
  figures would be misstated by an energy-only scaling).
  `debt_sizing_case = p90` activates: the target-DSCR debt is sized
  against the haircut CFADS while the run's own cashflow stays the
  base case (a warning flags the degenerate factor-100 combination).

### Added (baseload PPA structure)

- `ppa_structure = 'baseload'` goes live (Eqs. P9-P11 + E45; it
  previously parsed but was rejected with guidance): a contracted
  flat band of `ppa_baseload_mw` settles a fixed per-step volume
  financially against the plant's TOTAL export - shortfall
  implicitly bought at spot, excess sold at spot, which under
  symmetric settlement is identical to the net leg Q x (strike -
  DAM) on top of full merchant revenue (the identity is also why v1
  is cfd-only: a physical sleeved variant totals the same and only
  the deferred flow attribution would differ).  The per-step volume
  honours the timeseries resolution (MW x dt).  Dispatch is provably
  unchanged (P11): the leg carries no decision variables, so
  merchant-optimal dispatch is already baseload-optimal - a genuine
  firming incentive needs asymmetric imbalance pricing, sketched as
  v2 in the design doc and deliberately not built.  Raw
  shortfall/excess coverage diagnostics (P10) join the Year-1 KPIs.
  Classification, each with a lock test: the fixed-volume leg
  neither availability-derates nor rides the PV fade, and the E45
  yearly stream drops the fade on both legs with no post-term
  reversion.  Existing PPA columns, theme labels and figures are
  reused unchanged; zero-band runs are bit-identical.  Validation:
  band > 0, cfd-only with the equivalence guidance, share-ignored
  warning.

### Added (TaxRate tornado driver + post-tax cumulative line)

- `sensitivity_tax_rate_delta_pp` on the economics sheet (default 5)
  arms a TaxRate tornado driver whenever the tax layer is on
  (`corporate_tax_rate_pct` > 0).  Taxes are nonlinear (taxable-base
  clamp, loss carry-forward), so each leg is a full cashflow +
  tax-layer rebuild at the shifted statutory rate, and the driver
  reports POST-TAX deltas in dedicated sensitivity columns that join
  the frame only when it ran - its pre-tax metric columns stay NaN so
  the published pre-tax tornado is untouched.  The
  cumulative-cashflow figure gains a dashed 'Cumulative discounted
  cash-flow (post-tax)' line rendered only while the rate is
  positive; zero-rate outputs stay bit-identical.

### Added (sliding Feed-in-Premium / two-way CfD support settlement)

- `support_scheme` on the ppa sheet (Eqs. E55-E57; default 'none',
  bit-identical): reference-period state-support settlement on the
  eligible PV export.  'sliding_fip' pays max(strike - reference, 0)
  per month on the volume-weighted monthly DAM reference price (the
  Greek DAPEEP sliding Feed-in-Premium; `support_strike_eur_per_mwh`
  is the reference tariff, Timi Anaforas); 'cfd_two_way' settles
  strike - reference both ways (repayment years go negative and the
  net cashflow carries the sign).  The premium is a settlement
  overlay - dispatch still sells at the DAM - and is mutually
  exclusive with a corporate PPA.
  `support_negative_hour_suspension` removes negative-DAM hours from
  both sides of the reference-price weighting, reusing the PPA
  suspension clause's strict p < 0 classifier;
  `support_ref_period='hourly'` degenerates to the per-step CfD
  algebra as a cross-check mode.  The Year-1 KPI pair and its
  per-month detail carry both operating derates; the cashflow
  projects a FLAT strike leg against a dam-inflation-indexed
  reference and PV-fade volume with the sliding clamp re-applied per
  month per year, cutting off after `support_term_years`.
  Fee-exempt, excluded from LCOE/LCOS, its own signed figure band,
  monthly allocation on the Year-1 settlement shape with exact
  reconciliation, a lifetime SUMMARY row, and a dedicated
  SupportStrike tornado driver (exact full rebuild at the perturbed
  strike; the Revenue driver leaves the mixed column untouched).

### Added (NPV tail risk: VaR / CVaR)

- `risk_metrics_enabled` + `risk_alpha_pct` on the simulation sheet
  (Eqs. U10/U11; default FALSE, bit-identical - nothing is computed
  or written): the left tail of the NPV distribution over the
  rolling-horizon Monte Carlo seeds.  Each seed's realised (derated)
  Year-1 profit maps onto an NPV via a pro-rata rescale of the
  Year-1 revenue bases and a re-run of the analytic cashflow
  (documented approximation); VaR is the linear-interpolated
  empirical alpha-quantile and CVaR the mean of the tail at or below
  it (CVaR <= VaR by construction).  Outputs: a `risk_metrics`
  results-workbook sheet and `npv_var_eur` / `npv_cvar_eur` rows in
  the SUMMARY rolling section next to the seed count (small
  ensembles give noisy tails).  With a scenario deck the same
  estimators are appended to the scenario-comparison workbook table
  over the scenarios' NPVs (equal weights; the comparison plots keep
  one bar per real scenario).

### Added (guarantees-of-origin revenue)

- `go_price_eur_per_mwh` on the economics sheet (Eq. E54; default 0 =
  off, bit-identical): sells guarantees of origin on the eligible
  renewable injection - the availability- and curtailment-derated PV
  grid export (BESS discharge and self-consumed energy excluded: GOs
  are issued on metered renewable injection; the export basis is
  stated explicitly as jurisdiction-dependent).  Flat contracted
  price, PV-fade volume; fee-exempt (certificates settle outside the
  power market) and excluded from LCOE/LCOS.  Its own go_revenue_eur
  cashflow column (monthly on the PV production shape with exact
  reconciliation), a lifetime total in SUMMARY when non-zero, a "GO
  revenue" band in the revenue stack / yearly bars / NPV waterfall,
  and Revenue-driver membership in the sensitivity tornado.

### Added (merit-order activation-probability curve)

- `bm_merit_order_enabled` on the balancing sheet (Eq. B10; default
  FALSE, bit-identical - the constant-beta code path is preserved,
  not just its values): replaces the scalar per-product activation
  probability with a piecewise price-to-probability curve read from
  the optional `bm_merit_order` sheet (columns product,
  price_eur_per_mwh, activation_probability_pct; aFRR/mFRR products
  only; validated monotone non-increasing in price with guidance on
  swapped columns).  beta_k(t) interpolates the curve at each step's
  activation price - expensive bids activate less - and the same
  per-step array feeds the MILP objective, the SOC drift, the
  expected-activation KPIs, the SOC-dynamics audit mirror and the
  Monte Carlo realisation, so the model stays linear and internally
  consistent.  Bids are assumed at the input activation price level
  (documented modelling assumption).

### Added (mid-life re-solve validation)

- `midlife_resolve_year` on the simulation sheet (Eq. E53; default 0
  = off, bit-identical - no extra solve, no workbook sheet, no
  SUMMARY section): re-solves the MILP at the given project year
  with degraded parameters - BESS energy capacity scaled by its
  year-k capacity factor (power kept at nameplate), the PV column by
  the year-k production factor, prices held at Year-1 levels - and
  reports a scaled-vs-resolved delta table per lifetime-yearly KPI,
  validating the analytic lifetime-scaling recipe against a fresh
  dispatch.  The resolved side is aggregated and
  availability-derated exactly the way the scaled side is built
  (`lifetime.factors_for_year` shares the projection's resolution
  path), so the delta isolates degradation nonlinearity (SOC
  headroom, cycle-cap interaction, binary commitment).  The table
  carries the requested MIP gap as its own row - deltas within the
  gap are solver noise, not scaling bias.  Strictly diagnostic: the
  re-solve runs after the financial bundle and never feeds the
  cashflow or any KPI; combined with the rolling-horizon Monte Carlo
  it validates the deterministic path only (the loader warns).

### Added (BESS augmentation + day-1 DC overbuild)

- A pooled capacity engine
  (`lifetime.bess_capacity_factors_pooled`, Eq. E50) generalises the
  single replacement: every installed pool fades on its own
  calendar + cycle curve, the plant factor is the nameplate-clamped
  pool sum, and plant throughput is apportioned pro-rata to surviving
  pool capacity.  With no events and no overbuild the engine delegates
  to the single-pool accumulator (bit-identity, replacement included).
  Two default-off surfaces drive it from the bess sheet:
  - `bess_augmentation_years` (CSV, e.g. `8,15`) schedules staged
    augmentation events — `top_up` mode restores the plant to
    nameplate, `fixed_kwh` adds `bess_augmentation_kwh` — each priced
    on the declining unit-cost curve
    (`bess_cost_decline_pct_per_year`, Eq. E51) and booked as its own
    `augmentation_capex_eur` cashflow column (month-12 investment
    convention, matching depreciation tranche the year after, CAPEX
    sensitivity driver membership, "Augmentation CAPEX" bar in the
    yearly stack and NPV waterfall when non-zero, lifetime total in
    SUMMARY, LCOS numerator inclusion).  Mutually exclusive with
    `bess_replacement_year` (scheduled or `auto`) — the loader
    rejects the combination rather than picking silently.
  - `bess_overbuild_pct` installs `(1 + ob) x` nameplate at Year-0
    prices (Eq. E52) with usable capacity clamped at nameplate, so
    fade consumes the overbuilt margin first; dispatch always solves
    at nameplate and the premium flows into Year-0 CAPEX, the
    depreciation base and the LCOS numerator.
  The degradation report and SOH figure ride the pooled curve (an
  `augmentation_added_kwh` column appears when events are set), and
  the lifetime dispatch projection scales BESS flows on the same
  factors as the cashflow.

### Added (exogenous curtailment: expected quota + hour-resolved signal)

- `curtailment_pct` on the project sheet (Eq. E48; default 0 = off,
  bit-identical — the derate returns the KPI dict without new keys):
  an expected grid-operator curtailment share applied as a post-solve
  derate on the **export side only** (export energies, per-origin
  export profits, DAM revenues and the pay-as-produced PPA leg;
  self-consumption, load and grid import untouched; the baseload
  fixed-volume leg exempt via the same production-decoupled marker as
  the availability derate).  `profit_total_eur` is recomposed from
  its scaled components so the nine-aggregate scope identity
  survives.  `curtailment_compensated_pct` and
  `curtailment_compensation_price_eur_per_mwh` reimburse a share of
  the curtailed energy at an administered price (Eq. E49): a
  dedicated `curtailment_compensation_eur` cashflow column
  (revenue-classified, blended PV/BESS fade, DAM-inflation indexed,
  monthly via the fee-share weights) totalling into
  `lifetime_curtailment_compensation_eur`, with its own revenue band
  in the cashflow figures and membership in the sensitivity Revenue
  driver.  Availability and curtailment now compose through a single
  entry point (`availability.apply_operating_derates`, availability
  first) across all five KPI paths.
- `curtailment_signal` timeseries column (values in `[0, 1]`):
  hour-resolved curtailment that multiplies the export cap INSIDE the
  MILP, letting the optimizer re-dispatch around the restriction
  (e.g. charge the BESS instead of spilling) rather than scaling
  results after the fact.  The dispatch-frame cap columns mirror the
  composed cap so audit invariant 7 checks the true limit.  Mutually
  exclusive with `curtailment_pct` — the loader rejects the
  combination as double-counting.

### Added (annual cycle cap with warranty basis)

- `max_cycles_per_year` on the bess sheet (Eq. E46; default 0 = off,
  bit-identical): an annual full-equivalent-cycle warranty cap
  enforced in the Year-1 dispatch as one year-long linear constraint
  (`CYC_ANNUAL`), coexisting with the daily cap — a warning flags a
  daily cap that already binds tighter.  `cycle_cap_basis`
  (`nameplate` default | `faded`) picks the capacity basis of the
  cycle ACCOUNTING (Eq. E47): Year-1 dispatch is identical under both
  (the Year-1 factor is 1), while the projected years are checked
  analytically — the faded-basis utilisation is constant and the
  nameplate one maximal in Year 1 or a replacement reset year, which
  is exactly why the single Year-1 constraint is sufficient.  The
  degradation sheet gains `cycles_on_basis` /
  `warranty_utilisation_pct` columns (only when the cap is set) via
  the new `lifetime.warranty_cycle_utilisation` helper, and the
  pipeline warns when a replacement reset projects utilisation above
  100 %.  Under rolling-horizon dispatch the annual cap applies to
  the deterministic Year-1 solve only (documented; a warning notes
  the combination).

### Added (balancing reservation blocks)

- `bm_block_hours` on the balancing sheet (Eq. B9; default 0 =
  per-settlement-period reservations, bit-identical): with a positive
  value (e.g. 4, the common European capacity-auction block) every
  per-product balancing reservation is pinned to its block-anchor
  value via gated linking equalities (`BM_BLOCK_LINK`), anchored on
  hour-of-year multiples so rolling-horizon windows that bisect a
  block stay aligned with the year grid.  A pure restriction of the
  per-step feasible set: the B1-B8 machinery, the objective and the
  audit invariants apply unchanged, and the blocked objective can
  never exceed the per-step one (locked by a direct two-solve test).
  Validation requires a whole multiple of the dispatch step that
  divides 24 evenly.

### Added (grid import capacity limit)

- `p_grid_import_max_kw` on the project sheet (Eq. S35): a
  connection-point import limit capping grid-to-load plus
  grid-to-BESS charging per step, mirroring the export-cap machinery
  minus the injection profile (a flat limit; same empty / `inf` /
  `unlimited` / `disabled` token parsing, strict positivity when
  finite).  The `IMPORT_CAP` constraint is attached ONLY when the
  value is finite, so an absent / unlimited key changes nothing in
  the model topology (bit-identity); a finite cap also validly
  tightens the no-simultaneous-grid-I/O big-M.  In merchant mode the
  cap collapses to a grid-charging power limit.  A two-tier
  feasibility guard fires at load time: a step whose load exceeds
  PV + BESS power + the cap is rejected pre-solve with the worst
  timestamp and the numbers (the load balance is infeasible for
  every state of charge); load above the cap alone only warns, and
  the solver-level infeasibility error remains the documented
  fallback (the certificate is necessary, not sufficient).  The
  dispatch frame gains a `grid_import_cap_kwh` column (finite caps
  only) and the audit suite a tenth invariant
  (`invariant_10_import_cap_excess_kwh`, Eq. S36, vacuous when
  unlimited).

### Added (low-price-deck debt sizing case)

- `debt_sizing_case = low_price` activates (it previously parsed but
  was rejected with guidance): the target-DSCR debt is sized on the
  yearly cashflow of the price deck named by the new
  `debt_sizing_deck` key (default `low`), obtained by re-dispatching
  the year with that deck's prices through the multi-deck scenario
  machinery - a genuine re-solve (BESS arbitrage adapts to the
  deck's spreads), unlike the cashflow-level P90 haircut; the run's
  solve time roughly doubles and the workbook note says so.  E41-E44
  apply verbatim to the deck CFADS; the deck run forces its own
  sizing / lender / sensitivity extras off so the resolution
  terminates after one level.  Validation requires matching
  `<column>__<deck>` variant columns on the timeseries sheet and
  lists the decks actually available.  The lender case table gains a
  `low_price` row when the sizing case already re-dispatched the
  deck - the same frame serves both surfaces, and the table alone
  never triggers a solve.

### Added (DSCR-profile figure)

- `dscr_profile.pdf` in the financial-plot family: per-year debt
  service coverage over the tenor from the debt_schedule machinery,
  with an optional `DSCR P90 case` companion line (Eq. E44 - same
  committed debt, haircut cashflow) and, in target-DSCR sizing mode,
  the target drawn as a dashed reference series carried in the
  legend (house rule: no computed values as axes text; the DSCR = 1
  break-even line is the same neutral rule line every cashflow
  figure draws at zero).  Rendered only when a debt layer is active
  (`gearing_pct > 0` or `debt_sizing_mode = target_dscr`) and gated
  by the new `plot_dscr_profile` key (default TRUE - all-equity runs
  emit no file, so default output directories are bit-identical).
  Three theme registrations (`DSCR base case`, `DSCR P90 case`,
  `Target DSCR`) with unique palette hexes join the canonical
  label/colour/legend registries and their consistency sweeps.

### Fixed (debt-layer follow-ups)

- The corporate-tax layer's debt-interest schedule now threads the
  yearly CFADS vector, so `debt_repayment = sculpted` combined with
  `corporate_tax_rate_pct > 0` and `gearing_pct > 0` computes the
  sculpted interest deduction instead of raising ("sculpted repayment
  requires the yearly cashflow").
- The run snapshot's `[economic]` section skips internal
  underscore-prefixed keys (derived state such as the frozen sized
  debt); the visible keys that produced them are already in the
  snapshot.

### Added (post-tax financial KPIs)

- `npv_post_tax_eur` / `irr_post_tax_pct` / `equity_irr_post_tax_pct`
  (post-tax equity flows via the E20 schedule) + the post-tax payback
  pair + `total_corporate_tax_eur_lifecycle` /
  `total_depreciation_eur_lifecycle` and a `corporate_tax_rate_pct`
  echo (Eq. E39), reported ALONGSIDE the pre-tax baseline - the
  pre-tax KPI keys and values are untouched in every configuration
  (regression-locked).  Every post-tax KPI is NaN while the rate is 0
  (the all-equity equity_irr precedent: 'n/a' = tax not modelled), so
  the SUMMARY optional-row renderer self-skips the four new rows and
  zero-default digests stay byte-identical.  min_dscr deliberately
  stays pre-tax (a CFADS-based post-tax DSCR is a stated non-goal).
  Users-guide section 'Tax, depreciation and the revenue levy' and a
  README feature paragraph for the Greek contracted + fiscal layer.

### Added (imbalance settlement exposure)

- `imbalance_enabled` on the simulation sheet (default FALSE,
  bit-identical when off — the nomination capture consumes no rng
  draws, so existing Monte Carlo seeds reproduce exactly): ex-post
  settlement of forecast-error deviations on the rolling-horizon
  machinery (Eqs. U6-U9).  Each window's noisy lookahead slice is the
  day-ahead nomination for the next commit block; the realised net
  grid position settles against it at actual prices under a dual-
  (incentive-compatible, non-negative cost) or single-price
  (sign-indefinite) regime, with sign-aware DAM proxies when the
  optional imbalance price columns are absent.  A paired analytic
  PV-only counterfactual yields `bess_imbalance_hedge_value_eur` — the
  quantified co-location benefit of the BESS against deviation
  exposure.  Per-seed columns join the rolling_horizon_mc output; the
  pipeline aggregates mean and P10/P50/P90 into the KPI dict and
  SUMMARY digest; the availability derate applies uniformly (cancels
  in the paired hedge).  The MC mean projects into the yearly cashflow
  as its own `imbalance_cost_eur` column (Eq. E28: PV-curve volume,
  DAM-series prices; PV-shape monthly allocation E28a), excluded from
  LCOE/LCOS, scaled by the Revenue tornado driver, with a lifetime
  total, conditional SUMMARY row and its own "Imbalance cost" figure
  band.

### Changed (aggregator fee template default)

- The `aggregator_fee_pct_revenue` template default drops from 10 % to
  0 % (fee-free; opt-in).  Real-world route-to-market charges are
  typically a few EUR/MWh of sold energy (Greek FoSE representation,
  German Direktvermarktung) or a share of market revenue only — a flat
  10 % of ALL revenue (including self-consumption savings) sits far
  above European market practice, so the template no longer pre-fills
  it.  Existing workbooks keep whatever value they carry (the polish
  script preserves values by key); the shipped `inputs/input.xlsx` case
  study is updated to 0.

### Fixed (fee-audit follow-ups)

- `docs/economics_design.md` carried two equations tagged E9 (the
  availability grid-import correction reused the yearly-cashflow
  stream tag): the import correction is renumbered E8a and the
  implementation map now covers E8a and E13b-E13d.
- A boolean typed into a numeric workbook field is now rejected with a
  clear error naming the sheet and key instead of silently coercing
  (`float(TRUE) == 1.0`) — e.g. `unavailability_pct = TRUE` silently
  became a 1 % availability derate.  To make the guard reliable, the
  workbook kv sheets are now read with openpyxl-faithful cell types
  (`_read_kv_flat`) instead of `pd.read_excel`, which could mis-surface
  a genuinely numeric 0/1 cell as a Python boolean in a mixed-type value
  column and would have tripped the guard on legitimate zeros.  The
  YAML/JSON config path applies the same guard on its faithful native
  types.
- The tornado OPEX and Revenue driver values now report the Year-1
  figures their row labels promise ("Total annual OPEX", "Year-1
  revenue base") instead of lifetime sums, matching the convention the
  CAPEX driver already followed (Year-0 outlay).  The perturbation
  itself is unchanged — it scales every year of the stream by the same
  factor — only the EUR endpoint annotations move.

### Fixed (PVGIS input-path audit)

- Explicit zeros in the PVGIS geometry fields now survive to the
  fetch: `losses_pct = 0` (a loss-free array) was silently replaced
  by the 14 % default through a falsy-`or` fallback, and `azimuth = 0`
  (due south) only worked because it coincided with the default.
  Geometry parsing is now None-explicit — a blank cell / YAML null in
  `tilt`, `azimuth`, `losses_pct` or `weather_year` falls back to the
  documented default, while every explicit value (0 included) passes
  through verbatim; a null no longer risks a `float(None)` crash on
  hand-built configs.
- The "PV data ignored" warning now also fires when a pv-sheet
  `timeseries_path` file carries the PV column while `pv_source`
  resolves to PVGIS (previously only a filled inline `pv_kwh` column
  warned), with one unified message: the location wins and the
  workbook/file PV data is ignored; the price columns of the
  timeseries are consumed as usual.  A field-wiring audit locks the
  full path: every pv-sheet geometry field reaches the PVGIS request
  verbatim from both the Excel and the YAML surface, the fetched
  per-kWp profile is scaled by the nameplate and OVERWRITES any
  workbook PV, and `pv_source = file` never contacts PVGIS even when
  a location is present.

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

- The availability derate now raises grid import instead of scaling it
  down.  Generation, storage, export and revenue all fall with plant
  availability, but the load is fixed exogenous demand that the grid
  must serve in full while the plant is offline, so
  `system_total_import_mwh` becomes `A * import_raw + a * load`
  (`a` the unavailability fraction, `A = 1 - a`).  This closes the
  derated annual energy balance against the never-derated load; the
  previous uniform derate understated import by `a * load` (~1 % of the
  annual load).  The annual energy Sankey
  (`plotting.emissions.plot_energy_sankey`) takes an
  `availability_factor` and applies the identical rule, so its Load node
  reads the true demand and its ribbons conserve energy — the figure now
  agrees with the availability-derated energy tables instead of showing
  the raw dispatch.  Grid import is not a monetised stream, so every
  financial KPI (NPV, IRR, LCOE, LCOS, payback) is unchanged.
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
