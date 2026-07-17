# Market-data spike notes (P0)

Pre-implementation reconnaissance for the market-data ingestion layer
(`pvbess_opt/marketdata/`) and the multi-year price-scenario layer
(`pvbess_opt/pricedata/`).  Working notes, not a design document — the
design ships as `docs/market_scenarios_design.md` with the feature.
Nothing in this phase changes behaviour.

## 1. Grid-assumption audit (hardcoded 35 040 / 8 760 / steps-per-hour)

Every hit of `35040|8760|8784|steps_per_hour` in `pvbess_opt/` and
`scripts/`, classified:

| Site | Kind | Verdict |
|---|---|---|
| `pipeline.py:1526` (`len(ts) / 35040.0 * 126.5`) | Monte-Carlo runtime estimate | Benign — scaled by the actual step count; a coarser grid estimates lower, nothing binds. |
| `pipeline.py:1542-1544` (`ref_steps = 35040.0`) | balancing solve-time estimate | Benign — same scaling pattern. |
| `economics.py:3205` (`pv_kwp * 8760.0 / 1000.0`) | `pv_capacity_factor` KPI (Eq. E23) | Annualisation constant by industry convention.  On an 8 784-step leap workbook the CF is overstated by 24/8784 ≈ 0.27 % — cosmetic, KPI-only, no cashflow impact. |
| `resource/pvgis.py:28` (`HOURS_PER_NON_LEAP_YEAR = 8760`) | PVGIS provider contract | By design: the provider rejects leap weather years with a clear error (`tests/test_resource_pvgis.py:69-71`). |
| `resource/resample.py` (`8760 * steps_per_hour`) | hourly→grid upsample | By design, and **energy-only**: it splits each hourly kWh across sub-steps (extensive quantity).  Prices are intensive — the marketdata layer must step-hold (repeat), never divide.  The two conventions already coexist in `scripts/resample_timeseries.py` (flows split/sum, stocks ffill/mean). |
| `timeutils.py:47-63` (`apply_fixed_utc_offset`) | fixed-offset UTC shift (`np.roll`) | The repo's uniform-grid convention.  Its docstring explicitly delegates: "callers that need wall-clock DST alignment must re-grid the transition days first" — the marketdata layer is that caller (fill spring-forward, drop fall-back, per zone). |
| `io.py:2722` (`detect_timestep_minutes`) | cadence auto-detection | Grid-agnostic: single regular step required, no year-length assumption anywhere in the loader. |

Conclusion: the engine is **cadence-agnostic and year-length-agnostic**
(the model grid is whatever the workbook carries; tests pin 35 040 for
the shipped workbook only).  The only year-length couplings are the E23
annualisation constant and the PVGIS 8 760 contract.  A fetched market
series therefore has exactly one obligation: match the workbook grid in
length and alignment (the §4.A invariant the P1 loader asserts).

## 2. Smoke runs (hourly + leap-year workbooks)

Method: the shipped `inputs/input.xlsx` (15-min, calendar 2026,
35 040 steps) was downsampled to 60 min (prices averaged, energies
summed), and a leap variant was built by extending the hourly frame to
8 784 rows re-stamped onto calendar 2024 (Feb 29 included).  Both ran
end-to-end through `python main.py <wb>` (HiGHS).

* **Hourly (8 760 steps, 2026):** end-to-end SUCCESS (exit 0).  Loader
  auto-detects `dt_minutes = 60`; MILP, invariant verification, KPI /
  cashflow / plot / report stack all complete (LCOE 44.78 EUR/MWh,
  capacity factor 0.1695, 20-year projection).  Confirms the pipeline
  has no 15-minute assumption anywhere on the default path.
