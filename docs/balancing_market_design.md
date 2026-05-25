# Balancing market participation — design note

## Mission

Extend the `pvbess_opt` package with stochastic participation in the European
balancing markets (ENTSO-E framework) alongside the existing Day-Ahead Market
(DAM) co-optimisation. The aim is to model FCR, aFRR, and mFRR with the same
rigour applied today to DAM dispatch and self-consumption: per-product capacity
reservations are explicit decision variables, expected revenues drive the MILP
objective, and a Monte Carlo realisation step gives a P10 / P50 / P90 view of
realised balancing income for risk analysis.

The extension is fully **opt-in**: when `balancing_enabled = FALSE` (the default)
the MILP, the KPI dictionary, the lifetime cashflow, the Monte Carlo output, and
the PDF report are bit-identical to the previous release. The nine existing
dispatch invariants continue to hold; six new balancing-specific invariants are
added to the test suite and documented below.

## Five products

| Key        | Direction  | Capacity payment | Activation payment | Notes                                                |
|------------|------------|------------------|--------------------|------------------------------------------------------|
| `fcr`      | symmetric  | yes              | no                 | ENTSO-E FCR is capacity-only; symmetric reservation. |
| `afrr_up`  | up only    | yes              | yes                | Upward aFRR (battery discharges).                    |
| `afrr_dn`  | down only  | yes              | yes                | Downward aFRR (battery charges).                     |
| `mfrr_up`  | up only    | yes              | yes                | Upward mFRR.                                         |
| `mfrr_dn`  | down only  | yes              | yes                | Downward mFRR.                                       |

`fcr` carries no activation payment because the duty cycle is implicit in the
capacity certification (ENTSO-E SAFA convention). Symmetric reservation means
that the same kW counts in **both** the up and down power-budget directions.

## High-level MILP extension

For each product `k` and timestep `t` the model gains a continuous
non-negative variable `r[k, t]` (kW reserved). Auxiliary inputs that
parameterise its economic effect:

* `α_k = acceptance_probability[k]` — probability that a submitted bid clears.
* `β_k = activation_probability[k]` — probability that a cleared bid is called.
* `p_cap_k(t)` — capacity price (€/MWh) at time `t`.
* `p_act_k(t)` — activation price (€/MWh), zero for FCR.

Reservation bounds (per product share of `bess_power_kw`):

```
0 ≤ r[k, t] ≤ s_k · bess_power_kw
```

Power-budget per direction (FCR counts in BOTH directions):

```
charge_dam(t)    + r[fcr, t] + r[afrr_dn, t] + r[mfrr_dn, t]  ≤ bess_power_kw
discharge_dam(t) + r[fcr, t] + r[afrr_up, t] + r[mfrr_up, t]  ≤ bess_power_kw
```

SOC headroom (per direction), including a safety buffer `h_buf` and the
FCR-specific duration requirement `D_fcr`:

```
soc(t) − soc_min  ≥ (1 + h_buf) · dt · ( r[afrr_up] + r[mfrr_up] ) / η_d
                  + (1 + h_buf) · D_fcr · r[fcr]   / η_d

soc_max − soc(t)  ≥ (1 + h_buf) · dt · ( r[afrr_dn] + r[mfrr_dn] ) · η_c
                  + (1 + h_buf) · D_fcr · r[fcr]                   · η_c
```

Expected activation energy enters the existing SOC recursion as a deterministic
drift (FCR symmetric activations cancel in expectation):

```
soc(t+1) = soc(t) + η_c · charge_dam(t) · dt − discharge_dam(t) · dt / η_d
                  + E_act_charge(t) − E_act_discharge(t)

E_act_charge(t)    = η_c · dt · Σ_{k in DN}  α_k · β_k · r[k, t]
E_act_discharge(t) = (dt / η_d) · Σ_{k in UP} α_k · β_k · r[k, t]
```

Expected balancing revenue (the new objective contribution, summed over `t`):

```
R_cap(t) = dt · Σ_{k in ALL}             α_k · p_cap_k(t) · r[k, t] / 1000
R_act(t) = dt · Σ_{k in WITH_ACTIVATION} α_k · β_k · p_act_k(t) · r[k, t] / 1000
```

Both up and down activation prices are treated as positive payments to the
balancing service provider per MWh activated. Sign conventions are the user's
responsibility on input prices; the shipped defaults follow the most common EU
convention.

## Monte Carlo realisation

After the MILP fixes the optimal `r*[k, t]`, the rolling-horizon module
samples `n_scenarios` × per-product per-step independent Bernoullis for
acceptance (`α_k`) and activation (`β_k`), plus log-normal multiplicative
noise (`bm_price_sigma_*_pct`) on the capacity and activation prices. SOC
trajectories are tracked: scenarios that would breach `soc_min` / `soc_max`
are flagged as `soc_constrained` and truncated. The aggregate output exposes
P10 / P50 / P90 of balancing revenue and a per-product breakdown.

## Six new invariants

* **INV-B1** Sum of per-product capacity shares ≤ 100 % of `bess_power_kw`.
* **INV-B2** Per-product reservation `r[k, t] ≤ s_k · bess_power_kw`.
* **INV-B3** Up-direction SOC headroom satisfied at every `t`.
* **INV-B4** Down-direction SOC headroom satisfied at every `t`.
* **INV-B5** Per-direction power-budget satisfied at every `t`.
* **INV-B6** `balancing_enabled = FALSE` ⇒ all `r[k, t] = 0` and the model is
  bit-identical to the previous release.

## Workbook schema

