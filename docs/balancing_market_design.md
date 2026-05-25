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
