# Self-consumption mode — design

Domain design document for the `mode = self_consumption` regulatory
regime: inputs, decision variables, every hard constraint, the
objective, the post-solve audit invariants, settlement wiring, and the
carve-outs relative to `merchant` mode (see
`docs/merchant_design.md`).  Notation follows the shared table in
`docs/README.md`.

The companion conformance test
`tests/test_logic_spec_conformance.py` parses the constraint headings
under "Hard constraints — formal statements" and the invariant headings
under "Nine audit invariants" out of THIS file and asserts each symbol
is attached to a freshly built Pyomo model, so the document cannot
drift from the code without breaking CI.

> **Line references.** `file.py:NN` references are indicative of the
> audited revision; lines drift as the code evolves.  The NAMED symbols
> (constraints, variables, functions) are the stable anchors enforced
> by the conformance test.

## Purpose & scope

Implementation of the Greek "self-consumption" regulatory regime per
**MD YPEN/DAPEEK/93976/2772/2024**.  The asset stack is a co-located
load with a behind-the-meter PV array and an optional BESS:

* The retail tariff values load coverage (avoided cost).
* Surplus PV / BESS energy may be exported to the day-ahead market
  (DAM) under the combined per-step cap derived from
  `p_grid_export_max_kw` and the `max_injection_profile` sheet
  (MD YPEN/DAPEEK/53563/1556/2023).
* Settlement is 15-minute under the regulation; the optimization
  timestep is auto-detected from the timeseries cadence
  (`pvbess_opt.io.detect_timestep_minutes`), so the canonical workbook
  ships a 15-minute grid ($N = 35\,040$, $\Delta t = 0.25$ h).

Hard guarantees of the mode: load balance, exact PV→load priority, no
simultaneous grid import/export, surplus-only export, and the
unconditional grid-injection cap.  Self-consumption *emerges* from the
profit objective whenever the retail tariff exceeds the DAM price; the
hard `LOAD_PV_PRIORITY` constraint keeps the dispatch correct for any
retail/DAM ratio.

## Inputs

Keys consumed by the mode (defaults from the `io.py` sheet tables;
full workbook reference: `docs/source/users.guide/inputs.rst`):

| Sheet | Key | Default | Role |
|---|---|---|---|
| project | `mode` | `self_consumption` | regime switch |
| project | `retail_tariff_eur_per_mwh` | 120.0 | scalar tariff $\pi^{\mathrm{ret}}$ (per-step `retail_price_eur_per_mwh` column overrides) |
| project | `p_grid_export_max_kw` | 5000.0 | export nameplate $P^{G}$ (empty/`inf` token disables the cap) |
| project | `grid_cap_includes_load` | FALSE | cap basis: surplus export (default) vs total plant injection |
| project | `allow_bess_grid_charging` | FALSE | enables `grid_to_bess` + the PV-gating binary |
| bess | `efficiency_charge` / `efficiency_discharge` | 0.97 / 0.97 | $\eta_c$, $\eta_d$ |
| bess | `soc_min_frac` / `soc_max_frac` | 0.20 / 0.95 | $\underline{e}$, $\overline{e}$ |
| bess | `initial_soc_frac` | 0.50 | $E_0 / E^{\mathrm{cap}}$ |
| bess | `terminal_soc_equal` | TRUE | year-close SOC condition |
| bess | `max_cycles_per_day` | 1.0 | daily throughput cap |
| bess | `bess_power_kw` / `bess_capacity_kwh` | 0 / 0 | $P^{B}$, $E^{\mathrm{cap}}$ |
| bess | `bess_wear_cost_eur_per_mwh` | 0.0 | discharge-throughput shadow price |
| timeseries | `load_kwh` | — | required in this mode (missing → `ValueError`) |
| timeseries | `pv_kwh`, `dam_price_eur_per_mwh` | — | exogenous series |
| max_injection_profile | 24×1 or 24×12 | 100 % | $\mu_t$ |
| max_injection_profile_pv / _bess | optional | — | per-source sub-cap fractions |
| balancing / ppa sheets | see their design docs | off | optional opt-in blocks |

## Mathematical formulation

### Sets, parameters, decision variables

