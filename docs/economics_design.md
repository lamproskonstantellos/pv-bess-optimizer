# Project-finance engine: design

Domain design document for the multi-year economics layer: year
conventions, escalation and discounting, the nine canonical revenue
aggregates, the aggregator fee, degradation scaling, replacement,
debt, the headline financial KPIs, and LCOE/LCOS.  Notation follows
the shared table in `docs/README.md`.

## Equation tag registry

Every numbered equation across the design documents draws its tag from
one global registry so tags never collide (the availability import
correction had to be renumbered E8a after a duplicate-E9 incident —
allocate here first, then write the equation).  One counter per
namespace; a tag, once merged, is never reused or renumbered.

| Namespace | Owner document | Scope | Highest allocated |
|---|---|---|---|
| E | `economics_design.md` | cashflow, fees, KPIs, LCOE/LCOS | E59 (+ suffixed E8a, E13a-E13d, E40a) |
| U | `uncertainty_design.md` | forecast noise, Monte Carlo, foresight | U12 (+ suffixed U8a) |
| P | `ppa_design.md` | PPA settlement and dispatch coupling | P11 |
| S | `self_consumption_design.md` | system/dispatch constraints (shared with merchant mode) | S36 |
| B | `balancing_market_design.md` | balancing product structure | B10 |
| I | `intraday_design.md` | intraday venue (two-stage re-dispatch) | I5 |

New equations take the next free tag in their namespace at merge time
and add a row to the owning document's implementation map.

## Purpose & scope

The MILP (see `docs/self_consumption_design.md` /
`docs/merchant_design.md`) solves a single Year-1 dispatch.  The
economics layer projects that Year-1 result over the project lifecycle
**analytically**: per-stream revenue bases are scaled by degradation
factors and inflation indices rather than re-solving N MILPs (the PV
profile, prices, and load repeat by assumption, so a re-solve would
reproduce the same dispatch shape scaled by capacity).  Scope:

* yearly cashflow (`economics.build_yearly_cashflow`),
* monthly / quarterly views (`economics.derive_monthly_cashflow`),
* financial KPIs (`economics.compute_financial_kpis`),
* debt layer (`economics.build_debt_schedule`),
* the per-step EUR columns and canonical revenue aggregates that feed
  it (`kpis.add_economic_columns`,
  `kpis._compute_canonical_revenue_aggregates`),
* degradation factors (`lifetime._pv_factor`, `lifetime._bess_factor`)
  and the availability derate (`availability.apply_unavailability_derate`).

## Inputs

| Sheet | Key | Default | Symbol / role |
|---|---|---|---|
| economics | `discount_rate_pct` | 7.0 | $\rho$ |
| economics | `opex_inflation_pct` | 1.0 | $i_{\mathrm{opex}}$ |
| economics | `retail_inflation_pct` | 0.0 | $i_{\mathrm{ret}}$ |
| economics | `dam_inflation_pct` | 0.0 | $i_{\mathrm{DAM}}$ (held nominal by default; DAM forecasts already embed a price view) |
| economics | `aggregator_fee_pct_revenue` | 0.0 | $\varphi$ (energy-aggregator fee on DAM + retail only; opt-in) |
| economics | `route_to_market_fee_eur_per_mwh` | 0.0 | $\phi_{\mathrm{rtm}}$ (representation fee per exported MWh; opt-in) |
| economics | `optimizer_revenue_share_pct` | 0.0 | $\varphi_{\mathrm{opt}}$ (BESS optimizer share of the positive trading margin; opt-in) |
| economics | `balancing_aggregator_fee_pct_revenue` | 0.0 | $\varphi_{\mathrm{bm}}$ (optional BSP / route-to-market fee on gross balancing revenue; default off) |
| economics | `bess_toll_eur_per_mw_year` | 0.0 | $\tau$ (tolling rate per MW of BESS power; default off) |
| economics | `bess_toll_year_from`, `bess_toll_year_to` | 1, 0 | toll phase window (E25; 0 = end of life) |
| economics | `bess_toll_merchant_treatment` | zeroed | E29a merchant gating (`zeroed` \| `retained`) |
| economics | `bess_toll_indexation_pct` | 0.0 | $i_\tau$ (contractual toll escalation) |
| economics | `optimizer_floor_enabled` | FALSE | E30 floor+share switch (FALSE = plain E13d share) |
| economics | `optimizer_floor_eur_per_kw_year` | 0.0 | $F$ (guaranteed floor per kW of BESS power) |
| economics | `optimizer_term_year_from`, `optimizer_term_year_to` | 1, 0 | optimizer term window (E25; default whole life) |
| economics | `optimizer_margin_basis` | dam | E30a margin base (`dam` \| `dam_plus_balancing`) |
| economics | `state_support_eur_per_mw_year` | 0.0 | $\sigma$ (fixed support per MW; RRF-style, Tameio Anakampsis / TAA reference) |
| economics | `state_support_year_from`, `state_support_year_to` | 1, 0 | support window (E25; 0 = end of life) |
| economics | `state_support_clawback_threshold_eur_per_mw_year` | 0.0 | $\theta$ (two-way netting reference level per MW) |
| economics | `state_support_clawback_share_pct` | 0.0 | $c$ (share of the difference netted, both directions) |
| economics | `state_support_indexation_pct` | 0.0 | $i_s$ (escalates support AND threshold) |
| economics | `capacity_market_eur_per_mw_year` | 0.0 | $\kappa$ (capacity payment per derated MW) |
| economics | `capacity_market_derating_pct` | 100.0 | $\delta$ (duration-based derating class factor) |
| economics | `capacity_market_year_from`, `capacity_market_year_to` | 1, 0 | capacity-contract window (E25) |
| economics | `capacity_market_indexation_pct` | 0.0 | $i_{cm}$ (clearing-price escalation) |
| economics | `revenue_levy_pct` | 0.0 | $\lambda$ (levy on gross market turnover, E33) |
| economics | `corporate_tax_rate_pct` | 0.0 | $\tau$ (income tax on E36 taxable income; 0 = pre-tax only) |
| economics | `depreciation_years_pv`, `depreciation_years_bess`, `depreciation_years_site` | 20, 10, 20 | $N_a$ (straight-line lives per asset class; inert at $\tau = 0$) |
| economics | `tax_loss_carryforward_years` | 0 | $W$ (FIFO loss expiry window; 0 = unlimited) |
| economics | `benchmark_lco{e,s}_{low,high}_eur_per_mwh` | 30/85, 157/274 | Lazard band overlays (plots only) |
| economics | `sensitivity_*` (6 delta keys) | 10/10/10/2/10/5 | tornado deltas: CAPEX/OPEX/revenue %, discount-rate pp, PPA-price %, tax-rate pp (`docs/uncertainty_design.md`) |
| economics | `gearing_pct`, `debt_interest_rate_pct`, `debt_tenor_years`, `debt_repayment` | 0, 5.0, 15, annuity | debt layer |
| economics | `grid_co2_intensity_kg_per_mwh`, `grid_co2_annual_decline_pct` | 0, 0 | emissions / 24-7 CFE (`pvbess_opt.emissions`) |
| project | `project_lifecycle_years` | 20 | $Y$ |
| project | `project_start_year` | 2026 | calendar anchor |
| project | `site_capex_eur`, `site_devex_eur` | 0, 0 | site-wide lump sums |
| project | `unavailability_pct` | 1.0 | $a$ |
| pv | `capex_pv_eur_per_kw`, `devex_pv_eur_per_kw`, `opex_pv_eur_per_kwp` | 525, 60, 7 | PV cost block |
| pv | `pv_degradation_year1_pct`, `pv_degradation_annual_pct` | 2.5, 0.55 | $d_1$, $d_a$ |
| bess | `capex_bess_eur_per_kwh`, `devex_bess_eur_per_kw`, `opex_bess_eur_per_kw` | 250, 30, 14 | BESS cost block (CAPEX per kWh of energy capacity; DEVEX / OPEX per kW of the power block) |
| bess | `bess_replacement_year`, `bess_replacement_cost_pct` | 0, 50 | replacement event: N = scheduled year, blank/`auto` = first year SOH reaches `bess_eol_soh_pct` (charged), 0 = never |
| bess | `bess_degradation_annual_pct`, `bess_degradation_pct_per_cycle` | 2.0, 0.008 | $d_B$, $d_c$ |
| bess | `bess_eol_soh_pct` | 80 | EOL SOH threshold driving the `auto` replacement |
| balancing | `bm_inflation_pct` | 2.0 | $i_{\mathrm{bm}}$ (balancing sheet key; tracks CPI by default) |
| ppa | `ppa_inflation_pct`, `ppa_term_years`, `ppa_settlement` | 0, 10, physical | PPA stream wiring (`docs/ppa_design.md`) |

Capacities $\mathrm{kWp}$, $P^{B}$, $E^{\mathrm{cap}}$ come from
`economics.derive_asset_capacities`.

## Mathematical formulation

### Year convention and discounting

Project year $y = 0$ is the construction year, calendar
`project_start_year` − 1; it carries the full CAPEX and DEVEX and no
revenue or OPEX.  Operating years $y = 1..Y$ map to calendar
`project_start_year` + $y$ − 1:

$$\mathrm{CAPEX}_0 = -\left(c^{PV} \cdot \mathrm{kWp} + c^{B} \cdot E^{\mathrm{cap}} + \mathrm{site\_capex}\right), \quad
\mathrm{DEVEX}_0 = -\left(v^{PV} \cdot \mathrm{kWp} + v^{B} \cdot P^{B} + \mathrm{site\_devex}\right) \tag{E1}$$

Escalation of a Year-1 base $X_1$ on index $i$ uses the
**(1+i)^(y−1)** convention (Year 1 = the nominal base):

$$X_y = X_1 \,(1+i)^{\,y-1} \tag{E2}$$

End-of-year discounting at rate $\rho$; the Year-0 row carries
$1/(1+\rho)^0 = 1$:

$$D_y = \frac{1}{(1+\rho)^{y}} \tag{E3}$$

Monthly flows land end-of-month at $t = (y-1) + m/12$ years, so
December of year $y$ carries exactly the yearly factor (E3):

$$D_{y,m} = \frac{1}{(1+\rho)^{\,(y-1)+m/12}} \tag{E4}$$

Investment events inside an operating year (the BESS replacement) are
booked in **month 12**, so the monthly and yearly DCFs agree on the
event by construction.

### Replacement semantics

`bess_replacement_year` resolves to ONE effective replacement year
before the finance layer runs
(`pvbess_opt.lifetime.resolve_bess_replacement_year`), and every
consumer (the yearly cashflow (E14), the monthly month-12 booking, the
LCOS numerator, the lifetime projection reset and the degradation
report) reads that single resolved value:

* **N (positive integer)**: scheduled replacement in project year N;
  `bess_eol_soh_pct` is ignored completely.
* **blank / `auto`**: automatic replacement in the first project year
  the analytic SOH curve falls to `bess_eol_soh_pct`.  This is a real
  replacement: the CAPEX is charged in the cashflow, the fade
  accumulator and the lifetime dispatch projection reset, and the
  degradation report shows the swap in the same year.  If the curve
  never reaches the threshold within the lifecycle, no replacement
  happens.
* **0**: never replace; the SOH report shows the fade continuing below
  the threshold without a swap.

Only the FIRST threshold crossing is charged.  If the fresh pack would
cross the threshold again within the remaining horizon the run log
carries a prominent warning and `SUMMARY.md` notes it, but the model
does not charge a second replacement.  Projects whose battery wears
through two packs need an explicit scheduled strategy — or the staged
augmentation surface below, which supersedes the single replacement.

### Augmentation and overbuild (Eqs. E50-E52)

Staged capacity additions and a day-1 DC overbuild generalise the
single replacement into a **pooled capacity engine**
(`lifetime.bess_capacity_factors_pooled`).  The plant is a set of
installed pools; pool $i$ of size $E_i$ installed in project year
$a_i$ fades on its own calendar-plus-cycle curve, and the plant
factor is the nameplate-clamped pool sum:

$$f_y = \min\!\left(1,\; \frac{\sum_i E_i\, \varphi_i(y)}{E_N}\right),
\qquad
\varphi_i(y) = \max\!\left(0,\, (1-d_{\mathrm{cal}})^{\,y-a_i}
- d_{\mathrm{cyc}} K_i(y)\right) \tag{E50}$$

where $K_i$ accumulates each pool's **pro-rata** share of the plant
throughput (apportioned by surviving pool capacity, cycled over the
pool's nameplate size — a documented modelling choice; FIFO
apportionment would differ only when fade rates diverge, and the two
coincide at equal rates).  With no events and no overbuild the engine
delegates to the single-pool `bess_capacity_factors` (bit-identity,
including the replacement reset).

