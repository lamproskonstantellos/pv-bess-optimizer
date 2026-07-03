# PPA contract engine — design

Domain design document for the pay-as-produced power purchase
agreement: contract structure, physical vs CfD settlement, the
dispatch treatment, financial wiring, and validation.  Notation
follows the shared table in `docs/README.md`.

## Purpose & scope

Maps the PPA modelling concepts of two reference products — Energy
Exemplar PLEXOS and Gridcog — onto the knobs this package implements,
and records the design decisions (structure, settlement, dispatch
treatment, fee and LCOE scope) with their rationale.

* **Gridcog** models commercial arrangements as *contract overlays* on
  physically simulated flows: pay-as-produced offtake, baseload
  (shaped) profiles, behind-the-meter PPAs, sleeved (physical) versus
  financial (CfD / virtual) settlement, fixed or indexed prices, on
  top of spot-exposed dispatch.  (Public blog material; full articles
  sit behind an access wall.)
* **PLEXOS** models PPAs/CfDs as *Financial Contract* objects settled
  against the simulated pool price; contract position can also feed
  bidding behaviour in its game-theoretic modes.  (Public product
  material; the help portal is not publicly accessible.)
* **CfD design literature** (Florence School of Regulation, Oxford
  Institute for Energy Studies, the production-decoupling strand)
  documents the standard two-way payoff and the known dispatch
  distortion of *generation-settled* CfDs: a contract settled on
  metered output keeps the covered volume generating through
  negative-price hours.

The engine implements **pay-as-produced on a share of PV export**,
with physical or two-way-CfD settlement.  Baseload is designed but
not implemented (see Assumptions & limitations).

## Inputs

A dedicated `ppa` sheet mirrors the `balancing` master-switch
pattern (7 keys; shipped disabled):

| Key | Default | Role |
|---|---|---|
| `ppa_enabled` | FALSE | master switch — disabled runs are bit-identical to a build without the feature |
| `ppa_structure` | `pay_as_produced` | `baseload` parses (old workbooks load) but validation rejects it with guidance |
| `ppa_settlement` | `physical` | `physical` (sleeved) or `cfd` (two-way) |
| `ppa_price_eur_per_mwh` | 65.0 | strike $\pi^{\mathrm{PPA}}$ on the covered volume |
| `ppa_volume_share_pct` | 100.0 | covered share $s$ of PV **export**, pro-rata per step |
| `ppa_term_years` | 10 | operating years 1..$T^{\mathrm{PPA}}$ under contract |
| `ppa_inflation_pct` | 0.0 | yearly strike indexation $(1+i_{\mathrm{PPA}})^{y-1}$ |

Validation (`io._validate_ppa_config`, active only when enabled):
share in [0, 100]; price non-negative; term ≥ 1; enums checked;
`baseload` rejected with guidance.  The YAML/JSON config accepts the
same section and the scenarios engine accepts `ppa.<key>` dotted
targets (`tests/test_input_surface_parity.py`).

## Mathematical formulation

### Contract basis

The contract covers the share $s$ of **actual PV export** $x^{pg}_t$,
per step, for $T^{\mathrm{PPA}}$ operating years:

* The basis is PV *export*, not generation: self-consumed PV settles
  at the retail tariff and is not offtake volume.  BESS export is not
  covered — this is a PV offtake contract (a BESS toll is a different
  instrument).
* The share applies pro-rata per step; a first-x-MW tranche would
  need a per-step ordering rule and is out of scope.
* The contract applies in both regulatory modes (in
  `self_consumption` it covers the surplus export).

### Settlement decomposition (per step)

Physical (sleeved) — the covered volume is paid the strike and never
touches the DAM:

$$\mathrm{revenue\_pv\_ppa\_eur}_t = s\, \frac{x^{pg}_t}{1000}\, \pi^{\mathrm{PPA}}, \qquad
\mathrm{profit\_export\_from\_pv\_eur}_t = (1-s)\, \frac{x^{pg}_t}{1000}\, \pi^{\mathrm{DAM}}_t \tag{P1}$$

CfD (virtual / financial) — all PV export sells at DAM; the covered
volume adds a two-way difference leg, negative whenever the DAM
exceeds the strike:

$$\mathrm{revenue\_pv\_ppa\_eur}_t = s\, \frac{x^{pg}_t}{1000} \left(\pi^{\mathrm{PPA}} - \pi^{\mathrm{DAM}}_t\right), \qquad
\mathrm{profit\_export\_from\_pv\_eur}_t = \frac{x^{pg}_t}{1000}\, \pi^{\mathrm{DAM}}_t \tag{P2}$$

