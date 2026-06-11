# Balancing logic verification (v0.9.0+)

Companion to `docs/balancing_market_design.md`. Each numbered section
states the
intended math or semantics, cites the implementing file:line, and
records a **PASS / FAIL** status. The accompanying conformance test
`tests/test_logic_spec_conformance.py` parses the section headings and
asserts that every named symbol exists on a balancing-enabled Pyomo
model, so the documented contract cannot drift from the code without
breaking CI.

> **Line references.** The `file.py:NN` references below are indicative
> of the audited revision; lines drift as the code evolves. The NAMED
> symbols (constraints, variables, functions) are the stable anchors —
> `tests/test_logic_spec_conformance.py` asserts they exist on a built
> model, so the documented contract cannot silently drift from the code.

The notation used below:

* `r[k, t]` — per-product, per-step reservation in kW.
* `dt_h` — settlement-period length in hours (`dt_minutes / 60`).
* `α_k` — bid-acceptance probability for product `k`.
* `β_k` — activation probability for product `k`.
* `η_c`, `η_d` — BESS charge and discharge efficiencies.
* `e_cap` — BESS energy capacity in kWh (the parameter, pinned to
  `bess_capacity_kwh`).
* `h_buf` — SOC safety buffer fraction (`bm_soc_headroom_pct / 100`).
* `d_fcr` — FCR sustained-output requirement in hours
  (`fcr_required_duration_hours`).
* `p_bess` — BESS rated power in kW.
* `p_cap_k(t)`, `p_act_k(t)` — capacity / activation prices in EUR/MWh.

The five product sets used throughout (`pvbess_opt/balancing.py:60`–`72`):

```
PRODUCTS_ALL          = (fcr, afrr_up, afrr_dn, mfrr_up, mfrr_dn)
PRODUCTS_WITH_ACTIVATION = (afrr_up, afrr_dn, mfrr_up, mfrr_dn)
PRODUCTS_UP           = (afrr_up, mfrr_up)
PRODUCTS_DN           = (afrr_dn, mfrr_dn)
PRODUCTS_SYMMETRIC    = (fcr,)
```

---

## 1. Product taxonomy — PASS

Every consumer iterates the canonical tuples without re-deriving them:

* MILP: `pvbess_opt/optimization.py:66-78` imports
  `PRODUCTS_ALL / DN / SYMMETRIC / UP / WITH_ACTIVATION` and uses them
  in the reservation variable, the power-budget rules
  (`optimization.py:682-699`), the SOC-headroom rules
  (`optimization.py:706-727`), and the revenue terms
  (`optimization.py:838-865`).
* KPIs: `pvbess_opt/kpis.py:39-47` imports the same tuples; the
  per-product revenue / drift loops in
  `kpis.py:599-682` iterate them directly.
* Monte Carlo: `pvbess_opt/rolling_horizon.py:37-46, 528-563` iterates
  `PRODUCTS_ALL` and `PRODUCTS_WITH_ACTIVATION` for the Bernoulli draws
  and reuses `PRODUCTS_UP + PRODUCTS_DN` for the SOC-trajectory check.

FCR's symmetric, capacity-only nature is consistent across all three
layers: it is included in `PRODUCTS_SYMMETRIC` for the power and SOC
budgets, excluded from `PRODUCTS_WITH_ACTIVATION` for the activation
revenue stream, and absent from `PRODUCTS_UP` / `PRODUCTS_DN` for the
expected-drift terms.

## 2. MILP power budget — PASS, `BM_POWER_DN` / `BM_POWER_UP`

The per-product, per-step reservation kW is the Pyomo variable
`r_balancing[k, t]` declared at `pvbess_opt/optimization.py:563-566`
with the bound `0 ≤ r_balancing[k, t] ≤ s_k · p_bess` where
`s_k = <product>_capacity_share_pct / 100`. The power budget is:

Implemented in `pvbess_opt/optimization.py:681-702`:

