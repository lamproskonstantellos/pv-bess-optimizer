# Merchant mode — design

Domain design document for the `mode = merchant` regulatory regime:
pure utility-scale DAM dispatch with no co-located load.  Sibling of
`docs/self_consumption_design.md` (which holds the shared constraint
statements); notation follows `docs/README.md`.

## Purpose & scope

A standalone PV plant, standalone BESS, or co-located hybrid selling
exclusively into the day-ahead market, optionally stacking balancing
services (`docs/balancing_market_design.md`) and a pay-as-produced PPA
(`docs/ppa_design.md`).  No retail tariff, no load priority, no
avoided-cost stream.  The regulatory grid-connection injection cap of
MD YPEN/DAPEEK/53563/1556/2023 applies **unconditionally** — merchant
mode never skips curtailment.

In the loader, `load_kwh` is optional in this mode: when present it is
read (an INFO message notes it) and the optimizer pins all
load-coverage flows to zero regardless.

## Inputs

The merchant-relevant subset (full reference:
`docs/source/users.guide/inputs.rst`):

| Sheet | Key | Default | Role |
|---|---|---|---|
| project | `mode` | `self_consumption` | set to `merchant` |
| project | `p_grid_export_max_kw` | 5000.0 | $P^{G}$; `inf`/empty disables |
| project | `allow_bess_grid_charging` | FALSE | grid-charge arbitrage (essential for BESS-only) |
| project | `grid_cap_includes_load` | FALSE | **no-op in merchant** (see Eq. M3 note) |
| project | `retail_tariff_eur_per_mwh` | 120.0 | unused by the merchant objective |
| pv / bess sheets | capacities, efficiencies, SOC bounds, cycles | — | as in the SC doc |
| timeseries | `pv_kwh`, `dam_price_eur_per_mwh` | — | `load_kwh` optional/ignored |
| balancing / ppa | their full key sets | off | optional stacking |

Asset configurations (the loader/test taxonomy):

* **hybrid** — `pv_nameplate_kwp` > 0 and `bess_power_kw` > 0;
* **pv_only** — `bess_power_kw = 0` (every BESS flow, SOC, and binary
  pinned by the `NOBESS_*` constraints);
* **bess_only** — `pv_nameplate_kwp = 0` (every PV flow pinned by
  `NOPV_*`); economically meaningful with
  `allow_bess_grid_charging = TRUE` (DAM arbitrage: buy at
  $\pi^{\mathrm{DAM}}$ low, sell high).

## Mathematical formulation

### Mode-pinning constraints

$$\mathrm{MERCHANT\_NO\_PV\_TO\_LOAD}: \; x^{pl}_t = 0, \quad
\mathrm{MERCHANT\_NO\_BESS\_TO\_LOAD}: \; x^{bl}_t = 0, \quad
\mathrm{MERCHANT\_NO\_GRID\_TO\_LOAD}: \; x^{gl}_t = 0 \tag{M1}$$

### Constraints absent in merchant

Relative to `self_consumption`
(`docs/self_consumption_design.md` Eqs. S6–S9, S17): `LOAD_BAL`,
`LOAD_PV_PRIORITY`, `LOAD_PRIORITY_SLACK_DEF`,
`LOAD_PRIORITY_EXPORT`, and `NO_SIM_GRID_IMPORT` /
`NO_SIM_GRID_EXPORT` are not built — the `slack` and `y_grid_io`
variables do not exist on a merchant model.  The audit verified that
simultaneous grid import/export does not occur in merchant dispatch
(grid import exists only as `grid_to_bess`, which never coincides
with profitable export of the same energy at the same price), so the
big-M pair is omitted for model size.

### Constraints shared with self-consumption (unchanged)

`PV_SPLIT` (S5), `SOC_DYN`/`SOC_MIN`/`SOC_MAX` (S10),
`SOC_INIT`/`SOC_TERM`[`_MIN`/`_MAX`] (S11–S13),
`CH_LIM`/`DIS_LIM`/`MODE_LINK` (S14), `EXPORT_CAP` (S15) and the
optional `EXPORT_CAP_PV`/`EXPORT_CAP_BESS` sub-caps (S16), `CYC`
(S19), `GRID_CHARGE_GATE`/`GRID_CHG_PV_GATE` (S20, only when grid
charging is enabled; the PV-zero gating keeps grid charging out of
PV-producing steps in both modes), the asset-pinning families
`NOPV_*`/`NOBESS_*`, and the balancing block
(`docs/balancing_market_design.md`) when enabled.

