# Balancing market participation — design

Domain design document for stochastic participation in the European
balancing markets (ENTSO-E framework: FCR, aFRR, mFRR) alongside DAM
dispatch: the reservation MILP extension, expected-revenue objective,
ex-post Monte Carlo, settlement wiring, KPIs, and the verification log.
Notation follows the shared table in `docs/README.md`.  The
conformance test `tests/test_logic_spec_conformance.py` asserts that
the symbols named here (`r_balancing`, `BM_POWER_DN`, `BM_POWER_UP`,
`BM_SOC_UP`, `BM_SOC_DN`) exist on a balancing-enabled Pyomo model.

## Purpose & scope

A BESS can stack capacity-reservation revenue (and, for aFRR/mFRR,
activation revenue) on top of DAM arbitrage.  The model treats
per-product reservations as explicit decision variables whose
*expected* revenue (probability-weighted) enters the MILP objective,
and quantifies realisation risk with a post-solve Monte Carlo
(`docs/uncertainty_design.md` Eqs. U4–U5).  The extension is fully
**opt-in**: with `balancing_enabled = FALSE` (the default) the MILP,
KPI dict, cashflow, Monte Carlo output and PDF report are bit-identical
to a workbook without the sheet.

Five products (`pvbess_opt/balancing.py` canonical tuples —
`PRODUCTS_ALL`, `PRODUCTS_WITH_ACTIVATION`, `PRODUCTS_UP`,
`PRODUCTS_DN`, `PRODUCTS_SYMMETRIC`):

| Key | Direction | Capacity payment | Activation payment | Notes |
|---|---|---|---|---|
| `fcr` | symmetric | yes | no | capacity-only (ENTSO-E SAFA convention); the same kW counts in both directions |
| `afrr_up` | up | yes | yes | battery discharges when called |
| `afrr_dn` | down | yes | yes | battery charges when called |
| `mfrr_up` | up | yes | yes | |
| `mfrr_dn` | down | yes | yes | |

## Inputs

The optional `balancing` sheet carries **34 keys** (kv structure like
every parameter sheet; the shipped workbook keeps the master switch
off):

| Group | Keys | Defaults |
|---|---|---|
| master switch | `balancing_enabled` | FALSE |
| capacity shares (% of `bess_power_kw`; sum across all six ≤ 100) | `dam_capacity_share_pct`, `fcr_capacity_share_pct`, `afrr_up_capacity_share_pct`, `afrr_dn_capacity_share_pct`, `mfrr_up_capacity_share_pct`, `mfrr_dn_capacity_share_pct` | 70 / 10 / 8 / 7 / 3 / 2 |
| bid-acceptance probabilities $\alpha_k$ (%) | `fcr_bid_acceptance_pct`, `afrr_up_…`, `afrr_dn_…`, `mfrr_up_…`, `mfrr_dn_…` | 70 / 55 / 55 / 40 / 40 |
| activation probabilities $\beta_k$ (%) | `fcr_activation_probability_pct` (informational-only, see Appendix §10), `afrr_up_…`, `afrr_dn_…`, `mfrr_up_…`, `mfrr_dn_…` | 15 / 10 / 8 / 5 / 4 |
| fallback capacity prices (EUR/MWh) | `<k>_default_capacity_price_eur_per_mwh` ×5 | 12 / 18 / 15 / 6 / 5 |
| fallback activation prices (EUR/MWh; no FCR) | `<k>_default_activation_price_eur_per_mwh` ×4 | 220 / 25 / 180 / 20 |
| FCR duration | `fcr_required_duration_hours` | 0.5 |
| settlement period | `bm_settlement_minutes` (validated == `dt_minutes`) | 15 |
| SOC safety buffer | `bm_soc_headroom_pct` | 10 |
| indexation | `bm_inflation_pct` | 2.0 |
| MC price sigmas (%) | `bm_price_sigma_capacity_pct`, `bm_price_sigma_activation_pct` | 25 / 35 |
| MC size / seed | `bm_mc_scenarios`, `bm_random_seed` | 200 / 1729 |

