# Self-consumption mode — canonical logic specification

This is the universal contract for the `mode = self_consumption`
regulatory regime: every decision variable, every hard constraint,
the objective, the post-solve audit invariants, and the carve-outs
relative to `merchant` mode. The companion conformance test
`tests/test_logic_spec_conformance.py` parses the constraint headings
under "Hard constraints" and asserts that each is attached to a freshly
built Pyomo model, so the spec cannot drift from the code without
breaking CI.

The notation throughout:

* `t` — timestep index, `0 ≤ t < N`.
* `dt_h` — settlement-period length in hours (`dt_minutes / 60`).
* `pv[t]`, `load[t]` — exogenous per-step PV generation and load (kWh).
* `dam[t]`, `retail[t]` — DAM and retail prices (EUR/MWh).
* `η_c`, `η_d` — BESS charge / discharge efficiencies.
* `e_cap` — BESS energy capacity in kWh (parameter, pinned to
  `bess_capacity_kwh`).
* `p_bess` — BESS rated power in kW (symmetric charge / discharge).
* `p_export` — grid-export nameplate in kW (`p_grid_export_max_kw`).
* `mi_frac[t]` — per-step max-injection fraction in `[0, 1]` derived
  from the `max_injection_profile` sheet.
* `M_imp`, `M_exp`, `M_charge`, `M_pv` — tight big-Ms returned by
  `derive_tight_big_m` in `pvbess_opt/optimization.py:286-315`.

## 1. Scope

Implementation of the Greek "self-consumption" regulatory regime per
**MD YPEN/DAPEEK/93976/2772/2024**. The user-side asset stack is a
co-located load with a behind-the-meter PV array and an optional BESS:

* The retail tariff covers load consumption (`avoided cost` revenue).
* Surplus PV / BESS energy may be exported to the DAM under the
  combined per-step cap derived from `p_grid_export_max_kw` and the
  `max_injection_profile` sheet.
* Settlement is 15-minute by default (`settlement_minutes = 15`); the
  optimization timestep is auto-detected from the timeseries cadence
  (`pvbess_opt/io.py:detect_timestep_minutes`).

## 2. Decision variables

Declared at `pvbess_opt/optimization.py:433-514` and pinned to zero
when an asset is absent (no-BESS / no-PV branches). The full set in
`self_consumption` mode:

| Variable                       | Domain                | File:line |
| ------------------------------ | --------------------- | --------- |
| `pv_to_load[t]`                | NonNegativeReals      | `optimization.py:435` |
| `pv_to_bess[t]`                | NonNegativeReals      | `optimization.py:436` |
| `pv_to_grid[t]`                | NonNegativeReals      | `optimization.py:437` |
| `pv_curtail[t]`                | NonNegativeReals      | `optimization.py:438` |
| `bess_dis_load[t]`             | NonNegativeReals      | `optimization.py:440` |
| `bess_dis_grid[t]`             | NonNegativeReals      | `optimization.py:441` |
| `grid_to_load[t]`              | NonNegativeReals      | `optimization.py:443` |
| `grid_to_bess[t]`              | NonNegativeReals      | `optimization.py:444` |
| `soc[t]`                       | NonNegativeReals      | `optimization.py:433` |
| `y_charge[t]`                  | Binary                | `optimization.py:478` |
| `y_dis[t]`                     | Binary                | `optimization.py:479` |
| `y_grid_io[t]`                 | Binary (self_consumption only) | `optimization.py:773` |
| `slack[t]`                     | NonNegativeReals (self_consumption only) | `optimization.py:758` |
| `z_pv_active[t]`               | Binary (only when `allow_bess_grid_charging`) | `optimization.py:794` |
| `r_balancing[k, t]`            | NonNegativeReals (only when `balancing_enabled` AND BESS present) | `optimization.py:563` |

