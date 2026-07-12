# Uncertainty machinery: design

Domain design document for the three uncertainty pillars: the
rolling-horizon Monte Carlo (imperfect foresight), the balancing-market
Monte Carlo (acceptance / activation / price realisation), and the
one-at-a-time sensitivity analysis.  Notation follows the shared table
in `docs/README.md`.

## Purpose & scope

The perfect-foresight (PF) MILP solves the whole year with exact
knowledge of prices, PV and load.  Real operation commits dispatch
decisions on a market cadence against forecasts.  This layer
quantifies three distinct uncertainty channels:

1. **Foresight risk**: how much of the PF profit survives when
   dispatch is committed window-by-window against noisy forecasts
   (`rolling_horizon.rolling_horizon_dispatch`,
   `rolling_horizon.monte_carlo_rolling`).
2. **Balancing realisation risk**: the deterministic MILP books
   *expected* balancing revenue (probability-weighted); the ex-post
   Monte Carlo realises Bernoulli acceptance/activation and log-normal
   price noise to produce a revenue distribution
   (`rolling_horizon.realise_balancing_scenario`,
   `rolling_horizon.monte_carlo_balancing`).
3. **Parameter risk**: one-at-a-time tornado sweeps of CAPEX, OPEX,
   revenue, discount rate and PPA strike
   (`sensitivity.run_sensitivity_analysis`).

Availability (unplanned outage) is handled separately as a
deterministic post-solve derate (Eq. E8, `docs/economics_design.md`),
applied identically to the PF benchmark and every MC seed.

**Not in scope here — the P90 production lender case.**  The
forecast-noise machinery above models INTRA-year dispatch realism:
what a well-nominated plant loses to imperfect foresight within the
modelled year.  The lender convention of a downside resource year —
the P90 exceedance annual energy — is a distinct, INTER-annual
channel and lives in the economics layer as a deterministic yearly
haircut (`production_p90_factor_pct`, Eq. E44 in
`docs/economics_design.md`): it consumes no U-tag and no rng, scales
the PV-linked cashflow lines only, and can serve as the sizing case
for target-DSCR debt.  Combining the two answers different questions
on purpose: the Monte Carlo distributes Year-1 profit, the P90 case
stresses multi-year debt service.

### Imbalance settlement exposure (Eqs. U6-U9)

`imbalance_enabled` settles each committed block's realised net grid
position against its day-ahead nomination — the noisy lookahead slice
[commit, 2 x commit) of the PREVIOUS window's solve, the only
forecast-based schedule the machinery produces (committed rows are
byte-identical to actuals by design).  Eq. U6 defines the deviation
Delta_t = (realised - nominated)/1000 MWh (block 0 settles at zero);
Eq. U7 the dual-price settlement cost (non-negative under
incentive-compatible long <= DAM <= short); Eq. U8 the sign-indefinite
single-price regime; Eq. U8a the sign-aware DAM proxy used per side
when a price column is absent (short = DAM + (m_s - 1)|DAM|,
long = DAM - (1 - m_l)|DAM|).  A PV-only counterfactual (Eq. U9) is
computed analytically inside the same seed — nominate min(forecast PV,
cap), deliver min(actual PV, cap) — yielding the paired
`bess_imbalance_hedge_value_eur`.  Capture consumes no rng draws, so
seeds reproduce bit-identically with the feature off.  All EUR/MWh
keys carry the standard availability derate (uniform factor; it
cancels in the paired hedge difference — real forced outages
increasing imbalance is a documented limitation).  The MC mean feeds
the yearly cashflow (Eq. E28); P10/P50/P90 carry the distribution.

### NPV tail risk over Monte Carlo seeds (Eqs. U10/U11)

`risk_metrics_enabled` (simulation sheet; FALSE = off, default,
bit-identical) reports the left tail of the NPV distribution over the
rolling-horizon Monte Carlo seeds.  Each seed's realised (derated)
Year-1 profit maps onto an NPV by rescaling every per-stream Year-1
revenue base pro-rata and re-running the analytic cashflow
(`economics.npv_for_year1_revenue`; the pro-rata scale ignores
per-stream composition shifts between seeds — a documented
approximation, acceptable for a tail summary).  The estimators:

$$\mathrm{VaR}_\alpha(\mathrm{NPV}) =
Q_\alpha\big(\{\mathrm{NPV}_s\}\big) \tag{U10}$$

the linear-interpolated empirical $\alpha$-quantile over seeds $s$,
and

$$\mathrm{CVaR}_\alpha(\mathrm{NPV}) =
\mathrm{mean}\{\mathrm{NPV}_s : \mathrm{NPV}_s \le
\mathrm{VaR}_\alpha\} \tag{U11}$$

the expected NPV in the $\alpha$ tail (CVaR <= VaR by construction).
`risk_alpha_pct` sets the tail level (default 5; range (0, 50]).
Outputs: the `risk_metrics` results sheet and `npv_var_eur` /
`npv_cvar_eur` rows in the SUMMARY rolling section, read next to
`mc_n_seeds` — the default 30 seeds give noisy 5 % tails, so raise
`uncertainty_n_seeds` for stable estimates.  When a scenario deck
runs with the flag on, the same estimators are appended to the
scenario-comparison workbook table over the scenarios' `npv_eur`
column (EQUAL WEIGHTS — a scenario list is not a probability
distribution; documented caveat), leaving the comparison plots
untouched.

### Two-stage rolling horizon with the intraday venue (Eq. U12)

With `id_enabled = TRUE` (`docs/intraday_design.md`) the rolling
horizon models the real two-venue information structure: Stage-1
windows commit the day-ahead dispatch under noisy forecasts exactly
as today (with an optional sign-aware log-normal noise on the
intraday auction price — `uncertainty_ida_enabled`,
`uncertainty_sigma_ida`, default 0.15 BELOW $\sigma_{\mathrm{DAM}}$
because intraday commitment happens closer to delivery), and then a
SINGLE annual Stage-2 pass re-dispatches the stitched committed
schedule against the actual (noise-free) intraday prices:

$$\Pi_s = \Pi^{\mathrm{2stage}}\big(g^{DA}_s(\text{noisy}),
\pi^{\mathrm{IDA}}(\text{actual})\big) \tag{U12}$$

one extra solve per seed instead of one per window.  Implementation
notes (all load-bearing):

* the IDA noise draws come from a SPAWNED child generator, so runs
  without the venue keep every DAM/PV/load multiplier bit-identical
  at the same seed;
* the Stage-2 timeseries carries the actual prices but the COMMITTED
  physical envelope (the forecast PV/load the windows dispatched
  against) — re-dispatching against actual PV would make an
  over-forecast commitment physically infeasible, and the residual
  volume error is the imbalance settlement's domain (mutually
  exclusive with the venue in v1);
* the pass honours the same soft year-close SOC pin the final window
  carried, and the cycle caps lift to the committed day's throughput
  where the window seams exceed them (the re-dispatch can never ADD
  cycling beyond the operational cap);
* the foresight benchmark is the TWO-STAGE perfect-foresight profit
  (deterministic Stage 1 + Stage 2, threaded by
  `pipeline._run_one` everywhere `pf_profit_eur` flows), so the U3
  gap compares two-stage against two-stage.  The day-ahead-only
  feasibility argument for a non-negative gap carries over
  approximately: a pathological IDA/DAM divergence can in principle
  reward a noisy commitment beyond the deterministic one because the
  two venues price different commitments — the PF-bound guard's
  tolerance absorbs the observed cases, and the bound is monitored
  by the same strict-mode check;
* `monte_carlo_rolling` appends an `id_net_revenue_eur` per-seed
  column (the imbalance conditional-column pattern — absent without
  the venue);
* Stage-2 determinism against actual IDA prices slightly flatters
  the intraday margin (documented limitation; per-window Stage-2
  noise is a deferred refinement).

## Inputs