$t \in \{0,\dots,N-1\}$, step length $\Delta t$ h.  Exogenous: PV
generation $G_t$, load $L_t$ (kWh/step), DAM price
$\pi^{\mathrm{DAM}}_t$ and retail price $\pi^{\mathrm{ret}}_t$
(EUR/MWh).  All decision variables are declared in
`optimization.build_model` and pinned to zero when an asset is absent
(no-BESS / no-PV branches).  The full set in `self_consumption` mode:

| Variable | Symbol | Domain |
|---|---|---|
| `pv_to_load[t]` | $x^{pl}_t$ | $\mathbb{R}_{\ge 0}$ |
| `pv_to_bess[t]` | $x^{pb}_t$ | $\mathbb{R}_{\ge 0}$ |
| `pv_to_grid[t]` | $x^{pg}_t$ | $\mathbb{R}_{\ge 0}$ |
| `pv_curtail[t]` | $x^{pc}_t$ | $\mathbb{R}_{\ge 0}$ |
| `bess_dis_load[t]` | $x^{bl}_t$ | $\mathbb{R}_{\ge 0}$ |
| `bess_dis_grid[t]` | $x^{bg}_t$ | $\mathbb{R}_{\ge 0}$ |
| `grid_to_load[t]` | $x^{gl}_t$ | $\mathbb{R}_{\ge 0}$ |
| `grid_to_bess[t]` | $x^{gb}_t$ | $\mathbb{R}_{\ge 0}$ |
| `soc[t]` | $E_t$ | $\mathbb{R}_{\ge 0}$ (kWh) |
| `y_charge[t]`, `y_dis[t]` | $u^{c}_t, u^{d}_t$ | $\{0,1\}$ |
| `y_grid_io[t]` | $u^{io}_t$ | $\{0,1\}$ (this mode only) |
| `slack[t]` | $\sigma_t$ | $\mathbb{R}_{\ge 0}$ (this mode only) |
| `z_pv_active[t]` | $z_t$ | $\{0,1\}$ (only when `allow_bess_grid_charging`) |
| `r_balancing[k, t]` | $r_{k,t}$ | $\mathbb{R}_{\ge 0}$ kW (only when `balancing_enabled` and a BESS is present) |

Two derived Pyomo Expressions (not variables):
`grid_export_total[t]` $= x^{pg}_t + x^{bg}_t$ (the export metric) and
`grid_injection_total[t]` (the cap basis $g_t$ of `EXPORT_CAP`, see
below).

### Objective

Profit maximisation over the dispatch window
(`optimization.build_model`, objective `OBJ`):

$$\max \;\; \Pi = \Pi^{\mathrm{ret}} + \Pi^{\mathrm{exp}} - C^{\mathrm{chg}} - C^{\mathrm{wear}} + R^{\mathrm{bm}} - \varepsilon \sum_t x^{pc}_t \tag{S1}$$

$$\Pi^{\mathrm{ret}} = \sum_t \pi^{\mathrm{ret}}_t \left(x^{pl}_t + x^{bl}_t\right) / 1000 \tag{S2}$$

$$\Pi^{\mathrm{exp}} = \sum_t \left(p^{\mathrm{eff}}_t\, x^{pg}_t + \pi^{\mathrm{DAM}}_t\, x^{bg}_t\right) / 1000 \tag{S3}$$

$$C^{\mathrm{chg}} = \sum_t \pi^{\mathrm{DAM}}_t\, x^{gb}_t / 1000, \qquad C^{\mathrm{wear}} = c^{w} \sum_t \left(x^{bl}_t + x^{bg}_t\right)/1000 \tag{S4}$$

where $p^{\mathrm{eff}}_t = (1-s)\,\pi^{\mathrm{DAM}}_t + s\,\pi^{\mathrm{PPA}}$
is the PPA-adjusted PV export price (equal to $\pi^{\mathrm{DAM}}_t$
when no contract is active — `docs/ppa_design.md`),
$c^{w}$ = `bess_wear_cost_eur_per_mwh` (a dispatch shadow price, never
added to the reported cashflow), $R^{\mathrm{bm}}$ is the expected
balancing revenue (`docs/balancing_market_design.md`), and
$\varepsilon = 10^{-5}$ EUR/kWh is the curtailment tie-breaker
(`_WEIGHT_CURTAIL_TIEBREAK_EUR_PER_KWH`, module-private, not a
workbook knob).