The `grid_export_total[t] = pv_to_grid[t] + bess_dis_grid[t]`
Expression is a derived expression (the export metric), not a variable.
The companion `grid_injection_total[t]` Expression is the cap basis used
by `EXPORT_CAP` (see §3 below): it equals `grid_export_total[t]` by
default and, when `grid_cap_includes_load = true` in `self_consumption`
mode, the total plant injection
`pv_to_load + bess_dis_load + pv_to_grid + bess_dis_grid`.

## 3. Hard constraints — formal statements

Each subsection states the constraint, the file:line of the rule, and
the active scope. Constraint names match the Pyomo attribute names on
the model so the conformance test can `hasattr(model, NAME)` against
them.

### PV_SPLIT(t)

```
pv[t] = pv_to_load[t] + pv_to_bess[t] + pv_to_grid[t] + pv_curtail[t]
```

Implemented at `pvbess_opt/optimization.py:521-527`. Active in both
modes; when PV is absent the four PV-side flows are independently
pinned to zero (`optimization.py:459-471`).

### LOAD_BAL(t)

```
load[t] = pv_to_load[t] + bess_dis_load[t] + grid_to_load[t]
```

Implemented at `pvbess_opt/optimization.py:530-536`. **Active in
`self_consumption` only**; `merchant` mode omits the load balance and
pins the three load-coverage flows to zero
(`optimization.py:446-455`).

### LOAD_PV_PRIORITY(t)

```
pv_to_load[t] ≥ min(pv[t], load[t])
```

Implemented at `pvbess_opt/optimization.py:538-547`. Combined with
`PV_SPLIT` and `LOAD_BAL` this forces `pv_to_load[t] == min(pv[t],
load[t])` exactly — the Section 2 hard load-coverage priority from the
MD spec. Active in `self_consumption` only.

### LOAD_PRIORITY_SLACK_DEF(t)

```
slack[t] ≥ pv[t] + bess_dis_load[t] + bess_dis_grid[t] − load[t]
```

Implemented at `pvbess_opt/optimization.py:759-765`. Active in
`self_consumption` only. The slack underpins the surplus-only export
rule of Section 5 of the MD spec.

### LOAD_PRIORITY_EXPORT(t)

```
pv_to_grid[t] + bess_dis_grid[t] ≤ slack[t]
```

Implemented at `pvbess_opt/optimization.py:766-771`. After
substituting `PV_SPLIT` and `LOAD_BAL`, the inequality reduces to
`grid_to_load[t] ≤ pv_to_bess[t] + pv_curtail[t]`, i.e. an hour can
only export when its load is fully covered without grid import. Active
in `self_consumption` only.

### SOC_DYN(t)

```
soc[t+1] = soc[t] + η_c · (pv_to_bess[t] + grid_to_bess[t])
                  − (bess_dis_load[t] + bess_dis_grid[t]) / η_d
                  + drift_charge(t) − drift_discharge(t)            [when balancing_enabled]
```

Implemented at `pvbess_opt/optimization.py:569-593`. Active for every
`t < N − 1`; the terminal step is closed by `SOC_TERM` /
`SOC_TERM_MIN` / `SOC_TERM_MAX` (below). The optional drift terms are
defined in `docs/balancing_logic_verification.md` §4.

### SOC_INIT, SOC_TERM / SOC_TERM_MIN / SOC_TERM_MAX

```
SOC_INIT:      soc[0] = initial_soc_frac · e_cap         (or initial_soc_kwh override)

SOC_TERM:      soc_post_N−1 == soc[0]                    [terminal_soc_equal == True]

SOC_TERM_MIN:  soc_post_N−1 ≥ soc_min_frac · e_cap       [terminal_soc_equal == False]
SOC_TERM_MAX:  soc_post_N−1 ≤ soc_max_frac · e_cap       [terminal_soc_equal == False]
```

Implemented at `pvbess_opt/optimization.py:604-647`. `soc_post_N−1` is
`soc[N−1]` plus the same per-step charge / discharge (and optional
balancing drift) used in `SOC_DYN` — see `optimization.py:613-635`.
The rolling-horizon dispatcher always passes
`terminal_soc_free=True`, so a single window in
`pvbess_opt/rolling_horizon.py:rolling_horizon_dispatch` never gets
the closed-cycle constraint.

