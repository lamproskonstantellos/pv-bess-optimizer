# Intraday market participation: design

Domain design document for participation in the intraday auction (IDA)
as a second wholesale venue next to the day-ahead market (DAM):
two-stage sequential re-dispatch, the intraday block of the MILP, the
settlement wiring through the financial stack, and the uncertainty
coupling.  This document owns the **I** equation-tag namespace (see the
registry in `docs/economics_design.md`).  Notation follows the shared
table in `docs/README.md`.

## Purpose & scope

A merchant plant can correct its committed day-ahead position close to
delivery on the intraday auctions: sell additional energy into high IDA
prices, or buy back committed volume when the IDA price drops below the
value of the stored energy.  The model treats this as **two-stage
sequential re-dispatch**:

1. **Stage 1 (unchanged):** the existing solve commits the day-ahead
   dispatch on `dam_price_eur_per_mwh` — deterministic run or
   rolling-horizon windows, exactly as without the feature.
2. **Stage 2:** the SAME model is re-solved with the Stage-1 day-ahead
   net position pinned as data and an intraday block added: per-step
   IDA sells (from incremental PV export or BESS discharge) and IDA
   buys (reducing export or charging the BESS), bounded by a deviation
   cap and the unchanged physical constraint families.

Two rejected alternatives, for the record: a single co-optimised
dispatch against the better of the two venues assumes ex-ante venue
picking with perfect knowledge of both prices — a pure upper bound that
cannot carry deviation limits or the DA-then-ID information structure;
ex-post spread capture on the frozen DA dispatch cannot re-cycle the
BESS (the main intraday value driver) and has no clean physical energy
accounting.  Sequential re-dispatch reuses the existing skeleton — the
intraday block extends `build_model` the same way the balancing block
does — so invariants, `model_to_dataframe`, KPI plumbing and the
Monte Carlo actuals-restore path carry over.

The extension is fully **opt-in**: with `id_enabled = FALSE` (the
default) the MILP, KPI dict, cashflow, Monte Carlo output and PDF
report are bit-identical to a workbook without the sheet.

**Physical-only trading:** every intraday trade maps to a real energy
flow change in the same settlement period; pure financial position
closing is excluded (Eq. I5).  This keeps the energy balance and the
availability-derate story exact.

**v1 restrictions (loader-enforced, `io._validate_intraday_config`):**

- Merchant mode only (`mode = merchant`); the load-priority constraint
  family of the self-consumption regime interacts with IDA buys and is
  deferred until that coupling is designed.
- A finite positive `p_grid_export_max_kw` is required — the deviation
  cap (Eq. I2) is defined as a fraction of it.
- Mutually exclusive with `balancing_enabled` (reservations commit
  day-ahead; a combined two-venue re-dispatch would re-decide them),
  with `ppa_enabled` and the support schemes (both settle volumes the
  re-dispatch would move), with `uncertainty_enabled` (the
  rolling-horizon Monte Carlo is single-stage until the two-stage
  benchmark lands) and with `midlife_resolve_year` (the diagnostic
  re-solves the day-ahead stage only).
- Stage 2 runs deterministically against actual IDA prices, defensible
  because intraday trades commit close to delivery; forecast noise on
  the IDA column is wired into the rolling-horizon machinery as mild
  optimism is quantified there (`docs/uncertainty_design.md`).
- `id_max_deviation_frac_of_cap = 0` disables trading: the pipeline
  skips the Stage-2 solve (the committed dispatch is already the
  result) instead of pinning every flow to a zero-slack equality.

## Inputs

The optional `intraday` sheet carries **5 keys** (kv structure like
every parameter sheet; the shipped workbook keeps the master switch
off):

| Key | Default | Unit | Meaning |
|---|---|---|---|
| `id_enabled` | FALSE | bool | Master switch for the IDA venue. |
| `id_max_deviation_frac_of_cap` | 0.25 | - | Per-step bound on the traded intraday volume as a fraction of `p_grid_export_max_kw` x dt (Eq. I2). |
| `id_allow_purchases` | TRUE | bool | Allow IDA buys (physical only; BESS charging from buys additionally requires `allow_bess_grid_charging`). |
| `id_fee_eur_per_mwh` | 0.0 | EUR/MWh | Venue trading fee on both buy and sell volume (Eq. E59). |
| `id_inflation_pct` | 0.0 | %/yr | Yearly indexation of the intraday margin in the multi-year cashflow. |

The quarter-hourly (or workbook-cadence) auction price arrives as the
`ida_price_eur_per_mwh` timeseries column.  The column is **required**
when `id_enabled = TRUE`: unlike the balancing capacity prices there is
deliberately no scalar fallback, because a constant IDA price produces
zero spread against a constant fallback and silently misleading
results.  The column is forward-filled/NaN-validated alongside
`dam_price_eur_per_mwh` and accepts price-deck variant columns
(`ida_price_eur_per_mwh__<deck>`).