**Augmentation events (`bess_augmentation_years`, CSV).**  Each event
year $a$ installs a fresh pool: `top_up` mode restores the plant to
exactly nameplate ($\Delta E_a = \max(0, E_N - \sum_i E_i
\varphi_i(a))$), `fixed_kwh` adds `bess_augmentation_kwh`.  The event
is priced on the declining unit-cost curve and books as its own
signed cashflow column:

$$X_a = -\Delta E_a \cdot c_{\mathrm{bess}}
\cdot (1 - d_{\mathrm{cost}})^{a} \tag{E51}$$

(`augmentation_capex_eur`; month-12 booking in the monthly frame —
the replacement-CAPEX convention; a matching depreciation tranche
enters service the year after).  The lifetime total is
`total_augmentation_capex_eur_lifecycle` (SUMMARY-optional), the
events join the **LCOS numerator** (storage cost, same class as
replacement CAPEX; market fees stay excluded), the CAPEX sensitivity
driver scales the column (unit-cost-linked) while the Revenue driver
leaves it fixed, and the cashflow figures draw an "Augmentation
CAPEX" bar only when non-zero.  Augmentation is **mutually exclusive
with `bess_replacement_year`** (scheduled or `auto`): the loader
rejects the combination rather than applying a silent precedence.

**Day-1 DC overbuild (`bess_overbuild_pct`).**  Installs
$(1 + ob)\,E_N$ at Year-0 prices with usable capacity clamped at
nameplate, so fade consumes the overbuilt margin first:

$$\mathrm{capex}^{\mathrm{BESS}}_0 = -c_{\mathrm{bess}}
(1 + ob)\, E_N \tag{E52}$$

Dispatch always solves at nameplate — the overbuild changes only the
factor curve (through the E50 clamp), Year-0 CAPEX, the depreciation
base and the LCOS numerator.  Zero defaults are bit-identical for
both surfaces.  Note for sizing sweeps: `sizing.py` re-runs the
Year-1 dispatch only, so augmentation affects the frontier ranking
solely through the NPV of the factor curve and event CAPEX (no code
interaction; recorded here).

### Wear cost vs replacement cost (no double counting)

Battery degradation enters the model through two strictly separated
channels:

* `bess_wear_cost_eur_per_mwh` is a **dispatch shadow price**: it is
  subtracted in the MILP objective only ($C^{\mathrm{wear}}$ in the
  mode specs), so the optimizer skips marginal cycles whose spread does
  not beat the wear cost. It never appears in `profit_total_eur`, the
  cashflow, NPV, IRR, LCOE or LCOS. The wear term penalises DAM and
  self-consumption discharge only; expected balancing-activation
  throughput carries **no wear penalty** by design (a modelling
  decision: activation energy is probabilistic and its expected volume
  is small next to scheduled discharge, so a wear charge on it would
  double-damp balancing participation that the SOC-headroom and
  power-budget constraints already limit).
* `bess_replacement_cost_pct` is a **cash cost**: the replacement CAPEX
  (E14) books exactly once in the effective replacement year of the
  cashflow (month 12 in the monthly frame) and additionally enters the
  LCOS numerator as a reporting metric.

The invariant "wear in the objective only, replacement in the cashflow
only" is locked by `tests/test_wear_cost_objective.py` (solver-free)
and the end-to-end guard in `tests/test_degradation.py`.

### Degradation factors

PV production factor (LID year 1, linear-in-log thereafter;
`lifetime._pv_factor`):

$$f^{PV}_y = \begin{cases}
1 & y \le 1 \\
(1-d_1)\,(1-d_a)^{\,y-2} & y \ge 2
\end{cases} \tag{E5}$$

BESS capacity factor: multiplicative calendar fade minus a linear
cycle-fade term, floored at zero, with a replacement reset
(`lifetime._bess_factor`):

$$f^{B}_y = \max\!\Big(0,\; (1-d_B)^{\,y - y_0(y)} - d_c\, K_{y} \Big), \qquad
y_0(y) = \begin{cases} y_r & y \ge y_r > 0 \\ 1 & \text{else} \end{cases} \tag{E6}$$

