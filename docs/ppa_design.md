# PPA contract engine — design note

This note maps the PPA modelling concepts of two reference products —
Energy Exemplar PLEXOS and Gridcog — onto the knobs this package
implements, and records the design decisions (structure, settlement,
dispatch treatment, fee and LCOE scope) with their rationale.

## Sources and their limits

* **Gridcog** models commercial arrangements as *contract overlays* on
  the physically simulated flows: pay-as-produced / as-generated
  offtake, baseload (shaped) profiles, behind-the-meter PPAs, and
  sleeved (physical) versus financial (CfD / virtual) settlement, with
  fixed or indexed prices, on top of spot-exposed dispatch.  Public
  blog posts ("How to Model Offtake Contracts and Subsidies for
  Co-located Renewable Projects", "Modelling a Behind-the-Meter Power
  Purchase Agreement", "Modelling a Baseload or Shaped PPA",
  "Five kinds of solar curtailment") describe this behaviour; the full
  articles sit behind an access wall, so the characterisation here is
  drawn from the publicly visible material.
* **PLEXOS** models PPAs/CfDs as *Financial Contract* objects settled
  against the simulated pool price (alongside FTRs); contract position
  can also feed bidding behaviour in its game-theoretic modes.  The
  Energy Exemplar help portal is not publicly accessible, so this
  characterisation likewise relies on public product material.
* **CfD design literature** (Florence School of Regulation, Oxford
  Institute for Energy Studies, and the production-decoupling strand of
  the academic literature) documents the standard two-way CfD payoff
  and the known dispatch distortion of *generation-settled* CfDs: a
  contract settled on metered output keeps the covered volume
  generating through negative-price hours.

## What is implemented

### Structure: pay-as-produced on a share of PV export

The contract covers a configurable share ``s = ppa_volume_share_pct /
100`` of the **actual PV export** (``pv_to_grid``), per step, for
``ppa_term_years`` operating years at ``ppa_price_eur_per_mwh`` (the
strike), escalated by ``ppa_inflation_pct`` per ``(1 + i)^(y - 1)``.

* The basis is PV **export**, not PV generation: self-consumed PV is
  settled at the retail tariff and is not offtake volume.  BESS export
  is not covered — this is a PV offtake contract (a BESS toll is a
  different instrument).
* The share applies pro-rata per step.  A first-x-MW tranche structure
  would need a per-step ordering rule and is out of scope.
* The contract applies in both regulatory modes (in
  ``self_consumption`` it covers the surplus export).

### Settlement: physical or two-way CfD

``ppa_settlement`` selects the reporting decomposition:

* ``physical`` (sleeved): the covered volume is paid the strike and
  never touches the DAM.  Per step::

      revenue_pv_ppa_eur        = s · pv_to_grid/1000 · strike
      profit_export_from_pv_eur = (1-s) · pv_to_grid/1000 · DAM

* ``cfd`` (virtual / financial): all PV export sells at DAM; the
  covered volume adds a two-way contract-for-difference leg that is
  negative whenever DAM exceeds the strike::

      revenue_pv_ppa_eur        = s · pv_to_grid/1000 · (strike − DAM)
      profit_export_from_pv_eur = pv_to_grid/1000 · DAM   (unchanged)

Both settlements yield ``s · E · strike`` on the covered volume in
total — the standard equivalence of a sleeved PPA and a two-way CfD on
metered export — so the dispatch problem is identical (next section)
and only the revenue decomposition differs.  ``ppa_covered_dam_value_eur``
(= ``s · pv_to_grid/1000 · DAM``) is carried alongside as the
counterfactual market value of the covered volume; the multi-year
cashflow needs it to hand the volume back to the DAM stream after the
contract term.

### Dispatch treatment: the covered share enters the MILP objective

The PV-export term of the objective prices each step at::

    p_eff(t) = (1 − s) · DAM(t) + s · strike

which is algebraically the same for both settlements.  Consequences:

* In **negative-DAM hours** the uncovered share curtails (merchant
  rational) while the covered share keeps exporting as long as
  ``p_eff > 0`` — exactly the documented behaviour of as-produced,
  generation-settled contracts.  This is deliberate: the engine models
  the contract the user signed, distortion included.