**Cadence note:** the column is consumed at the workbook cadence.  At
15-minute cadence it is the native IDA granularity; on an hourly
workbook it is the hour-averaged IDA price, which averages away
sub-hourly spread (an INFO log points to
`scripts/resample_timeseries.py`).

## Equations

### I1 — Day-ahead net position (data into Stage 2)

For every step `t` of the committed Stage-1 dispatch, the day-ahead net
grid position

```
g_DA_t = x_pg_t + x_bg_t - x_gb_t
```

(PV-to-grid plus BESS-to-grid minus grid-to-BESS, in kWh per step) is
extracted from the Stage-1 result frame and enters the Stage-2 solve as
a fixed data column.  Stage 2 settles the day-ahead position at the DAM
price regardless of the re-dispatch; only deviations from `g_DA_t`
trade at the IDA price.

### I2 — Deviation cap

Per step, the total traded intraday volume is bounded by a fraction of
the connection-cap energy:

```
id_sell_pv_t + id_sell_bess_t + id_buy_t <= delta * P_G * dt,
delta = id_max_deviation_frac_of_cap
```

a liquidity and TSO nomination-change proxy.  The combined physical
injection additionally honours the unchanged per-step injection cap
(the S15/S16 basis): because every trade is a physical flow change
(Eq. I5), `g_DA_t + id_sell_t - id_buy_t` IS the plant injection and
the existing `EXPORT_CAP` family bounds it — no second cap constraint
is needed.

### I3 — Stage-2 objective increment

The Stage-2 solve maximises the day-ahead objective plus the intraday
margin in **spread form**:

```
Pi_ID = sum_t [ (pi_IDA_t - pi_DAM_t) * (id_sell_t - id_buy_t)
                - phi_id * (id_sell_t + id_buy_t) ] / 1000
```

Because the model prices every physical flow at the DAM and
`dam*physical + (ida-dam)*(sell-buy) = dam*g_DA + ida*(g - g_DA)`, the
committed position settles day-ahead and only the deviation trades at
the IDA price — the day-ahead revenue terms stay structurally
untouched.  The existing `bess_wear_cost_eur_per_mwh` term runs on
physical discharge, so it automatically prices the INCREMENTAL Stage-2
throughput: thin spreads do not re-cycle the battery.  A tie-break
penalty (1e-6 EUR/kWh on traded volume, an order below the curtailment
tie-break) makes zero-spread steps deterministically trade nothing.

### I4 — Origin split

Each origin's Stage-2 flow equals its committed day-ahead leg plus the
origin's intraday delta:

```
x_pg_t                = x_pg_t^DA + id_sell_pv_t  - id_buy_pv_t
x_bg_t - x_gb_t       = (x_bg_t^DA - x_gb_t^DA) + id_sell_bess_t - id_buy_bess_t
```

Summing both recovers the net-position identity of Eq. I1.  The split
feeds the route-to-market volume bases and the per-origin degradation
indexing of the multi-year cashflow.

### I5 — Physical-only trading

Every intraday trade maps to a physical flow change in the same step;
pure financial position closing (sell-then-buy-back with no dispatch
change) is excluded by a per-step complementarity binary:
`id_sell_t <= delta*P_G*dt * y_t` and `id_buy_t <= delta*P_G*dt *
(1 - y_t)`, whose shared big-M is the deviation budget — the pair
jointly enforces Eqs. I2 and I5.  IDA buys are gated by
`id_allow_purchases`; BESS charging from buys additionally requires
`allow_bess_grid_charging` (otherwise a buy can only reduce committed
discharge).

## Dispatch invariants (INV-I1..INV-I4)

`optimization.verify_dispatch_invariants` extends its report with four
intraday residuals (`INTRADAY_INVARIANT_KEYS`), 0.0 (vacuously
satisfied) whenever the Stage-2 block did not fire:

* `invariant_i1_position_link_kwh` — net-position link (Eq. I1).
* `invariant_i2_deviation_cap_excess_kwh` — deviation cap (Eq. I2).
* `invariant_i3_sell_buy_overlap_kwh2` — no wash trading (Eq. I5;
  kWh^2 product, strict-mode tolerance `tol^2` like invariant 5).
* `invariant_i4_origin_split_kwh` — origin split (Eq. I4).

## Settlement through the financial stack (Eqs. E58/E59, I6)

The Year-1 KPI pair (`id_net_revenue_eur`, `id_venue_fee_eur`) and the
per-origin volume split feed the multi-year cashflow rows
`intraday_revenue_eur` / `intraday_fee_eur` — per-origin fade indexed
by `id_inflation_pct`, flat venue rate on the fading traded volume;
see the E58/E59 section of `docs/economics_design.md` for the full
projection and monthly-reconciliation rules.

### I6 — Fee applicability matrix (normative)