| Sheet | Key | Default | Role |
|---|---|---|---|
| simulation | `uncertainty_enabled` | FALSE | master switch for the RH/MC path |
| simulation | `uncertainty_compare_sources` | FALSE | four-ensemble source comparison |
| simulation | `uncertainty_n_seeds` | 30 | $S$ |
| simulation | `uncertainty_window_hours` | 48 | $W$ |
| simulation | `uncertainty_commit_hours` | 24 | $C$ |
| simulation | `uncertainty_dam_enabled` / `_pv_enabled` / `_load_enabled` | TRUE | per-source noise toggles |
| simulation | `uncertainty_sigma_dam` / `_pv` / `_load` | 0.20 / 0.12 / 0.05 | $\sigma_{\mathrm{DAM}}, \sigma_{\mathrm{PV}}, \sigma_{L}$ |
| simulation | `uncertainty_ida_enabled` / `uncertainty_sigma_ida` | TRUE / 0.15 | intraday-price noise toggle and $\sigma_{\mathrm{IDA}}$ (Eq. U12; meaningful only with `id_enabled`) |
| simulation | `uncertainty_diagnostics_enabled` | TRUE | input-uncertainty diagnostic PDFs |
| balancing | `bm_price_sigma_capacity_pct` / `bm_price_sigma_activation_pct` | 25 / 35 | $\sigma^{\mathrm{cap}}, \sigma^{\mathrm{act}}$ (percent → fraction) |
| balancing | `bm_mc_scenarios` / `bm_random_seed` | 200 / 1729 | balancing MC size / seed |
| economics | `sensitivity_enabled` + 5 `sensitivity_*` deltas | TRUE; 10/10/10/2 pp/10 | U6-U9 imbalance settlement | `rolling_horizon.settle_imbalance`, `resolve_imbalance_prices`, nomination capture in `rolling_horizon_dispatch`, MC columns in `monte_carlo_rolling`, pipeline aggregation |
| tornado drivers |
| project | `unavailability_pct` | 1.0 | $a$ |

CLI overrides (merged in `pipeline._resolve_uncertainty_config`; a
flag overrides the workbook value only when supplied):
`--rolling-horizon` (forces `uncertainty_enabled`), `--window-hours`,
`--commit-hours`, `--monte-carlo` (n seeds; `0` = single deterministic
noiseless RH), `--seed` (base seed, default 42),
`--compare-uncertainty-sources`.

## Mathematical formulation

### Forecast noise (unit-mean log-normal)

For an enabled source with sigma $\sigma > 0$, each forecast step
beyond the commit horizon is multiplied by an i.i.d. draw

$$X \sim \mathrm{LogNormal}\!\left(\mu = -\tfrac{\sigma^2}{2},\ \sigma\right) \;\Rightarrow\; \mathbb{E}[X] = 1 \tag{U1}$$

(`rolling_horizon._lognormal_multiplier`) so the forecast is unbiased
in expectation.  Per source (`rolling_horizon.add_forecast_noise`):

* **DAM** is sign-aware: noise multiplies $|\pi^{\mathrm{DAM}}_t|$ and
  the sign is restored, so negative-price hours stay negative.
* **PV** is clipped to $[0, \mathrm{kWp} \cdot \Delta t]$, the physical
  nameplate ceiling per step (clipping at the per-window max would
  bias the realised mean downward; a legacy fallback warns once).
* **Load** is clipped below at 0 and is skipped when the column is
  absent (merchant mode).

Rows $[0, C_{\mathrm{steps}})$ of each window (the committed slice)
are byte-identical to the input; only the lookahead is noisy.

### Rolling-horizon dispatch

With $W_{\mathrm{steps}} = 60W/\mathrm{dt}$ and
$C_{\mathrm{steps}} = 60C/\mathrm{dt}$
(`rolling_horizon._hours_to_steps`), windows start at
$c \in \{0, C_{\mathrm{steps}}, 2C_{\mathrm{steps}}, \dots\}$:

$$\text{solve MILP on } ts[c : c+W_{\mathrm{steps}}] \text{ (noisy beyond } C_{\mathrm{steps}}\text{)}, \quad
\text{commit } [c : c + C_{\mathrm{steps}}), \quad
E^{\mathrm{init}}_{\mathrm{next}} = E_{c + C_{\mathrm{steps}}} \tag{U2}$$