```
BM_POWER_DN(t): pv_to_bess[t] + grid_to_bess[t]
                + dt_h · Σ_{k ∈ PRODUCTS_DN ∪ PRODUCTS_SYMMETRIC} r[k, t]
              ≤ p_bess · dt_h

BM_POWER_UP(t): bess_dis_load[t] + bess_dis_grid[t]
                + dt_h · Σ_{k ∈ PRODUCTS_UP ∪ PRODUCTS_SYMMETRIC} r[k, t]
              ≤ p_bess · dt_h
```

Both sides resolve to kWh per step (the DAM flows are kWh per step;
each `r[k, t]` is multiplied by `dt_h` to lift from kW to kWh per
step). FCR is included on both sides per the symmetric-reservation
rule.

## 3. SOC headroom — PASS, `BM_SOC_UP` / `BM_SOC_DN`

Implemented in `pvbess_opt/optimization.py:706-727`:

```
BM_SOC_UP(t): soc[t] − soc_min · e_cap
            ≥ (1 + h_buf) · [ (dt_h · Σ_{k ∈ PRODUCTS_UP} r[k, t])
                            + (d_fcr · Σ_{k ∈ PRODUCTS_SYMMETRIC} r[k, t]) ] / η_d

BM_SOC_DN(t): soc_max · e_cap − soc[t]
            ≥ (1 + h_buf) · η_c · [ (dt_h · Σ_{k ∈ PRODUCTS_DN} r[k, t])
                                  + (d_fcr · Σ_{k ∈ PRODUCTS_SYMMETRIC} r[k, t]) ]
```

η placement matches the design note (`docs/balancing_market_design.md`
section "SOC headroom"). FCR uses `d_fcr` instead of `dt_h` because the
sustained-output requirement is set independently of the settlement
period.

## 4. Expected SOC drift in `soc_dynamics` — PASS

The MILP adds a deterministic drift to the SOC recursion
(`pvbess_opt/optimization.py:569-591` and the terminal-step copy at
`612-635`):

```
drift_charge(t)    = η_c · dt_h · Σ_{k ∈ PRODUCTS_DN} α_k · β_k · r[k, t]
drift_discharge(t) = (dt_h / η_d) · Σ_{k ∈ PRODUCTS_UP} α_k · β_k · r[k, t]

soc[t+1] = soc[t] + η_c · (pv_to_bess[t] + grid_to_bess[t])
                  − (bess_dis_load[t] + bess_dis_grid[t]) / η_d
                  + drift_charge(t) − drift_discharge(t)
```

The KPI helper `_balancing_soc_drift` in `pvbess_opt/kpis.py:66-101`
implements the same formula term-for-term against the dispatch frame,
and `verify_dispatch_invariants` in
`pvbess_opt/optimization.py:1132-1163` consumes it so invariants 3
(`soc_dynamics`), 4 (`rte_bound`) and 8 (`closed_cycle`) stay aligned
with the MILP. FCR is absent from both `PRODUCTS_UP` and
`PRODUCTS_DN`, so it contributes zero net drift in expectation, which
matches the symmetric-FCR simplification documented in
`docs/balancing_market_design.md`.

## 5. Expected revenue dimensions — PASS

Capacity term (`pvbess_opt/optimization.py:838-848`):

```
R_cap(t) = α_k · dt_h · Σ_t ( p_cap_k(t) · r[k, t] ) / 1000          → EUR
```

Activation term (`pvbess_opt/optimization.py:854-865`):

```
R_act(t) = α_k · β_k · dt_h · Σ_t ( p_act_k(t) · r[k, t] ) / 1000    → EUR
```

The `/1000` converts EUR/MWh × kW × h → EUR. FCR is excluded from the
activation loop by iterating `PRODUCTS_WITH_ACTIVATION`, so its
activation term is identically zero. The KPI mirror lives at
`pvbess_opt/kpis.py:630-658` and uses the same numeric formula.

## 6. MC SOC-violation coupling — PASS