where $y_r$ = `bess_replacement_year` and $K_y$ is the cumulative
full-equivalent cycle count **through year $y-1$** (a one-year lag:
year $y$'s fade reflects cycling already endured).  Cycles accumulate
as degraded Year-1 discharge over nameplate energy, and reset at the
replacement year:

$$K_y = \sum_{j=\,y_0(y)}^{\,y-1} \frac{\mathrm{discharge\ MWh}_1 \cdot f^{B}_j}{E^{\mathrm{cap}}/1000}, \qquad K_{y_0} = 0 \tag{E7}$$

The final-year fade decomposition reported by
`compute_financial_kpis` splits $1 - f^{B}_Y$ into the calendar
component $1-(1-d_B)^{Y-y_0}$ and the cycle component $d_c K_Y$
(`bess_calendar_fade_pct_y_final`, `bess_cycle_fade_pct_y_final`,
`bess_total_fade_pct_y_final`); calendar + cycle = total whenever the
$\max(0,\cdot)$ floor is inactive.

**Annual throughput cap (Eqs. E46/E47; `max_cycles_per_year`).**
Warranties are quoted in cycles per year — sometimes on remaining
(faded) capacity — while the only dispatch cap today is daily.  With
`max_cycles_per_year` $= C_{yr} > 0$ the Year-1 MILP adds one
year-long constraint (`CYC_ANNUAL`; a plain linear sum, no big-M):

$$\sum_t \left(x^{bl}_t + x^{bg}_t\right) \le C_{yr}\, E^{\mathrm{cap}} \tag{E46}$$

Both `cycle_cap_basis` settings coincide in Year 1 (the Year-1 factor
is 1), so one constraint serves both.  The projected years need no
constraint — under the analytic scaling recipe the year-$y$ discharge
is $D_1 f^{B}_y$ against capacity $E^{\mathrm{cap}}$ (nameplate) or
$E^{\mathrm{cap}} f^{B}_y$ (faded), so the utilisation

$$\mathrm{FEC}_y = \frac{D_1 f^{B}_y}{E^{\mathrm{cap}}}
\;\; (\text{nameplate}) \qquad \text{or} \qquad
\mathrm{FEC}_y = \frac{D_1}{E^{\mathrm{cap}}}
\;\; (\text{faded}) \tag{E47}$$

is constant on the faded basis and maximal in Year 1 (or a
replacement reset year) on nameplate — the Year-1 constraint is
sufficient and `lifetime.warranty_cycle_utilisation` proves it per
year in the degradation sheet (`cycles_on_basis`,
`warranty_utilisation_pct`, written only when the cap is set); the
pipeline warns when a reset year projects above 100 %.  The cap is a
constraint, not a cost line — LCOE / LCOS classification is
unaffected.  Under rolling-horizon dispatch the annual cap applies
only to the deterministic Year-1 solve (windows cover sub-year
horizons; a warning notes this).

### Mid-life re-solve validation (Eq. E53)

The analytic lifetime-scaling recipe (the `lifetime.py` header;
reconciliation invariant within 0.1 % in
`docs/source/technical.documentation/lifetime_scaling.rst`) can be
validated against a fresh dispatch: `midlife_resolve_year` = $k$ (a
`simulation`-sheet key; 0 = off, default) re-solves the MILP with
degraded parameters — BESS energy capacity $E_N f_k$ with **power
kept at nameplate** (the fade model degrades energy, not the
converter), the PV column scaled by $\mathrm{pv\_factor}(k)$, prices
held at Year-1 levels — and reports, per lifetime-yearly KPI $X$:

$$\Delta_k(X) = \frac{X^{\mathrm{resolve}}_k - X^{\mathrm{scaled}}_k}
{\max(|X^{\mathrm{scaled}}_k|,\, \varepsilon)} \tag{E53}$$

The resolved side is aggregated and availability-derated exactly the
way the scaled side was built (`lifetime.factors_for_year` feeds the
faded parameters through the same resolution path the projection
uses), so the delta isolates degradation NONLINEARITY: SOC-headroom
effects, cycle-cap interaction and binary commitment — the
mechanisms a linear per-year scaling cannot see.  The table
(results-workbook sheet `midlife_resolve` + a `SUMMARY.md` section,
both absent when off) carries the requested MIP gap as its own row:
deltas at or below the gap are solver noise, not scaling bias.
Strictly diagnostic — the re-solve runs AFTER the financial bundle
and never feeds the cashflow, NPV or any KPI; under a
rolling-horizon run it validates the deterministic path only (the
loader warns).  Terminal-SOC fractions are capacity-relative, so the
faded solve keeps the same SOC conventions by construction.

### Availability

The MILP assumes the plant is online every step.  A single post-solve
derate by the availability factor

$$A = 1 - a, \qquad a = \texttt{unavailability\_pct}/100 \tag{E8}$$

is applied **exactly once** to every revenue-bearing and energy KPI
(`availability.apply_unavailability_derate`; the derate list spans the
per-stream EUR keys, the PPA columns, the per-product and total
balancing revenues, the canonical aggregates, and `profit_total_eur`).
Both the perfect-foresight KPI path (`pipeline._run_one`) and the
rolling-horizon path apply the same derate, so foresight-gap
comparisons are derate-invariant (`docs/uncertainty_design.md`).

**Grid import is the one energy KPI that rises rather than falls.**
Generation, storage, export and revenue all scale by $A$, but the load
is fixed exogenous demand that the grid must serve in full while the
plant is offline.  `system_total_import_mwh` is therefore set to

$$\mathrm{import} = A \cdot \mathrm{import}_{\mathrm{raw}} + a \cdot L \tag{E8a}$$

with $L$ the (never-derated) annual load: the uniform $A$ step covers
the grid-charging leg (which genuinely stops during downtime), and the
$a \cdot L$ term adds the load the grid imports while the plant is out.
The derated annual energy balance then closes exactly against $L$, and
the annual energy Sankey (`plotting.emissions.plot_energy_sankey`,
passed `kpis['availability_factor']`) applies the same rule so its Load
node stays at the true demand and its ribbons conserve energy.  Because
grid import is not a monetised stream — the self-consumption savings,
which *are* derated, already carry the downtime cost — Eq. E8a leaves
every financial KPI unchanged.

### Exogenous curtailment

Two mutually exclusive input surfaces model grid-operator curtailment
(`read_workbook` raises if both are set, since a quota on top of a
signal would double-count the same lost energy).

**Expected-quota derate (Eqs. E48/E49; `curtailment_pct`).**  A flat
expected curtailment share $q = \texttt{curtailment\_pct}/100$ scales
the **export-side** KPIs only, after the availability derate:

$$X^{\mathrm{curt}} = (1 - q)\, X^{\mathrm{avail}} \tag{E48}$$

(`availability.apply_curtailment_derate`; the derated list spans the
export energies, the per-origin export profits, the DAM revenues and
the PPA contract leg — self-consumption, load and grid import are
untouched because curtailment binds the grid connection, not the
plant, and the baseload-shortfall marker keys are exempt since a
baseload PPA settles financially regardless of physical delivery).
`profit_total_eur` is recomposed from the deltas of its constituent
streams so the scope identity above survives the derate.  A share
$c = \texttt{curtailment\_compensated\_pct}/100$ of the curtailed
energy is reimbursed at the administered price
$p = \texttt{curtailment\_compensation\_price\_eur\_per\_mwh}$:

$$K^{\mathrm{curt}} = q \cdot c \cdot p \cdot E^{\mathrm{export,avail}}_1 \tag{E49}$$

which enters the cashflow as its own column
(`curtailment_compensation_eur`, revenue-classified for LCOE), fades
on the blended PV/BESS export mix, indexes on the DAM inflation rate
(administered prices track the market index, not a trajectory), and
totals into `lifetime_curtailment_compensation_eur`.  Both derates
share one entry point — `availability.apply_operating_derates`
(availability first, then curtailment) — so every KPI path composes
them identically.

**Hour-resolved signal (`curtailment_signal` timeseries column).**  A
per-step factor in $[0,1]$ multiplies the export cap *inside the
MILP* (`optimization.build_model`), so the optimizer re-dispatches
around the restriction (e.g. charging the BESS instead of spilling).
This is a physical constraint, not a KPI derate: no post-solve
scaling occurs, and audit invariant 7 sees the composed cap.  Use the
quota for lifecycle economics, the signal for operational studies.

### Guarantees-of-origin revenue (Eq. E54)

`go_price_eur_per_mwh` (economics sheet; 0 = off, default,
bit-identical) sells guarantees of origin on the **eligible renewable
injection** — the availability- and curtailment-derated PV grid
export.  BESS discharge and self-consumed energy are excluded: GOs
are issued on metered renewable injection, and the export basis is
stated explicitly because the eligibility definition is
jurisdiction-dependent.

$$R^{\mathrm{GO}}_y = p_{\mathrm{GO}} \cdot E^{\mathrm{PV,exp}}_1
\cdot f^{PV}_y \qquad (y \ge 1) \tag{E54}$$

The price is flat over the horizon (GO prices are contracted, not
CPI-indexed) while the eligible MWh fade on the PV degradation curve.
Classification: **fee-exempt** (certificates settle outside the power
market, so neither the aggregator fee nor the route-to-market fee
touches the stream) and **excluded from LCOE/LCOS** (revenue-agnostic
metrics).  Its own cashflow column (`go_revenue_eur`, monthly on the
PV production shape with exact reconciliation), a lifetime total
(`total_go_revenue_eur_lifecycle`, SUMMARY-optional), a "GO revenue"
band in the revenue stack / yearly bars / NPV waterfall drawn only
when non-zero, and Revenue-driver membership in the sensitivity
tornado (certificate prices move with the renewables market).

### Reference-period support settlement (Eqs. E55-E57)

`support_scheme` on the ppa sheet ('none' = off, default,
bit-identical) settles state support on the **eligible PV export** —
the Greek DAPEEP sliding Feed-in-Premium ('sliding_fip') or a
two-way CfD ('cfd_two_way').  The premium is a settlement OVERLAY:
dispatch still sells at the DAM (the per-step CfD philosophy), and a
plant settles under a corporate PPA **or** a support scheme, never
both (the loader rejects the combination).  Per month $m$ the
volume-weighted reference price over eligible steps is

$$P^{\mathrm{ref}}_m = \frac{\sum_{t \in m,\ \mathrm{elig}}
p^{\mathrm{DAM}}_t\, e^{\mathrm{exp}}_t}
{\sum_{t \in m,\ \mathrm{elig}} e^{\mathrm{exp}}_t} \tag{E55}$$

and the settlement premium (with $K$ =
`support_strike_eur_per_mwh`, the reference tariff — Timi Anaforas):

$$R^{\mathrm{sup}}_m = E^{\mathrm{elig}}_m \cdot
\begin{cases}
\max(K - P^{\mathrm{ref}}_m,\, 0) & \text{sliding\_fip} \\
K - P^{\mathrm{ref}}_m & \text{cfd\_two\_way}
\end{cases} \tag{E56}$$

zero after `support_term_years`.  `support_negative_hour_suspension`
removes the negative-DAM steps from BOTH sides of Eq. E55 (the EU
market-design rule adopted in the Greek scheme):

$$\mathrm{elig} = \{t : p^{\mathrm{DAM}}_t \ge 0\}
\quad\text{(else all steps)} \tag{E57}$$

reusing the strict $p < 0$ classifier the PPA suspension clause
defined (`ppa.negative_price_mask` — one definition, two consumers).
`support_ref_period = 'hourly'` degenerates Eq. E56 to the per-step
CfD algebra as a cross-check mode.  The Year-1 KPI pair
(`support_settlement_eur`, `support_eligible_export_mwh`) carries
both operating derates (availability and curtailment — the
settlement rides metered export), as does the per-month detail the
cashflow consumes.  Projection: the strike leg is FLAT (an
administered tariff), the reference leg rides `dam_inflation_pct`,
the volume rides the PV fade, and the sliding clamp re-applies per
month per year — so a rising reference can extinguish the premium
mid-life.  Real reference-price dynamics differ from the flat-index
projection (a documented limitation of the same class as the DAM
curve assumption).  Classification: no aggregator fee (settles with
the market operator) and EXCLUDED from LCOE/LCOS.  The signed
cashflow column (`support_settlement_eur`; two-way years can be
NEGATIVE and net_cf carries the sign), monthly on the Year-1
settlement shape (exact reconciliation), a lifetime SUMMARY row, a
signed figure band, and a dedicated `SupportStrike` tornado driver
(a full rebuild at the perturbed strike is exact, including the
clamp; the Revenue driver deliberately leaves the mixed column
untouched).

### Intraday venue rows (Eqs. E58/E59)

The two-stage intraday re-dispatch (`docs/intraday_design.md`,
Eqs. I1-I5) delivers a Year-1 GROSS spread margin
$R^{\mathrm{ID}}_1$ (the KPI pair `id_net_revenue_eur` +
`id_venue_fee_eur`) and a Year-1 traded volume $V^{\mathrm{ID}}_1$
(`id_traded_volume_mwh`).  Both are apportioned per origin on the
Year-1 SELL volumes (`id_sell_pv_mwh` / `id_sell_bess_mwh`; a
buy-only year books BESS-origin — buys are a storage action), each
leg fading on its own curve:

$$R^{\mathrm{ID}}_y = \left( R^{\mathrm{ID,PV}}_1 f^{PV}_y
+ R^{\mathrm{ID,B}}_1 f^{B}_y \right) (1+i_{\mathrm{id}})^{y-1}
\tag{E58}$$

$$F^{\mathrm{ID}}_y = -\varphi_{\mathrm{id}} \left(
V^{\mathrm{ID,PV}}_1 f^{PV}_y + V^{\mathrm{ID,B}}_1 f^{B}_y \right)
\tag{E59}$$

with $i_{\mathrm{id}}$ = `id_inflation_pct` on the margin and the
venue fee rate $\varphi_{\mathrm{id}}$ = `id_fee_eur_per_mwh` FLAT on
the fading volume (the route-to-market convention: per-MWh charges
are quoted flat, the charged MWh fade).  Both columns join $CF_y$
(Eq. E15 amended); the margin is $\ge 0$ by construction (the Stage-2
solve only trades profitably net of the fee and wear).  Fee
applicability (Eq. I6, normative): the energy-aggregator ad-valorem
fee (Eq. E13) does NOT charge the ID margin — intraday intermediation
is priced by the explicit venue fee, and an ad-valorem share on top
would double-charge it (the balancing/E13b precedent); the
route-to-market fee (Eq. E13c) needs no new term — its volume bases
(`pv_export_mwh` / `bess_export_mwh`) are computed from the Stage-2
frame, so ID sells raise and ID buys lower the charged export
automatically; the optimizer revenue share (Eq. E13d) DOES charge the
BESS-origin ID margin (optimizers price total trading margin) — the
base extends by $R^{\mathrm{ID,B}}_1 f^B_y (1+i_{\mathrm{id}})^{y-1}$
in both the plain-share and floor+share variants, still zero-clamped,
and the same leg joins the E25a netting base; the
balancing-aggregator fee (Eq. E13b) does not apply by definition.  In
'zeroed' toll years the BESS-origin leg is zeroed like every other
BESS merchant base (Eq. E29a).  Monthly: the margin rides its Year-1
monthly |margin| shape recomputed from the Stage-2 frame, the fee its
Year-1 monthly traded-volume shape (flat 1/12 fallbacks; shares sum
to one — exact yearly reconciliation).  Classification: EXCLUDED from
LCOE/LCOS (revenue-agnostic metrics, market fees excluded); both
lifetime totals are SUMMARY-optional rows; the availability AND
curtailment derates scale the seven `id_*` KPI keys (physical trading
stops when the plant is offline and rides the restricted connection).
Sensitivity: the margin scales with the Revenue driver (a price
spread times volume); the venue fee does not (volume-based).

### Year-1 revenue bases and the nine canonical aggregates

`kpis.add_economic_columns` writes the per-step EUR columns
(Eqs. S30-S32 in `docs/self_consumption_design.md`; PPA carve-out per
`docs/ppa_design.md`).  Summed over Year 1 and availability-derated,
they produce the **nine canonical revenue aggregates**
(`kpis._compute_canonical_revenue_aggregates` + the PPA column sum):

| Aggregate | Construction |
|---|---|
| `revenue_pv_dam_eur` | $\sum_t \pi^{\mathrm{DAM}}_t x^{pg}_t/1000$ (uncovered share only under a physical PPA) |
| `revenue_pv_ppa_eur` | PPA contract leg (the ninth aggregate; see `docs/ppa_design.md`) |
| `revenue_bess_dam_eur` | $\sum_t \pi^{\mathrm{DAM}}_t x^{bg}_t/1000 - \sum_t \pi^{\mathrm{DAM}}_t x^{gb}_t/1000$ (grid-charge expense bundled into the BESS-DAM stream, per `pvbess_opt/conventions.md`) |
| `revenue_self_consumption_eur` | $\sum_t \pi^{\mathrm{ret}}_t (x^{pl}_t + x^{bl}_t)/1000$; ≡ 0 in merchant mode |
| `revenue_bess_fcr_eur` | FCR capacity revenue (no activation payment) |
| `revenue_bess_afrr_up_eur` | aFRR-up capacity + activation revenue (`docs/balancing_market_design.md`) |
| `revenue_bess_afrr_dn_eur` | aFRR-down capacity + activation revenue |
| `revenue_bess_mfrr_up_eur` | mFRR-up capacity + activation revenue |
| `revenue_bess_mfrr_dn_eur` | mFRR-down capacity + activation revenue |

Scope identity (regression-guarded): `profit_total_eur` is the
per-step DAM + retail + PPA profit, i.e. exactly the sum of the four
non-balancing aggregates.  Balancing revenue settles per window via
expected values, never enters the per-step `profit_*` columns, and
joins the project economics through its own cashflow column
(`kpis._compute_balancing_kpis` denominator note;
`tests/test_kpi_and_dt_contracts.py`).  The Σ of all nine aggregates
therefore equals `profit_total_eur` + `bm_total_balancing_revenue_eur`.

### Yearly cashflow

`economics.build_yearly_cashflow` splits the Year-1 base per origin and
stream, then projects (operating years $y \ge 1$):

$$R^{\mathrm{ret}}_y = \left(R^{\mathrm{ret,PV}}_1 f^{PV}_y + R^{\mathrm{ret,B}}_1 f^{B}_y\right)(1+i_{\mathrm{ret}})^{y-1} \tag{E9}$$

$$R^{\mathrm{DAM}}_y = \left(R^{\mathrm{DAM,PV}}_1 f^{PV}_y + R^{\mathrm{DAM,B}}_1 f^{B}_y\right)(1+i_{\mathrm{DAM}})^{y-1} \;\left[+\; V^{\mathrm{cov}}_1 f^{PV}_y (1+i_{\mathrm{DAM}})^{y-1}\right]_{\substack{\text{physical PPA,}\\ y > T^{\mathrm{PPA}}}} \tag{E10}$$

with $R^{\mathrm{DAM,B}}_1 = $ `profit_export_from_bess_eur` −
`expense_charge_bess_grid_eur` (the bundling convention) and
$V^{\mathrm{cov}}_1$ = `ppa_covered_dam_value_eur` (the covered
volume's counterfactual DAM value, which **reverts into the DAM
stream after the contract term** under physical settlement, where the
aggregator fee then applies to it as market revenue).

Balancing (both legs on the BESS fade curve, indexed by
$i_{\mathrm{bm}}$):

$$R^{\mathrm{bm,cap}}_y = R^{\mathrm{bm,cap}}_1 f^{B}_y (1+i_{\mathrm{bm}})^{y-1}, \qquad
R^{\mathrm{bm,act}}_y = R^{\mathrm{bm,act}}_1 f^{B}_y (1+i_{\mathrm{bm}})^{y-1} \tag{E11}$$

PPA stream while under contract ($1 \le y \le T^{\mathrm{PPA}}$;
zero afterwards):

$$R^{\mathrm{PPA}}_y = \begin{cases}
S_1\, f^{PV}_y (1+i_{\mathrm{PPA}})^{y-1} & \text{physical} \\
S_1\, f^{PV}_y (1+i_{\mathrm{PPA}})^{y-1} - V^{\mathrm{cov}}_1 f^{PV}_y (1+i_{\mathrm{DAM}})^{y-1} & \text{cfd}
\end{cases} \tag{E12}$$

where $S_1$ is the Year-1 strike-leg value: `revenue_pv_ppa_eur`
under physical settlement, `revenue_pv_ppa_eur` +
`ppa_covered_dam_value_eur` under CfD (reconstructing strike × covered
from the two-way difference leg).

Baseload structure (`ppa_structure = 'baseload'`; Eqs. P9-P11 in
`docs/ppa_design.md`): the contract volume is a FIXED band, not a
PV-degrading share, so the stream drops the fade factor on **both**
legs and has **no** post-term reversion (cfd-only — nothing was
sleeved):

$$R^{\mathrm{PPA,bl}}_y = S_1 (1+i_{\mathrm{PPA}})^{y-1}
- V^{\mathrm{cov}}_1 (1+i_{\mathrm{DAM}})^{y-1}
\quad (1 \le y \le T^{\mathrm{PPA}};\; 0 \text{ otherwise}) \tag{E45}$$

The same production-decoupled classification holds everywhere the
pay-as-produced leg rides PV: the availability derate skips the two
PPA keys for baseload and the lifetime frame excludes them from the
$f^{PV}$ scaling (each with its own lock test).

The energy-aggregator fee is applied **once**, to the gross DAM +
retail revenue only, and is clamped so a negative gross never flips
the fee into a rebate; PPA revenue carries **no** fee
(bilateral-offtake settlement):

$$F_y = -\varphi \cdot \max\!\left(R^{\mathrm{ret}}_y + R^{\mathrm{DAM}}_y,\; 0\right) \tag{E13}$$

The fee is split across the retail/DAM net columns pro-rata to their
gross contribution so per-stream nets sum exactly to `revenue_eur`.

Balancing-aggregator fee: balancing revenue carries **no**
energy-aggregator fee (ancillary services settle directly with the TSO),
but it **may** carry an optional, separate route-to-market (BSP /
balancing-aggregator) fee when participation is routed through an
aggregator that keeps a share. It is a non-negative deduction on the
**gross** balancing revenue, clamped the same way, and **defaults to 0**
($\varphi_{\mathrm{bm}} = 0$) so existing results are bit-identical:

$$F^{\mathrm{bm}}_y = -\varphi_{\mathrm{bm}} \cdot \max\!\left(R^{\mathrm{bm,cap}}_y + R^{\mathrm{bm,act}}_y,\; 0\right) \tag{E13b}$$

It is escalated with the balancing revenue it deducts from (the gross is
already on the BESS fade curve indexed by $i_{\mathrm{bm}}$), surfaces as
its own signed `balancing_aggregator_fee_eur` cashflow column, and is
**excluded from LCOE/LCOS** by the same convention that excludes
balancing revenue. A realistic range is ~5-20 % for behind-the-meter /
smaller assets; 0 for utility-scale BSPs that self-dispatch.

### Per-year trajectory vectors (Eq. E24/E24a)

Every stream's flat inflation index generalises to a per-year
escalation series sourced from the optional `trajectories` input
surface (workbook sheet / YAML block, `docs/source/users.guide/inputs.rst`):

$$g^s_y = \begin{cases}
(1+i_s)^{y-1} & \text{no trajectory for stream } s\\
m^s_y & \text{mode replace}\\
(1+i_s)^{y-1}\, m^s_y & \text{mode overlay}
\end{cases} \qquad m^s_1 = 1 \tag{E24}$$

E24 substitutes the escalation factor wherever a stream index appears:
retail (E9), DAM (E10) **including** the CfD DAM leg and the post-term
PPA physical reversion (E12) and the optimizer-share base (E13d) — all
DAM-priced quantities ride the same series — and balancing capacity /
activation (E11, independently shapeable; the BSP fee E13b inherits the
shape through proportionality).  Deliberate exclusions: the PPA strike
escalates contractually via $i_{\mathrm{PPA}}$, and the route-to-market
fee (E13c) is a flat per-MWh volume charge.  Typical uses: a DAM
capture-rate decline as PV build-out compresses solar-hour prices, an
ancillary-services price decay as the balancing fleet saturates, and
stepped OPEX (post-warranty LTSA step, insurance).

The OPEX row decomposes per asset leg (Eq. E24a):

$$O_y = -\left(o_{\mathrm{PV}}\,\mathrm{kWp}\; g^{\mathrm{opex\_pv}}_y
+ o_{\mathrm{B}}\, P_{\mathrm{B}}\; g^{\mathrm{opex\_bess}}_y\right) \tag{E24a}$$

where the per-asset streams `opex_pv` / `opex_bess` (mutually exclusive
with the shared `opex` stream) default to the shared series when not
declared.  The LCOE (E21) and LCOS (E22) discounted-OPEX numerators use
the **identical** series through the same helper
(`economics._opex_escalation_series`), so the cashflow OPEX and the
metric OPEX can never diverge; OPEX trajectories therefore move
LCOE/LCOS (plant O&M is in both metrics) while revenue and balancing
trajectories never do (the metrics are revenue-agnostic).  The Year-1
anchor $m^s_1 = 1$ keeps the Year-1 cashflow equal to the dispatch-KPI
base, preserving the `profit_total_eur` reconciliation guard.

In the sensitivity tornado the Revenue driver's uniform $\pm\delta$
scaling commutes with per-year multipliers (it perturbs the price
LEVEL on top of the trajectory SHAPE), so trajectory-shaped revenue
columns and `optimizer_fee_eur` scale with the driver while
`route_to_market_fee_eur` remains volume-based and untouched.

Price decks (`docs/source/users.guide/inputs.rst`) are the structural
complement: a scenario's deck swaps the Year-1 price inputs and
re-solves the dispatch, then the multi-year projection applies
E9-E15 (and any trajectories) unchanged — an input swap, deliberately
NOT a new equation.

### Route-to-market and optimizer fees (structural market-access costs)

Two structural fees model how European producers actually pay for
market access, both **default-off** (results bit-identical when unset)
and both **excluded from LCOE/LCOS**:

**Route-to-market fee** — the per-MWh representation charge of a
cumulative-representation aggregator: in Greece a FoSE (or the
last-resort FoSETeK operated by DAPEEP under regulated charges, per
YPEN/DAPEEK/25512/883/2019), in Germany a Direktvermarkter.  The
aggregator handles scheduling, injection declarations, balancing
responsibility and exchange access, and charges per MWh of **sold**
energy — typically 0.5-5 EUR/MWh (Greek examples ~1-3.5; German
Direktvermarktung 0.5-5).  The fee level is flat over the project life
(representation charges are quoted per MWh, not indexed); the charged
MWh fade on the per-origin degradation curves.  While a PHYSICAL
(sleeved) PPA is in term, its covered PV-export share $s$ is routed by
the offtaker and is exempt; a CfD (financial settlement) sells the full
volume at DAM through the aggregator and is not exempt.  Self-consumed
energy never crosses the market interface and carries no fee:

$$F^{\mathrm{rtm}}_y = -\phi_{\mathrm{rtm}} \left( E^{\mathrm{exp,PV}}_1 f^{PV}_y (1 - s \cdot \mathbb{1}[\mathrm{sleeved,\ in\ term}]) + E^{\mathrm{exp,B}}_1 f^{B}_y \right) \tag{E13c}$$

with $E^{\mathrm{exp,PV}}_1$ / $E^{\mathrm{exp,B}}_1$ the Year-1
exported MWh by origin (`pv_export_mwh` / `bess_export_mwh`,
availability-derated like the EUR bases).

**Optimizer revenue share** — the trading-services fee of a battery
optimizer, structured as a share of the **positive** annual BESS
wholesale trading margin (the merchant revenue-share / floor+share
structures documented for BESS optimizers; typical 10-25 %).  The base
is the battery's DAM stream net of grid-charging cost (exactly
$R^{\mathrm{DAM,B}}_1$, the `rev1_dam_bess` base), clamped at zero —
an optimizer never invoices a share of a trading loss:

$$F^{\mathrm{opt}}_y = -\varphi_{\mathrm{opt}} \cdot \max\!\left(R^{\mathrm{DAM,B}}_1 f^{B}_y (1+i_{\mathrm{DAM}})^{y-1},\; 0\right) \tag{E13d}$$

Neither fee touches self-consumption savings, PPA revenue, or
balancing revenue (the BSP fee (E13b) covers balancing).  Fees never
compound on other fees.  Stacking $\varphi$ (E13) with
$\varphi_{\mathrm{opt}}$ (E13d) double-charges the battery's
wholesale stream, so the loader warns when both are set.  Both surface
as their own signed cashflow columns (`route_to_market_fee_eur`,
`optimizer_fee_eur`) folded into `net_cashflow_eur`, allocated to
months by the same revenue-share weights as the energy-aggregator fee,
and roll up to `total_route_to_market_fee_eur_lifecycle` /
`total_optimizer_fee_eur_lifecycle` (rendered in `SUMMARY.md` only
when non-zero).  In the sensitivity tornado the optimizer share scales
with the revenue driver (price-proportional) while the
route-to-market fee does not (volume-based; the revenue delta perturbs
prices, not energy).

Who charges what, for reference: FoSE / FoSETeK (DAPEEP) and
Direktvermarkter charge (E13c)-style per-MWh representation fees;
battery optimizers (merchant revenue-share, floor+share or tolling
structures) charge (E13d)-style shares; retail/net-billing
self-consumption carries no aggregator fee (the netting is a supplier
service).

OPEX, replacement CAPEX, and the net cashflow:

$$O_y = -\left(o^{PV}\,\mathrm{kWp} + o^{B} P^{B}\right)(1+i_{\mathrm{opex}})^{y-1}, \qquad
C_y = \begin{cases} c^{B} E^{\mathrm{cap}} \cdot p_r/100 \cdot (-1) & y = y_r \\ 0 & \text{else} \end{cases} \tag{E14}$$

$$\mathrm{CF}_y = \underbrace{\left(R^{\mathrm{ret}}_y + R^{\mathrm{DAM}}_y + F_y\right)}_{\texttt{revenue\_eur}} + \underbrace{R^{\mathrm{bm}}_y}_{\text{gross}} + F^{\mathrm{bm}}_y + F^{\mathrm{rtm}}_y + F^{\mathrm{opt}}_y + F^{\mathrm{chg}}_y + L_y + R^{\mathrm{PPA}}_y + O_y + C_y + V_y \tag{E15}$$

($L_y$ = the E33 revenue levy; every later signed stream column —
imbalance $I_y$ (E28), toll (E29), floor top-up (E30), support and
netting (E31/E31a), capacity payment (E32) — joins the same row-wise
sum, as each section states)

with $V_y$ the DEVEX column (Year 0 only) and Year 0 carrying
$\mathrm{CF}_0 = \mathrm{CAPEX}_0 + \mathrm{DEVEX}_0$.  Discounted:
$\mathrm{DCF}_y = \mathrm{CF}_y \cdot D_y$; cumulative columns
accumulate both.

### Charging-side grid fee (Eq. E26)

Grid-charged BESS energy pays regulated network charges and levies on
top of the DAM price where storage is not exempt.  The effective wedge

$$w^{\mathrm{eff}} = \phi_{\mathrm{chg}} \cdot \left(1 - \mathbf{1}[\mathrm{exempt}]\right),
\qquad C^{\mathrm{chg}} = \sum_t \left(\pi^{\mathrm{DAM}}_t + w^{\mathrm{eff}}\right) x^{gb}_t / 1000 \tag{E26}$$

($\phi_{\mathrm{chg}}$ = `grid_charging_fee_eur_per_mwh` >= 0 on the
project sheet; `grid_charging_fee_exempt` = TRUE zeroes it) enters the
**MILP objective**, not just the cashflow: charging-side fees of
10-30 EUR/MWh erase a large share of arbitrage margin, and thin
spreads flip sign with the wedge — dispatch decided on the energy-only
price would grid-charge at a real-world loss.  The wedge actually paid
surfaces per step as `expense_grid_charging_fee_eur` (written only
when non-zero), is subtracted from `profit_total_eur` (keeping the KPI
algebraically consistent with the objective), is availability-derated
with the charging throughput it is proportional to, and is **not**
bundled into the BESS-DAM stream (`pvbess_opt/conventions.md`) so the
E13d/E25a bases stay market-only.

Over the lifecycle the fee projects as its own signed column
(Eq. E27): the Year-1 wedge actually paid (the KPI above) at the flat
regulated rate, with the charged grid-to-BESS volume fading on the
BESS capacity curve — the E13c flat-rate convention:

$$F^{\mathrm{chg}}_y = -\,e^{\mathrm{chg}}_1\, f^{B}_y \tag{E27}$$

It joins the net (E15), allocates monthly on the Year-1 per-step
charging shape (1/12 fallback, exact reconciliation), rolls up to
`total_grid_charging_fee_eur_lifecycle` (a SUMMARY row when non-zero),
joins every cashflow figure as its own deduction band ("Grid-charging
fee", drawn only when non-zero), is folded by the sensitivity net
recompute but NOT scaled by the Revenue driver (a regulated rate on
volume, no price component), and is **excluded from LCOE/LCOS** like
every market/venue fee.  The no-breakdown cashflow fallback adds the
fee back to the gross it derives from `profit_total_eur` (which
already nets it), so the column carries the deduction exactly once.

### Imbalance settlement line (Eqs. E28/E28a)

The rolling-horizon Monte Carlo's Year-1 settlement MEAN (unbiased,
additive expected value; a P50 would understate a right-skewed,
spike-driven cost — the percentiles carry the distribution) projects
as its own signed column:

$$I_y = -\,\bar{I}_1\; f^{PV}_y\; g^{\mathrm{dam}}_y \tag{E28}$$

— the deviation volume is PV-forecast-error-driven (fades on the PV
curve) and the settlement prices ride the DAM escalation series.
Monthly allocation follows the Year-1 PV production shape with exact
reconciliation (Eq. E28a).  Included in the net and NPV/IRR/payback;
**excluded from LCOE/LCOS** (market settlement cost, the market-fees
convention); folded by the sensitivity net recompute and SCALED by the
Revenue driver (price-spread times volume — price-proportional, like
the balancing columns).  Lifetime total
`total_imbalance_cost_eur_lifecycle` renders in SUMMARY.md when
non-zero and the "Imbalance cost" band joins every cashflow figure,
drawn only when non-zero.

### Contracted BESS revenue layer (foundations)

