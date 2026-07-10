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
| E | `economics_design.md` | cashflow, fees, KPIs, LCOE/LCOS | E26 (+ suffixed E8a, E13a-E13d) |
| U | `uncertainty_design.md` | forecast noise, Monte Carlo, foresight | U5 |
| P | `ppa_design.md` | PPA settlement and dispatch coupling | P8 |
| S | (reserved) | system/dispatch constraints outside the MILP docs' local numbering | — |
| B | (reserved) | balancing product structure | — |
| I | (reserved) | intraday venue | — |

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
| economics | `benchmark_lco{e,s}_{low,high}_eur_per_mwh` | 30/85, 157/274 | Lazard band overlays (plots only) |
| economics | `sensitivity_*` (5 keys) | 10/10/10/2/10 | tornado deltas (`docs/uncertainty_design.md`) |
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
through two packs need an explicit scheduled strategy.

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

$$\mathrm{CF}_y = \underbrace{\left(R^{\mathrm{ret}}_y + R^{\mathrm{DAM}}_y + F_y\right)}_{\texttt{revenue\_eur}} + \underbrace{R^{\mathrm{bm}}_y}_{\text{gross}} + F^{\mathrm{bm}}_y + F^{\mathrm{rtm}}_y + F^{\mathrm{opt}}_y + R^{\mathrm{PPA}}_y + O_y + C_y + V_y \tag{E15}$$

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
`equity_irr_pct` = IRR of $\mathrm{CF}^{eq}$ and
`min_dscr` $= \min_y \mathrm{CF}_y / s_y$
(`economics._leverage_kpis`; full table via
`economics.build_debt_schedule`).

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
* No tax, depreciation, or working-capital layers.
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