Nine optional per-step price columns may sit on the `timeseries`
sheet (`<k>_capacity_price_eur_per_mwh` ×5,
`<k>_activation_price_eur_per_mwh` ×4); each is read verbatim when
present and filled with the sheet's scalar fallback otherwise
(`balancing.resolve_balancing_timeseries`; a synthetic diurnal
generator `balancing.generate_synthetic_balancing_timeseries` exists
for studies).

## Mathematical formulation

For each product $k$ and step $t$ the model gains the continuous
variable `r_balancing[k, t]` $= r_{k,t} \ge 0$ (kW reserved), bounded
by the per-product share:

$$0 \le r_{k,t} \le s_k\, P^{B} \tag{B1}$$

Per-direction power budgets — DAM flows plus reservations must fit the
rated power; FCR counts in **both** directions
(`BM_POWER_DN` / `BM_POWER_UP`):

$$x^{pb}_t + x^{gb}_t + \Delta t \sum_{k \in \mathrm{DN} \cup \mathrm{SYM}} r_{k,t} \;\le\; P^{B} \Delta t \tag{B2}$$

$$x^{bl}_t + x^{bg}_t + \Delta t \sum_{k \in \mathrm{UP} \cup \mathrm{SYM}} r_{k,t} \;\le\; P^{B} \Delta t \tag{B3}$$

SOC headroom per direction with safety buffer $h$ and the
FCR sustained-duration $d_{\mathrm{fcr}}$ (`BM_SOC_UP` / `BM_SOC_DN`):

$$E_t - \underline{e}\,E^{\mathrm{cap}} \;\ge\; (1+h)\left[\Delta t \sum_{k \in \mathrm{UP}} r_{k,t} + d_{\mathrm{fcr}} \sum_{k \in \mathrm{SYM}} r_{k,t}\right] / \eta_d \tag{B4}$$

$$\overline{e}\,E^{\mathrm{cap}} - E_t \;\ge\; (1+h)\,\eta_c \left[\Delta t \sum_{k \in \mathrm{DN}} r_{k,t} + d_{\mathrm{fcr}} \sum_{k \in \mathrm{SYM}} r_{k,t}\right] \tag{B5}$$

Expected activation energy enters the SOC recursion (S10) as a
deterministic drift; FCR's symmetric activations cancel in
expectation:

$$\delta^{c}_t = \eta_c\, \Delta t \sum_{k \in \mathrm{DN}} \alpha_k \beta_k\, r_{k,t}, \qquad
\delta^{d}_t = \frac{\Delta t}{\eta_d} \sum_{k \in \mathrm{UP}} \alpha_k \beta_k\, r_{k,t} \tag{B6}$$

Expected revenue added to the objective (S1):

$$R^{\mathrm{bm}} = \sum_{k \in \mathrm{ALL}} \alpha_k\, \Delta t \sum_t \pi^{\mathrm{cap}}_{k,t}\, r_{k,t} / 1000 \;+\; \sum_{k \in \mathrm{ACT}} \alpha_k \beta_k\, \Delta t \sum_t \pi^{\mathrm{act}}_{k,t}\, r_{k,t} / 1000 \tag{B7}$$

Both up and down activation prices enter as positive payments per MWh
activated (the common EU convention); sign-correctness of the input
prices is the user's responsibility.  Dimension check:
EUR/MWh × kW × h / 1000 → EUR.

The ex-post Monte Carlo realises Eq. (B7)'s expectation with Bernoulli
acceptance/activation draws and log-normal price noise — equations
(U4)–(U5) and the SOC-coupling rule in `docs/uncertainty_design.md`.

## Settlement & cashflow equations

Year-1 expected balancing revenue lands in the KPI dict
(`bm_total_capacity_revenue_eur`, `bm_total_activation_revenue_eur`),
is availability-derated once, and projects per Eq. (E11) — on the BESS
fade curve, indexed by `bm_inflation_pct`:

$$R^{\mathrm{bm,cap/act}}_y = R^{\mathrm{bm,cap/act}}_1\, f^{B}_y\, (1+i_{\mathrm{bm}})^{y-1} \tag{B8}$$

The yearly cashflow carries three gross balancing columns
(`balancing_capacity_revenue_eur`, `balancing_activation_revenue_eur`,
`balancing_revenue_eur`) plus an optional fee column
(`balancing_aggregator_fee_eur`); the monthly view allocates them by the
Year-1 monthly reservation weights (flat 1/12 fallback).  Balancing
revenue carries **no energy-aggregator fee** (that fee applies to
DAM + retail only — ancillary services settle directly with the TSO).
It **may** carry an optional, separate route-to-market (BSP /
balancing-aggregator) fee, `balancing_aggregator_fee_pct_revenue`, when
participation is routed through an aggregator that keeps a share — a
non-negative deduction on the **gross** balancing revenue
(Eq. (E13b) in `docs/economics_design.md`) that **defaults to 0**
(fee-free, bit-identical to today), with a realistic ~5–20 % range for
behind-the-meter / smaller assets (per-stream route-to-market cost,
Gridcog convention).  Balancing revenue **and** its BSP fee are
**excluded from LCOE/LCOS** (revenue-agnostic Lazard convention; see
`docs/economics_design.md`).  Default `bm_inflation_pct = 2.0` tracks
CPI while DAM stays nominal — `pvbess_opt/conventions.md`.

## KPI definitions

Emitted on every balancing run (zero when the gate is off):
`bm_<k>_capacity_revenue_eur` (×5), `bm_<k>_activation_revenue_eur`
(×4), `bm_total_capacity_revenue_eur`,
`bm_total_activation_revenue_eur`, `bm_total_balancing_revenue_eur`,
`bm_revenue_share_pct` (balancing share of total revenue: the denominator is the non-balancing per-step profit plus the balancing total, so the share cannot double-count),
`bm_expected_activation_energy_up_kwh` / `_dn_kwh` (Eq. B6 sums), and
`bm_reservation_avg_kw_<k>` (×5).  The canonical aggregates
`revenue_bess_<k>_eur` add capacity + activation per product
(capacity-only for FCR) — `docs/economics_design.md`.  The Monte Carlo
adds P10/P50/P90 of total realised revenue, per-product breakdowns,
`bm_mc_soc_violation_share`, and the raw realisations for the
histogram.  Financial lifecycle totals:
`total_balancing_{capacity,activation,}revenue_eur_lifecycle`.

## Implementation map

| Equation | Implementing symbol |
|---|---|
| (B1) | `optimization.build_model` → `r_balancing` bounds (`balancing.capacity_share_kw`) |
| (B2)–(B3) | `BM_POWER_DN`, `BM_POWER_UP` |
| (B4)–(B5) | `BM_SOC_UP`, `BM_SOC_DN` |
| (B6) | `soc_dynamics` drift terms; KPI mirror `kpis._balancing_soc_drift` |
| (B7) | `build_model` → `m.balancing_revenue_expr` (`balancing.acceptance_probability`, `activation_probability`); KPI mirror `kpis._compute_balancing_kpis` |
| (B8) | `economics.build_yearly_cashflow` balancing rows; `lifetime._BALANCING_RESERVATION_COLUMNS` |
| MC realisation | `rolling_horizon.realise_balancing_scenario`, `monte_carlo_balancing` |
| config / prices | `balancing.resolve_balancing_config`, `resolve_balancing_timeseries`; validators `io._validate_balancing_config` |
| plots | `plotting.balancing.plot_balancing_reservation_profile`, `plot_balancing_mc_distribution` (both return None without balancing columns) |

## Validation & tests

Six balancing invariants (post-solve, `optimization._balancing_invariants`):

