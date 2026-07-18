# Market data & price scenarios: design

Domain design document for the two opt-in price layers: automated
market-data ingestion (historical day-ahead and balancing prices per
bidding zone, replacing the workbook columns under override semantics)
and the multi-year, multi-scenario price projection (years 2..N priced
on scenario curves instead of flat inflation indices).  This document
owns the **G** equation-tag namespace (see the registry in
`docs/economics_design.md`).  Notation follows the shared table in
`docs/README.md`.

## Purpose & scope

Two independent switches, both **OFF by default** with bit-identical
outputs when off:

1. **Layer A — market-data ingestion** (`market_data` sheet,
   `pvbess_opt/marketdata/`): a country/bidding-zone selector plus
   per-dataset source keys.  An API source fetches the configured
   historical reference year and **REPLACES** the matching workbook
   price column(s) for the whole horizon — the workbook values are
   used only under the `file` default.  Provenance is recorded on the
   results workbook and the input snapshot re-runs the exact fetched
   prices offline.
2. **Layer B — price scenarios** (`scenario_engine` +
   `price_scenarios` sheets, `pvbess_opt/pricedata/`): the Year-1
   dispatch stays the single MILP anchor, but years 2..N no longer
   reuse Year-1 prices under a flat index.  Per-scenario, per-year
   price curves reach the cashflow through the EXISTING per-year
   escalation machinery (Eq. E24) as auto-generated replace-mode
   trajectories on the split stream taxonomy (Eqs. E60/E61) — an
   input swap, deliberately NOT a new projection equation.  Two
   projection tiers (reprice / resolve), capture-price KPIs, and a
   weighted scenario ensemble sit on top.

The layers compose: Layer A fixes the Year-1 price basis from a real
market year; Layer B projects that basis forward under scenario
assumptions (capture-price cannibalization, BESS spread evolution,
balancing saturation).

## Layer A — market-data ingestion

### Zones and providers

The `bidding_zone` selector resolves through the
`pvbess_opt.marketdata.ZONES` registry (code, ENTSO-E EIC, local
timezone): `gr`, `de_lu`, `fr`, `it_nord`, `es`, `bg`, `ro`.  Sources
per dataset:

| Dataset | Providers | Notes |
|---|---|---|
| Day-ahead prices | `entsoe` (A44 Publication_MarketDocument) | PT60M before 2025-10-01, PT15M after (the SDAC 15-minute MTU switch) — both cadences within one fetched year are stitched. |
| Balancing prices | `entsoe` (A81/A84) or `admie` | The ENTSO-E balancing domain is EMPTY for GR (co-optimised integrated scheduling process), so GR uses the ADMIE file API (`getOperationMarketFile`); `auto` resolves GR → `admie`, every other zone → `entsoe`. An explicit zone/source mismatch raises. |
| Imbalance prices | `entsoe` (A85) or `admie` | Same registry rule. |

The ADMIE file categories and workbook header patterns ship as
PROVISIONAL constants pinned by `scripts/probe_market_data.py` (run
locally with network access; this environment blocks the market
hosts — see `docs/notes/market_data_spike.md`).  The HEnEx daily DAM
workbook serves as an independent cross-check of the GR day-ahead
series (`pvbess_opt/marketdata/henex.py`).

### G1 — Intensive resampling (fixed policy)

Prices are intensive quantities.  Between a native cadence
$\Delta_n$ and the model cadence $\Delta_m$:

$$p^{(m)}_t = p^{(n)}_{\lfloor t \rfloor}\ \ (\Delta_n > \Delta_m,\ \text{step-hold}),\qquad p^{(m)}_T = \frac{1}{k}\sum_{t \in T} p^{(n)}_t\ \ (\Delta_n < \Delta_m,\ \text{mean over the } k \text{ finer steps}) \tag{G1}$$

An hourly price repeats over its four quarters — it is NEVER divided;
finer data averages onto a coarser grid.  Cadences must be
commensurable (whole-multiple), enforced everywhere a curve is laid
onto a grid (`marketdata.resample_intensive`; also reused by the
Layer-B store loader and the Tier-2 re-solve grid).