* **Leap year (8 784 hourly steps, calendar 2024, Feb 29 included):**
  end-to-end SUCCESS (exit 0).  The loader accepts the 366-day grid
  (`detect_timestep_minutes` checks regularity, not year length), the
  MILP dispatches all 8 784 steps, and the lifetime projection's
  Feb-29-safe year shift (`lifetime.py:649-668`, `relativedelta`)
  handles the leap timestamps.  `pv_capacity_factor` came out 0.1697
  vs 0.1695 on the 8 760-step run — the predicted ≈0.27 % E23
  overstatement (the divisor stays 8 760), cosmetic and KPI-only.
  Conclusion for the marketdata layer: dropping Feb 29 from fetched
  data (the industry 8 760 convention) is a *policy* choice for
  grid alignment with the shipped non-leap workbook, not an engine
  requirement — a user-supplied leap workbook still runs.

Two workbook-handling hazards surfaced while building the variants —
both directly relevant to P1's "materialise the fetched workbook copy"
requirement:

1. **kv sheets do not survive a pandas round-trip.**  The parameter
   sheets carry a mixed-type `value` column; `pd.read_excel` infers the
   column and mis-surfaces a genuinely numeric 0 as Python `False`
   (exactly the failure mode the `_read_kv_flat` docstring at
   `io.py:4055-4064` documents — the loader reads kv sheets through
   openpyxl `values_only` for this reason).  Writing such a frame back
   with `DataFrame.to_excel` materialises a real boolean cell, and the
   loader then rejects the workbook (`Sheet 'project':
   'grid_charging_fee_eur_per_mwh' expects a number, got boolean
   False`).  `scripts/resample_timeseries.py` carries non-timeseries
   sheets through exactly this pandas round-trip, so its output
   workbook fails to load against the current schema.  Any workbook
   materialisation must copy sheets via openpyxl (the
   `scripts/polish_input_workbook.py` pattern), never via pandas.
2. **The resample script's column whitelist is stale.**
   `_PRICE_COLS`/`_ENERGY_COLS` (`scripts/resample_timeseries.py:52-53`)
   recognise only `dam/retail` prices and `load/pv` energies; the nine
   balancing price columns, `ida_price_eur_per_mwh` and the imbalance
   columns are silently DROPPED on resample (they fall to their scalar
   fallbacks / loader gates downstream).  Related: with
   `balancing_enabled=TRUE`, `bm_settlement_minutes` must equal the
   workbook cadence (`io.py:2913-2919`), so a resampled balancing
   workbook also needs that key updated in step.  Not fixed here (P0 is
   no-behaviour-change); the marketdata layer sidesteps it by fetching
   at (or resampling to) the model grid directly.

## 3. Probe script + network status

`scripts/probe_market_data.py` (new, this phase) probes the three
endpoint families of the design: ENTSO-E A44 day-ahead (two windows
bracketing the 2025-10-01 SDAC 15-min MTU go-live, so the PT60M/PT15M
mix is observed directly), A81 contracted balancing capacity per
product (FCR A52 / aFRR A51 / mFRR A47), A84 activated balancing
energy prices, A85 imbalance prices — for GR plus a DE_LU control zone
— then the ADMIE `getOperationMarketFile` candidate-category sweep and
the HEnEx daily DAM workbook URL versions.  Token resolution follows
the future `market_data` sheet contract (workbook `entsoe_token` key →
`entsoe_token_env`-named env var, default `ENTSOE_API_TOKEN`); the
token is never printed (masked to its first 8 characters).  Responses
are dissected (CIM XML vs ZIP-of-XML vs `Acknowledgement_MarketDocument`
no-data marker; TimeSeries/Point counts; distinct `resolution` values)
and optionally saved with `--save-dir` as candidate test fixtures.

