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

**v1 restrictions:**

- Merchant mode only (`mode = merchant`); the load-priority constraint
  family of the self-consumption regime interacts with IDA buys and is
  deferred until that coupling is designed.
- Stage 2 runs deterministically against actual IDA prices, defensible
  because intraday trades commit close to delivery; forecast noise on
  the IDA column is wired into the rolling-horizon machinery as mild
  optimism is quantified there (`docs/uncertainty_design.md`).

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

The I2-I5 dispatch equations (deviation cap, Stage-2 objective, origin
split, physical-trade restriction) and the E58/E59 settlement rows land
with the dispatch and economics layers; this document is extended in
place as they ship.

## Implementation map

| Equation | Implementing symbol |
|---|---|
| (I1) | `io.read_workbook` / `io._typed_to_flat` (input surface: `intraday` sheet, `ida_price_eur_per_mwh` column, `rolling_horizon.PRICE_COLUMNS` registration) |

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