### G2 — Local-year assembly

A fetched UTC series becomes the workbook's local calendar year via
the zone timezone: the spring-forward gap is filled by repeating the
previous step, the fall-back duplicate keeps the FIRST occurrence,
Feb 29 is dropped.

$$n_{\text{steps}} = 365 \cdot \frac{24 \cdot 60}{\Delta_m},\qquad t_0 = \text{Jan 1, 00:00 local} \tag{G2}$$

Exact step-count and Jan-1 alignment are asserted, never coerced.
Segment stitching across the PT60M→PT15M switch asserts continuity
(no gap, no overlap) in UTC before conversion.

### Bypass (override) semantics

An API source **replaces the whole column** — there is no blending
with workbook values.  On fetch: a consolidated INFO log with
provenance (zone, dataset, reference year, source, cache state), one
`market_data_provenance` row per bypassed column on the results
workbook, and the input snapshot under `01_inputs/` is materialised
with the fetched values written into the timeseries, the source keys
flipped to `file`, and the token cell blanked — the snapshot re-runs
the exact prices offline with no secret embedded.

Fetches cache on disk (the PVGIS pattern: SHA-256 of the request
parameters, JSON payloads) under `market_cache_dir`, with
`market_fetch_mode` choosing `cache_first` / `refresh` / `offline`
(offline errors on a cache miss — reproducible CI runs).  A
process-level memo avoids duplicate fetches within one run.  The
ENTSO-E token is read from the workbook cell or the environment
variable named by `entsoe_token_env`; the shipped template keeps the
cell EMPTY (never commit a token) and logs mask it to its first
8 characters.

### Inputs (`market_data` sheet, key/value)

| Key | Default | Role |
|---|---|---|
| `price_source` | `file` | `file` keeps the workbook DAM column; `entsoe` fetches and replaces. |
| `bidding_zone` | `gr` | Zone registry key (EIC + timezone). |
| `price_reference_year` | 2025 | Historical calendar year fetched as the Year-1 basis. |
| `price_resample_policy` | `step_hold` | The single accepted value (Eq. G1); the key exists so a future alternative is an explicit, versioned choice. |
| `balancing_source` | `file` | `file` / `auto` / `entsoe` / `admie` (registry rule above). |
| `imbalance_source` | `file` | Same options. |
| `entsoe_token` | (empty) | Literal token, or empty to read the env var below. |
| `entsoe_token_env` | `ENTSOE_API_TOKEN` | Environment variable consulted when the cell is empty. |
| `market_cache_dir` | `~/.cache/pvbess/market` | On-disk fetch cache. |
| `market_fetch_mode` | `cache_first` | `cache_first` / `refresh` / `offline`. |

## Layer B — price scenarios

### Scenario store schema

A scenario is a **directory** referenced from the `price_scenarios`
sheet (`store_path`, relative paths resolved against the workbook):

* `meta.yaml` — provider, vintage, zone, currency, basis
  (`nominal` | `real`, `base_year` required for `real`), license,
  provider-specific blocks;
* `dam.csv` / `dam.parquet` — tidy per-year curves
  (`year, step, dam_price_eur_per_mwh`, operating year 1..N, 1-based
  step);
* `balancing_annual.csv` — `year, product, capacity_price_eur_per_mwh,
  activation_price_eur_per_mwh` over
  fcr / afrr_up / afrr_dn / mfrr_up / mfrr_dn (FCR has no
  activation by design).

Curve years follow the Layer-A calendar contract and are laid on by
Eq. G1.  Years past the last declared one hold the last curve
(`hold_last`, logged); missing interior years are a hard error.  Real
vendor curves bridge to the engine basis via the deflator
(`price_basis` + `price_base_year` + `cpi_pct`), so every deck the
engine sees is on ONE declared basis.