* **INV-B1** sum of the six capacity shares ≤ 100 % (validator) —
  `dam_capacity_share_pct` participates in the sum only.
* **INV-B2** $r_{k,t} \le s_k P^{B}$ per product and step.
* **INV-B3 / INV-B4** up/down SOC headroom satisfied at every step.
* **INV-B5** per-direction power budget satisfied at every step.
* **INV-B6** `balancing_enabled = FALSE` ⇒ all $r_{k,t} = 0$ and the
  run is bit-identical to the pre-balancing release.

Test anchors: `tests/test_balancing_module.py` (config/data model),
`tests/test_balancing_optimization.py` + `tests/test_balancing_invariants.py`
(B1–B7 on the model), `tests/test_balancing_io.py` +
`tests/test_balancing_validator_resolved.py` (schema/validators),
`tests/test_balancing_mc.py` + `tests/test_balancing_mc_coupling.py`
(U4–U5, SOC coupling), `tests/test_balancing_lifetime_cashflow.py`
(B8, LCOE/LCOS exclusion), `tests/test_balancing_bess_only.py`,
`tests/test_balancing_runtime_invariants.py`,
`tests/test_logic_spec_conformance.py::test_balancing_verification_symbols_present`
(symbols on a built model), `tests/test_kpi_and_dt_contracts.py`
(`bm_revenue_share_pct` denominator, KPI key-set parity off-vs-on).

## Worked example

1 MW / 4 MWh BESS, hourly cadence: `fcr_capacity_share_pct = 10`,
$d_{\mathrm{fcr}} = 0.5$ h, $\alpha_{\mathrm{fcr}} = 0.70$,
$\pi^{\mathrm{cap}}_{\mathrm{fcr}} = 12$ EUR/MWh, $h = 0.10$,
$\eta = 0.97$, SOC bounds 800–3 800 kWh.

* Eq. (B1): $r_{\mathrm{fcr}} \le 0.10 \cdot 1000 = 100$ kW.
* Eq. (B4) at full reservation:
  $(1.10)(0.5)(100)/0.97 \approx 56.7$ kWh of up-headroom required.
* Eq. (B7) per step: $0.70 \cdot 1 \cdot 12 \cdot 100/1000 = 0.84$ EUR
  ⇒ ≈ 7 358 EUR over 8 760 hourly steps at the default share/price.

## Assumptions & limitations

* **Single settlement cadence.** All five products settle on the
  dispatch cadence; `io._validate_balancing_config` rejects
  `bm_settlement_minutes != dt_minutes`.  Real markets differ (FCR
  sub-second, aFRR 4–15 min, mFRR 15 min); the worst-case headroom in
  `bm_soc_headroom_pct` absorbs the within-step variability a
  sub-cadence model would resolve.
* **FCR symmetric in expectation** — zero net SOC drift;
  `fcr_activation_probability_pct` is documentation-only (Appendix §10).
* **`dam_capacity_share_pct` is declarative** — share-sum validation
  only; DAM dispatch is bounded indirectly by (B2)–(B3) consuming the
  residual power.
* Acceptance/activation independent across steps and products;
  activation duration equals one step; no re-dispatch after rejection.
* Expected-value objective: the MILP is risk-neutral; risk shows up
  only in the ex-post MC distribution.

## References

* ENTSO-E balancing framework (FCR/aFRR/mFRR product definitions;
  SAFA convention for FCR).
* `docs/uncertainty_design.md` (MC realisation),
  `docs/economics_design.md` (cashflow/fee/LCOE-LCOS scope),
  `docs/README.md` (notation), `pvbess_opt/conventions.md`.

---

## Appendix: verification and falsification log

Each numbered section states the intended math, cites the implementing
symbol, and records a **PASS / FAIL** status from the audit.  The
named symbols are pinned by
`tests/test_logic_spec_conformance.py::test_balancing_verification_symbols_present`
against a built model, so this log cannot silently drift from the
code.  (`file.py:NN` references are indicative of the audited
revision; the symbols are the stable anchors.)