### CH_LIM(t), DIS_LIM(t), MODE_LINK(t)

```
CH_LIM(t):    pv_to_bess[t]  + grid_to_bess[t]  ≤ p_bess · dt_h · y_charge[t]
DIS_LIM(t):   bess_dis_load[t] + bess_dis_grid[t] ≤ p_bess · dt_h · y_dis[t]
MODE_LINK(t): y_charge[t] + y_dis[t] ≤ 1
```

Implemented at `pvbess_opt/optimization.py:480-484, 650-664`. The
charge / discharge limit is the symmetric `bess_power_kw` — the
asymmetric (`p_charge_max`, `p_dis_max`) pair is not supported.
`MODE_LINK` is the Section 4 simultaneity rule from the MD spec.

### EXPORT_CAP(t)

```
grid_injection_total[t] ≤ p_export · dt_h · mi_frac[t]
```

Implemented in `pvbess_opt/optimization.py` (`EXPORT_CAP` over the
`grid_injection_total` Expression). Active in **both** modes — it is the
regulatory grid-connection limit from MD YPEN/DAPEEK/53563/1556/2023 and
merchant mode does not skip it.

What the cap binds on is selected by the optional `grid_cap_includes_load`
project input:

* **Default** (`grid_cap_includes_load = false`) — binds on surplus
  export only: `grid_injection_total[t] = pv_to_grid[t] + bess_dis_grid[t]`.
  This is the historical behaviour and is bit-for-bit backward compatible.
* **Strict** (`grid_cap_includes_load = true`, `self_consumption` only) —
  binds on the total plant injection at the connection point:
  `pv_to_load[t] + bess_dis_load[t] + pv_to_grid[t] + bess_dis_grid[t]`.
  Under Virtual Net-Billing the energy virtually allocated to the remote
  load is physically injected at the plant too, so the cap models a
  physical plant-injection limit, not only a surplus-export limit. Strict
  load priority is never relaxed; a run where `min(pv, load)` exceeds the
  per-step cap at any step is rejected pre-solve with a clear error.
  Merchant mode has no co-located load, so the basis collapses to surplus
  export and the flag is a no-op.

### NO_SIM_GRID_IMPORT(t), NO_SIM_GRID_EXPORT(t)

```
NO_SIM_GRID_IMPORT(t): grid_to_load[t] + grid_to_bess[t]  ≤ M_imp · y_grid_io[t]
NO_SIM_GRID_EXPORT(t): pv_to_grid[t]   + bess_dis_grid[t] ≤ M_exp · (1 − y_grid_io[t])
```

Implemented at `pvbess_opt/optimization.py:773-787`. Active in
`self_consumption` only — the audit verified that simultaneous
import / export does not occur in practice in `merchant` mode so the
big-M overhead is omitted there.

### CYC

```
Σ_{t ∈ day d} (bess_dis_load[t] + bess_dis_grid[t])
              ≤ max_cycles_per_day · e_cap
```

Implemented at `pvbess_opt/optimization.py:666-670`. One `ConstraintList`
entry per calendar day in the dispatch window.

### GRID_CHARGE_GATE, GRID_CHG_PV_GATE

```
GRID_CHARGE_GATE(t):  grid_to_bess[t] ≤ M_charge · (1 − z_pv_active[t])
GRID_CHG_PV_GATE(t):  pv[t]           ≤ M_pv     · z_pv_active[t]
```

Implemented at `pvbess_opt/optimization.py:789-804`. Only declared
when `allow_bess_grid_charging == True` AND the project carries a
BESS. Together they implement the Section 6 gating rule: the BESS may
charge from the grid only in periods where PV is effectively zero.
When the option is disabled, `grid_to_bess[t] == 0` is pinned at
`optimization.py:473-476`.

## 4. Objective

Profit maximisation (`pvbess_opt/optimization.py:806-873`). For
`mode = self_consumption`:

```
profit = Σ_t  retail[t] · (pv_to_load[t]  + bess_dis_load[t])  / 1000      [avoided cost]
       + Σ_t  dam[t]    · (pv_to_grid[t]  + bess_dis_grid[t])  / 1000      [export revenue]
       − Σ_t  dam[t]    · grid_to_bess[t]                       / 1000     [grid-charge cost]
       + cycles_bonus                                                       [tie-breaker, default 0]
       − Σ_t  _WEIGHT_CURTAIL_TIEBREAK_EUR_PER_KWH · pv_curtail[t]          [curtail tie-breaker]
       + balancing_revenue                                                   [when balancing_enabled]

OBJ:    maximise profit
```

The two `_WEIGHT_*` constants at the top of
`pvbess_opt/optimization.py:100-104` are tie-breakers, not project
knobs, and stay private to the module. `balancing_revenue` is the sum
of the capacity and activation terms from
`docs/balancing_logic_verification.md` §5.

In `merchant` mode the `avoided_cost` term is identically zero (the
three load-coverage flows are pinned to zero by the merchant guards),
and the remaining terms behave the same.

## 5. Nine audit invariants

The post-solve `verify_dispatch_invariants` helper
(`pvbess_opt/optimization.py:1072-1225`) returns the per-residual
dictionary below. Tolerance is `ENERGY_TOLERANCE = 1.0e-3 kWh`
(`pvbess_opt/kpis.py:63`).

### invariant_1_pv_balance_kwh

```
max_t  | pv[t] − pv_to_load[t] − pv_to_bess[t]
                − pv_to_grid[t] − pv_curtail[t] |
```

Active in both modes. Source: `optimization.py:1114-1117`.

### invariant_2_load_balance_kwh

```
max_t  | load[t] − pv_to_load[t]
                − bess_dis_load[t] − grid_to_load[t] |
```

Active in `self_consumption` only; identically zero in `merchant`.
Source: `optimization.py:1119-1122`.

### invariant_3_soc_dynamics_kwh

```
max_t  | (soc[t+1] − soc[t])
       − ( η_c · (pv_to_bess[t] + grid_to_bess[t])
         − (bess_dis_load[t] + bess_dis_grid[t]) / η_d
         + drift[t] ) |
```

`drift[t]` is the per-step expected-activation drift from
`_balancing_soc_drift` (zero when balancing is off). Source:
`optimization.py:1124-1138`.

### invariant_4_rte_bound_excess_kwh

```
total_discharge ≤ η_c · η_d · total_charge
                + η_d · (soc[0] − final_state)
                + η_d · drift_total
```

`drift_total` is the sum of the per-step drift, included so the
bound stays consistent with `SOC_DYN`. Source:
`optimization.py:1140-1164`.

### invariant_5_no_sim_grid_io_max_product_kwh2

```
max_t  ((grid_to_load[t] + grid_to_bess[t])
      · (pv_to_grid[t] + bess_dis_grid[t]))
```

Active in `self_consumption` only. Source: `optimization.py:1166-1171`.

### invariant_6_load_priority_violations

```
count_t  ((export[t] > tol) AND (grid_to_load[t] > tol))
```

`export[t] = pv_to_grid[t] + bess_dis_grid[t]`. Active in
`self_consumption` only. Source: `optimization.py:1173-1178`.

### invariant_7_curtail_behavior_kwh

```
count_t  ((cap[t] − export[t] > tol) AND (pv_curtail[t] > tol))
```

The cap-not-binding ⇒ curtail-zero rule, checked in both modes per
the design. Source: `optimization.py:1180-1191`.

### invariant_8_soc_closed_cycle_kwh

```
| final_state − soc[0] |              [when terminal_soc_equal == True]
```

`final_state` includes the same drift term as invariant 3. Source:
`optimization.py:1193-1206`.

### invariant_9_pv_load_priority_kwh

```
max_t  | pv_to_load[t] − min(pv[t], load[t]) |
```

Active in `self_consumption` only. Source: `optimization.py:1208-1213`.