Providers on the sheet: `file` (ready-made store), `parametric`
(Eq. G3), `tyndp` (free ENTSO-E TYNDP milestone curves, linear
per-step interpolation between milestones), and four vendor stubs
(`retwin` / `ffe` / `maon` / `afry`) that raise a documented error
until sample deliverables exist — their GR coverage is unconfirmed.

### G3 — Parametric deck

Generated from the workbook's own Year-1 DAM column with three
interpretable knobs (`meta.yaml`, `parametric` block).  With
$\bar p_d$ the daily mean laid back on the steps,
$\delta(t) = p_1(t) - \bar p_d(t)$ the intra-day deviation,
$w(t) = pv(t)/\max pv$ the PV weight, and knobs $\ell$ (level drift),
$d$ (capture decline), $s$ (spread evolution) in %/yr:

$$p_y(t) = \bigl(\bar p_d(t) + \delta(t)\,(1+s)^{y-1}\bigr)\,(1+\ell)^{y-1}\,\Bigl(1 - \bigl(1-(1-d)^{y-1}\bigr)\,w(t)\Bigr) \tag{G3}$$

Solar-hour prices fall faster than the average (the capture-rate
story); the spread path scales deviations from the daily mean
independently of the level path.  Optional per-product
`{capacity,activation}_pct_per_yr` paths build the balancing table
from the workbook's Year-1 per-product prices.

### G4 — Tier-1 reprice factors (`scenario_projection_mode = 'reprice'`)

The frozen Year-1 dispatch is priced against each (scenario, year)
curve; the per-stream factor is the revenue ratio

$$g_s[y] = \frac{R_s(\text{dispatch}_1,\ p_y)}{R_s(\text{dispatch}_1,\ p_1)} \tag{G4}$$

for the three DAM streams (`revenue_dam_pv`,
`revenue_dam_bess_export`, `expense_dam_bess_charge`; volumes from
`pv_to_grid_kwh` / `bess_dis_grid_kwh` / `bess_charge_grid_kwh`) and,
from the store's annual table, per-product balancing capacity /
activation price ratios.  The factors enter Eq. E24 as replace-mode
auto-trajectories on the split taxonomy (Eqs. E60/E61).  Properties:

* the denominator is the DECK's year-1 curve, so $g[1] = 1$ by
  construction and the Year-1 cashflow stays anchored to the
  dispatch-KPI base (the Eq. E24 $m_1 = 1$ contract) — a deck whose
  year-1 level drifts from the workbook's own Year-1 mean beyond
  10 % is flagged (the path is RELATIVE, the base stays the
  dispatch);
* a zero-volume stream keeps a flat factor of 1.0 (inert, no
  division by zero);
* a user-declared trajectory on any engine-owned price stream
  conflicts loudly; opex / retail streams ride along untouched.

The same pass produces the per-year price-path / capture KPI table
(PV capture price and rate against the DAM baseload mean, realized
BESS spread as discharge minus charge capture price, per-product
balancing paths) — the `scenario_price_paths` sheet and the fan /
capture figures.

### G5 — Tier-2 support-year re-solves (`scenario_projection_mode = 'resolve'`)

The reprice tier freezes the dispatch; the resolve tier re-solves the
MILP at the configured support years with that year's scenario prices
AND the degraded plant (PV volume scaled by the analytic PV factor,
BESS capacity by the pooled BESS factor), at a coarser grid
(`scenario_resolve_resolution`, hourly by default), day-ahead stage
only (balancing and intraday blocks OFF — the Eq. E53 midlife
contract).  The cashflow applies
$\text{base}_1 \cdot f_s(y) \cdot g_s[y]$, and a re-solved revenue
$R^{(2)}_s(y)$ already carries the degraded plant, so the Tier-2
factor normalises the analytic degradation back out (no double
counting):

$$g^{(2)}_s[y] = \frac{R^{(2)}_s(y)\,/\,R^{(2)}_s(1)}{f_s(y)} \tag{G5}$$