### Cap basis in merchant

$$g_t = x^{pg}_t + x^{bg}_t \quad \text{(always surplus export)} \tag{M2}$$

With $x^{pl}_t = x^{bl}_t = 0$ (Eq. M1), the strict total-injection
basis would collapse to Eq. (M2) anyway, so
`grid_cap_includes_load = TRUE` is a **no-op** in merchant mode; a
once-per-process warning (`_MERCHANT_CAP_FLAG_WARNED` — latched so
rolling-horizon runs do not repeat it per window) records that the
flag has no effect.

### Objective

$$\max \;\; \Pi = \sum_t \left(p^{\mathrm{eff}}_t\, x^{pg}_t + \pi^{\mathrm{DAM}}_t\, x^{bg}_t - \pi^{\mathrm{DAM}}_t\, x^{gb}_t\right)/1000 \;-\; C^{\mathrm{wear}} \;+\; R^{\mathrm{bm}} \;-\; \varepsilon \sum_t x^{pc}_t \tag{M3}$$

identical to the self-consumption objective (S1) minus the
avoided-cost term $\Pi^{\mathrm{ret}}$ ($\equiv 0$: no load flows).
$p^{\mathrm{eff}}_t = (1-s)\,\pi^{\mathrm{DAM}}_t + s\,\pi^{\mathrm{PPA}}$
is the PPA-adjusted PV export price (equal to $\pi^{\mathrm{DAM}}_t$
without a contract); $C^{\mathrm{wear}}$ and $R^{\mathrm{bm}}$ as in
(S4) and the balancing design doc.

### Negative-price behaviour