Terminal-SOC handling: every window solves with
`terminal_soc_free=True` (a window must not close its own annual
cycle), **except** that when `terminal_soc_equal` is TRUE the window
reaching the end of the horizon pins its post-final-step SOC to the
year-initial SOC (`terminal_soc_target_kwh`).  The stitched dispatch
then satisfies the same closed-cycle condition as the PF benchmark.
Without it the last window drains the battery for profit the
benchmark cannot take and the foresight gap goes spuriously negative
(`pvbess_opt/conventions.md`).

Realised evaluation (`evaluate_with_actuals=True`, the MC default):
the stitched dispatch is re-priced against the noise-free inputs.
Every column in `rolling_horizon.PRICE_COLUMNS` (DAM, retail, and the
nine balancing price columns) is restored, and the per-step `*_eur`
columns are dropped and re-derived via `kpis.add_economic_columns`,
so KPIs reflect what the schedule actually earned, not what the
solver believed.

KPI scope: `compute_kpis` + `apply_unavailability_derate`, identical
to the pipeline's headline Year-1 path, so PF-vs-RH comparisons are
derate-invariant.

### Monte Carlo ensemble and the foresight gap

Seeds $s_i = \mathrm{base\_seed} + i$, $i = 0..S-1$
(`rolling_horizon.monte_carlo_rolling`).  Per seed:

$$\mathrm{gap}_i = 100\left(1 - \frac{\Pi^{\mathrm{RH}}_i}{\Pi^{\mathrm{PF}}}\right) \;\%, \qquad \Pi^{\mathrm{PF}} = \text{derated PF } \texttt{profit\_total\_eur} \tag{U3}$$

Every seed's stitched dispatch is feasible for the PF MILP (same
constraints incl. the year-close SOC pin), so
$\Pi^{\mathrm{RH}}_i \le \Pi^{\mathrm{PF}}$ up to `mip_gap` slack:
the gap is non-negative within solver tolerance and the PF marker
sits at or above the MC histogram's upper tail.  The slack case is
handled, not tolerated: if any realisation beats the incumbent, the
pipeline re-solves the PF benchmark at 10x tighter gaps (floor
`1e-6`) until it is the best case, recomputes the gap column and
percentiles against the final benchmark, and records the gap used as
`pf_benchmark_mip_gap` (see `pvbess_opt/conventions.md`).  Output
frame: one row per seed (`profit_total_eur`, grid import/export MWh,
curtailed MWh, BESS cycles, `foresight_gap_pct`); the pipeline
reports P10/P50/P90 (`foresight_gap_pct_p50` etc.) and writes the
`rolling_horizon_mc` sheet.

### Four-source comparison

With `uncertainty_compare_sources`, four ensembles run with the noise
toggles set to DAM-only, PV-only, Load-only, and All-combined; each
produces its own gap distribution
(`foresight_gap_pct_p50_dam` / `_pv` / `_load` / `_all`) and the
comparison plot ranks the channels by P50 gap.

### Balancing Monte Carlo

Inputs: the deterministic dispatch's per-product reservation columns
`bm_reservation_<k>_kw` and per-step price columns.  Per scenario and
product $k$ (`rolling_horizon.realise_balancing_scenario`):

$$A_{k,t} \sim \mathrm{Bernoulli}(\alpha_k), \qquad
B_{k,t} \mid A_{k,t} \sim \mathrm{Bernoulli}(\beta_k) \tag{U4}$$

$$R^{\mathrm{cap}}_k = \sum_t A_{k,t}\, r_{k,t}\, \Delta t\, \pi^{\mathrm{cap}}_{k,t}\, \xi^{\mathrm{cap}}_{k,t} / 1000, \qquad
R^{\mathrm{act}}_k = \sum_t A_{k,t} B_{k,t}\, r_{k,t}\, \Delta t\, \pi^{\mathrm{act}}_{k,t}\, \xi^{\mathrm{act}}_{k,t} / 1000 \tag{U5}$$