Two primitives every contracted BESS structure (tolling, optimizer
floor + share, state support with clawback, capacity market) will
read; both land ahead of the structures themselves and change no
result.

Phase-window indicator (`economics._contract_phase`):

$$\chi_y(y_f, y_t) = \mathbf{1}\left[\, y_f \le y \le y_t' \,\right],
\qquad y_t' = \begin{cases} Y & y_t = 0\\ y_t & \text{else} \end{cases} \tag{E25}$$

with $y_f \ge 1$; Year 0 is never inside any phase.  This generalises
the `y <= ppa_term` in-term gating the PPA stream already uses.

BESS market-revenue base (informational `bess_market_revenue_eur`
column):

$$M_y = R^{\mathrm{DAM,B}}_1\, f^{B}_y\, g^{\mathrm{dam}}_y
+ R^{\mathrm{bm,cap}}_y + R^{\mathrm{bm,act}}_y + F^{\mathrm{bm}}_y \tag{E25a}$$

— the battery's wholesale trading margin (the E13d base, **unclamped**:
a loss year stays negative) plus balancing revenue net of the BSP fee,
riding the DAM escalation series $g^{\mathrm{dam}}_y$ (E24).  It
excludes self-consumption savings, the PPA stream and every contracted
stream, and is availability-derated by construction (all inputs carry
$A$ per E8).  The column is **informational**: it is NOT summed into
`net_cashflow_eur` (the sensitivity `_recompute_net` excludes it
explicitly) and has no monthly counterpart — it is the single netting
base the contracted structures will read, and the Revenue tornado
driver scales it (price-proportional) so piecewise contract terms can
be recomputed exactly from a scaled base.

### BESS tolling agreement (Eqs. E29/E29a)

A tolling agreement pays the owner a fixed annual rate per MW of BESS
power for the right to dispatch the battery — the fixed-payment end of
the contracted-revenue spectrum.  Five `economics` keys, all
default-off (`bess_toll_eur_per_mw_year = 0` ⇒ bit-identical):
`bess_toll_eur_per_mw_year` ($\tau$), `bess_toll_year_from` /
`bess_toll_year_to` (the E25 window), `bess_toll_merchant_treatment`
(`zeroed` | `retained`) and `bess_toll_indexation_pct` ($i_\tau$).

$$R^{\mathrm{toll}}_y = \tau \cdot \frac{P^{B}}{1000} \cdot A \cdot
(1+i_\tau)^{\,y-1} \cdot \chi_y\!\left(y_f^{\mathrm{toll}},
y_t^{\mathrm{toll}}\right) \tag{E29}$$

The availability factor $A$ (E8) applies **here, once** — the toll is
a new stream, not derived from the already-derated Year-1 KPIs
(availability-conditioned capacity payments are the market norm) — and
there is deliberately **no** $f^{B}$ fade: the payment is on the
contracted power block, not on delivered energy.  Surfaces as the
`toll_revenue_eur` column (monthly: exact flat $1/12$ — a level
contractual payment), folds into `net_cashflow_eur`, is **excluded
from LCOE/LCOS** (revenue-agnostic convention) and does **not** scale
with the Revenue tornado driver (fixed contractual EUR/MW; the driver
perturbs market prices the toll is insulated from).

Merchant zeroing (default treatment `zeroed`):

$$\chi_y = 1:\quad R^{\mathrm{DAM,B}} \to 0,\;
R^{\mathrm{bm,cap}}_y, R^{\mathrm{bm,act}}_y, F^{\mathrm{bm}}_y \to 0,\;
E^{\mathrm{exp,B}} \to 0 \text{ in } F^{\mathrm{rtm}},\;
F^{\mathrm{opt}}_y \to 0,\;
F^{\mathrm{gcf}}_y \to 0 \tag{E29a}$$

— in toll years the toller holds dispatch rights, so every BESS-origin
merchant stream is gated to zero for that year: the $R^{\mathrm{DAM,B}}$
contribution to E10 (which nets the grid-charging energy cost), both
E11 balancing legs and their BSP fee (E13b), the BESS-export term of
the route-to-market fee (E13c), the optimizer share (E13d) **and the
charging-side grid fee (E27)** — the wedge follows the grid-charging
cost it accompanies, both being dispatch costs the toller bears (an
extension of the workstream design, which predates E26/E27).  The
gating substitutes the Year-1 bases per year inside the projection
loop and never mutates them, so the Year-1 revenue-split
reconciliation guard is untouched and `bess_market_revenue_eur` (E25a)
reflects the zeroing (it reports *realised* market revenue: zero in
toll years).  Deliberately **not** zeroed: PV-origin streams, the PPA
leg, self-consumption savings (a warning fires when
`profit_load_from_bess_eur` is non-zero alongside a toll — a tolled
grid-scale battery has no retail leg) and the PV-forecast-error-driven
imbalance cost (E28).  Under `retained` no gating occurs — the toll
stacks on top of the full merchant streams (a capacity-overlay
contract; a warning flags the double-monetisation).

Stacking warnings (validation-time, never blocking): toll rate set
with `bess_power_kw = 0` (no-op), treatment `retained`
(double-monetises the MW), and toll + `optimizer_revenue_share_pct`
both active (under `zeroed` the share is gated in toll years; the two
double-charge the same wholesale stream otherwise).

### Optimizer floor + share above floor (Eqs. E30/E30a)

The plain optimizer revenue share (E13d) is the $\varphi$-share special
case of the floor+share structure BESS optimizers commonly offer: the
optimizer guarantees an annual floor and takes its share of the margin
**above** the floor; shortfalls are topped up.  Gated by the explicit
`optimizer_floor_enabled` switch (default FALSE ⇒ E13d bit-identically)
so a floor *value* of zero never silently converts trading losses into
top-ups — note that a floor of 0 with the switch ON still guarantees a
non-negative margin.  A shared term window
(`optimizer_term_year_from/to`, default whole life) gates both share
and floor; outside the term the year is merchant.

$$\mathrm{Floor}_y = F \cdot P^{B} \cdot A, \qquad
F^{\mathrm{opt}}_y = -\varphi_{\mathrm{opt}}
\max\!\left(M_y - \mathrm{Floor}_y,\, 0\right), \qquad
T^{\mathrm{opt}}_y = +\max\!\left(\mathrm{Floor}_y - M_y,\, 0\right)
\tag{E30}$$

within the term window $\chi_y$ (both zero outside); $F$ in EUR/kW/yr
on the power block, availability-scaled ($\times A$, the E29
convention — a new stream, derated once), flat nominal (no fade, no
indexation).  The owner's realised optimizer-managed margin is
$M_y + F^{\mathrm{opt}}_y + T^{\mathrm{opt}}_y =
\max\!\left(\mathrm{Floor}_y,\, \mathrm{Floor}_y +
(1-\varphi_{\mathrm{opt}})(M_y - \mathrm{Floor}_y)\right)$ — never
below the floor.  The top-up is a separate `optimizer_floor_topup_eur`
column ($\ge 0$) so `optimizer_fee_eur` keeps its $\le 0$ sign
contract (plot stacking and fee-inference helpers rely on it).

Margin basis (Eq. E30a):

$$M_y = \begin{cases}
R^{\mathrm{DAM,B}}_1\, f^{B}_y\, g^{\mathrm{dam}}_y &
\texttt{dam} \text{ (default; the E13d base)}\\[2pt]
\text{E25a base (DAM margin + balancing net of the BSP fee)} &
\texttt{dam\_plus\_balancing}
\end{cases} \tag{E30a}$$

Under `dam_plus_balancing` the share applies **after** the BSP fee —
fees never compound on other fees (house rule).  Monthly: the top-up
books in **month 12** (annual ex-post settlement, the
replacement-CAPEX convention, so monthly and yearly DCFs agree on the
event); the fee keeps its revenue-share weights.  Sensitivity: the
fee/top-up pair is piecewise in the margin, so the Revenue driver
recomputes both from the scaled E25a base against the **un-scaled**
contractual floor (`_scale_revenue` gains an optional `econ`
parameter; the `None`-default legacy path is exact for the plain share
because $\max(fM,0) = f\max(M,0)$ for $f>0$).  The tornado is
therefore exact at the $M_y = \mathrm{Floor}_y$ kink, and contracted
floors visibly damp the Revenue bars.  Excluded from LCOE/LCOS.
Stacking: a `zeroed` toll window overlapping the optimizer term warns
— the toll zeroes the margin, forcing a full floor top-up every
overlap year.

### State support with two-way clawback (Eqs. E31/E31a)

A fixed annual support per MW of BESS power over a support window,
with a TWO-WAY netting against realised market revenue relative to a
threshold — the settlement form used by storage-support auctions
funded through the Recovery and Resilience Facility (the Greek Tameio
Anakampsis kai Anthektikotitas / TAA auctions are the reference; the
mechanism here is neutral and jurisdiction-agnostic).  Six `economics`
keys, all default-off (`state_support_eur_per_mw_year = 0` ⇒
bit-identical).

$$S_y = \sigma \cdot \frac{P^{B}}{1000} \cdot A \cdot
(1+i_s)^{\,y-1} \cdot \chi_y\!\left(y_f^{s}, y_t^{s}\right) \tag{E31}$$

— availability-conditioned on the power block (the E29 convention), no
$f^{B}$ fade (support is per installed MW).  The two-way netting:

$$\mathrm{CB}_y = -c\,\left(M^{\mathrm{mkt}}_y - \theta_y\right)\chi_y,
\qquad \theta_y = \theta \cdot \frac{P^{B}}{1000}\,(1+i_s)^{\,y-1},
\qquad c = \frac{\texttt{share\_pct}}{100} \tag{E31a}$$

with $M^{\mathrm{mkt}}_y$ the realised market revenue: the E25a base
(plus the capacity-market revenue E32 when present — market-facing
capacity income counts as realised revenue for the netting).
$\mathrm{CB}_y < 0$ (clawback) when realised market revenue exceeds
the threshold, $\mathrm{CB}_y > 0$ (compensation) when it falls short;
$S_y + \mathrm{CB}_y$ may turn negative — a **net repayment year**, no
floor is applied by design, and the run log flags the affected years
once.  PPA, self-consumption savings and toll revenue are excluded
from the netting base by construction (they are not market revenue);
under a `zeroed` toll the base is zero, so the netting tops up to
$\theta_y$ every overlap year — the warned two-capacity-payments
stacking case.

Columns: `state_support_eur` ($\ge 0$, flat $1/12$ monthly — a level
payment) and the signed `state_support_clawback_eur` (month-12 booking
— annual ex-post settlement).  Both fold into `net_cashflow_eur`, are
excluded from LCOE/LCOS, and carry SUMMARY-optional lifetime totals.
Sensitivity: the gross support does NOT scale with the Revenue driver
(fixed EUR/MW); the netting is recomputed (linear, exact) from the
scaled market base against the UN-scaled threshold — the netting is
revenue-stabilising, so Revenue-tornado bars narrow as the share
rises, reaching full stabilisation of the market component at
$c = 1$.  The clawback reads the deterministic analytic projection;
per-seed Monte Carlo netting is out of scope (stated limitation).

### Capacity-market payment with derating factor (Eq. E32)

The simplest contracted structure: an annual payment on the DERATED
power block over a contract window.  Five `economics` keys,
default-off (`capacity_market_eur_per_mw_year = 0` ⇒ bit-identical).

$$R^{\mathrm{cm}}_y = \kappa \cdot \frac{P^{B}}{1000} \cdot \delta
\cdot A \cdot (1+i_{cm})^{\,y-1} \cdot
\chi_y\!\left(y_f^{\mathrm{cm}}, y_t^{\mathrm{cm}}\right),
\qquad \delta = \frac{\texttt{derating\_pct}}{100} \tag{E32}$$