## 6. Mode-specific exclusions

What `self_consumption` carries that `merchant` does NOT:

* `LOAD_BAL`, `LOAD_PV_PRIORITY` (load balance + Section 2 hard
  priority).
* `LOAD_PRIORITY_SLACK_DEF`, `LOAD_PRIORITY_EXPORT` (Section 5
  surplus-only export rule and its slack variable).
* `NO_SIM_GRID_IMPORT`, `NO_SIM_GRID_EXPORT` (and their `y_grid_io`
  binary).
* Invariants 2, 5, 6, 9 — all return 0.0 in `merchant`.
* Retail-tariff `avoided_cost` term in the objective.

What `merchant` carries that `self_consumption` does NOT:

* `MERCHANT_NO_PV_TO_LOAD`, `MERCHANT_NO_BESS_TO_LOAD`,
  `MERCHANT_NO_GRID_TO_LOAD` pinning the three load-coverage flows to
  zero (`optimization.py:446-455`).

Mode resolution is centralised in
`pvbess_opt/modes.py:resolve_mode`; the only two valid values are
`self_consumption` and `merchant`.

What BOTH modes carry:

* `PV_SPLIT`, `SOC_DYN`, `SOC_INIT`, `SOC_TERM*`, `CH_LIM`, `DIS_LIM`,
  `MODE_LINK`, `CYC`, `EXPORT_CAP`.
* The grid-charge gate (`GRID_CHARGE_GATE`, `GRID_CHG_PV_GATE`) when
  `allow_bess_grid_charging` is enabled.
* The balancing block (`BM_POWER_UP / DN`, `BM_SOC_UP / DN`,
  `r_balancing`) when `balancing_enabled` is set AND a BESS is present.
* Invariants 1, 3, 4, 7, 8.

## 7. Test contract

Any conformance test reading this spec should assert that a freshly
built `self_consumption` Pyomo model satisfies, at minimum:

* The constraint attributes listed under §3 are present:
  `PV_SPLIT`, `LOAD_BAL`, `LOAD_PV_PRIORITY`,
  `LOAD_PRIORITY_SLACK_DEF`, `LOAD_PRIORITY_EXPORT`, `SOC_DYN`,
  `SOC_INIT`, `CH_LIM`, `DIS_LIM`, `MODE_LINK`,
  `EXPORT_CAP`, `NO_SIM_GRID_IMPORT`, `NO_SIM_GRID_EXPORT`, `CYC`.
* When the workbook sets `terminal_soc_equal = True`, `SOC_TERM` is
  present; otherwise the two `SOC_TERM_MIN` and `SOC_TERM_MAX` are
  present in its place.
* When `allow_bess_grid_charging = True`, `GRID_CHARGE_GATE` and
  `GRID_CHG_PV_GATE` are present.
* After a solve, every invariant key returned by
  `verify_dispatch_invariants` is present and within
  `ENERGY_TOLERANCE`: `invariant_1_pv_balance_kwh`,
  `invariant_2_load_balance_kwh`, `invariant_3_soc_dynamics_kwh`,
  `invariant_4_rte_bound_excess_kwh`,
  `invariant_5_no_sim_grid_io_max_product_kwh2`,
  `invariant_6_load_priority_violations`,
  `invariant_7_curtail_behavior_kwh`,
  `invariant_8_soc_closed_cycle_kwh`,
  `invariant_9_pv_load_priority_kwh`.
* For a balancing-enabled model: the symbols cited as PASS in
  `docs/balancing_logic_verification.md` §§2–3
  (`BM_POWER_DN`, `BM_POWER_UP`, `BM_SOC_UP`, `BM_SOC_DN`) and the
  reservation variable `r_balancing` are attached.

The conformance test `tests/test_logic_spec_conformance.py` parses
the constraint names directly out of this document (the `###` headings
under "Hard constraints") and out of `docs/balancing_logic_verification.md`
(the symbols cited as PASS in §§2–3) so the doc and the code stay
locked together.