with $\xi^{\mathrm{cap}}, \xi^{\mathrm{act}}$ unit-mean log-normal
price multipliers (Eq. U1 with $\sigma^{\mathrm{cap}}$,
$\sigma^{\mathrm{act}}$).  FCR earns capacity only.  In expectation
Eq. (U5) reproduces the MILP's expected-revenue objective terms
(Eqs. B7-B8 in `docs/balancing_market_design.md`).

**SOC coupling**: the same activation draws $B_{k,t}$ that price the
revenue also drive the realised SOC excursion
($-B\,r\,\Delta t/\eta_d$ for up-products,
$+B\,r\,\Delta t\,\eta_c$ for down-products) around the scheduled
trajectory; a scenario whose excursion violates the SOC bounds
(±10⁻⁶ kWh tolerance) is flagged, and the pipeline reports the
violating share (as a **percent** of scenarios) as
`bm_soc_constrained_scenarios_pct`.  Revenue and SOC views of one
scenario are bit-consistent by construction.

Outputs (`rolling_horizon.monte_carlo_balancing`):
`bm_total_balancing_revenue_p10/p50/p90_eur` (total realised revenue
quantiles), per-product `bm_<product>_capacity_revenue_p10/p50/p90_eur`
and `bm_<product>_activation_revenue_p10/p50/p90_eur` breakdowns,
`bm_soc_constrained_scenarios_pct` (the SOC-violation share), and
`bm_mc_total_realised_eur` (the raw realisations for the histogram
plot); every figure is scaled by the same availability factor as the
deterministic `bm_*` KPIs so P50 is comparable with
`bm_total_balancing_revenue_eur`.

### Sensitivity analysis (one-at-a-time tornado)

Gate: `sensitivity_enabled`; drivers with delta ≤ 0 are dropped.  Each
driver perturbs the yearly cashflow by ±δ and recomputes the financial
KPIs (`sensitivity.run_sensitivity_analysis`):

| Driver | δ key | Transformation |
|---|---|---|
| CAPEX | `sensitivity_capex_delta_pct` | scales every `capex_eur` and `devex_eur` row: Year-0 outlay AND the scheduled BESS replacement (`_scale_capex`) |
| OPEX | `sensitivity_opex_delta_pct` | scales `opex_eur` (`_scale_opex`) |
| Revenue | `sensitivity_revenue_delta_pct` | recovers the true per-year gross as `revenue_eur + |aggregator_fee_eur|` (exact in both fee-applied and fee-clamped years), scales it, re-derives the fee with the same inferred fraction (`_infer_aggregator_fee_frac`) and clamp, re-splits per stream; balancing and PPA columns scale directly with the driver, and the optional `balancing_aggregator_fee_eur` column scales with them so the BSP fee stays in sync with gross balancing revenue (`_scale_revenue`) |
| DiscountRate | `sensitivity_discount_rate_delta_pp` (absolute pp) | rebuilds discounting at $\rho \pm \delta$; NPV-only by construction: IRR/payback are rate-independent (`_rebuild_with_discount_rate`) |
| PpaPrice | `sensitivity_ppa_price_delta_pct` | active only when the contract is on, the strike > 0 and the Year-1 strike-leg value is nonzero; rescales the strike-leg base by ±δ (physical: contract leg; CfD: leg + covered DAM value, the strike part), rebuilds the FULL yearly cashflow from the rescaled KPI bases so term/reversion/escalation stay exact |