### 1. Product taxonomy — PASS

Every consumer iterates the canonical tuples
(`balancing.PRODUCTS_ALL/UP/DN/SYMMETRIC/WITH_ACTIVATION`) without
re-deriving them: the MILP (reservation variable, power-budget and
SOC-headroom rules, revenue terms), the KPI layer, and the Monte Carlo
(Bernoulli draws + SOC-trajectory check).  FCR is consistently
symmetric and capacity-only across all three layers.

### 2. MILP power budget — PASS, `BM_POWER_DN` / `BM_POWER_UP`

Implemented exactly as Eqs. (B2)–(B3): both sides resolve to kWh per
step ($r_{k,t}$ lifted by $\Delta t$); FCR included in both
directions.

### 3. SOC headroom — PASS, `BM_SOC_UP` / `BM_SOC_DN`

Implemented exactly as Eqs. (B4)–(B5); the η placement matches (divide
by $\eta_d$ for up-delivery, multiply by $\eta_c$ for down-absorption),
and FCR uses $d_{\mathrm{fcr}}$ instead of $\Delta t$ because the
sustained-output requirement is independent of the settlement period.

### 4. Expected SOC drift in `soc_dynamics` — PASS

The drift terms of Eq. (B6) appear in the SOC recursion and its
terminal-step copy; the KPI helper `kpis._balancing_soc_drift`
implements the same formula term-for-term and
`verify_dispatch_invariants` consumes it, keeping invariants 3, 4 and
8 aligned with the MILP.  FCR is absent from both directional tuples
— zero net drift, matching the symmetric-FCR simplification.

### 5. Expected revenue dimensions — PASS

Eq. (B7) as implemented: `/1000` converts EUR/MWh × kW × h → EUR; FCR
is excluded from the activation loop by iterating
`PRODUCTS_WITH_ACTIVATION`.  The KPI mirror uses the same numeric
formula.

### 6. MC SOC-violation coupling — PASS

`rolling_horizon.realise_balancing_scenario` captures the per-product
`activated` arrays in the revenue pass and reuses them in the SOC
pass: a scenario cannot report revenue from activations missing from
its SOC trace, nor "SOC OK" on a trace that never earned.  Falsified
and fixed during the audit; regression:
`tests/test_balancing_mc_coupling.py`.

### 7. Lifetime / cashflow scaling — PASS

`lifetime._BALANCING_RESERVATION_COLUMNS` scales reservations on
`bess_factor(y)`; the cashflow composes
year-$y$ revenue = year-1 revenue × $f^{B}_y$ × $(1+i_{\mathrm{bm}})^{y-1}$
(Eq. B8), with $i_{\mathrm{bm}}$ from the balancing sheet.

### 8. LCOE / LCOS exclusion — PASS

The LCOE and LCOS numerators are built strictly from per-asset CAPEX /
DEVEX / OPEX / replacement; no `bm_*` term enters either, and
balancing produces no DAM-discharge MWh for the LCOS denominator.
Balancing reaches NPV / IRR / payback via `build_yearly_cashflow`
only.  Toggling `balancing_enabled` leaves LCOE/LCOS unchanged
(regression: `tests/test_balancing_lifetime_cashflow.py`).

### 9. `dam_capacity_share_pct` semantics — PASS, declarative-only

Validator-only: the share-sum rule
$\sum$ shares (DAM + five products) ≤ 100 %.  No MILP or KPI consumer
caps DAM flows on it — DAM dispatch is bounded indirectly by
(B2)–(B3).

### 10. `fcr_activation_probability_pct` informational-only — PASS

Declared, range-validated, stored on `BalancingConfig`, and never read
by the MILP, KPI, or Monte Carlo paths
(`PRODUCTS_WITH_ACTIVATION` excludes FCR).  The field is preserved on
the schema so a future FCR activation-revenue stream can land without
a workbook migration.