> **Balancing under self-consumption — practical caveat.**
> Balancing-market participation ($R^{\mathrm{bm}}$, opt-in via
> `balancing_enabled`, off by default) is valid in **both**
> `self_consumption` and `merchant` mode; the activation gate keys on
> `balancing_enabled and bess_present` only, with **no** mode gate.
> Stacking FCR/aFRR/mFRR revenue on top of a self-consumption scheme,
> however, in practice requires routing through an **aggregator/BSP**
> and **TSO prequalification**, and not every self-consumption support
> scheme permits market cumulation. The pipeline emits **one**
> load/resolve-time warning when balancing runs under `self_consumption`
> with a BESS present; verify your scheme permits cumulation and
> consider the optional `balancing_aggregator_fee_pct_revenue`
> route-to-market cost (default 0). See `docs/balancing_market_design.md`.

## Hard constraints — formal statements

Each subsection states the constraint and its active scope.  Constraint
names match the Pyomo attribute names on the model so the conformance
test can `hasattr(model, NAME)` against them.

### PV_SPLIT(t)

$$G_t = x^{pl}_t + x^{pb}_t + x^{pg}_t + x^{pc}_t \tag{S5}$$

Active in both modes; when PV is absent the four PV-side flows are
independently pinned to zero.

### LOAD_BAL(t)

$$L_t = x^{pl}_t + x^{bl}_t + x^{gl}_t \tag{S6}$$

**Active in `self_consumption` only**; `merchant` omits the load
balance and pins the three load-coverage flows to zero.

### LOAD_PV_PRIORITY(t)

$$x^{pl}_t \ge \mathrm{floor}_t, \qquad
\mathrm{floor}_t = \begin{cases}
\min(G_t, L_t) & \text{default} \\
\min(G_t, L_t, \mathrm{cap}_t, \mathrm{cap}^{pv}_t) & \texttt{grid\_cap\_includes\_load}
\end{cases} \tag{S7}$$

Combined with `PV_SPLIT` and `LOAD_BAL` this forces
$x^{pl}_t = \mathrm{floor}_t$ exactly — the Section 2 hard
load-coverage priority from the MD.  In the default mode the floor is
$\min(G_t, L_t)$: the load-serving flow sits behind the meter and never
crosses the capped connection point.  Under the strict total-injection
cap that flow is itself injected, so the floor is additionally bounded
by the per-step cap $\mathrm{cap}_t = P^{G} \Delta t\, \mu_t$ and, when
supplied, the PV sub-cap $\mathrm{cap}^{pv}_t$.  Load priority stays
exact and absolute over surplus export but is bounded by the injection
the cap physically admits; the uncovered remainder is served by
$x^{gl}_t$ at retail.  Active in `self_consumption` only.

### LOAD_PRIORITY_SLACK_DEF(t)

$$\sigma_t \ge G_t + x^{bl}_t + x^{bg}_t - L_t \tag{S8}$$

Active in `self_consumption` only.  The slack underpins the
surplus-only export rule of Section 5 of the MD.

### LOAD_PRIORITY_EXPORT(t)

$$x^{pg}_t + x^{bg}_t \le \sigma_t \tag{S9}$$

After substituting `PV_SPLIT` and `LOAD_BAL`, the inequality reduces to
$x^{gl}_t \le x^{pb}_t + x^{pc}_t$: a step can only export when its
load is fully covered without grid import.  Active in
`self_consumption` only.

### SOC_DYN(t)

$$E_{t+1} = E_t + \eta_c \left(x^{pb}_t + x^{gb}_t\right)
          - \frac{x^{bl}_t + x^{bg}_t}{\eta_d}
          + \delta^{c}_t - \delta^{d}_t \tag{S10}$$

for every $t < N-1$; the terminal step is closed by `SOC_TERM` /
`SOC_TERM_MIN` / `SOC_TERM_MAX`.  The optional expected-activation
drift terms $\delta^{c}_t, \delta^{d}_t$ are nonzero only when
`balancing_enabled` (Eq. (B6) in `docs/balancing_market_design.md`).
`SOC_MIN` / `SOC_MAX` bound $E_t$ inside
$[\underline{e}\,E^{\mathrm{cap}},\ \overline{e}\,E^{\mathrm{cap}}]$.

