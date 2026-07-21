# Documentation index

The domain design documents below are the authoritative mathematical
specification of `pvbess_opt`.  Every document follows one template
(Purpose & scope → Inputs → Mathematical formulation → Settlement &
cashflow equations → KPI definitions → Implementation map →
Validation & tests → Worked example → Assumptions & limitations →
References), and every numbered equation cites the implementing
module/symbol, so the documents can be cited directly and verified
against the code.  The Sphinx manual under `docs/source/` (user's
guide + technical pages) links here rather than duplicating the
mathematics.

| Document | One-paragraph summary |
|---|---|
| [`self_consumption_design.md`](self_consumption_design.md) | The `self_consumption` regulatory regime (15-minute netting): decision variables, the profit objective, every hard constraint as a formal statement (load balance, exact PV→load priority, surplus-only export, no simultaneous grid I/O, SOC dynamics and year-close, injection caps), and the ten post-solve audit invariants.  Machine-checked: `tests/test_logic_spec_conformance.py` parses its constraint and invariant headings and asserts each symbol on a built model. (Equations S1-S36.) |
| [`merchant_design.md`](merchant_design.md) | The `merchant` regime: pure utility-scale DAM dispatch with no co-located load.  Covers the three load-flow pinning constraints, which self-consumption constraints are absent vs shared, the cap-basis no-op of `grid_cap_includes_load`, the merchant objective with the PPA-adjusted export price, negative-price behaviour, and the three asset configurations. (Equations M1-M3.) |
| [`balancing_market_design.md`](balancing_market_design.md) | Stochastic FCR/aFRR/mFRR participation: per-product reservations as decision variables, power-budget and SOC-headroom constraints, expected-activation SOC drift, the expected-revenue objective terms, cashflow/fee/LCOE scope, the 36-key workbook surface, and the six balancing invariants, with the full verification and falsification log as an appendix. (Equations B1-B10.) |
| [`ppa_design.md`](ppa_design.md) | The pay-as-produced PPA contract engine: covered share of PV export, physical vs two-way-CfD settlement and their equivalence, the PPA-adjusted dispatch price (1−s)·DAM + s·strike, term cutoff and post-term reversion, fee exemption and LCOE/LCOS exclusion, the baseload band with spot-settled shortfall/excess, the negative-price suspension clause, and the sliding-FiP / two-way-CfD support engine. (Equations P1-P11.) |
| [`intraday_design.md`](intraday_design.md) | The intraday venue: the committed day-ahead position pinned as data, the second-stage re-dispatch of the same MILP, the per-step deviation budget, the per-origin (PV/BESS) trade split, the sell/buy complementarity (no wash trading), the spread-form margin with the venue fee and wear on physical throughput, the four intraday invariants, the fee applicability matrix, and the E58/E59 cashflow stream. (Equations I1-I6.) |
| [`economics_design.md`](economics_design.md) | The project-finance engine: Year-0/Year-y conventions, per-stream escalation and end-of-year/monthly discounting, the nine canonical revenue aggregates, the four route-to-market fee structures (aggregator %, BSP %, per-MWh route-to-market, optimizer share) with their clamps, PV/BESS degradation factors with cycle fade and replacement reset, the contracted-revenue and fiscal blocks (tolling, optimizer floor+share, state support with clawback, capacity market, levy, GO revenue, depreciation + corporate tax), debt sizing and sculpting, NPV/IRR/ROI/BCR/payback, and Lazard-style LCOE/LCOS with their exclusions. (Equations E1-E61.) |
| [`market_scenarios_design.md`](market_scenarios_design.md) | The two opt-in price layers: market-data ingestion (bidding-zone registry, ENTSO-E / ADMIE / HEnEx providers, the intensive-resampling and local-calendar contract, whole-column bypass semantics with provenance and snapshot materialisation) and the multi-year price-scenario layer (per-scenario store schema, the parametric / TYNDP deck adapters, Tier-1 repricing of the frozen Year-1 dispatch, Tier-2 support-year re-solves with degradation-normalised factors, capture-price KPIs, the support-reference rule, and the weighted scenario ensemble on one shared debt sizing). (Equations G1-G7.) |
| [`uncertainty_design.md`](uncertainty_design.md) | The uncertainty machinery: rolling-horizon dispatch with unit-mean log-normal forecast noise, the Monte Carlo ensemble and the foresight gap 100·(1−RH/PF), the four-source comparison, the balancing-revenue Monte Carlo with revenue/SOC coupling, the imbalance settlement, VaR/CVaR tail metrics, the two-stage intraday ensemble, the sensitivity tornado drivers, and the availability derate symmetry. (Equations U1-U12.) |

Also in this directory: [`CHANGELOG.md`](CHANGELOG.md) (release log).
Cross-module lockstep rules live in
[`pvbess_opt/conventions.md`](../pvbess_opt/conventions.md).

## Shared notation

All design documents use these symbols.  Workbook keys are quoted
verbatim in each document's Inputs section.

### Sets and indices

| Symbol | Meaning |
|---|---|
| $t \in \{0,\dots,N-1\}$ | dispatch timestep; canonical workbook: $N = 35\,040$ (one year at 15 min) |
| $\Delta t$ | step length in hours (`dt_minutes/60`, via `timeutils.dt_hours_from`); 0.25 h canonical |
| $d$ | calendar day within the dispatch window |
| $y \in \{1,\dots,Y\}$ | operating year; $Y$ = `project_lifecycle_years`; year 0 = construction |
| $m \in \{1,\dots,12\}$ | calendar month |
| $k \in \mathcal{K}$ | balancing product; $\mathcal{K}$ = {fcr, afrr_up, afrr_dn, mfrr_up, mfrr_dn} |