with $R^{(2)}_s(1)$ a year-1 re-solve at the SAME resolution (the
resolution bias cancels in the ratio).  At a support year the
cashflow reproduces $\text{base}_1 \cdot R^{(2)}_s(y)/R^{(2)}_s(1)$
exactly: the pure price effect PLUS the dispatch adaptation the
frozen-dispatch tier cannot see (SOC re-timing under the new shape).
The re-solves refine the three DAM streams only; balancing paths stay
on the store's annual table.  Tier-1 runs first for every scenario —
the fan and the `scenario_price_paths` sheet deliberately STAY Tier-1
so every scenario compares on the same frozen-dispatch footing — and
the Tier-2 − Tier-1 factor gap is reported per support year on the
`scenario_resolve_delta` sheet (the E53 diagnostic style).

### G6 — Interpolation between support years

$$\log g[y] \text{ linear in } y \text{ between supports (loglinear)};\qquad g[y] = g[y_{\max}] \text{ beyond the last support} \tag{G6}$$

Log-linear keeps multiplicative paths multiplicative; a non-positive
support factor makes the log undefined, so that stream falls back to
LINEAR interpolation with a WARNING.  Year 1 is always forced into
the support set (it anchors the ratios).

### G7 — Weighted scenario ensemble

One dispatch, N cashflows: every enabled scenario applies its factors
to a fresh copy of the economic inputs and rebuilds the yearly
cashflow and financial KPIs — the MILP is never re-solved for the
ensemble (under resolve mode the applied scenario reuses its Tier-2
block verbatim; the others fall back to their reprice factors, a
documented approximation).  With weights $w_i$ (validated to sum to
100 %):

$$\mathbb{E}[\mathrm{NPV}] = \sum_i \frac{w_i}{100}\,\mathrm{NPV}_i,\qquad P_q = \min\Bigl\{\mathrm{NPV}_{(i)} : \sum_{j \le i} w_{(j)} \ge q\Bigr\} \tag{G7}$$

P10/P50/P90 are weighted empirical-CDF percentiles over the DISCRETE
scenario set (no interpolation between scenarios — a P90 that never
occurred in any scenario would be an invented outcome).  E[IRR]
averages over the scenarios with a finite IRR.  Monte Carlo stays
Year-1 forecast-error-only: price-LEVEL risk is the scenario
dimension — no double counting.

**Shared capital structure:** debt is sized ONCE, on
`debt_sizing_scenario` (the applied scenario of the single run; empty
= the first enabled row), and every ensemble member inherits the
frozen sized-debt keys — the table compares OPERATING outcomes on one
committed capital structure.  Pick a downside scenario for bankable
sizing.

### Support-reference rule (`support_ref_follows_scenario`)

With the engine armed, every support REFERENCE leg — the CfD
difference legs (Eqs. E45/E46) and the Eq. E56 settlement reference —
follows one rule: the scenario's PV-leg DAM path when TRUE (the
default: a market reference settles on scenario prices, so the
capture decline reaches the support settlement), or the plain
`dam_inflation_pct` scalar when FALSE (a decoupled administered
index).  Disarmed, each site keeps its historical series — the CfD
legs ride `g_dam_pv`, the E56 reference the scalar — whatever the
toggle says (bit-identity for existing workbooks).  The post-term PPA
reversion and the imbalance stream are MERCHANT flows, not reference
legs; they stay on `g_dam_pv` in every configuration.

### Inputs (`scenario_engine` sheet, key/value)