**Network result from this run environment:** the egress policy denies
CONNECT to all three hosts (`web-api.tp.entsoe.eu`, `www.admie.gr`,
`www.enexgroup.gr` — gateway 403 per the local proxy diagnostics), so
the live probe could not execute here.  The script degrades gracefully
(per-request failure lines, exit 0).  ACTION for a network-enabled
machine: `python scripts/probe_market_data.py --save-dir
tests/fixtures/marketdata/recorded` to pin (a) the exact ADMIE
`FileCategory` names and per-file xlsx column maps, and (b) recorded
ENTSO-E bodies for the GR/DE_LU dataset-availability matrix (the
expectation from the design notes: GR A44 populated; GR 17.1.B/C/F/G
effectively empty — Greek balancing comes from ADMIE).

Consequence for P1/P2 test assets: CI fixtures are synthesised to the
documented IEC 62325 CIM formats (A44 `Publication_MarketDocument`
including a mixed PT60M+PT15M reference-year stitch, the ZIP envelope,
the Acknowledgement no-data case) rather than recorded from live
responses; the ADMIE category names ship as a data registry with the
probe as the pinning tool, and recorded-fixture refresh is a documented
local step, not a CI dependency.

## 4. Anchor re-verification (design notes → code, this checkout)

Re-verified at v1.2.x HEAD while auditing:

| Anchor | Verified location |
|---|---|
| Trajectories: streams / modes / inflation-key map | `io.py:1721-1748` (`TRAJECTORY_STREAMS`, `_TRAJECTORY_MODES`, `_TRAJECTORY_INFLATION_KEYS`) |
| Eq. E24 escalation helper (replace/overlay, hold-last) | `economics.py:578-608` (`_escalation_series`) |
| Defect 1 — net BESS spread on the PV index | `economics.py:980-982` (`rev1_dam_bess = export − grid charge`), applied `economics.py:1309-1311` on `g_dam` |
| Defect 2 — aggregate balancing escalation | `economics.py:1399-1404` (`bm_cap/act × bess_factor × g_bm_*`), per-product Year-1 bases already in `kpis.py:926-940` |
| CfD/FiP DAM-leg coupling to `g_dam` | `economics.py:1340-1360` (in-term CfD legs + post-term physical reversion) |
| Lifetime per-year copy: prices never scaled | `lifetime.py:641-678` (chunk copy; PV/BESS column factor loops only) |
| Mid-life re-solve template for Tier-2 | `pipeline.py:931-1041` (`_run_midlife_resolve`) |
| Price-deck loader (external CSV/Parquet) | `io_read.py:261-319` (`_resolve_price_decks`); Parquet needs an optional engine (pyarrow is NOT in `requirements/base.txt`) — the scenario store treats CSV as first-class accordingly |
| Deck-variant registry + ffill whitelist | `io.py:2828` (`PRICE_DECK_BASE_COLUMNS`), `io.py:2667-2672` (ffill loop), `io.py:2799` (`_BALANCING_TS_COLUMN_DEFAULTS`) |
| PV-source resolution hook (provider pattern) | `io.py:4238-4245` (`resolve_pv_source` call site), `resource/pvgis.py` (cache key = sha256 of sorted request params, JSON payload `{params, data}`, provenance metadata dataclass) |
| JSON-schema autogeneration from `_SHEET_DEFAULTS` | `io_read.py:752-822` (`config_json_schema` — a new kv sheet registers itself; top-level blocks like `price_decks` need explicit entries) |
| Workbook migration pattern | `scripts/polish_input_workbook.py` (`_PARAMETER_SHEETS`, `_ensure_parameter_sheets`, `_ensure_trajectories_sheet`, `_sync_param_sheet` preserves existing values by key — so a user-entered `entsoe_token` survives polishing while the template default stays empty) |
| Scenario overrides surface | `scenarios.py:87-93` (`_OVERRIDE_SECTIONS` — extended by the new sheets), `scenarios.py:111-122` (`price_deck` special) |

Solve-time reference confirmed at `pipeline.py:1536-1544` (~565 s for
35 040 steps with balancing; linear-ish in steps) — hourly support-year
re-solves (Tier-2) are the only affordable default cadence, matching
the design's tiered engine.