* Storage arbitrage and curtailment decisions see the contract price,
  so a high strike shifts PV-vs-BESS export priority under binding
  injection caps.

Limitations (documented, not modelled): negative-price suspension
clauses (payments paused while DAM < 0) and deemed-volume /
production-decoupled CfDs change the dispatch incentive and are left as
follow-ups; the Year-1 dispatch is optimised under the contract and
Years 2..N reuse its shape per the fast-mode recipe, so a contract that
expires mid-horizon does not re-shape post-term dispatch (the post-term
cashflow reverts the covered volume to DAM value, but the underlying
physical dispatch stays the Year-1 one).

### Baseload / fixed-volume profiles: designed, not implemented

A baseload (shaped) PPA settles a *fixed* hourly volume against
actuals, which needs shortfall pricing rules (buy the deficit at spot,
sell the excess at spot) and, done honestly, a dispatch incentive to
firm the profile with the BESS.  That is a contract-vs-physics
optimisation feature of its own; the workbook enum reserves
``ppa_structure = 'baseload'`` as a rejected-with-guidance value and
the engine ships ``pay_as_produced`` only.

## Financial wiring (source-of-truth table compliance)

* **Per-step columns** — ``revenue_pv_ppa_eur`` and
  ``ppa_covered_dam_value_eur`` are written by ``add_economic_columns``
  when the contract is enabled (absent otherwise, keeping disabled runs
  bit-identical), and are part of ``kpis.ECONOMIC_COLUMNS``.
* **KPI aggregates** — ``compute_kpis`` emits ``revenue_pv_ppa_eur``
  (the ninth canonical revenue aggregate) and
  ``ppa_covered_dam_value_eur``; ``profit_total_eur`` includes the PPA
  leg.  Both keys join the availability-derate list, so unavailability
  applies exactly once.
* **Lifetime scaling** — both columns are PV-origin
  (``_PV_REVENUE_COLUMNS``) and scale on ``pv_factor``.
* **Cashflow** — ``build_yearly_cashflow`` adds a ``ppa_revenue_eur``
  column: within the term, the strike leg escalates at
  ``ppa_inflation_pct`` and (for CfD) the DAM leg at
  ``dam_inflation_pct``; after the term the stream is zero and, for
  physical settlement, the covered volume's DAM value rejoins the DAM
  revenue stream (where the aggregator fee applies to it like any other
  market revenue).  ``ppa_inflation_pct`` is the contract's own
  indexation knob — it is deliberately independent of
  ``retail_inflation_pct`` (CPI-linked tariffs) and
  ``dam_inflation_pct`` (wholesale view).
* **Aggregator fee** — NOT applied to PPA revenue while under contract:
  a bilateral offtake settles directly with the offtaker, mirroring the
  balancing convention (BSPs settle with the TSO).  The fee continues
  to apply to DAM/retail market revenue, including the post-term
  reverted volume.
* **LCOE/LCOS** — unchanged: Lazard-style cost-per-MWh metrics are
  revenue-agnostic, so the PPA (like balancing revenue) never enters
  them.
* **Monthly cashflow** — ``ppa_revenue_eur`` is allocated to months by
  the Year-1 monthly share of the per-step PPA column (falling back to
  the revenue-share split when the column is absent), and the monthly
  net reconciles to the yearly rows.
* **Sensitivity** — the Revenue tornado driver scales the PPA stream
  together with DAM/retail and balancing (it is Year-1 revenue).

## Workbook surface

A dedicated ``ppa`` sheet mirrors the ``balancing`` master-switch
pattern::

    ppa_enabled              FALSE
    ppa_structure            pay_as_produced
    ppa_settlement           physical | cfd
    ppa_price_eur_per_mwh    65.0
    ppa_volume_share_pct     100.0
    ppa_term_years           10
    ppa_inflation_pct        0.0

Validation (loader): share in [0, 100]; price non-negative; term >= 1
when enabled; enums checked.  The YAML/JSON config accepts the same
section; the scenarios engine accepts ``ppa.<key>`` dotted targets; and
``ppa_enabled = FALSE`` leaves every output numerically identical to a
build without the feature (locked by a regression test).