| Key | Default | Role |
|---|---|---|
| `price_scenarios_enabled` | FALSE | Master switch; FALSE keeps years 2..N on the flat indices, bit-identical. |
| `scenario_projection_mode` | `reprice` | `reprice` (G4) / `resolve` (G5) / `trajectory_only` (declared trajectories only, no auto-generation). |
| `scenario_resolve_years` | `1,5,10,15,20,25` | Tier-2 support years (CSV; year 1 forced in; out-of-lifecycle years rejected). |
| `scenario_resolve_resolution` | 60 | Re-solve grid in minutes; must be a whole multiple of the workbook cadence, never finer. |
| `scenario_interp` | `loglinear` | Eq. G6 (`loglinear` / `linear`). |
| `price_basis` | `nominal` | Engine basis for every deck (the repo's cashflow convention). |
| `price_base_year` | 0 | Base year of `real` vendor curves (deflator bridge). |
| `cpi_pct` | 2.0 | Deflator rate of the real→nominal bridge. |
| `debt_sizing_scenario` | (empty) | Which enabled scenario sizes the debt (empty = first row); every member inherits that schedule. |
| `support_ref_follows_scenario` | TRUE | The support-reference rule above. |

### Inputs (`price_scenarios` sheet, tidy)

Gated by the first row's `enabled` cell.  Columns: `name` (unique),
`provider` (`file` / `parametric` / `tyndp` / vendor stubs),
`vintage`, `weight_pct` (must sum to 100 across enabled rows),
`store_path` (required; resolved against the workbook), `notes`.

## Restrictions

* Scenario curves cover the DAM (and reserved: IDA) plus per-product
  balancing scalars; intraday / imbalance scenario curves are out of
  scope by design (the store schema reserves the columns).  Retail
  stays on its inflation index (self-consumption tariffs are not
  wholesale curves).
* The Tier-2 re-solves run the day-ahead stage only and reject a
  resolution finer than the workbook cadence (detail cannot be
  invented).
* `resolve` needs the dispatch params (the pipeline threads them);
  the arming call sits AFTER the replacement-year resolver in
  `_build_financials` (the pooled degradation factors need the
  effective replacement year) and BEFORE the cashflow build.
* The in-run ensemble re-prices cashflows only; re-solving every
  scenario is the scenarios-harness path, not the in-run ensemble.
* Layer A market hosts are unreachable from CI (egress-blocked);
  fixtures synthesise the documented CIM / ADMIE formats and
  `scripts/probe_market_data.py` verifies the live formats locally.

## Pipeline integration

* `read_workbook` parses the three sheets (absent sheets fall back to
  the canonical defaults); `resolve_market_data` runs after workbook
  validation and before the balancing scalar fallback, so a fetched
  balancing column suppresses the fallback exactly like a workbook
  column would.
* `read_economic_params` merges the `scenario_engine` keys and
  carries the parsed `price_scenarios` block; the `market_data` sheet
  deliberately stays OUT of the economics merge (its token must never
  reach the assumptions sheets).
* `apply_price_scenarios` (the arming entry point) merges the applied
  scenario's factors into `econ['trajectories']`; everything
  downstream — cashflow, LCOE/LCOS OPEX, sensitivity, debt sizing —
  flows through the existing Eq. E24 machinery unchanged.
* Results workbook sheets (all absent while disarmed):
  `market_data_provenance`, `scenario_price_paths`,
  `scenario_resolve_delta`, `price_scenario_ensemble`.  SUMMARY.md
  carries the applied-scenario digest, the Tier-2 line under resolve,
  and the ensemble E[NPV] / percentile lines.
* Figures (emitted only when armed, the conditional-figure pattern):
  `price_path_fan.pdf` (yearly mean DAM price per enabled scenario,
  ordered `SCENARIO_PATH_COLORS` palette) and `capture_kpis.pdf`
  (capture price / capture rate / realized spread, canonical
  financial colours).

## Implementation map

| Equation / rule | Implementation |
|---|---|
| (G1) | `marketdata.base.resample_intensive` (shared by store loader and resolve grid) |
| (G2) | `marketdata.base.sample_local_year`, `stitch_segments_utc`, `validate_model_year_grid` |
| Bypass semantics | `marketdata.base.resolve_market_data`, `materialize_bypassed_workbook`; providers in `marketdata.entsoe` / `marketdata.admie` / `marketdata.henex` |
| Store schema | `pricedata.store.load_scenario_store`, `ScenarioDeck` |
| (G3) | `pricedata.adapters.build_parametric_deck` (TYNDP: `build_tyndp_deck`) |
| (G4) | `pricedata.engine.derive_reprice_trajectories`, `apply_price_scenarios` |
| (G5) | `pricedata.resolve.derive_resolve_trajectories`, `build_resolve_grid`, `build_resolve_delta` |
| (G6) | `pricedata.resolve.interpolate_support_factors`, `parse_support_years` |
| (G7) | `pricedata.ensemble.run_price_scenario_ensemble`, `weighted_percentile` |
| Support-reference rule | `economics.build_yearly_cashflow` (`_g_cfd_ref` / `_g_support_ref`) |
| Split-stream application | `economics.build_yearly_cashflow` (Eqs. E24/E60/E61, `_split_dam` gating) |

## Verification log

- `tests/test_marketdata_calendar.py` — Eq. G1 resampling rules
  (step-hold / mean, commensurability; hourly→15-min step-hold
  preserves revenue exactly for a fixed dispatch), UTC stitching
  continuity across the 2025-10-01 SDAC switch, Eq. G2 DST fill/drop
  and Feb-29 handling, a hand-computed one-day revenue to the cent.
- `tests/test_marketdata_entsoe.py` — A44 parsing across the
  PT60M/PT15M switch, Acknowledgement (no-data) handling, window
  bisection, balancing/imbalance document parsing with MW→MWh unit
  normalisation, token resolution and masking.
- `tests/test_marketdata_admie.py` — daily-workbook header patterns,
  nominal-day DST normalisation, missing/duplicate day errors, the
  provisional category constants.
- `tests/test_marketdata_henex.py` — GR DAM cross-check workbook
  parsing, version fallback, divergence statistics.
- `tests/test_marketdata_io.py` / `tests/test_workbook_schema.py` —
  the `market_data` sheet surface, defaults bit-identity, bypass
  column replacement + provenance, source resolution (`auto` per
  zone, explicit mismatches), fetch modes and cache behaviour,
  snapshot materialisation (source keys flipped, token blanked),
  schema / template / polish-script parity.
- `tests/test_pricedata_store.py` — meta validation, basis bridge,
  hold_last vs interior-gap errors, balancing table rules, stub
  providers.
- `tests/test_pricedata_engine.py` — Eq. G4 closed-form micro-cases
  (constant / uniform / cannibalization asymmetry / charge-leg /
  spread / hold_last / balancing products), zero-volume guard, arming
  semantics (selection, `debt_sizing_scenario`, conflicts, gating,
  armed marker), the resolve-branch wiring (Tier-2 overrides, Tier-1
  fan, delta table).
- `tests/test_pricedata_resolve.py` — Eq. G5 factor contracts with a
  faked MILP (price-ratio tracking, degradation normalised out,
  market blocks off), Eq. G6 interpolation (geometric between
  supports, linear fallback, year-1 anchor), grid resampling rules,
  support-year parsing, delta table.
- `tests/test_pricedata_ensemble.py` — Eq. G7 weighted stats and
  percentile convention, shared-debt inheritance, verbatim reuse of
  the applied block, weight validation, the results-sheet stat rows.
- `tests/test_support_ref_follows_scenario.py` — the
  support-reference rule: armed decoupling (CfD and E56, closed
  form), disarmed bit-identity under either toggle value.
- `tests/test_pricedata_io.py` — `scenario_engine` /
  `price_scenarios` sheet parsing (weight sum, unique names, provider
  enum, store_path required), YAML surface, writer gating of the four
  results sheets, SUMMARY gating.
- `tests/test_pricedata_plots.py` /
  `tests/test_plotting_universality.py` — figure smoke + palette
  registrations under the universality rules.
- `tests/test_trajectory_application.py` — the Eq. E24/E60/E61
  machinery the factors feed (bit-identity, equivalence locks,
  stream routing).
- `tests/test_pricedata_pipeline.py` (slow) — two end-to-end runs
  through `pipeline.run`: armed reprice (factors reach the cashflow,
  sheets + figures + SUMMARY + ensemble) and armed resolve (delta
  sheet lands, Tier-1 paths preserved, zero-volume charge stream on
  the flat-factor guard).
