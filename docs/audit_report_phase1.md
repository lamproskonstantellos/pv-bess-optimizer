# Audit report — Phase 1: balancing-market mathematical correctness

Scope: MILP balancing block, expected-value SOC drift, Monte Carlo
realisation, KPI roll-up, lifetime / cashflow scaling.

## 1.1 Product taxonomy — confirmed

`pvbess_opt/balancing.py:60-72`:

* `PRODUCTS_ALL = ("fcr", "afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn")`
* `PRODUCTS_WITH_ACTIVATION = ("afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn")`
* `PRODUCTS_UP = ("afrr_up", "mfrr_up")`
* `PRODUCTS_DN = ("afrr_dn", "mfrr_dn")`
* `PRODUCTS_SYMMETRIC = ("fcr",)`

Walked every consumer of these tuples:

* MILP power budget `optimization.py:681-702` — FCR enters both
  `PRODUCTS_DN + PRODUCTS_SYMMETRIC` (charge direction) and
  `PRODUCTS_UP + PRODUCTS_SYMMETRIC` (discharge direction). ✓
* MILP SOC headroom `optimization.py:706-727` — asymmetric branch uses
  `dt_h` for `PRODUCTS_UP/DN`; the FCR (symmetric) branch uses
  `fcr_required_duration_hours` and runs in both directions. ✓
* Expected SOC drift `optimization.py:577-585` and
  `kpis.py:_balancing_soc_drift` — iterate over `PRODUCTS_DN` (positive,
  scaled by `eta_c`) and `PRODUCTS_UP` (negative, scaled by `1/eta_d`);
  FCR is deliberately excluded — symmetric in expectation. ✓
* MILP expected revenue `optimization.py:838-865` — capacity sum runs
  over `PRODUCTS_ALL` (FCR earns capacity payment); activation sum runs
  over `PRODUCTS_WITH_ACTIVATION` only. ✓
* KPI roll-up `kpis.py:_compute_balancing_kpis` and
  `_compute_canonical_revenue_aggregates` — same iteration pattern. ✓
* Lifetime scaling `lifetime.py:_BALANCING_RESERVATION_COLUMNS:93-99`
  — all five product reservation columns scaled by `bess_factor`. ✓
* Monte Carlo `rolling_horizon.py:521-554` — capacity revenue across
  `PRODUCTS_ALL`; activation revenue across `PRODUCTS_WITH_ACTIVATION`. ✓

## 1.2 BM_POWER_DN / BM_POWER_UP unit consistency — confirmed

`optimization.py:676-699`:

```
bess_step_lim_bm = p_bess * dt_h          # kWh per step
m.pv_to_bess[t], m.grid_to_bess[t]        # kWh per step
m.r_balancing[k, t] * dt_h                # kW × h = kWh per step
```

Both inequalities compare kWh-per-step on both sides. FCR enters both
directions per the symmetric convention. ✓ No unit inconsistency.

## 1.3 SOC headroom (BM_SOC_UP / BM_SOC_DN) formula audit — confirmed

`optimization.py:706-724`:

* Up direction: `soc[t] − soc_min*E_cap ≥ (1+h_buf) · dt_h · Σr_up/η_d +
  (1+h_buf) · d_fcr · Σr_fcr/η_d`. Asymmetric products need to cover one
  settlement period; FCR must cover `fcr_required_duration_hours` of
  sustained output. ✓
* Down direction: `soc_max*E_cap − soc[t] ≥ (1+h_buf) · dt_h · Σr_dn ·
  η_c + (1+h_buf) · d_fcr · Σr_fcr · η_c`. The η_c placement is correct —
  charging adds `η_c · kWh_AC_in` to SOC, so the AC headroom required is
  the DC headroom multiplied by η_c. ✓
* `bm_soc_headroom_pct ∈ [0, 50]` per `io.py:1290-1295`; stacks
  multiplicatively (`1 + h_buf`), not additively. ✓

## 1.4 Expected-value SOC drift — confirmed and FCR probability flagged

`optimization.py:soc_dynamics:577-585` and
`kpis.py:_balancing_soc_drift:87-100` are term-for-term identical.
Both sum over `PRODUCTS_DN` (scaled by `+eta_c * dt_h * alpha*beta`)
and `PRODUCTS_UP` (scaled by `-(dt_h / eta_d) * alpha*beta`), with FCR
excluded. ✓