Contracted-revenue interaction (Eqs. E29-E32, no new U-tags —
behaviour, not a new equation): fixed contracted streams
(`toll_revenue_eur`, `state_support_eur`,
`capacity_market_revenue_eur`) do **not** scale with the Revenue
driver — the driver perturbs market prices, which those payments are
contractually insulated from — while the piecewise contract terms are
recomputed exactly instead of constant-scaled: the optimizer
floor+share pair from the scaled margin base against the un-scaled
floor (`_scale_revenue`'s optional `econ` parameter), and the
state-support netting from the scaled market base against the
un-scaled threshold.  Tornado bars are therefore asymmetric around
contract kinks (the E30 floor) and **shrink as the contracted share of
revenue rises** — the two-way netting is revenue-stabilising, fully
absorbing the market component at a 100 % netting share.

NPV tornado shows all active drivers; the IRR tornado drops
DiscountRate (`variables_for_irr_sensitivity`).  Output: an 11-column
tidy frame written to the `sensitivity_analysis` sheet with columns
`variable`, `label`, `scenario` (base/low/high), `delta_value`,
`value`, `npv_eur`, `irr_pct`, `payback_years`, `delta_npv_eur`,
`delta_irr_pp`, `delta_payback_years`.  (The frame itself carries no
LCOE/LCOS columns; the LCOE/LCOS tornado plot instead derives its
ranges from the exported `lcoe_disc_*` / `lcos_disc_*` discounted
components, per the Eq. E21-E22 note, rather than a multiplicative
approximation.)

### TaxRate driver (post-tax tornado columns)

`sensitivity_tax_rate_delta_pp` (economics sheet; default 5 pp) arms
a TaxRate driver whenever the tax layer is on
(`corporate_tax_rate_pct` > 0).  Taxes are NONLINEAR (the
taxable-base clamp, the loss carry-forward), so each leg is a full
cashflow + tax-layer rebuild at the shifted statutory rate — never a
scaled copy — and the driver reports its deltas in dedicated
POST-TAX columns (`npv_post_tax_eur`, `delta_npv_post_tax_eur`,
`irr_post_tax_pct`, `delta_irr_post_tax_pp`) that join the
sensitivity frame only when the driver ran.  Its pre-tax metric
columns stay NaN, so the pre-tax tornado layouts skip the driver and
the published pre-tax tornado is untouched.  The companion opt-in
figure element: the cumulative-cashflow chart draws a dashed
`Cumulative discounted cash-flow (post-tax)` line only while the
rate is positive (zero-rate figures stay bit-identical).

## Settlement & cashflow equations

The uncertainty layer does not alter the cashflow algebra
(`docs/economics_design.md`): the RH MC reports Year-1 profit
distributions next to the deterministic projection; the balancing MC
distribution contextualises the expected-value `balancing_revenue_eur`
column; sensitivity rebuilds the same cashflow under perturbed
parameters.

## KPI definitions

* `foresight_gap_pct` per seed (Eq. U3); pipeline aggregates
  `foresight_gap_pct_p10/p50/p90` and, in comparison mode, the four
  `foresight_gap_pct_p50_<source>` keys, plus the run metadata
  `mc_n_seeds` / `mc_window_hours` / `mc_commit_hours`.
* `bm_total_balancing_revenue_p10/p50/p90_eur`,
  `bm_soc_constrained_scenarios_pct`, `bm_mc_total_realised_eur` (raw
  realisations), and per-product
  `bm_<product>_{capacity,activation}_revenue_p10/p50/p90_eur`.
* The sensitivity sheet's columns per driver case (above).

## Implementation map

| Equation | Implementing symbol |
|---|---|
| (U1) | `rolling_horizon._lognormal_multiplier` (forecast noise via `add_forecast_noise`); `_lognormal_unit_mean` is the same unit-mean draw for the balancing-MC price multipliers $\xi$ in (U5) |
| noise per source | `rolling_horizon.add_forecast_noise` |
| (U2) | `rolling_horizon.rolling_horizon_dispatch` (window loop, SOC carry, year-close pin) |
| actuals restore | `rolling_horizon.PRICE_COLUMNS` + `kpis.add_economic_columns` |
| (U3) | `rolling_horizon.monte_carlo_rolling` |
| (U4)-(U5) | `rolling_horizon.realise_balancing_scenario` |
| MC aggregation | `rolling_horizon.monte_carlo_balancing` |
| derate symmetry | `availability.apply_unavailability_derate` at `rolling_horizon_dispatch` and `pipeline._run_one` |
| tornado drivers | `sensitivity.run_sensitivity_analysis`, `_scale_capex`, `_scale_opex`, `_scale_revenue`, `_infer_aggregator_fee_frac`, `_rebuild_with_discount_rate`, `variables_for_npv_sensitivity`, `variables_for_irr_sensitivity` |
| CLI/workbook merge | `pipeline._resolve_uncertainty_config` |
| (U10)-(U11) | `economics.var_cvar` + `economics.npv_for_year1_revenue`; `pipeline._compute_risk_metrics`; scenario-set rows in `scenarios.run_scenarios` |
| (U12) | `rolling_horizon.add_forecast_noise` (spawned-rng IDA block); `rolling_horizon_dispatch` annual Stage-2 pass; two-stage benchmark threading in `pipeline._run_one`; `monte_carlo_rolling` id_net_revenue_eur column |
| TaxRate driver | `sensitivity.variables_for_npv_sensitivity` + the full-rebuild branch in `run_sensitivity_analysis`; post-tax line in `plotting.financial.plot_cumulative_cashflow` |

## Validation & tests

* Noise unbiasedness and sign/clipping:
  `tests/test_forecast_noise_unbiasedness.py`,
  `tests/test_rolling_horizon.py`.
* Window mechanics, SOC carryover, year-close pin, actuals restore:
  `tests/test_rolling_horizon.py`,
  `tests/test_rolling_horizon_price_restore.py`,
  `tests/test_rolling_horizon_realscale.py` (slow lane).
* Derate symmetry / gap derate-invariance:
  `tests/test_unavailability_derate_symmetry.py`,
  `tests/test_rolling_horizon_scope.py`.
* Balancing MC (revenue/SOC coupling, expectation consistency,
  quantiles): `tests/test_balancing_mc.py`,
  `tests/test_balancing_mc_coupling.py`.
* Sensitivity, covering driver mechanics, no-op identity
  (`_scale_revenue(cf, 1.0)` on mixed-sign cashflows), monotonicity,
  revenue identity, and PPA driver gating:
  `tests/test_sensitivity.py`,
  `tests/test_sensitivity_monotonicity.py`,
  `tests/test_sensitivity_revenue_identity.py`,
  `tests/test_ppa_surface.py::test_ppa_price_tornado_driver_present_and_monotonic`,
  `tests/test_ppa_surface.py::test_ppa_price_driver_absent_when_disabled`.
* Config resolution (CLI vs workbook):
  `tests/test_uncertainty_config.py`.
* Plot layer: `tests/test_plotting_uncertainty.py`,
  `tests/test_plotting_sensitivity.py`.

## Worked example

Noise: $\sigma_{\mathrm{DAM}} = 0.20$ ⇒ multiplier
$X = e^{Z}$, $Z \sim \mathcal{N}(-0.02, 0.04)$; median
$e^{-0.02} \approx 0.980$, mean 1, P10/P90 ≈ 0.76/1.27.  A 100 EUR/MWh
forecast therefore spreads to roughly [76, 127] one day ahead.  A −50 EUR/MWh
hour perturbs to −50·X (sign preserved).

Gap: PF profit 1 000 000 EUR (derated); a seed realises 962 000 EUR ⇒
$\mathrm{gap} = 100(1 - 0.962) = 3.8\%$.  With $W=48$, $C=24$,
$\Delta t = 0.25$: 192-step windows, 96-step commits, 365 windows per
year-long horizon.

## Assumptions & limitations

* Noise is i.i.d. per step and per source: no temporal
  autocorrelation, no cross-source correlation (a cold dark windless
  week perturbs PV and load independently), no forecast-horizon
  widening within a window beyond the single commit/lookahead split.
* Log-normal multiplicative noise cannot flip a price's sign and
  keeps PV/load non-negative; additive shocks are not modelled.
* Balancing acceptance/activation draws are independent across steps
  and products; activation duration equals one step.
* The balancing MC realises revenue around the *scheduled*
  reservations.  It does not re-dispatch after a rejected bid.
* Sensitivity is one-at-a-time on the analytic cashflow; drivers are
  not re-optimized through the MILP and joint perturbations are out
  of scope.

## References

* `docs/balancing_market_design.md`: the expected-value objective the
  MC realises; `docs/economics_design.md`: cashflow and derate algebra.
* `pvbess_opt/conventions.md`: "Perfect-foresight benchmark and the
  MC ensemble share one scope".
* Conejo, Carrión & Morales, *Decision Making Under Uncertainty in
  Electricity Markets* (2010): rolling-horizon and scenario-based
  framing.