For an hour with $\pi^{\mathrm{DAM}}_t < 0$ and no better use of the
energy (BESS full or absent): exporting uncovered PV loses money, so
the optimizer curtails it ($x^{pc}_t > 0$ is profit-maximising;
invariant 7's $\pi^{\mathrm{DAM}}_t > 0$ gate accepts this).  Under a
physical PPA the covered share earns
$s\,\pi^{\mathrm{PPA}} + (1-s)\,\pi^{\mathrm{DAM}}_t$ per kWh — while
$p^{\mathrm{eff}}_t > 0$ the covered volume keeps exporting through
negative hours, the documented behaviour of generation-settled
as-produced contracts (`docs/ppa_design.md`).  A BESS charges from PV
(or from the grid at negative prices when grid charging is enabled
and PV is zero) and discharges into later high-price hours.

## Settlement & cashflow equations

Live canonical revenue aggregates (`docs/economics_design.md`):
`revenue_pv_dam_eur` (uncovered share under a physical PPA),
`revenue_pv_ppa_eur`, `revenue_bess_dam_eur` (net of the
grid-charging expense — the BESS-DAM bundling convention), and the
five `revenue_bess_<product>_eur` balancing aggregates.
`revenue_self_consumption_eur` ≡ 0 by construction
(`kpis._compute_canonical_revenue_aggregates` zeroes it for
merchant).  The energy-aggregator fee applies once to the (here purely
DAM) gross market revenue; PPA carries no fee, and balancing carries no
energy-aggregator fee but MAY carry the optional, separate
balancing-aggregator (BSP / route-to-market) fee
(`balancing_aggregator_fee_pct_revenue`, default 0 — see
`docs/economics_design.md`).  The cashflow projection, degradation
scaling, LCOE/LCOS and debt algebra are mode-agnostic.

## KPI definitions

* Coverage KPIs are zeroed in merchant: `load_energy_mwh`,
  `pv_direct_to_load_mwh`, `bess_to_load_mwh` and every
  `load_coverage_*` / `*_self_consumption_frac` ratio report 0
  (`kpis.compute_kpis` merchant branch).
* Invariants (S21–S29): 1, 3, 4, 7, 8 are live; **2, 5, 6, 9 are
  identically zero** (their flows/constraints do not exist —
  `verify_dispatch_invariants(mode="merchant")` reports them as 0.0
  so the nine-key contract is stable across modes).
* Dispatch metrics (`pv_generation_mwh`, export/import MWh, cycles,
  SOC stats, curtailment) and `profit_total_eur` as in the SC doc.

## Implementation map

| Equation | Implementing symbol |
|---|---|
| (M1) | `optimization.build_model` → `MERCHANT_NO_PV_TO_LOAD`, `MERCHANT_NO_BESS_TO_LOAD`, `MERCHANT_NO_GRID_TO_LOAD` |
| (M2) | `build_model._cap_basis_rule` (strict branch unreachable in merchant) + `_MERCHANT_CAP_FLAG_WARNED` warning |
| (M3) | `build_model` objective (`avoided_cost = 0.0` merchant branch) |
| asset pinning | `NOPV_TO_LOAD`/`NOPV_TO_BESS`/`NOPV_TO_GRID`/`NOPV_CURTAIL`; `NOBESS_SOC`/`NOBESS_PV_TO_BESS`/`NOBESS_GRID_TO_BESS`/`NOBESS_DIS_LOAD`/`NOBESS_DIS_GRID`/`NOBESS_Y_CHARGE`/`NOBESS_Y_DIS`; `NO_GRID_CHARGE` |
| mode resolution | `modes.resolve_mode` (`VALID_MODES`, loader validation `io._parse_value`) |
| merchant KPI zeroing | `kpis.compute_kpis`, `kpis._compute_canonical_revenue_aggregates` |
| invariant scoping | `optimization.verify_dispatch_invariants` |

## Validation & tests

* `tests/test_asset_modes.py`, `tests/test_mode_switch_matrix.py` —
  pinning constraints and mode carve-outs per asset configuration.
* `tests/test_realscale_all_combos.py` — energy balance + all nine
  invariants for merchant × {hybrid, pv_only, bess_only} (1-day fast,
  full-year slow lane).
* `tests/test_dispatch_matrix_robustness.py` — dispatch sanity across
  the mode/asset matrix.
* `tests/test_balancing_bess_only.py` — merchant BESS-only with
  balancing stacking.
* `tests/test_merchant_plots.py` — the merchant energy-plot trio.
* `tests/test_ppa_engine.py`, `tests/test_ppa_surface.py` — the
  PPA-adjusted export price and merchant + PPA revenue stack.
* `tests/test_plotting_source_rules.py` (plot-layer invariants) and
  `tests/test_logic_spec_conformance.py` (constraint symbols, via the
  SC spec whose shared constraints merchant reuses).

## Worked example

Three hours ($\Delta t = 1$), PV-only, $G = (80, 100, 60)$ kWh, cap
$30$ kWh/h, $\pi^{\mathrm{DAM}} = (50, -20, 200)$ EUR/MWh, no PPA.

* $t=0$: export 30 (cap), curtail 50. Revenue $50 \cdot 0.03 = 1.5$.
* $t=1$: price negative → export 0, curtail 100 (invariant 7's price
  gate accepts curtailment-with-headroom here).
* $t=2$: export 30, curtail 30. Revenue $200 \cdot 0.03 = 6.0$.

Objective (M3): $\Pi = 7.5$ EUR minus the tie-break
$\varepsilon \cdot 180 = 0.0018$ — the tie-break never changes a
revenue-relevant decision, only breaks degeneracy among equal-profit
dispatches.  With a physical PPA at $s = 0.8$,
$\pi^{\mathrm{PPA}} = 65$: $p^{\mathrm{eff}}_1 = 0.2\cdot(-20) +
0.8\cdot 65 = 48$ EUR/MWh > 0, so the covered hour exports 30 kWh
instead of curtailing.

## Assumptions & limitations

* Price-taker: dispatch does not move the DAM price; no bid curves or
  block orders.
* No intraday/imbalance markets — the only spot market is the DAM;
  balancing products are reservation-based (see the balancing doc's
  own limitations).
* `retail_tariff_eur_per_mwh` and a supplied `load_kwh` column are
  inert; merchant projects with on-site auxiliaries netting against
  generation are not modelled.
* Grid charging requires `allow_bess_grid_charging` and is gated to
  PV-zero steps even in merchant (shared `GRID_CHG_PV_GATE`).
* Single price zone, single connection point.

## References

* MD YPEN/DAPEEK/53563/1556/2023 (injection cap).
* `docs/self_consumption_design.md` (shared constraint statements),
  `docs/ppa_design.md`, `docs/balancing_market_design.md`,
  `docs/economics_design.md`, `docs/README.md` (notation).