Invariant 4 (`rte_bound`) at `optimization.py:1159-1163`:

```
rte_bound = eta_c * eta_d * total_charge + eta_d * (soc0 - final_state)
            + eta_d * drift_total
```

Walking the dimensions: `drift_total` is kWh (DC), so `eta_d * drift_total`
converts to AC kWh. When down-activation dominates (positive drift), the
SOC ends higher than DAM dispatch alone would explain and the rte bound
is loosened by the AC-equivalent of that extra energy. When up-activation
dominates (negative drift), the SOC ends lower and the rte bound tightens
because the missing energy was discharged through the (un-tracked-by-
`bess_dis_*`) balancing path. ✓ Physically correct.

**`fcr_activation_probability_pct = 15.0` default (⚠ informational only).**
FCR never appears in `PRODUCTS_WITH_ACTIVATION`, `PRODUCTS_UP`, or
`PRODUCTS_DN`, and `expected_activation_revenue_per_kw_per_step` returns
zero for FCR. A `grep` across `pvbess_opt/`, `tests/`, and `scripts/`
finds no consumer of `activation_probability(cfg, "fcr")`. The field is
dead config. Resolution applied: row docstring in `io.py:525-530` updated
to declare the field informational and retained for future use should an
FCR activation revenue stream be added.

## 1.5 Expected revenue in the MILP objective — confirmed

`optimization.py:835-869`:

* Capacity term per product: `α_k · dt_h · Σ_t(price_t · r_t) / 1000`.
  Units: `[1] · [h] · [EUR/MWh · kW] / 1000 = EUR`. ✓
* Activation term per product (in `PRODUCTS_WITH_ACTIVATION`):
  `α_k · β_k · dt_h · Σ_t(price_t · r_t) / 1000`. ✓
* Both up- and down-direction activation prices enter as POSITIVE
  payments per the documented convention; sign-correctness of input
  prices is the user's responsibility. The workbook default
  `afrr_dn_default_activation_price = 25 EUR/MWh` vs.
  `afrr_up_default_activation_price = 220 EUR/MWh` correctly reflects
  the typical asymmetry of activation pay (down-activations pay less).
* `_compute_balancing_kpis` at `kpis.py:630-658` uses the same per-step
  product (`α · dt_h · Σ(price · r) / 1000` for capacity; with
  additional `β` for activation). ✓ Matches the objective.

## 1.6 `dam_capacity_share_pct` semantic mismatch — resolved (option a)

`optimization.py:649-664` bounds `CH_LIM` and `DIS_LIM` with the FULL
`bess_step_lim = p_bess * dt_h`, not a `dam_capacity_share_pct`-scaled
cap. The only mechanism that throttles DAM dispatch is
`BM_POWER_UP/DN`, which says

```
dam_charge_kwh + r_dn_share * dt_h <= p_bess * dt_h
dam_discharge_kwh + r_up_share * dt_h <= p_bess * dt_h
```

so DAM consumes the residual of `p_bess` left over after the balancing
reservations in each step. `dam_capacity_share_pct` only enters
`_validate_balancing_config:1237-1255` as part of the share-sum check
that the total across DAM + every balancing product is ≤ 100 %.

**Resolution (option a)**: workbook row docstring at `io.py:502-507`
rewritten to make the declarative semantic explicit; validator comment
at `io.py:1166-1175` updated to point future readers at the active
constraint (`BM_POWER_UP / BM_POWER_DN`). Option (b) — adding an
explicit cap — would materially change every existing project's results
and is not the documented intent of the workbook field.

## 1.7 Monte Carlo SOC-violation check coupling — fixed

`rolling_horizon.py:realise_balancing_scenario` previously spawned a
fresh child generator (`sub_rng = np.random.default_rng(rng.integers(...))`)
for the SOC trajectory check and resampled the Bernoulli outcomes from
scratch. A single Monte Carlo scenario could therefore:

* report revenue from activation events that never appeared in the SOC
  trace, and
* report "SOC OK" for a trace that never accrued the matching revenue.