The optional `balancing` sheet uses the same key/value structure as the existing
parameter sheets. The 33 keys cover the master switch, six capacity shares
(DAM + five products), ten probabilities (five acceptance × five activation),
nine default prices (five capacity + four activation; FCR has no activation
price), the FCR sustained-duration requirement, the balancing settlement
period, an SOC safety buffer, a balancing-revenue inflation rate, two Monte
Carlo price sigmas and a default seed. The reference workbook keeps the master
switch off by default so a fresh checkout behaves identically to the previous
release.

Nine optional per-step timeseries columns sit on the existing `timeseries`
sheet alongside `pv_kwh`, `load_kwh`, and `dam_price_eur_per_mwh`. Each
per-product price column is read verbatim when present, or filled with the
scalar default from the balancing sheet otherwise.

## KPI reference

Every balancing run emits the following keys on the headline KPI dict (zero
when the gate is off): `bm_<product>_capacity_revenue_eur` for the five
products, `bm_<product>_activation_revenue_eur` for the four
activation-paying products, `bm_total_capacity_revenue_eur`,
`bm_total_activation_revenue_eur`, `bm_total_balancing_revenue_eur`,
`bm_revenue_share_pct`, the two expected activation energies
(`bm_expected_activation_energy_up_kwh` and `..._dn_kwh`), and per-product
average reservation `bm_reservation_avg_kw_<product>`.

The Monte Carlo helper extends the dict with the P10 / P50 / P90 quantiles
of total balancing revenue, the per-product capacity and activation
quantiles, the share of scenarios that hit a SOC bound, and the raw realised
totals for the histogram plot.

The yearly cashflow gains three columns: `balancing_capacity_revenue_eur`,
`balancing_activation_revenue_eur`, and their sum `balancing_revenue_eur`.
The financial KPIs include the lifecycle totals
(`lifetime_bm_revenue_total_eur`, the capacity and activation totals) and a
per-year list (`lifetime_bm_revenue_eur_per_year`).

## Plotting reference

Two dedicated balancing plots ship in `pvbess_opt/plotting/balancing.py`:

* `plot_balancing_reservation_profile` — 24-hour average reservation by
  product, rendered as a stacked area chart.
* `plot_balancing_mc_distribution` — histogram of realised balancing
  revenue across the Monte Carlo scenarios with P10 / P50 / P90 lines.

Both helpers return `None` when the dispatch frame does not carry the
balancing columns so the main pipeline can skip the page write without
conditionals.

## Modelling simplifications

* **Settlement period.** All five products share a single settlement period
  equal to `dt_minutes` from the dispatch timeseries; the validator in
  `pvbess_opt.io._validate_balancing_config` rejects loads where
  `bm_settlement_minutes != dt_minutes`. In the real European markets the
  settlement cadences differ — FCR is sub-second, aFRR is typically 4–15 min,
  mFRR is 15 min — and a higher-fidelity model would resolve each product on
  its own cadence and re-aggregate to the DAM step. The audit decided to keep
  the simplification because (a) the workbook cadence is the natural
  reservation horizon for project-finance analysis, and (b) the worst-case
  SOC headroom carried by `bm_soc_headroom_pct` already absorbs the within-
  step variability the sub-cadence model would otherwise add.
* **FCR symmetry in expectation.** FCR up- and down-activations are assumed
  to cancel in expectation, so FCR contributes zero net SOC drift. The
  `fcr_activation_probability_pct` field is retained for documentation and
  is not consumed by the MILP, KPI, or Monte Carlo paths.
* **DAM capacity share is declarative.** `dam_capacity_share_pct` is a
  validator-only field used to ensure `sum(shares) <= 100 %`; DAM dispatch
  is bounded indirectly by `BM_POWER_UP / BM_POWER_DN` consuming the residual
  of `bess_power_kw` left over after the balancing reservations in each step.

## Worked numerical example

Project: 1 MW / 4 MWh BESS, hourly cadence, one settlement period of dispatch.
Configuration: `fcr_capacity_share_pct = 10`, `fcr_required_duration_hours = 0.5`,
`fcr_bid_acceptance_pct = 70`, `fcr_default_capacity_price_eur_per_mwh = 12`,
`bm_soc_headroom_pct = 10`, `eta_charge = eta_discharge = 0.97`,
`soc_min = 800 kWh`, `soc_max = 3 800 kWh`.

* Reservation cap: `r_max[fcr] = 0.10 · 1 000 kW = 100 kW`.
* SOC headroom (up): `(1 + 0.10) · 0.5 · 100 / 0.97 ≈ 56.7 kWh`. The MILP
  must keep `soc(t) − 800 ≥ 56.7` kWh at every step where `r[fcr, t] = 100`.
* Expected capacity revenue per step: `0.70 · 1 · 12 · 100 / 1000 = 0.84 EUR`.
* Over one year of hourly steps that is `0.84 · 8 760 = 7 358 EUR` at the
  default share and FCR price.

## Build sequence

The work is structured as eleven self-contained increments, each landing as one
local commit:

* setup and this design note
* workbook schema in `io.py`
* `pvbess_opt/balancing.py` module (data model + synthetic generator)
* MILP extension in `optimization.py`
* KPIs in `kpis.py`
* economics and lifetime cashflow integration
* Monte Carlo realisation in `rolling_horizon.py`
* plotting (yearly stack, MC histogram, reservation profile)
* tests (unit, integration, invariant, Monte Carlo)
* reference workbook update with the new sheet and synthetic timeseries
* documentation, changelog, version bump