### SOC_INIT, SOC_TERM / SOC_TERM_MIN / SOC_TERM_MAX

$$E_0 = \texttt{initial\_soc\_frac} \cdot E^{\mathrm{cap}} \tag{S11}$$

$$\texttt{terminal\_soc\_equal} = \mathrm{TRUE}:\quad E^{\mathrm{post}}_{N-1} = E_0 \tag{S12}$$

$$\texttt{terminal\_soc\_equal} = \mathrm{FALSE}:\quad
\underline{e}\,E^{\mathrm{cap}} \le E^{\mathrm{post}}_{N-1} \le \overline{e}\,E^{\mathrm{cap}} \tag{S13}$$

$E^{\mathrm{post}}_{N-1}$ is $E_{N-1}$ plus the same per-step
charge/discharge (and optional balancing drift) used in `SOC_DYN`.  The
rolling-horizon dispatcher always passes `terminal_soc_free=True`, so a
single window never receives the closed-cycle constraint; windows that
reach the end of the horizon are pinned back to the year-initial SOC
(`docs/uncertainty_design.md`).

### CH_LIM(t), DIS_LIM(t), MODE_LINK(t)

$$x^{pb}_t + x^{gb}_t \le P^{B} \Delta t\, u^{c}_t, \qquad
x^{bl}_t + x^{bg}_t \le P^{B} \Delta t\, u^{d}_t, \qquad
u^{c}_t + u^{d}_t \le 1 \tag{S14}$$

The charge/discharge limit is the symmetric `bess_power_kw` — an
asymmetric pair is not supported.  `MODE_LINK` is the Section 4
simultaneity rule from the MD.

### EXPORT_CAP(t)

$$g_t \le P^{G} \Delta t\, \mu_t \tag{S15}$$

Active in **both** modes — the regulatory grid-connection limit from
MD YPEN/DAPEEK/53563/1556/2023; merchant mode does not skip it.  The
cap basis $g_t$ (`grid_injection_total`) is selected by
`grid_cap_includes_load`:

* **Default** (`FALSE`) — surplus export only:
  $g_t = x^{pg}_t + x^{bg}_t$.  Historical behaviour, bit-for-bit
  backward compatible.
* **Strict** (`TRUE`, `self_consumption` only) — total plant injection
  at the connection point:
  $g_t = x^{pl}_t + x^{bl}_t + x^{pg}_t + x^{bg}_t$.  Under Virtual
  Net-Billing the energy virtually allocated to the remote load is
  physically injected at the plant, so the cap models a physical
  plant-injection limit.  Load priority shares the cap
  (Eq. (S7) floor); when the cap cannot fit the full load the
  uncovered remainder is met by $x^{gl}_t$ at retail while surplus PV
  is curtailed / stored — the run degrades to maximum feasible
  coverage, never infeasibility.  Merchant mode has no co-located
  load, so the basis collapses to surplus export and the flag is a
  no-op (a once-per-process warning records this).

**Optional per-source sub-caps** (attached only when the corresponding
sheet is supplied; both modes):

$$\mathrm{EXPORT\_CAP\_PV}:\ g^{pv}_t \le P^{G} \Delta t\, \mu^{pv}_t,
\qquad
\mathrm{EXPORT\_CAP\_BESS}:\ g^{b}_t \le P^{G} \Delta t\, \mu^{b}_t \tag{S16}$$

$g^{pv}_t$ / $g^{b}_t$ mirror the combined basis split by origin
($x^{pl}_t + x^{pg}_t$ / $x^{bl}_t + x^{bg}_t$ under the strict cap;
$x^{pg}_t$ / $x^{bg}_t$ otherwise).  The combined `EXPORT_CAP` still
binds, so PV and BESS injection together never exceed the connection
nameplate.

### NO_SIM_GRID_IMPORT(t), NO_SIM_GRID_EXPORT(t)

$$x^{gl}_t + x^{gb}_t \le M_{\mathrm{imp}}\, u^{io}_t, \qquad
x^{pg}_t + x^{bg}_t \le M_{\mathrm{exp}} \left(1 - u^{io}_t\right) \tag{S17}$$