| Fee surface | Applies to the intraday stream? |
|---|---|
| Energy-aggregator ad-valorem fee (E13) | **No** — intraday intermediation is priced by the explicit venue fee (E59); an ad-valorem share on top would double-charge it (the balancing/E13b precedent).  This supersedes the pre-merge design note that had the margin join the E13 base. |
| Balancing-aggregator / BSP fee (E13b) | No — balancing-only by definition. |
| Route-to-market per-MWh fee (E13c) | **Yes, automatically** — the volume bases (`pv_export_mwh` / `bess_export_mwh`) are computed from the Stage-2 frame, so ID sells raise and ID buys lower the charged export without a new term. |
| Optimizer revenue share (E13d) | **Yes** — the BESS-origin ID margin joins the share base in both variants (optimizers charge on total trading margin), still zero-clamped; the same leg joins the E25a netting base. |

Classification: EXCLUDED from LCOE/LCOS (revenue-agnostic metrics,
market fees excluded); both lifetime totals are SUMMARY-optional
rows; the availability and curtailment derates scale the seven
`id_*` KPI keys; the Revenue tornado driver scales the margin (a
price spread times volume) but not the venue fee (volume-based).
Figures: an `Intraday revenue` band (cyan `#26C6DA`) joins the
positive stack and an `Intraday fee` band (pink `#E91E63`) the
deduction stack of the yearly/monthly/NPV/lifecycle cashflow plots,
each drawn only when non-zero; the lifetime dispatch sheet rebuilds
`id_revenue_eur` / `id_fee_eur` from the per-origin-scaled trades so
the two sheets agree.

## Pipeline integration

`pipeline._run_one` re-solves after the deterministic Stage-1 run via
`intraday.redispatch_intraday` (the FULL-PRECISION Stage-1 frame is
pinned — a round(4) frame would force spurious micro-trades to absorb
the rounding noise) and the Stage-2 frame becomes the headline result:
cycles, degradation, KPIs and the financial stack reflect the combined
DA + ID operation.  The Stage-1 profit is kept as
`id_stage1_profit_total_eur` for the two-stage uplift audit.  The
E58/E59 settlement rows of the multi-year cashflow land with the
economics layer; this document is extended in place as they ship.

## Implementation map

| Equation | Implementing symbol |
|---|---|
| (I1) | `intraday.extract_da_position` / `io.read_workbook` (input surface: `intraday` sheet, `ida_price_eur_per_mwh` column, `rolling_horizon.PRICE_COLUMNS` registration) |
| (I2) | `optimization.build_model` (`ID_SELL_GATE` / `ID_BUY_GATE`, shared deviation big-M; `EXPORT_CAP` reused for the combined injection) |
| (I3) | `optimization.build_model` (`intraday_margin_expr`, spread form + venue fee + wear on physical throughput) |
| (I4) | `optimization.build_model` (`ID_LINK_PV` / `ID_LINK_BESS`); `kpis.compute_kpis` (`id_sell_pv_mwh` / `id_sell_bess_mwh`) |
| (I5) | `optimization.build_model` (`y_id` complementarity binary, `ID_NO_BUY` purchases gate) |
| (I6) | `economics.build_yearly_cashflow` (E13 exclusion, E13d/E25a base extensions, E13c via Stage-2 volume bases) |

## Verification log

- `tests/test_intraday_io.py` — sheet-absent vs defaults bit-identity
  on the typed dict and the flat `(params, ts)`; the
  required-column loader gate; deviation-fraction and fee range
  validation; the hourly-cadence INFO note; scenario dotted-target
  overrides (`intraday.id_enabled`); YAML round-trip; the
  `PRICE_COLUMNS` registration (noise-eligible, not actuals-only).
- `tests/test_workbook_schema.py` — the `intraday` sheet key set, its
  presence in the shipped workbook, and the schema/template parity.
- `tests/test_rolling_horizon_price_restore.py` — the actuals-restore
  contract covers `ida_price_eur_per_mwh` via the `PRICE_COLUMNS`
  sweep.
- `tests/test_intraday_dispatch.py` — zero-spread/zero-fee Stage-1
  identity; spread monotonicity; two-stage uplift with consistent
  settlement columns; deviation-budget and export-cap safety;
  purchases gate; curtailed-PV re-sale; wear-cost coupling on thin
  spreads; INV-I residuals within tolerance on the Stage-2 frame and
  vacuously 0.0 when disabled; Stage-1 bit-identity of a two-stage
  run against a venue-off build; the config-resolver coercions and
  the day-ahead position extractor identity (Eq. I1).
- `tests/test_intraday_cashflow.py` — zero-default cashflow
  bit-identity; the E58 per-origin fade and E59 flat-rate rows; the
  net-cashflow identity; the I6 decisions (E13 exclusion, E13d
  extension + clamp, E25a membership); monthly/quarterly
  reconciliation; lifetime totals + LCOE/LCOS invariance; SUMMARY
  gating; the sensitivity component list, scaling decisions and the
  `_scale_revenue(cf, 1.0)` no-op; the operating derates on the
  `id_*` keys; the theme registrations; the lifetime per-origin
  settlement recompute.