`realise_balancing_scenario` in `pvbess_opt/rolling_horizon.py:471-596`
captures the per-product `activated` Boolean arrays in the revenue
pass (`activated_by_product`) and reuses them in the SOC-trajectory
pass. A single Monte Carlo scenario therefore cannot report revenue
from activation events that did not appear in its SOC trace, nor
"SOC OK" on a trace that never produced revenue.

This fixes the Monte Carlo SOC-violation coupling; regression coverage
lives in `tests/test_balancing_mc_coupling.py`. The corresponding commit
`fix(balancing): correct Monte Carlo SOC-violation coupling and
clarify DAM share semantics` is on `main`.

## 7. Lifetime / cashflow scaling — PASS

`pvbess_opt/lifetime.py:224-281` declares the reservation columns in
`_BALANCING_RESERVATION_COLUMNS` (one per product) and scales them by
`bess_factor(y)` inside the year-loop alongside every other BESS-side
column. The yearly cashflow then composes balancing revenue
(`pvbess_opt/economics.py:324-422`):

```
year-y balancing revenue = year-1 balancing revenue
                         · bess_factor(y) · (1 + bm_infl)^(y − 1)
```

The `(1 + bm_infl)` factor is sourced from `bm_inflation_pct` on the
balancing sheet; the `bess_factor(y)` curve combines the calendar
fade, optional cycle fade, and any cell replacement
(`lifetime.py:111-143`).

## 8. LCOE / LCOS exclusion — PASS

`pvbess_opt/economics.py:746-750` carries an explicit comment recording
the Lazard convention: balancing capacity and activation revenue do
not enter either LCOE or LCOS, because both metrics measure cost per
delivered MWh and balancing produces no DAM-discharge MWh (the LCOS
denominator). The LCOE numerator at `economics.py:780-806` and the
LCOS numerator at `economics.py:808-863` are built strictly from
PV / BESS CAPEX, DEVEX, OPEX and replacement; no `bm_*` term enters
either. Balancing revenue is folded into the NPV / IRR / payback
metrics via `build_yearly_cashflow`.

## 9. `dam_capacity_share_pct` semantics — PASS, declarative-only

`pvbess_opt/io.py:502-507` documents the field as a validator-only
share, and `pvbess_opt/io.py:1175-1186` repeats the same wording on the
private `_BALANCING_SHARE_KEYS` tuple. The validator at
`io.py:1247-1265` enforces only the sum constraint:

```
Σ shares (DAM + every balancing product) ≤ 100 %
```

DAM dispatch is bounded indirectly by `BM_POWER_UP` / `BM_POWER_DN`
consuming the residual of `p_bess` left over after the balancing
reservations in each step. Grepping the MILP and KPI modules confirms
no consumer reads `dam_capacity_share_pct` to cap DAM flows.

## 10. `fcr_activation_probability_pct` informational-only — PASS

`pvbess_opt/io.py:528-533` documents the field as informational only.
Grep over the entire package
(`grep -rn "fcr_activation_probability_pct" pvbess_opt main.py`) shows
five hits, all in declaration / validation / configuration paths:

1. `pvbess_opt/balancing.py:105` — `BalancingConfig` field default.
2. `pvbess_opt/balancing.py:284` — `_ACTIVATION_PROB_KEYS` mapping.
3. `pvbess_opt/io.py:204` — `BALANCING_SHEET_DEFAULTS` entry.
4. `pvbess_opt/io.py:533` — row template docstring.
5. `pvbess_opt/io.py:1199` — `_BALANCING_PROBABILITY_KEYS` (range-check
   list only).

No MILP, KPI, or Monte Carlo consumer reads it. `activation_probability(cfg, "fcr")` is reachable through the accessor but is never invoked
for FCR by the live code paths because `PRODUCTS_WITH_ACTIVATION`
excludes FCR. The field is preserved on the schema so a future FCR
activation revenue stream could be added without a workbook migration.