Active in `self_consumption` only — the audit verified that
simultaneous import/export does not occur in `merchant` dispatch, so
the big-M overhead is omitted there.  Tight big-Ms
(`derive_tight_big_m`):

$$M_{\mathrm{imp}} = \left(\max_t L_t + P^{B} \Delta t\right) \cdot 1.001, \qquad
M_{\mathrm{exp}} = P^{G} \Delta t \cdot \max_t \mu_t \cdot 1.001 \tag{S18}$$

### CYC

$$\sum_{t \in \mathrm{day}\ d} \left(x^{bl}_t + x^{bg}_t\right)
\le \texttt{max\_cycles\_per\_day} \cdot E^{\mathrm{cap}}
\qquad \forall d \tag{S19}$$

One `ConstraintList` entry per calendar day in the dispatch window.

### GRID_CHARGE_GATE, GRID_CHG_PV_GATE

$$x^{gb}_t \le M_{\mathrm{ch}} \left(1 - z_t\right), \qquad
G_t \le M_{\mathrm{pv}}\, z_t \tag{S20}$$

with $M_{\mathrm{ch}} = P^{B} \Delta t \cdot 1.001$ and
$M_{\mathrm{pv}} = \max_t G_t \cdot 1.001$.  Declared only when
`allow_bess_grid_charging = TRUE` and a BESS is present: the BESS may
charge from the grid only in steps where PV is effectively zero
(Section 6 gating rule).  When disabled, $x^{gb}_t = 0$ is pinned.

## Nine audit invariants

After every solve `optimization.verify_dispatch_invariants` returns the
residual dictionary below; tolerance
`ENERGY_TOLERANCE` $= 10^{-3}$ kWh (`pvbess_opt.kpis`).  The `--strict`
CLI flag turns violations into errors.

### invariant_1_pv_balance_kwh

$$\max_t \left|G_t - x^{pl}_t - x^{pb}_t - x^{pg}_t - x^{pc}_t\right| \tag{S21}$$

Both modes.

### invariant_2_load_balance_kwh

$$\max_t \left|L_t - x^{pl}_t - x^{bl}_t - x^{gl}_t\right| \tag{S22}$$

`self_consumption` only; identically zero in `merchant`.

### invariant_3_soc_dynamics_kwh

$$\max_t \left|\left(E_{t+1} - E_t\right) - \left(\eta_c (x^{pb}_t + x^{gb}_t) - \frac{x^{bl}_t + x^{bg}_t}{\eta_d} + \delta_t\right)\right| \tag{S23}$$

$\delta_t$ is the per-step expected-activation drift from
`kpis._balancing_soc_drift` (zero when balancing is off).

### invariant_4_rte_bound_excess_kwh

$$\textstyle\sum \mathrm{discharge} \;\le\; \eta_c \eta_d \sum \mathrm{charge} + \eta_d \left(E_0 - E^{\mathrm{post}}_{N-1}\right) + \eta_d\, \delta^{\Sigma} \tag{S24}$$

with $\delta^{\Sigma}$ the summed drift; the invariant reports the
bound's positive excess.

### invariant_5_no_sim_grid_io_max_product_kwh2

$$\max_t \left(x^{gl}_t + x^{gb}_t\right) \cdot \left(x^{pg}_t + x^{bg}_t\right) \tag{S25}$$

`self_consumption` only.

### invariant_6_load_priority_violations

$$\#\left\{t : \left(x^{pg}_t + x^{bg}_t > \tau\right) \wedge \left(x^{gl}_t > \tau\right)\right\} \tag{S26}$$

A count, not a residual.  `self_consumption` only.

### invariant_7_curtail_behavior_kwh

$$\#\left\{t : \left(\mathrm{cap}_t - g_t > \tau\right) \wedge \left(x^{pc}_t > \tau\right) \wedge \left(\pi^{\mathrm{DAM}}_t > 0\right)\right\} \tag{S27}$$

The cap-not-binding ⇒ curtail-zero rule, both modes.  When a PV
sub-cap is supplied the headroom test additionally requires the PV
sub-cap to have room.  The $\pi^{\mathrm{DAM}}_t > 0$ gate is
mandatory: curtailing surplus PV is profit-maximising when the export
price is non-positive, so curtailment with cap headroom is anomalous
only when exporting would have been profitable.

### invariant_8_soc_closed_cycle_kwh