### Exogenous timeseries

| Symbol | Column / key | Unit |
|---|---|---|
| $G_t$ | `pv_kwh` | kWh/step |
| $L_t$ | `load_kwh` | kWh/step |
| $\pi^{\mathrm{DAM}}_t$ | `dam_price_eur_per_mwh` | EUR/MWh |
| $\pi^{\mathrm{ret}}_t$ | `retail_price_eur_per_mwh` column, else scalar `retail_tariff_eur_per_mwh` | EUR/MWh |
| $\pi^{\mathrm{cap}}_{k,t}$, $\pi^{\mathrm{act}}_{k,t}$ | `<k>_capacity/activation_price_eur_per_mwh` columns (scalar fallbacks on the balancing sheet) | EUR/MWh |

### Decision variables

| Symbol | Pyomo name | Unit |
|---|---|---|
| $x^{pl}_t, x^{pb}_t, x^{pg}_t, x^{pc}_t$ | `pv_to_load`, `pv_to_bess`, `pv_to_grid`, `pv_curtail` | kWh/step |
| $x^{bl}_t, x^{bg}_t$ | `bess_dis_load`, `bess_dis_grid` | kWh/step |
| $x^{gl}_t, x^{gb}_t$ | `grid_to_load`, `grid_to_bess` | kWh/step |
| $E_t$ | `soc` | kWh |
| $u^{c}_t, u^{d}_t, u^{io}_t, z_t$ | `y_charge`, `y_dis`, `y_grid_io`, `z_pv_active` | binary |
| $\sigma_t$ | `slack` (surplus-only export; self-consumption only) | kWh |
| $r_{k,t}$ | `r_balancing` | kW |

### Parameters

| Symbol | Key |
|---|---|
| $\eta_c, \eta_d$ | `efficiency_charge`, `efficiency_discharge` |
| $E^{\mathrm{cap}}$, $P^{B}$ | `bess_capacity_kwh`, `bess_power_kw` (symmetric) |
| $\underline{e}, \overline{e}$ | `soc_min_frac`, `soc_max_frac` |
| $P^{G}$, $\mu_t$ | `p_grid_export_max_kw`, per-step max-injection fraction (24×1 / 24×12 profile) |
| $M_{\mathrm{imp}}, M_{\mathrm{exp}}, M_{\mathrm{ch}}, M_{\mathrm{pv}}$ | tight big-Ms (`optimization.derive_tight_big_m`) |
| $\varepsilon$ | curtailment tie-breaker, $10^{-5}$ EUR/kWh (module-private) |
| $c^{w}$ | `bess_wear_cost_eur_per_mwh` |
| $s_k$, $\alpha_k$, $\beta_k$ | `<k>_capacity_share_pct`/100, `<k>_bid_acceptance_pct`/100, `<k>_activation_probability_pct`/100 |
| $h$, $d_{\mathrm{fcr}}$ | `bm_soc_headroom_pct`/100, `fcr_required_duration_hours` |
| $s$, $\pi^{\mathrm{PPA}}$, $T^{\mathrm{PPA}}$, $i_{\mathrm{PPA}}$ | `ppa_volume_share_pct`/100, `ppa_price_eur_per_mwh`, `ppa_term_years`, `ppa_inflation_pct`/100 |
| $\rho$ | `discount_rate_pct`/100 |
| $i_{\mathrm{opex}}, i_{\mathrm{ret}}, i_{\mathrm{DAM}}, i_{\mathrm{bm}}$ | `opex_inflation_pct`, `retail_inflation_pct`, `dam_inflation_pct`, `bm_inflation_pct` (each /100) |
| $\varphi$, $\varphi_{\mathrm{bm}}$ | `aggregator_fee_pct_revenue`/100 (DAM + retail), `balancing_aggregator_fee_pct_revenue`/100 (optional BSP fee on gross balancing, default 0) |
| $\phi_{\mathrm{rtm}}$, $\varphi_{\mathrm{opt}}$ | `route_to_market_fee_eur_per_mwh` (EUR/MWh on exported energy), `optimizer_revenue_share_pct`/100 (share of the positive BESS trading margin); both default 0 |
| $d_1, d_a$; $d_B, d_c$ | PV year-1 LID / annual degradation; BESS calendar / per-cycle fade (each pct/100) |
| $f^{PV}_y, f^{B}_y$ | degradation factors (`lifetime._pv_factor`, `lifetime._bess_factor`) |
| $a$, $A$ | `unavailability_pct`/100; availability factor $A = 1-a$ |
| $\sigma_{\mathrm{DAM}}, \sigma_{\mathrm{PV}}, \sigma_{L}$ | `uncertainty_sigma_dam` / `_pv` / `_load` |
| $W, C, S$ | `uncertainty_window_hours`, `uncertainty_commit_hours`, `uncertainty_n_seeds` |
| $\sigma^{\mathrm{cap}}, \sigma^{\mathrm{act}}$ | `bm_price_sigma_capacity_pct`/100, `bm_price_sigma_activation_pct`/100 |

### Conventions

Energies in kWh, powers in kW, prices in EUR/MWh.  EUR terms therefore
carry an explicit /1000.  Equations are numbered per document with a
domain prefix: S (self-consumption), M (merchant), B (balancing),
P (PPA), I (intraday), E (economics), U (uncertainty).  Constraint names in SMALL
CAPS (`SOC_DYN`, `BM_POWER_UP`, …) are the literal Pyomo attribute
names on the built model.