Both settlements pay $s \cdot E \cdot \pi^{\mathrm{PPA}}$ on the
covered volume in total — the standard sleeved-PPA ⇔ two-way-CfD
equivalence on metered export — so the dispatch problem is identical
and only the revenue decomposition differs.  The counterfactual market
value of the covered volume is carried alongside for the multi-year
cashflow's post-term reversion:

$$\mathrm{ppa\_covered\_dam\_value\_eur}_t = s\, \frac{x^{pg}_t}{1000}\, \pi^{\mathrm{DAM}}_t \tag{P3}$$

### Dispatch treatment

The PV-export term of the MILP objective prices each step at the
PPA-adjusted export price (identical for both settlements):

$$p^{\mathrm{eff}}_t = (1-s)\, \pi^{\mathrm{DAM}}_t + s\, \pi^{\mathrm{PPA}} \tag{P4}$$

Consequences:

* In **negative-DAM hours** the uncovered share curtails (merchant
  rational) while the covered share keeps exporting as long as
  $p^{\mathrm{eff}}_t > 0$ — exactly the documented behaviour of
  as-produced, generation-settled contracts.  Deliberate: the engine
  models the contract the user signed, distortion included.
* Storage arbitrage and curtailment decisions see the contract price,
  so a high strike shifts PV-vs-BESS export priority under binding
  injection caps.

## Settlement & cashflow equations

The yearly cashflow's `ppa_revenue_eur` column implements Eq. (E12)
(`docs/economics_design.md`): within the term the strike leg
escalates at $i_{\mathrm{PPA}}$ on the PV fade curve; the CfD's DAM
leg at $i_{\mathrm{DAM}}$; after the term the stream is zero and —
for physical settlement — the covered volume's DAM value (Eq. P3
base) rejoins the DAM revenue stream, where the aggregator fee
applies to it like any other market revenue:

$$R^{\mathrm{PPA}}_y = \begin{cases}
S_1 f^{PV}_y (1+i_{\mathrm{PPA}})^{y-1} & \text{physical, } y \le T^{\mathrm{PPA}} \\
S_1 f^{PV}_y (1+i_{\mathrm{PPA}})^{y-1} - V^{\mathrm{cov}}_1 f^{PV}_y (1+i_{\mathrm{DAM}})^{y-1} & \text{cfd, } y \le T^{\mathrm{PPA}} \\
0 \;\left[+ V^{\mathrm{cov}}_1 f^{PV}_y (1+i_{\mathrm{DAM}})^{y-1} \text{ into the DAM stream, physical}\right] & y > T^{\mathrm{PPA}}
\end{cases} \tag{P5}$$

with $S_1$ the Year-1 strike-leg value (under CfD reconstructed as
contract leg + covered DAM value) and $V^{\mathrm{cov}}_1$ the Year-1
covered DAM value.  `ppa_inflation_pct` is the contract's own
indexation knob — deliberately independent of `retail_inflation_pct`
(CPI-linked tariffs) and `dam_inflation_pct` (wholesale view).

Scope rules (one scope across every consumer —
`pvbess_opt/conventions.md` "PPA stream scope"):

* **Aggregator fee** — the energy-aggregator fee is NOT applied to PPA
  revenue while under contract: a bilateral offtake settles directly with
  the offtaker.  PPA carries neither the energy-aggregator fee nor the
  optional balancing-aggregator (BSP) fee — only balancing revenue may
  carry the latter.  The energy fee continues to apply to DAM/retail
  market revenue, including the post-term reverted volume.
* **LCOE/LCOS** — unchanged: Lazard-style cost-per-MWh metrics are
  revenue-agnostic, so the PPA (like balancing revenue) never enters
  them.
* **Lifetime frame** — both per-step columns are PV-origin
  (`lifetime._PV_REVENUE_COLUMNS`, scale on $f^{PV}_y$) and stay out
  of the frame's `revenue_eur_dam_retail` (per-step DAM+retail scope).
* **Monthly cashflow** — `ppa_revenue_eur` allocates to months by the
  Year-1 monthly |contract-leg| magnitude (flat 1/12 fallback);
  monthly nets reconcile to the yearly rows exactly.

## KPI definitions

* `revenue_pv_ppa_eur` — the **ninth canonical revenue aggregate**
  (Year-1 sum of the per-step contract leg, availability-derated);
  `profit_total_eur` includes it.
* `ppa_covered_dam_value_eur` — Year-1 sum of Eq. (P3), derated, the
  post-term reversion base.
* `total_ppa_revenue_eur_lifecycle` — lifecycle sum of the cashflow
  column.
* Both per-step columns are members of `kpis.ECONOMIC_COLUMNS` and
  the availability-derate list (derate exactly once); they are
  written only when a contract is active, keeping disabled runs
  bit-identical.