The derating factor is a plain user input (EU capacity mechanisms
derate storage by duration relative to the stress-event window — enter
the auction's published class factor); the model deliberately does NOT
derive it from `bess_kwh / bess_kw`.  Convention (stated to avoid
double-derating): the payment is **on the derated MW**.  No $f^{B}$
fade (the obligation is on derated nameplate); availability applies
(availability-tested payments).  Worked example:
$\kappa = 50{,}000$, $\delta = 40\,\%$, $P^{B} = 1$ MW, $A = 0.99$
⇒ 19,800 EUR in Year 1.

`capacity_market_revenue_eur` folds into `net_cashflow_eur` (flat
$1/12$ monthly — a level payment) and **counts toward the E31a netting
base**, computed before $\mathrm{CB}_y$ in the year loop (order locked
by test); the E25a base itself stays capacity-free (the payment is not
wholesale trading margin).  NOT scaled by the Revenue driver
(administered capacity price, the route-to-market precedent) — it
joins the netting recompute at its un-scaled value.  Excluded from
LCOE/LCOS.  Stacking warnings: overlap with a state-support window
(cumulation rules typically restrict stacking) and with a `zeroed`
toll (the toller usually holds the capacity obligation too).

### Contracted-layer conventions and stacking-interaction matrix

Per-structure conventions (one row per contracted stream):

| Structure | Column(s) | Availability $A$ | $f^{B}$ fade | Monthly booking | Revenue-driver scaling | Netting/share base |
|---|---|---|---|---|---|---|
| Tolling (E29/E29a) | `toll_revenue_eur` | yes (payment conditioned) | no (power block) | flat 1/12 | no (fixed contractual) | — (gates the merchant streams instead) |
| Optimizer floor+share (E30/E30a) | `optimizer_fee_eur` ($\le 0$), `optimizer_floor_topup_eur` ($\ge 0$) | yes (floor level) | no | fee: revenue-share weights; top-up: month 12 | piecewise recompute (scaled margin vs un-scaled floor) | E13d DAM margin or E25a base |
| State support (E31/E31a) | `state_support_eur` ($\ge 0$), `state_support_clawback_eur` (signed) | yes (support level; threshold NOT derated) | no | support: flat 1/12; netting: month 12 | support: no; netting: exact recompute vs un-scaled threshold | E25a base + E32 revenue |
| Capacity market (E32) | `capacity_market_revenue_eur` | yes (availability-tested) | no (derated nameplate) | flat 1/12 | no (administered price) | joins the E31a base |

All five columns fold into `net_cashflow_eur`, carry SUMMARY-optional
lifetime totals, and are **excluded from LCOE and LCOS** (the metrics
stay revenue-agnostic energy-cost figures; test-locked per structure).
When any structure is active, `compute_financial_kpis` emits one
`[contracted revenue]` INFO line in the run log with the five lifetime
totals (the LCOE/LCOS audit's noise discipline: silent in the
all-merchant default).

Stacking warnings live in one data-driven table
(`io._CONTRACT_STACKING_RULES`) — a validation-time pass evaluates
every rule against the parsed contract windows
(`io._phase_windows_overlap`; phase-disjoint configurations never
warn, locked by the parametrised matrix test).  Warned combinations
and why:

| Rule | Fires when | Why |
|---|---|---|
| `toll_no_op` | toll rate set, `bess_power_kw = 0` | the stream is a no-op |
| `toll_retained` | toll active with treatment `retained` | toll + full merchant streams double-monetise the same MW |
| `toll_x_optimizer_share` | toll and optimizer share active, windows overlap | under `zeroed` the share is gated in toll years; otherwise the two double-charge the same wholesale stream |
| `toll_x_optimizer_floor` | floor enabled, `zeroed` toll overlaps the optimizer term | the toll zeroes the margin — a full floor top-up every overlap year (double-charging the counterparties) |
| `toll_x_state_support` | support window overlaps a `zeroed` toll | the netting base is zero, so the netting tops up to $\theta_y$ every overlap year — two capacity payments for the same MW |
| `capacity_x_state_support` | capacity and support windows overlap | support-cumulation rules typically restrict stacking (the capacity revenue does count toward the E31a base) |
| `capacity_x_toll` | capacity window overlaps a `zeroed` toll | the toller usually holds the capacity obligation too |

A matrix row is reserved for the Phase-5 sliding-FiP / two-way-CfD
support scheme × state-support cumulation warning (activated when
those keys land).

### Revenue levy on gross market turnover (Eq. E33)

A configurable percentage levy on gross **market** turnover — the
mechanism of the 3 % special RES turnover levy applied in Greece,
expressed neutrally.  One `economics` key, `revenue_levy_pct`
(default 0 ⇒ bit-identical, validated in $[0, 100]$).

$$L_y = -\lambda \, \max\!\left(0,\;
R^{\mathrm{DAM,gross}}_y + R^{\mathrm{bm,cap}}_y +
R^{\mathrm{bm,act}}_y + R^{\mathrm{PPA}}_y\right), \qquad
\lambda = \frac{\texttt{revenue\_levy\_pct}}{100}, \qquad L_0 = 0
\tag{E33}$$

Base conventions: $R^{\mathrm{DAM,gross}}_y$ is the DAM stream
**before** the E13 aggregator fee and the balancing legs are gross of
the BSP fee — a turnover levy charges gross sales, and fees never
compound; the PPA contract leg is invoiced turnover (a CfD difference
leg can be negative and reduce the base — the clamp stops a negative
total turnover from ever producing a rebate); the post-term physical
PPA reversion joins the base through the DAM stream automatically.
Excluded by construction: the retail/self-consumption stream (avoided
cost, not invoiced turnover — the route-to-market "sold energy only"
precedent), the contracted streams E29–E32 (not market turnover; the
E29a toll gating already removes the tolled merchant legs from the
base) and the imbalance settlement.  The levy sits inside EBITDA, so
it is automatically deductible from taxable income once the tax layer
(E34–E38) lands.

Column `revenue_levy_eur` ($\le 0$) folds into `net_cashflow_eur`
(E15); monthly it rides the revenue-share weights (the structural-fee
approximation of the market-turnover shape; shares sum to one so the
yearly reconciliation is exact).  Sensitivity: the base is a
uniform-scaling sum of price-driven streams and $f > 0$ preserves the
clamp ($\max(f\,b, 0) = f\max(b, 0)$), so the levy **scales with the
Revenue driver** exactly (constant scale); the net recompute folds
it.  Excluded from LCOE/LCOS; lifetime total renders in SUMMARY.md
only when set.  Note the levy changes PRE-tax headline KPIs when set —
deliberate: it is an operating cost, not an income tax.

### Tax and depreciation layer (Eqs. E34-E38; pre-tax when the rate is 0)

`economics.apply_tax_layer(yearly_cf, econ, capacities)` is a pure
post-processing layer called at the end of `build_yearly_cashflow`, so
the frame always carries the post-tax column family.  The pre-tax
columns are **never touched** — `net_cashflow_eur` keeps its E15
definition and the published pre-tax KPIs remain the baseline.  With
`corporate_tax_rate_pct = 0` (default) every tax column is an exact
zero and the post-tax family passes through value-identical to the
pre-tax family (no schedule is computed — noise-free bit-identity).

Straight-line depreciation over asset classes
$a \in \{\mathrm{PV}, \mathrm{BESS}, \mathrm{site},
\mathrm{BESS\text{-}replacement}\}$:

$$\mathrm{DEP}_y = \sum_a \frac{\mathrm{base}_a}{N_a}\,
\mathbf{1}\!\left[\, y_{a0} \le y \le \min\!\left(Y,\,
y_{a0} + N_a - 1\right) \right] \tag{E34}$$

with $\mathrm{base}_{PV} = (c^{PV} + v^{PV})\,\mathrm{kWp}$,
$\mathrm{base}_B = c^{B} E^{\mathrm{cap}} + v^{B} P^{B}$,
$\mathrm{base}_{site} = \mathrm{site\_capex} + \mathrm{site\_devex}$
(all $y_{a0} = 1$), and the replacement tranche
$\mathrm{base}_r = c^{B} E^{\mathrm{cap}}\, p_r / 100$ in service from
$y_{r0} = y_r + 1$ (the asset enters service after its month-12
booking, Eq. E4) over $N_{\mathrm{BESS}}$ years.  $N_a = 0$ ⇒ no
claim; tranches truncate at the horizon $Y$ (unclaimed depreciation is
lost — no terminal write-off).

$$\mathrm{EBITDA}_y = \mathrm{CF}_y - C_y - V_y \;\; (y \ge 1),
\qquad \mathrm{EBITDA}_0 = 0 \tag{E35}$$

— the operating net before investment events: revenue net of every
E13-family fee and the E33 levy (the levy is therefore deductible by
construction), plus balancing, PPA, the contracted streams and OPEX.

$$\mathrm{TI}_y = \mathrm{EBITDA}_y - \mathrm{DEP}_y - \mathrm{INT}_y,
\qquad \mathrm{TB}_y = \max\!\left(0,\, \mathrm{TI}_y -
L_{y-1}\right) \tag{E36}$$

with $\mathrm{INT}_y$ the E20 schedule interest on
$\text{gearing} \times |\mathrm{CF}_0|$ (zero when all-equity or
beyond the tenor) and $L_y$ the loss pool: losses accumulate as
vintages, profits absorb them FIFO, and with
`tax_loss_carryforward_years` $= W > 0$ a vintage expires $W$ years
after it arose ($W = 0$ = unlimited, the default).

$$\mathrm{TAX}_y = -\tau\, \mathrm{TB}_y \;\le\; 0 \tag{E37}$$

(no negative-tax rebates; losses only carry forward).

$$\mathrm{CF}^{pt}_y = \mathrm{CF}_y + \mathrm{TAX}_y, \qquad
\mathrm{DCF}^{pt}_y = \mathrm{CF}^{pt}_y \cdot D_y \tag{E38}$$

— the same discount rate E3 (single-WACC convention: the levered
interest shield mixes capital-structure effects into project NPV,
collapsing to unlevered at zero gearing; documented in Assumptions &
limitations).  Appended columns: `depreciation_eur`,
`debt_interest_eur`, `taxable_income_eur`,
`tax_loss_carryforward_eur` (the balance carried OUT of the year),
`corporate_tax_eur` ($\le 0$) and the post-tax family
`net_cashflow_post_tax_eur` / `discounted_cf_post_tax_eur` /
`cumulative_cf_post_tax_eur` / `cumulative_dcf_post_tax_eur`.
Monthly: `corporate_tax_eur` books in **month 12** (annual
settlement; December's E4 factor equals the yearly E3 factor, so the
monthly and yearly post-tax DCFs agree exactly); depreciation,
taxable income and the carry-forward stay yearly-only (annual
accounting concepts).  Sensitivity: the scaled-frame helpers **drop**
every tax-layer column from perturbed frames — taxes are nonlinear
(the TB clamp and the carry-forward), so scaled copies would be
silently stale; the pre-tax tornado is unaffected and stays the
published baseline.  No default figures change (the post-tax net is a
separate column family, so every existing stack keeps its
segment-sum == net-line identity).  Worked example: 4/2/8-year lives
over an 8-year horizon put the early years into loss (carry-forward
builds), the loss pool absorbs FIFO once depreciation runs out, and
tax turns on in the late years — locked to hand-computed cents in
`tests/test_tax_depreciation.py`, alongside an independent levered
reference case.

Post-tax KPIs (Eq. E39) surface the layer as headline metrics
**alongside** (never replacing) the pre-tax baseline:

$$\mathrm{CF}^{eq,pt}_0 = \mathrm{CF}^{pt}_0 + B, \qquad
\mathrm{CF}^{eq,pt}_y = \mathrm{CF}^{pt}_y - s_y \;\;
(1 \le y \le T_d), \qquad
\mathrm{equity\ IRR}^{pt} = \mathrm{IRR}\!\left(
\mathrm{CF}^{eq,pt}\right) \tag{E39}$$

with $B$, $s_y$ from the E20 schedule.  `compute_financial_kpis`
emits `npv_post_tax_eur` (sum of the discounted post-tax column),
`irr_post_tax_pct`, `equity_irr_post_tax_pct`,
`simple_payback_post_tax_years` /
`discounted_payback_post_tax_years` (the E19 interpolation on the
post-tax cumulatives), `total_corporate_tax_eur_lifecycle` ($\le 0$),
`total_depreciation_eur_lifecycle` and a `corporate_tax_rate_pct`
echo (the `gearing_pct` precedent).  Every post-tax KPI reports
**NaN while the rate is 0** (the all-equity `equity_irr_pct`
precedent: n/a = not modelled — never a duplicate of the pre-tax
value), so the SUMMARY optional-row renderer self-skips them and
zero-default digests stay byte-identical.  `min_dscr` deliberately
stays pre-tax (a CFADS-based post-tax DSCR is a stated non-goal).

### Financial KPIs

$$\mathrm{NPV} = \sum_{y=0}^{Y} \mathrm{DCF}_y \tag{E16}$$

$$\mathrm{IRR}: \;\; \sum_{y=0}^{Y} \frac{\mathrm{CF}_y}{(1+\mathrm{IRR})^{y}} = 0 \tag{E17}$$

solved by Newton-Raphson with bisection fallback over
$(-0.999, 10]$ (`economics.calculate_irr`); NaN when all flows share a
sign.

$$\mathrm{ROI} = \frac{\sum_{y\ge 1} \mathrm{CF}_y}{\left|\mathrm{CF}_0\right|} \cdot 100\%, \qquad
\mathrm{BCR} = \frac{\sum_y \max(\mathrm{DCF}_y, 0)}{\sum_y \max(-\mathrm{DCF}_y, 0)} \tag{E18}$$

Payback (simple on $\mathrm{CF}$, discounted on $\mathrm{DCF}$) is the
linear interpolation of the first zero-crossing of the cumulative
column, measured in project years **from the CAPEX year** (year 0),
with NaN for no crossing or a degenerate flat crossing
(`economics._payback_year`):

$$\mathrm{PB} = y^{*}-1 + \frac{-\mathrm{cum}_{y^{*}-1}}{\mathrm{CF}_{y^{*}}}, \qquad
y^{*} = \min\{y : \mathrm{cum}_y \ge 0\} \tag{E19}$$

### Debt layer (optional; all-equity when `gearing_pct` = 0)

Debt $B = g \cdot |\mathrm{CF}_0|$ with $g$ = `gearing_pct`/100.
Annuity service or linear principal over the tenor $T_d$
(`economics._amortization_schedule`):

$$\mathrm{annuity}: \; s = B\,\frac{r_d}{1-(1+r_d)^{-T_d}}; \qquad
\mathrm{linear}: \; P_y = B/T_d, \; s_y = P_y + r_d B_{y-1} \tag{E20}$$

Equity cashflow: $\mathrm{CF}^{eq}_0 = \mathrm{CF}_0 + B$;
$\mathrm{CF}^{eq}_y = \mathrm{CF}_y - s_y$ for $y \le T_d$.  KPIs:
`equity_irr_pct` = IRR of $\mathrm{CF}^{eq}$,
`min_dscr` $= \min_y \mathrm{CF}_y / s_y$ and
`avg_dscr` $= \mathrm{mean}_y\, \mathrm{CF}_y / s_y$ over
positive-service tenor years
(`economics._leverage_kpis`; full table via
`economics.build_debt_schedule`).  The three render in SUMMARY.md
only when finite (all-equity runs carry NaNs).

Sculpted repayment (`debt_repayment = sculpted`, Eqs. E40/E40a) —
the lender profile in which debt service tracks CFADS at a constant
DSCR instead of binding in one year:

$$s_y = \frac{\max(\mathrm{CFADS}_y, 0)}{\mathrm{DSCR}_s}, \qquad
P_y = \max\!\left(0,\, s_y - r_d B_{y-1}\right), \qquad
B_y = B_{y-1} - P_y \tag{E40}$$

with $\mathrm{CFADS}_y =$ `net_cashflow_eur`$[y]$ (operating net,
replacement CAPEX included — the same numerator convention as the
per-year DSCR column).  For a GIVEN debt $B$ (manual mode) the
implied constant DSCR follows from the identity that the PV of debt
service at the debt rate equals the outstanding principal:

$$\mathrm{DSCR}_s = \frac{\sum_{y=1}^{T_d}
\max(\mathrm{CFADS}_y, 0)\,(1+r_d)^{-y}}{B} \tag{E40a}$$

A $\mathrm{CFADS}_y \le 0$ year pays nothing (unpaid interest is not
capitalised in this simple model) and later years absorb it; any
clamp residual sweeps into the final year's principal so the balance
amortises to ~0 exactly.  Under sculpting `min_dscr` $=$ `avg_dscr`
by construction.

### Target-DSCR debt sizing (Eqs. E41-E43; `debt_sizing_mode`)

`debt_sizing_mode = target_dscr` inverts the amortization schedule:
instead of deriving DSCR from a user-chosen gearing, the maximum
debt $B^{*}$ that holds `target_dscr` ($\mathrm{DSCR}_t \ge 1$) on
the sizing-case CFADS is solved in closed form per repayment profile
(`economics.size_debt`), and **gearing becomes an output**.  The
annuity's level service binds at the minimum-CFADS tenor year:

$$B^{*}_{\mathrm{ann}} = \frac{\min_{1\le y\le T_d} \mathrm{CFADS}_y}
{\mathrm{DSCR}_t}\cdot\frac{1-(1+r_d)^{-T_d}}{r_d}
\quad (r_d>0; \; T_d\,\min_y \mathrm{CFADS}_y/\mathrm{DSCR}_t
\text{ at } r_d=0) \tag{E41}$$

Linear service $s_y = B/T_d + r_d B (T_d-y+1)/T_d$ is linear in $B$,
so every tenor year yields an upper bound and $B^{*}$ is their
minimum; sculpted sizing is the E40a inverse (coverage is level at
the target in every positive-CFADS year by construction):

$$B^{*}_{\mathrm{lin}} = \min_{1\le y\le T_d}
\frac{\mathrm{CFADS}_y}{\mathrm{DSCR}_t\left(\tfrac{1}{T_d}
+ r_d\,\tfrac{T_d-y+1}{T_d}\right)}; \qquad
B^{*}_{\mathrm{sculpt}} = \frac{\sum_{y=1}^{T_d}
\max(\mathrm{CFADS}_y,0)\,(1+r_d)^{-y}}{\mathrm{DSCR}_t} \tag{E42}$$

Debt can only fund the Year-0 outlay, so the committed amount caps
there and the sized gearing follows:

$$D = \min\!\left(B^{*}, |\mathrm{CF}_0|\right); \qquad
\texttt{gearing\_sized\_pct} = 100\,D/|\mathrm{CF}_0|; \qquad
\texttt{debt\_capacity\_eur} = B^{*} \tag{E43}$$

(the capacity is reported uncapped so the user sees when the outlay,
not the DSCR, binds).  Equity cashflow and the leverage KPIs then
follow E20 with debt $= D$.  Conventions, all deliberate:

* **Non-circular by construction** — `net_cashflow_eur` contains no
  financing lines and no fee is debt-funded, so sizing on the base
  cashflow needs no iteration.
* **Frozen debt** — `economics.resolve_debt_sizing` runs exactly
  once per run (pipeline, right after the base cashflow) and stashes
  the result under internal underscore keys; sensitivity and
  uncertainty replays consume the frozen amount and never re-size
  per perturbation (debt is committed at financial close).  The
  corporate-tax layer re-applies afterwards so its E20 interest
  deduction runs on the sized debt.
* **Infeasible target is not an error** — a non-positive CFADS year
  inside the tenor gives the level-service profiles capacity 0
  (`dscr_target_met = False`); the run completes all-equity with a
  neutral SUMMARY line.  A replacement-year CAPEX dip inside the
  tenor therefore craters annuity/linear capacity — correct under
  the stated CFADS convention; prefer `sculpted` for
  replacement-bearing projects.
* **`gearing_pct` is an input echo only** in this mode (a UserWarning
  says so when it is non-zero); `gearing_input_pct` re-echoes it next
  to `gearing_sized_pct` in the KPI dict and SUMMARY block.
* **LCOE / LCOS are untouched** — financing costs remain excluded
  (E21/E22 never read financing lines; sizing introduces no new cost
  line), and `net_cashflow_eur` does not change, so every default
  figure is bit-identical.
* `debt_sizing_case` fixes the CFADS the debt is sized against:
  `base` (the run's own cashflow), `p90` (the E44 production
  haircut; a warning flags the degenerate factor-100 case) or
  `low_price` — the yearly cashflow of the price deck named by
  `debt_sizing_deck` (default `low`), re-dispatched with that deck's
  prices through the multi-deck scenario machinery
  (`pipeline._low_price_sizing_cashflow`; `<column>__<deck>` variant
  columns required on the timeseries, checked by the validator, and
  the run's solve time roughly doubles).  E41-E44 apply verbatim to
  the deck CFADS — unlike the P90 haircut this is a genuine
  re-dispatch, so BESS arbitrage adapts to the deck's spreads.  The
  deck run forces its own sizing / lender / sensitivity extras off,
  so the recursion terminates after one level.

### P90 production lender case (Eq. E44; `production_p90_factor_pct`)

Lenders size against a downside resource year: with
$f = $ `production_p90_factor_pct`$/100$, the P90 case applies a
deterministic INTER-ANNUAL haircut to the PV-linked streams of the
yearly cashflow (`lender.apply_production_case`) and re-evaluates
the leverage metrics on the resulting CFADS:

$$\mathrm{CFADS}^{P90}_y = \mathrm{CFADS}_y\Big|_{\,G_y \to f G_y,\;
\mathrm{PPA}_y \to f\,\mathrm{PPA}_y,\;
\mathrm{RtM}_y \to f\,\mathrm{RtM}_y,\;
\mathrm{IMB}_y \to f\,\mathrm{IMB}_y} \tag{E44}$$

where $G_y$ is the retail/DAM **gross** (recovered from the base
frame per the `sensitivity._scale_revenue` identity
`revenue_eur + |aggregator_fee_eur| == gross`, the aggregator fee
then rederived with the same fraction and non-negative-gross clamp),
PPA is the pay-as-produced volume leg, RtM the per-MWh
route-to-market fee (E13c — export VOLUME falls with production,
unlike under the price-perturbing Revenue tornado driver, where it
stays fixed) and IMB the E28 imbalance line (PV-curve volume).
Everything else is deliberately NOT scaled: the balancing family
(BESS reservation revenue; the partial PV-coupling of activation is
a flagged approximation), toll / capacity-market / state-support
payments (contractual EUR/MW), the optimizer floor+share pair
(BESS-margin structures), the grid-charging fee (grid-import
volume) and the revenue levy (mixed base, kept at the base value —
conservative), OPEX / CAPEX / DEVEX.  This factor multiplies
already-availability-derated revenue (E8/E8a applied upstream), so
it is NOT an availability derate — adding it to the derate list
would double-count.

Scope split with the uncertainty machinery (cross-referenced in
`docs/uncertainty_design.md`): the U1 forecast-noise Monte Carlo
models intra-year dispatch realism against imperfect foresight; the
P90 factor is an inter-annual resource case.  No re-dispatch happens
— the scaling is a documented cashflow-level approximation (a real
P90 irradiance year moves BESS arbitrage volumes and curtailment
nonlinearly); re-solving the dispatch with a scaled PV profile
through the scenario engine is recorded as future work.

`lender_cases_enabled` evaluates the case table
(`lender.build_lender_cases`): rows `base` and `p90`, columns
min/avg DSCR, equity IRR (E20 on the case CFADS with the run's
resolved — frozen — debt), case NPV and E41/E42 debt capacity at the
configured `target_dscr`; written to a `lender_cases` results sheet
and a SUMMARY block, both absent by default.  A `low_price` row
joins them when `debt_sizing_case = low_price` already re-dispatched
the deck (the same frame serves both surfaces — the table alone
never triggers a solve; being a price case it keeps full production,
so its factor column reads 100).  LCOE / LCOS are
deliberately excluded from the table: they are Lazard cost figures,
and scaling the energy denominator without the cost numerator would
misstate them.  `debt_sizing_case = p90` sizes the debt (E41-E43) on
$\mathrm{CFADS}^{P90}$ while the run's own cashflow stays the base
case — the sized run then shows the base-case coverage cushion above
the target.

### LCOE and LCOS (Lazard-style, revenue-agnostic)

Both metrics are **per-asset cost ÷ discounted delivered energy**;
they never read the cashflow's `capex_eur` column.  Deliberately
excluded from both: site-wide lump sums (`site_capex_eur` /
`site_devex_eur` are neither PV-only nor BESS-only), balancing
revenue, and PPA revenue (Lazard's bands are revenue-agnostic
energy-cost figures).  Toggling `balancing_enabled` or `ppa_enabled`
with identical capacities leaves LCOE/LCOS unchanged.

$$\mathrm{LCOE} = \frac{(c^{PV}+v^{PV})\,\mathrm{kWp} \cdot D_0 + \sum_{y\ge1} D_y\, o^{PV}\mathrm{kWp}\,(1+i_{\mathrm{opex}})^{y-1}}{\sum_{y\ge1} D_y\, E^{PV}_y} \tag{E21}$$

$$\mathrm{LCOS} = \frac{(c^{B} E^{\mathrm{cap}}+v^{B} P^{B}) D_0 + c^{B} E^{\mathrm{cap}}\frac{p_r}{100} D_{y_r} + \sum_{y\ge1} D_y\, o^{B} P^{B} (1+i_{\mathrm{opex}})^{y-1}}{\sum_{y\ge1} D_y\, E^{B}_y} \tag{E22}$$

where $E^{PV}_y$ = `lifetime_yearly['pv_generation_mwh']` and $E^{B}_y$
= `lifetime_yearly['bess_discharge_mwh']` (both degraded by
Eqs. E5-E6 and availability-derated upstream).  The discounted
numerator/denominator components are exported
(`lcoe_disc_pv_capex_eur` …) so the sensitivity plot computes exact
ranges instead of a multiplicative approximation.  Auxiliary:

$$\texttt{bess\_lifetime\_cycles} = \frac{1000 \sum_y E^{B}_y}{E^{\mathrm{cap}}}, \qquad
\texttt{pv\_capacity\_factor} = \frac{E^{PV}_1}{\mathrm{kWp} \cdot 8760/1000} \tag{E23}$$

### Emissions / 24-7 CFE (optional)

With `grid_co2_intensity_kg_per_mwh` > 0 (declining at
`grid_co2_annual_decline_pct`/yr, or per-step via a
`grid_co2_kg_per_mwh` timeseries column), `pvbess_opt.emissions`
computes avoided emissions for self-consumed energy and the hourly
24/7 carbon-free-energy score; off by default (intensity 0).

## Settlement & cashflow equations

The monthly view (`economics.derive_monthly_cashflow`) allocates each
yearly row to months:

* **Revenue** uses the Year-1 monthly net-revenue shape (per-step EUR
  columns net of grid-charge expense, grouped by month), rescaled so
  the monthly sum equals the yearly `revenue_eur` row exactly.
* **Aggregator fee** is allocated by each month's revenue share (the
  fee is already inside `revenue_eur`; the column restates it).
* **OPEX** is flat 1/12 of the year's row.
* **Balancing** is allocated by the Year-1 monthly sum of the
  aggregate `bm_reservation_<product>_kw` columns (flat 1/12 fallback
  when reservations are absent or all-zero), the same weighting as
  the BESS-revenue-by-month plot.
* **PPA** is allocated by the Year-1 monthly |contract-leg| magnitude
  (stable under CfD sign flips), flat 1/12 fallback.
* **CAPEX/DEVEX events** book in month 12 (Eq. E4 ⇒ monthly and
  yearly DCF agree); Year 0 stays on the yearly sheet only.
* `pv_production_mwh` is the Year-1 monthly PV shape × $f^{PV}_y$ ×
  availability, reconciling with `kpis_year1` and the lifetime sheet.

Guarantee (regression-locked): monthly `net_cashflow_eur` sums to the
yearly row **for every operating year**, including a replacement year;
quarterly aggregates by $q = \lceil m/3 \rceil$.

## KPI definitions

`compute_financial_kpis` emits (NaN-safe): `npv_eur`, `irr_pct`,
`equity_irr_pct`, `min_dscr`, `gearing_pct`, `roi_pct`, `bcr`,
`simple_payback_years`, `discounted_payback_years`,
`initial_investment_eur` (Year-0 outlay only),
`total_capex_eur` / `total_devex_eur` / `total_capex_devex_eur`
(lifecycle incl. replacement), `total_opex_eur_lifecycle`,
`total_revenue_eur_lifecycle`, `total_aggregator_fee_eur_lifecycle`,
`total_route_to_market_fee_eur_lifecycle` /
`total_optimizer_fee_eur_lifecycle` (the structural market-access
fee totals, ≤ 0; rendered in `SUMMARY.md` only when non-zero),
`lifetime_bm_revenue_total_eur` (gross) /
`lifetime_bm_capacity_revenue_total_eur` /
`lifetime_bm_activation_revenue_total_eur` /
`lifetime_bm_aggregator_fee_total_eur` (the optional BSP fee, ≤ 0) /
`lifetime_bm_revenue_net_total_eur` (gross + fee),
`lifetime_ppa_revenue_total_eur`, `lcoe_eur_per_mwh`,
`lcos_eur_per_mwh` (+ their `lcoe_disc_*`/`lcos_disc_*` components),
`pv_capacity_factor`, `bess_lifetime_cycles`, the three
`bess_*_fade_pct_y_final` keys, `project_start_year` /
`project_end_year` / `capex_year`, and the
`revenue_breakdown_y1_*` block.

## Implementation map

| Equation | Implementing symbol |
|---|---|
| (E1) | `economics.build_yearly_cashflow` (Year-0 rows) |
| (E2)-(E3) | `build_yearly_cashflow` escalation / `discount_factor` column |
| (E4) | `economics.derive_monthly_cashflow` (end-of-month exponent) |
| (E5) | `lifetime._pv_factor` (mirrored inline in `build_yearly_cashflow`) |
| (E6)-(E7) | `lifetime._bess_factor` + the cumulative-cycles loop in `build_yearly_cashflow` / `lifetime.build_lifetime_dispatch` |
| (E8) | `availability.availability_factor`, `availability.apply_unavailability_derate` |
| (E8a) | `availability.apply_unavailability_derate` (grid-import downtime correction) |
| (E9)-(E12) | `build_yearly_cashflow` stream loop |
| (E13) | `build_yearly_cashflow` fee clamp |
| (E13b) | `build_yearly_cashflow` balancing-aggregator (BSP) fee clamp |
| (E13c)-(E13d) | `build_yearly_cashflow` structural market-access fees (route-to-market / optimizer share) |
| (E14)-(E15) | `build_yearly_cashflow` OPEX/replacement/net rows |
| (E16)-(E19) | `economics.compute_financial_kpis`, `economics.calculate_irr`, `economics._payback_year` |
| (E20) | `economics._amortization_schedule`, `_leverage_kpis`, `build_debt_schedule` |
| (E21)-(E23) | `compute_financial_kpis` LCOE/LCOS block |
| (E24) | `economics._escalation_series` (all six escalation sites in `build_yearly_cashflow`) |
| (E24a) | `economics._opex_escalation_series` (cashflow OPEX row + LCOE/LCOS OPEX numerators) |
| (E25) | `economics._contract_phase` |
| (E25a) | `build_yearly_cashflow` bess_market_revenue_eur column |
| (E26) | `build_model` grid-charge wedge; `kpis.add_economic_columns` fee column |
| (E27) | `build_yearly_cashflow` grid_charging_fee_eur column + monthly allocation |
| (E28)-(E28a) | `build_yearly_cashflow` imbalance_cost_eur column + PV-shape monthly allocation |
| (E29)-(E29a) | `build_yearly_cashflow` toll_revenue_eur column + per-year merchant gating |
| (E30)-(E30a) | `build_yearly_cashflow` optimizer fee/top-up pair; `sensitivity._scale_revenue` econ-threaded kink recompute |
| (E31)-(E31a) | `build_yearly_cashflow` state_support_eur / state_support_clawback_eur pair + repayment-year flag |
| (E32) | `build_yearly_cashflow` capacity_market_revenue_eur column (computed before the E31a netting) |
| (E33) | `build_yearly_cashflow` revenue_levy_eur clamp + fee-share monthly allocation |
| (E34)-(E38) | `economics.apply_tax_layer` (straight-line tranches, EBITDA, FIFO carry-forward, tax clamp, post-tax family) |
| (E39) | `compute_financial_kpis` post-tax KPI block (NaN-gated on the rate) |
| (E40)-(E40a) | `economics._amortization_schedule` sculpted branch + implied-DSCR closed form |
| (E41)-(E42) | `economics.size_debt` closed-form debt capacity per repayment profile |
| (E43) | `economics.size_debt` outlay cap + `resolve_debt_sizing` frozen-debt resolution; sizing KPI family in `compute_financial_kpis` |
| (E44) | `lender.apply_production_case` per-column haircut + `lender.build_lender_cases` case table |
| (E45) | `economics.build_yearly_cashflow` baseload PPA no-fade / no-reversion branch |
| (E46) | `optimization.build_model` → `CYC_ANNUAL` (Year-1 basis) |
| (E47) | `lifetime.warranty_cycle_utilisation`; degradation-sheet columns + pipeline reset warning |
| (E48) | `availability.apply_curtailment_derate` (export-side scaling; composed via `apply_operating_derates`) |
| (E49) | `apply_curtailment_derate` compensation term + `build_yearly_cashflow` curtailment_compensation_eur column |
| (E50) | `lifetime.bess_capacity_factors_pooled` (per-pool fade + nameplate clamp; delegates when inactive) |
| (E51) | `build_yearly_cashflow` augmentation_capex_eur column + LCOS numerator events + `apply_tax_layer` tranches |
| (E52) | `build_yearly_cashflow` Year-0 BESS CAPEX x (1 + ob); LCOS / depreciation bases |
| (E53) | `pipeline._run_midlife_resolve` (+ `lifetime.factors_for_year`); io writers' conditional sheet/section |
| (E54) | `build_yearly_cashflow` go_revenue_eur column (PV-fade volume, flat price); fee-exempt / LCOE-excluded |
| (E55) | `ppa.compute_support_settlement` volume-weighted monthly reference price |
| (E56) | `ppa.compute_support_settlement` premium; `build_yearly_cashflow` support_settlement_eur projection; `sensitivity` SupportStrike driver |
| (E57) | eligibility via `ppa.negative_price_mask` (shared strict p < 0 classifier) |
| (E58) | `build_yearly_cashflow` intraday_revenue_eur row (per-origin fade x id_inflation_pct; E13d base extension; E25a membership) |
| (E59) | `build_yearly_cashflow` intraday_fee_eur row (flat venue rate on per-origin fading volume); `sensitivity._scale_revenue` no-scale decision |
| aggregates table | `kpis.add_economic_columns`, `kpis._compute_canonical_revenue_aggregates` |

## Validation & tests

* Year convention & calendar mapping:
  `tests/test_year0_convention.py`, `tests/test_economics.py`.
* Stream escalation, fee scope and clamp, balancing/PPA exemption:
  `tests/test_economics_retail_dam_split.py`,
  `tests/test_financial_kpis_balancing.py`,
  `tests/test_ppa_engine.py` (cent-level locks),
  `tests/test_monthly_discounting_conventions.py`.
* Monthly/yearly reconciliation incl. replacement year:
  `tests/test_monthly_cashflow_reconciliation.py`.
* Degradation factors & cycle fade:
  `tests/test_bess_degradation_cycle.py`, `tests/test_lifetime.py`,
  `tests/test_degradation.py`.
* LCOE/LCOS exclusions: `tests/test_lcoe_lcos_summary.py`,
  `tests/test_site_lump_sum_costs.py`,
  `tests/test_site_lump_sums_cent_level.py`,
  `tests/test_balancing_lifetime_cashflow.py`.
* IRR/NPV/payback: `tests/test_financial_kpis.py`,
  `tests/test_financial_reference.py` (independent reference
  implementation), `tests/test_cumulative_payback_dedup.py`,
  `tests/test_economic_model_acceptance.py`.
* Debt layer: `tests/test_finance_leverage.py`.
* Availability single-application:
  `tests/test_unavailability_derate_symmetry.py`.
* Emissions/CFE: `tests/test_emissions_cfe.py`.

## Worked example

2-year project, $\rho = 10\%$: Year 0 CAPEX+DEVEX = −1000.
Year-1 bases: retail 300 (PV-origin), DAM 200 (PV-origin), fee
$\varphi = 10\%$, $i_{\mathrm{ret}} = 2\%$, $i_{\mathrm{DAM}} = 0$,
$d_1 = 2\%$ (no annual term), OPEX −50 at $i_{\mathrm{opex}} = 0$.

Year 1: gross = 500, fee = −50, revenue_eur = 450, CF₁ = 400,
DCF₁ = 400/1.1 = 363.64.
Year 2: $f^{PV}_2 = 0.98$; retail = 300·0.98·1.02 = 299.88; DAM =
200·0.98 = 196; gross = 495.88; fee = −49.59; CF₂ = 396.29; DCF₂ =
396.29/1.21 = 327.51.  NPV = −1000 + 363.64 + 327.51 = −308.85.
Cumulative CF: −1000, −600, −203.71.  There is no crossing, so
payback = NaN (Eq. E19).  IRR solves $-1000 + 400/(1+x) + 396.29/(1+x)^2 = 0$ →
$x \approx -13.95\%$ (the positive root $u = 1/(1+x) \approx 1.162$ of
$396.29\,u^2 + 400\,u - 1000 = 0$).

## Assumptions & limitations

* Years 2..N are analytic projections of the Year-1 dispatch: no
  re-optimization against degraded capacity or evolved prices;
  per-stream inflation indices are deterministic single rates.
* The cycle-fade term uses the Year-1 cycle count scaled by the fade
  curve itself (Eq. E7), not a re-simulated dispatch.
* The aggregator fee is a single project-level rate; no per-stream
  fee schedules.
* Debt sizes on gearing × Year-0 outlay only (no DSCR-sculpted
  sizing); interest during construction is not modelled.
* The tax layer (Eqs. E34-E38) models straight-line depreciation,
  interest deductibility and FIFO loss carry-forward only — no
  deferred tax, no VAT, no working capital, no interest during
  construction, no terminal book-value write-off (a replacement
  tranche truncates at the horizon, understating the shield of a late
  replacement), and the post-tax project cashflow discounts at the
  single WACC (the levered interest shield mixes capital-structure
  effects into project NPV; collapses to unlevered at
  ``gearing_pct = 0``).  The clawback and tax layers read the
  deterministic analytic projection, not per-seed Monte Carlo paths.
* LCOE/LCOS exclude site-wide lump sums and all revenue, because they
  are comparability metrics, not project-cost accounting.

## References

* Lazard, *Levelized Cost of Energy v17* (2024) and *Levelized Cost
  of Storage v9* (2024): benchmark bands and LCOE/LCOS scoping.
* IRENA, *Renewable Power Generation Costs in 2023* (2024): default
  cost levels.
* NREL ATB 2024: availability benchmark (~99 % fixed-tilt PV).
* `pvbess_opt/conventions.md`: stream-bundling, fee, lifetime-scope
  and derate contracts.
* `docs/ppa_design.md`, `docs/balancing_market_design.md`: stream
  origins.