$$\left|E^{\mathrm{post}}_{N-1} - E_0\right| \qquad [\texttt{terminal\_soc\_equal} = \mathrm{TRUE}] \tag{S28}$$

### invariant_9_pv_load_priority_kwh

$$\max_t \left|x^{pl}_t - \mathrm{floor}_t\right| \tag{S29}$$

`self_consumption` only; $\mathrm{floor}_t$ as in Eq. (S7).

## Settlement & cashflow equations

Per-step EUR columns are written by `kpis.add_economic_columns` (always
run `compute_kpis` before the financial pipeline — ordering contract in
`pvbess_opt/conventions.md`):

$$\mathrm{savings\_self\_consumption\_eur}_t = \pi^{\mathrm{ret}}_t \left(x^{pl}_t + x^{bl}_t\right)/1000 \tag{S30}$$

$$\mathrm{profit\_export\_from\_pv\_eur}_t = \pi^{\mathrm{DAM}}_t\, x^{pg}_t / 1000 \quad (\text{PPA-covered share carved out per } \texttt{docs/ppa\_design.md}) \tag{S31}$$

$$\mathrm{profit\_export\_from\_bess\_eur}_t = \pi^{\mathrm{DAM}}_t\, x^{bg}_t / 1000, \qquad
\mathrm{expense\_charge\_bess\_grid\_eur}_t = \pi^{\mathrm{DAM}}_t\, x^{gb}_t / 1000 \tag{S32}$$

The avoided-cost stream feeds the canonical aggregate
`revenue_self_consumption_eur`; exports feed `revenue_pv_dam_eur` /
`revenue_bess_dam_eur` (grid-charging expense is bundled into the
BESS-DAM stream by convention).  Escalation, degradation scaling, the
aggregator fee, and discounting are defined once in
`docs/economics_design.md`.

## KPI definitions

Dispatch and coverage KPIs from `kpis.compute_kpis` (fractions rounded
to 4 dp):

$$\mathrm{pv\_direct\_self\_consumption\_frac} = \frac{\sum_t x^{pl}_t}{\sum_t G_t}, \qquad
\mathrm{system\_pv\_self\_consumption\_frac} = \frac{\sum_t x^{pl}_t + \sum_t \mathrm{green\ BESS{\to}load}}{\sum_t G_t} \tag{S33}$$

$$\mathrm{load\_coverage\_from\_pv\_frac} = \frac{\sum_t x^{pl}_t}{\sum_t L_t}, \qquad
\mathrm{system\_load\_green\_coverage\_frac} = \frac{\sum_t x^{pl}_t + \sum_t \mathrm{green\ BESS{\to}load}}{\sum_t L_t} \tag{S34}$$

plus `bess_from_pv_self_consumption_frac`,
`load_coverage_from_bess_frac`, `load_coverage_from_bess_total_frac`
(the green BESS→load attribution comes from
`kpis.attribute_green_discharge`, which splits each discharge by the
PV share of the energy charged into the battery), SOC statistics
(`soc_min_pct` / `soc_max_pct` / `soc_avg_pct`), energy totals, and the
nine invariant keys above.  The headline profit KPI is
`profit_total_eur`; the nine canonical revenue aggregates are defined
in `docs/economics_design.md`.

## Implementation map

| Equation | Implementing symbol |
|---|---|
| (S1)–(S4) objective | `optimization.build_model` → `OBJ` |
| (S5) | `optimization.build_model` → `PV_SPLIT` |
| (S6) | `LOAD_BAL` |
| (S7) | `LOAD_PV_PRIORITY` |
| (S8) | `LOAD_PRIORITY_SLACK_DEF` |
| (S9) | `LOAD_PRIORITY_EXPORT` |
| (S10) | `SOC_DYN` (+ `SOC_MIN`, `SOC_MAX`) |
| (S11)–(S13) | `SOC_INIT`, `SOC_TERM`, `SOC_TERM_MIN`, `SOC_TERM_MAX` |
| (S14) | `CH_LIM`, `DIS_LIM`, `MODE_LINK` |
| (S15) | `EXPORT_CAP` over `grid_injection_total` |
| (S16) | `EXPORT_CAP_PV`, `EXPORT_CAP_BESS` |
| (S17)–(S18) | `NO_SIM_GRID_IMPORT`, `NO_SIM_GRID_EXPORT`; `derive_tight_big_m` |
| (S19) | `CYC` |
| (S20) | `GRID_CHARGE_GATE`, `GRID_CHG_PV_GATE` |
| (S21)–(S29) | `optimization.verify_dispatch_invariants` |
| (S30)–(S32) | `kpis.add_economic_columns` |
| (S33)–(S34) | `kpis.compute_kpis`, `kpis.attribute_green_discharge` |