## Implementation map

| Equation | Implementing symbol |
|---|---|
| config resolution | `ppa.PpaConfig`, `ppa.resolve_ppa_config` (`active` = enabled ∧ share>0 ∧ term≥1) |
| (P1)–(P3) | `kpis.add_economic_columns` PPA branch |
| (P4) | `optimization.build_model` PV-export price (`pv_export_price`) |
| (P5) | `economics.build_yearly_cashflow` PPA rows (term cutoff + physical post-term reversion) |
| ninth aggregate | `kpis.compute_kpis` (direct column sum; see `kpis._compute_canonical_revenue_aggregates` docstring) |
| derate membership | `availability._BASE_DERATED_KEYS` |
| lifetime scaling | `lifetime._PV_REVENUE_COLUMNS` |
| monthly allocation | `economics.derive_monthly_cashflow` PPA share block |
| PpaPrice tornado | `sensitivity.run_sensitivity_analysis` PpaPrice branch |
| validation | `io._validate_ppa_config`; baseload rejection `io.py` (`_ALLOWED_VALUES['ppa_structure']`) |
| plots | PPA bar in the yearly revenue stack + lifecycle stack; `PPA price` tornado driver (`docs/source/users.guide/financial_plots.rst`) |

## Validation & tests

* `tests/test_ppa_engine.py` — cent-level locks on Eqs. (P1)–(P5):
  settlement decompositions, equivalence of total covered value,
  term cutoff, post-term reversion, fee exemption, escalation.
* `tests/test_ppa_surface.py` — workbook/YAML/scenario surface,
  baseload rejection with guidance, knob validation,
  `test_disabled_ppa_run_is_numerically_identical` (the bit-identity
  lock), revenue-stack bar reconciliation, negative-stack rendering,
  `test_ppa_price_tornado_driver_present_and_monotonic` /
  `test_ppa_price_driver_absent_when_disabled`,
  SUMMARY.md row gating.
* `tests/test_input_surface_parity.py` — `ppa.<key>` dotted targets
  resolve on both scenario surfaces; the disabled
  "Merchant hybrid + PPA" example ships in the workbook scenarios
  sheet and `examples/scenarios.yaml`.
* `tests/test_lcoe_lcos_summary.py`, `tests/test_devex_availability_fees.py` —
  LCOE/LCOS exclusion holds with the contract on.

## Worked example

One step, $x^{pg} = 1000$ kWh, $\pi^{\mathrm{DAM}} = 80$,
$\pi^{\mathrm{PPA}} = 65$, $s = 0.8$:

* Physical (P1): contract leg $0.8 \cdot 1 \cdot 65 = 52$ EUR; market
  leg $0.2 \cdot 1 \cdot 80 = 16$ EUR; total 68 EUR.
* CfD (P2): market leg $1 \cdot 80 = 80$ EUR; difference leg
  $0.8 \cdot (65 - 80) = -12$ EUR; total 68 EUR — identical, as the
  equivalence requires; the CfD leg is negative because DAM > strike.
* Covered DAM value (P3): $0.8 \cdot 80 = 64$ EUR — under physical
  settlement this is what reverts to the DAM stream after the term.
* Dispatch price (P4): $0.2 \cdot 80 + 0.8 \cdot 65 = 68$ EUR/MWh.

## Assumptions & limitations

* **Baseload / fixed-volume profiles: designed, not implemented.**  A
  baseload (shaped) PPA settles a fixed hourly volume against
  actuals, which needs shortfall pricing (buy deficit at spot, sell
  excess at spot) and, done honestly, a dispatch incentive to firm
  the profile with the BESS.  That is a contract-vs-physics feature
  of its own; the enum reserves `ppa_structure = 'baseload'` as a
  rejected-with-guidance value.
* Negative-price suspension clauses (payments paused while DAM < 0)
  and deemed-volume / production-decoupled CfDs change the dispatch
  incentive and are left as follow-ups.
* The Year-1 dispatch is optimised under the contract and Years 2..N
  reuse its shape (the analytic projection), so a contract expiring
  mid-horizon does not re-shape post-term *physical* dispatch — only
  the cashflow reverts the covered volume to DAM value.
* Single offtaker, single strike; no volume floors/caps or
  availability guarantees.

## References

* Gridcog public material on offtake-contract overlays;
  Energy Exemplar PLEXOS public product material on Financial
  Contract objects.
* Florence School of Regulation / Oxford Institute for Energy Studies
  CfD design literature (two-way payoffs; generation-settled
  distortion).
* `docs/economics_design.md` (Eq. E12), `docs/README.md` (notation),
  `pvbess_opt/conventions.md` ("PPA stream scope").