Visible consequence in `scripts/audit_runs/results/*_on.json`: all four
balancing-ON cases reported `bm_soc_constrained_scenarios_pct = 0.0`,
a likely false negative.

**Fix applied**: the activation Boolean arrays from the revenue pass are
captured in `activated_by_product` and reused unchanged in the SOC
trajectory pass. The fresh child generator is gone; the SOC view and
the revenue view of one scenario now consume identical Bernoulli draws.

**Regression coverage**: `tests/test_balancing_mc_coupling.py` adds:

* `test_soc_check_couples_to_revenue_draws` — single-product
  deterministic scenario with α=β=1.0 and a reservation sized to breach
  the SOC floor. Asserts SOC violation flag is `True` whenever the
  (identical) Bernoulli draw produced revenue.
* `test_soc_check_no_violation_when_no_reservation` — control case.
* `test_full_mc_reports_nonzero_constrained_fraction_with_tight_headroom`
  — end-to-end MILP solve with `bm_soc_headroom_pct = 0` and 95 %
  activation probabilities; asserts the empirical constrained fraction
  is strictly positive.

## 1.8 Settlement-period equality is a simplification — documented

`io.py:1280-1288` rejects loads where `bm_settlement_minutes != dt_minutes`.
Real-world cadences differ (FCR sub-second, aFRR 4–15 min, mFRR 15 min).
Implementation unchanged; `docs/balancing_market_design.md` now carries
a "Modelling simplifications" section recording the choice and pointing
the reader at the validator rule.

## 1.9 Cashflow and lifetime scaling consistency — confirmed

* `lifetime.py:_BALANCING_RESERVATION_COLUMNS:93-99` × `bess_factor` at
  line 281. ✓
* `economics.py:build_yearly_cashflow:396-401` scales the year-1
  balancing capacity / activation revenue by `bess_factor * (1 +
  bm_inflation_pct)^(y-1)`. ✓
* `compute_financial_kpis` at `economics.py:705-711` aggregates the
  per-year `balancing_capacity_revenue_eur` and
  `balancing_activation_revenue_eur` columns over years ≥ 1. ✓

Unit-test coverage: `tests/test_balancing_lifetime_cashflow.py` asserts
that for `y ∈ {1, 3, 5, 7, 10}` the projected per-year balancing
revenue equals `year1 × _bess_factor(y) × (1 + bm_inflation)^(y-1)`,
with `_bess_factor` imported from `pvbess_opt.lifetime` so any future
drift between the two modules trips the test.

## 1.10 LCOE / LCOS exclusion of balancing revenue — confirmed

`economics.py:compute_financial_kpis` builds LCOE from PV-only CAPEX +
DEVEX + OPEX over PV generation MWh (lines 758-801), and LCOS from
BESS-only CAPEX + DEVEX + OPEX over BESS discharge MWh (lines 803-858).
Balancing revenue (a BESS-side income stream) correctly enters neither
numerator nor denominator. A one-line `# Balancing capacity and
activation revenue do not enter either LCOE or LCOS …` comment was
added at the top of the extras block at `economics.py:745-751` to
record the intent for future maintainers.

## Phase 1 exit summary

| Check | Status | Notes |
| --- | --- | --- |
| 1.1 Product taxonomy | ✓ | Every consumer iterates the correct tuple. |
| 1.2 BM_POWER unit consistency | ✓ | kWh on both sides; FCR in both directions. |
| 1.3 BM_SOC headroom formula | ✓ | η placement and `(1+h_buf)` multiplicative. |
| 1.4 SOC drift sign / FCR exclusion | ✓ | FCR symmetry assumption explicit. |
| 1.4 `fcr_activation_probability_pct` | ⚠ → resolved | Marked informational in workbook docstring. |
| 1.5 Expected revenue dimensions | ✓ | EUR/MWh × kW × h / 1000 = EUR. |
| 1.6 DAM share semantics | ⚠ → resolved | Docstring + validator comment rewritten. |
| 1.7 MC SOC coupling | ⚠ → fixed | Activation arrays reused; regression test added. |
| 1.8 Settlement-period simplification | ⚠ → documented | Note added to design doc. |
| 1.9 Lifetime / cashflow scaling | ✓ | Regression test added. |
| 1.10 LCOE / LCOS exclusion | ✓ | Comment added. |