## Validation & tests

* `tests/test_logic_spec_conformance.py` — parses the constraint H3s
  under "Hard constraints — formal statements" (≥ 12 names) and the
  invariant H3s under "Nine audit invariants" (exactly 9) out of this
  file; asserts every constraint is attached to a freshly built
  `self_consumption` model and every invariant is reported within
  `ENERGY_TOLERANCE` after a real solve.
* `tests/test_optimization.py`, `tests/test_dispatch_analytic.py` —
  constraint-level behaviour against hand-computed dispatches.
* `tests/test_dispatch_invariant_hardening.py`,
  `tests/test_realscale_all_combos.py` — the nine invariants across
  all mode × asset combinations (1-day fast lane; full-year slow lane).
* `tests/test_logic_spec_conformance.py::test_balancing_verification_symbols_present`
  — balancing symbols on a balancing-enabled model (appendix of
  `docs/balancing_market_design.md`).
* `tests/test_max_injection_default_is_no_curtailment.py`,
  `tests/test_max_injection_profile.py` — Eq. (S15) default and
  profile semantics.
* `tests/test_mode_switch_matrix.py` — mode carve-outs of
  "Assumptions & limitations" below.

## Worked example

One hour ($\Delta t = 1$), $G = 100$ kWh, $L = 60$ kWh,
$\pi^{\mathrm{ret}} = 120$, $\pi^{\mathrm{DAM}} = 50$, no BESS, cap
$P^{G}\Delta t\,\mu = 30$ kWh.  Eq. (S7) pins $x^{pl} = 60$.  The
surplus 40 kWh meets the cap: $x^{pg} \le 30$ (Eq. S15), so
$x^{pg} = 30$, $x^{pc} = 10$ (Eq. S5; export is profitable at
$\pi^{\mathrm{DAM}} > 0$, and the tie-breaker drives curtailment to the
minimum).  Objective (S1):
$\Pi = 120 \cdot 60/1000 + 50 \cdot 30/1000 = 7.2 + 1.5 = 8.7$ EUR.
Invariant (S27) reports 0: the cap is binding in the curtailed step.

## Assumptions & limitations

* Single symmetric BESS power rating; no asymmetric charge/discharge
  limits.
* `LOAD_PV_PRIORITY` is regulatory, not economic: even when
  $\pi^{\mathrm{DAM}} > \pi^{\mathrm{ret}}$ the load is served first.
* The no-simultaneous-I/O rule uses one binary per step; sub-step
  netting is not modelled.
* Mode carve-outs (enforced by `tests/test_mode_switch_matrix.py`):
  `merchant` drops `LOAD_BAL`, `LOAD_PV_PRIORITY`,
  `LOAD_PRIORITY_SLACK_DEF/EXPORT`, `NO_SIM_GRID_IMPORT/EXPORT`
  (+ `y_grid_io`, `slack`), the avoided-cost term, and invariants
  2, 5, 6, 9; it adds the three load-flow pinning constraints
  (`docs/merchant_design.md`).
* Mode resolution is centralised in `modes.resolve_mode`; the only
  valid values are `self_consumption` and `merchant`.
* Degradation, multi-year scaling, and fees live outside the MILP
  (`docs/economics_design.md`); the dispatch is a Year-1 problem.

## References

* MD YPEN/DAPEEK/93976/2772/2024 (self-consumption regime).
* MD YPEN/DAPEEK/53563/1556/2023 (grid-connection injection cap).
* `docs/README.md` (shared notation), `docs/merchant_design.md`,
  `docs/balancing_market_design.md`, `docs/ppa_design.md`,
  `docs/economics_design.md`, `docs/uncertainty_design.md`.
* `pvbess_opt/conventions.md` (cross-module ordering and scope
  contracts).
