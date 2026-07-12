Input workbook
==============

The optimiser consumes a single Excel workbook.  Ten core data sheets
(``timeseries``, ``project``, ``pv``, ``bess``, ``economics``,
``balancing``, ``ppa``, ``intraday``, ``simulation``,
``max_injection_profile``)
carry the run, plus the optional per-source sub-cap sheets
(``max_injection_profile_pv`` / ``max_injection_profile_bess``) and two
optional opt-in sheets ``sizing``, ``scenarios`` and ``trajectories``
(each gated by an ``enabled`` toggle and shipped disabled).  All keys
use lowercase snake_case.

Sheet ``timeseries``
--------------------

Per-step data (one row per timestep; the timestep is auto-detected).
The case-study workbook ships at 15-minute cadence (35 040 rows for
one year), matching the 15-minute settlement of the
``self_consumption`` regime.

==============================  =======================  ====================================
Column                          Required                 Notes
==============================  =======================  ====================================
``timestamp``                   yes                      Datetime; regular cadence required.
``pv_kwh``                      column                   PV production per step. The single PV column: leave its cells blank to source the profile from a location instead (see ``pv_source`` on the ``pv`` sheet). The deprecated ``pv_kwh_override`` column is read only as a fallback when ``pv_kwh`` is empty.
``load_kwh``                    self_consumption only    Required when ``mode=self_consumption``.  In
                                                         ``mode=merchant`` the column is
                                                         ignored if present (an INFO log
                                                         message is emitted) and
                                                         ``pv_to_load`` / ``bess_dis_load`` /
                                                         ``grid_to_load`` are pinned to 0.
``dam_price_eur_per_mwh``       no                       Day-ahead price per step.  Negative
                                                         prices are accepted and preserved
                                                         sign-aware in the rolling-horizon
                                                         noise model.
``retail_price_eur_per_mwh``    no                       Time-varying retail tariff.  Falls
                                                         back to the scalar
                                                         ``retail_tariff_eur_per_mwh`` from
                                                         the ``project`` sheet.
Balancing price columns         balancing only           Optional per-step balancing-market
                                                         prices consumed when
                                                         ``balancing_enabled = TRUE``:
                                                         ``fcr_capacity_price_eur_per_mwh``,
                                                         ``{afrr,mfrr}_{up,dn}_capacity_price_eur_per_mwh``
                                                         and
                                                         ``{afrr,mfrr}_{up,dn}_activation_price_eur_per_mwh``
                                                         (see the ``balancing`` sheet
                                                         reference below).
``ida_price_eur_per_mwh``       intraday only            Intraday auction price per step.
                                                         Required when ``id_enabled = TRUE``
                                                         on the ``intraday`` sheet (there is
                                                         deliberately no scalar fallback — a
                                                         constant IDA price would produce
                                                         zero spread and misleading
                                                         results).  Consumed at the workbook
                                                         cadence: on an hourly workbook it
                                                         is the hour-averaged IDA price (an
                                                         INFO log notes the averaging); for
                                                         15-minute IDA granularity resample
                                                         via ``scripts/resample_timeseries.py``.
``curtailment_signal``          no                       Per-step export-availability factor
                                                         in ``[0, 1]`` (0 = export fully
                                                         curtailed, 1 = unrestricted).
                                                         Multiplies the export cap inside the
                                                         optimizer so dispatch adapts to the
                                                         restriction.  Mutually exclusive
                                                         with ``curtailment_pct`` on the
                                                         ``project`` sheet.
==============================  =======================  ====================================

Sheet ``project``
-----------------

High-level run configuration:

* ``project_lifecycle_years``: total project horizon (years).
* ``project_start_year``: calendar year of Year 1 (first operating
  year).  CAPEX is paid in Year 0 (``project_start_year - 1``).
* ``mode``: ``self_consumption`` | ``merchant``.  (The MILP timestep
  is auto-detected from the timeseries cadence; there is no timestep
  key to set.)
* ``p_grid_export_max_kw``: grid-connection export limit (kW).  A
  positive number caps the combined PV + BESS export flow.  Leave the
  cell empty, or set it to ``inf`` / ``infinity`` / ``unlimited`` /
  ``disabled`` / ``none`` (case-insensitive), to remove the cap; no
  injection limit is applied in that case.  Internally a finite
  Big-M is substituted for the disabled cap so the MILP stays
  solver-agnostic (HiGHS, Gurobi, CBC); the constraint itself is never
  removed.  A negative number or ``0`` remains a validation error.
* ``p_grid_import_max_kw``: grid-connection import limit (kW),
  capping grid-to-load plus grid-to-BESS charging per step (the
  export cap's mirror; same empty / ``inf`` / ``unlimited`` /
  ``disabled`` token semantics, default unlimited).  Unlike the
  export cap the constraint is attached only when the value is
  finite, so an unlimited cap leaves results bit-identical.  In
  merchant mode it collapses to a grid-charging power limit.  A
  workbook whose load exceeds every possible supply (PV + BESS power
  + this cap) in some step is rejected before the solve with the
  worst timestamp named; load above the cap alone only warns
  (PV/battery state of charge may still bridge it).
* ``retail_tariff_eur_per_mwh``: retail tariff used in self_consumption mode.
* ``allow_bess_grid_charging``: TRUE → BESS may charge from grid in
  PV-zero periods.
* ``grid_charging_fee_eur_per_mwh`` / ``grid_charging_fee_exempt``:
  regulated charging-side wedge on grid-charged BESS energy (network
  charges + levies; typical European range 10–30 EUR/MWh).  Enters the
  MILP objective as a buy-price adder — thin arbitrage spreads flip
  correctly — and the cashflow as its own expense line (equation E26).
  The exemption switch zeroes it (exempt regimes), keeping the
  exempt / non-exempt scenario pair a one-cell change.  Inert unless
  the dispatch actually grid-charges.
* ``grid_cap_includes_load`` (default FALSE): sets what the per-step
  grid-injection cap limits.  **FALSE** (default) models *physical /
  co-located* self-consumption: the load sits behind the plant meter and
  is served directly, so only the **surplus** reaches the grid and the cap
  limits surplus export (bit-identical to earlier behaviour).  **TRUE**
  models *Virtual Net-Billing*: the load is remote (no physical link to the
  plant), so the plant injects **all** generation into the grid and the
  offset against the remote load is computed each 15-minute settlement; the
  cap then limits the **total plant injection** (energy credited to the
  remote load plus any surplus).  Load priority stays strict but is bounded
  by the cap: the load takes all available injection capacity before any
  surplus export (floor ``min(pv, load, cap)``), and when the cap cannot fit
  the full load the uncovered remainder is bought at the retail tariff while
  surplus PV is curtailed; the run is never infeasible.  Only affects
  ``self_consumption`` mode.
* ``unavailability_pct``: annual outage / maintenance factor
  (default 1 %).  Applied as a post-solve derate on PV generation,
  BESS discharge, and revenue.
* ``curtailment_pct`` (default 0): expected grid-operator curtailment
  of **exported** energy, in percent.  Applied as a post-solve derate
  on the export-side energies and revenues only (after the
  availability derate); self-consumption, load and grid import are
  unaffected.  Mutually exclusive with the ``curtailment_signal``
  timeseries column below — setting both is an error.
* ``curtailment_compensated_pct`` (default 0): share of the curtailed
  energy that is financially compensated, in percent.
* ``curtailment_compensation_price_eur_per_mwh`` (default 0):
  administered compensation price for the compensated curtailed
  energy.  The product of the three keys produces the
  ``curtailment_compensation_eur`` cashflow column, indexed on DAM
  inflation, and the ``lifetime_curtailment_compensation_eur`` KPI.
* ``site_capex_eur`` (default 0): site-wide lump-sum CAPEX in
  absolute EUR for items that are not naturally per-kWp / per-kW
  (substation construction, MV/HV grid upgrades, interconnection
  works, …).  Paid in Year 0; folded into the Year-0 ``capex_eur``
  cash-flow row and reflected in NPV / IRR / ROI / BCR / payback.
  **Excluded** from LCOE / LCOS (Lazard convention; see below).
* ``site_devex_eur`` (default 0): site-wide lump-sum DEVEX in
  absolute EUR (environmental impact studies, land acquisition fees,
  permits not expressed per-kW, …).  Paid in Year 0; folded into the
  Year-0 ``devex_eur`` row.  Also excluded from LCOE / LCOS.
* ``currency_format``: ``auto`` | ``millions`` | ``raw`` for
  financial-axis labels.
* ``show_titles``: TRUE → render plot titles.

Sheet ``pv``
------------

* ``pv_source``: where the PV profile comes from: ``auto`` (default),
  ``file`` or ``pvgis``.  ``auto`` uses the ``timeseries`` ``pv_kwh``
  column (or a ``timeseries_path`` file) when it carries data, and
  otherwise fetches the profile from ``latitude`` / ``longitude``.  A
  blank cell means ``auto``.  See the **PV source and location** section
  below for the full resolution table.
* ``latitude`` / ``longitude``: site coordinates (degrees).  Required
  when ``pv_kwh`` is empty so the profile is fetched from PVGIS.
* ``tilt``: array tilt in degrees, or the literal ``optimal``
  (PVGIS picks the optimal inclination).
* ``azimuth``: array azimuth in degrees: ``0`` = south, ``90`` = west,
  ``-90`` = east.
* ``losses_pct``: PVGIS system losses (percent).  A blank cell means
  the PVGIS default (14); an explicit ``0`` is honoured (loss-free
  array).
* ``weather_year``: PVGIS weather year; use a non-leap year for a clean
  8760-hour profile, or ``tmy``.
* ``raddatabase``: optional PVGIS radiation-database override
  (e.g. ``PVGIS-SARAH3`` or ``PVGIS-ERA5``); blank lets PVGIS pick the
  regional default for the location.
* ``timeseries_path``: file sub-mode: an optional external CSV / Parquet
  whose ``pv_kwh`` column replaces the inline column.
* ``pv_nameplate_kwp``: PV nameplate.  ``0`` ⇒ no PV in this project.
  The ``pv_kwh`` timeseries is consumed verbatim (absolute kWh per step);
  nameplate is metadata for per-kW CAPEX / OPEX and the sizing-sweep axis.
* ``pv_degradation_year1_pct``: initial light-induced degradation
  (LID) applied at start of Year 2.
* ``pv_degradation_annual_pct``: linear PV degradation after Year 1.
* ``capex_pv_eur_per_kw``: per-kWp PV CAPEX.
* ``devex_pv_eur_per_kw``: per-kWp PV DEVEX
  (development / permitting).  Paid in Year 0 alongside CAPEX.
* ``opex_pv_eur_per_kwp``: annual O&M for PV.

Sheet ``bess``
--------------

* ``bess_power_kw``: symmetric charge / discharge limit.  ``0`` ⇒ no
  BESS in this project.
* ``bess_capacity_kwh``: pinned energy capacity (industry standard
  for sizing-as-input projects).
* ``efficiency_charge`` / ``efficiency_discharge``: one-way
  efficiencies.
* ``soc_min_frac`` / ``soc_max_frac`` / ``initial_soc_frac`` /
  ``terminal_soc_equal`` / ``max_cycles_per_day``: operating
  envelope.
* ``max_cycles_per_year`` (default 0 = off) /
  ``cycle_cap_basis`` (``nameplate`` default | ``faded``): annual
  full-equivalent-cycle warranty cap, enforced in the Year-1
  dispatch as one year-long constraint and checked analytically for
  the projected years (degradation sheet ``cycles_on_basis`` /
  ``warranty_utilisation_pct`` columns).  The basis only changes the
  projected-year accounting — Year-1 dispatch is identical under
  both.  A warning flags a daily cap that already binds tighter, a
  replacement reset projecting above 100 %, or the combination with
  rolling-horizon dispatch (the annual cap binds the deterministic
  solve only).
* ``capex_bess_eur_per_kwh`` (default 250 EUR/kWh): full installed
  BESS CAPEX per kWh of nameplate energy capacity (cells + PCS + BOP
  + EPC; Lazard band 215-315 EUR/kWh).  Set 0 for an existing BESS.
* ``devex_bess_eur_per_kw`` (default 30 EUR/kW) /
  ``opex_bess_eur_per_kw`` (default 14 EUR/kW/yr): development /
  permitting and fixed O&M stay on the power basis: both scale with
  the power block, not the energy capacity.
* ``bess_replacement_year`` / ``bess_replacement_cost_pct``:
  replacement policy and cost.  Three-way semantics: a positive integer
  N schedules the replacement in project year N (the SOH threshold is
  then ignored); a blank cell or the literal ``auto`` replaces in the
  first year state-of-health falls to ``bess_eol_soh_pct``, with the
  replacement CAPEX charged in the cashflow in that year; ``0`` never
  replaces.  Only one replacement is ever charged; if the fresh pack
  would cross the threshold again the run log warns.
* ``bess_overbuild_pct`` (default 0): day-1 DC overbuild.  Installs
  ``(1 + pct/100) x bess_capacity_kwh`` charged in Year-0 CAPEX, with
  usable capacity clamped at nameplate so fade consumes the overbuilt
  margin first.  Dispatch always solves at nameplate.  Cannot combine
  with ``bess_replacement_year``.
* ``bess_augmentation_years`` (default empty): comma-separated project
  years of staged augmentation events, e.g. ``8,15``.  Each event adds
  a fresh pool of cells; every pool fades on its own calendar + cycle
  curve and the plant capacity is the nameplate-clamped pool sum.  The
  event CAPEX books as its own ``augmentation_capex_eur`` cashflow
  column (month 12, like the replacement) and joins the LCOS
  numerator.  Supersedes — and cannot combine with — the single
  replacement.
* ``bess_augmentation_mode`` (``top_up`` default | ``fixed_kwh``):
  ``top_up`` restores usable capacity to nameplate at each event;
  ``fixed_kwh`` adds ``bess_augmentation_kwh`` per event (required
  > 0 in that mode).
* ``bess_cost_decline_pct_per_year`` (default 0, range 0-30): annual
  decline of the BESS unit cost applied to augmentation events — the
  event-year unit cost is ``capex_bess_eur_per_kwh x
  (1 - pct/100)^year``.
* ``bess_degradation_annual_pct``: linear calendar BESS capacity fade.
* ``bess_degradation_pct_per_cycle``: cycle-based capacity fade per
  full equivalent cycle, in percent (LFP default 0.008, range
  0.005-0.010; NMC ~0.010-0.020).  Layered additively on the calendar
  fade.  Set to 0 (or omit the row) to use calendar-only fade.
* ``bess_eol_soh_pct`` (default 80): end-of-life SOH threshold that
  drives the automatic replacement when ``bess_replacement_year`` is
  blank or ``auto``: the battery is replaced, and the replacement CAPEX
  charged, in the first project year SOH falls to this level.  Ignored
  under a scheduled replacement year and under ``0`` (never replace).
* ``bess_wear_cost_eur_per_mwh``: cycle wear cost penalised per MWh
  discharged in the dispatch objective (default 10; set 0 to disable).
  The optimizer only cycles when the price spread beats the wear cost.  It is
  a behavioural shadow price: it shapes dispatch but is **not** added to
  the reported cashflow / NPV (the replacement CAPEX already charges
  degradation), so the cost is never double-counted.  The penalty
  applies to DAM and self-consumption discharge only; expected
  balancing-activation throughput carries no wear penalty by design.
  Derive it with
  :func:`pvbess_opt.degradation.derive_wear_cost_eur_per_mwh` from

  .. code-block:: text

     replacement_cost_eur = capex_bess_eur_per_kwh x bess_capacity_kwh
                            x bess_replacement_cost_pct / 100
     wear_cost = replacement_cost_eur / (cycle_life_cycles x usable_energy_mwh)

  For the shipped case study, 200 EUR/kWh x 30,000 kWh x 50 % =
  3,000,000 EUR; over 6,000 cycles x 22.5 MWh usable that is roughly
  22 EUR/MWh as an upper bound, and LFP packs with higher cycle life
  land near 10 EUR/MWh.

Every run also writes a **degradation** report (a styled ``degradation``
sheet in ``03_results.xlsx`` plus an SOH-trajectory plot)
(:func:`pvbess_opt.degradation.build_degradation_report`).  The
state-of-health curve uses the **same calendar-plus-cycle fade model as
the finance layer** (:func:`pvbess_opt.lifetime._bess_factor`): the
multiplicative ``bess_degradation_annual_pct`` calendar fade minus the
additive ``bess_degradation_pct_per_cycle`` cycle fade, fed the same
Year-1 discharge throughput.  The plotted SOH therefore equals the
``bess_factor`` that scales dispatch / revenue, so it agrees with the
cashflow and the ``bess_total_fade_pct_y_final`` KPI.  The DoD-weighted
ASTM Rainflow ``equivalent_full_cycles`` from the SOC trace is reported
alongside as a diagnostic column.  The curve resets to a fresh battery in
the scheduled ``bess_replacement_year`` when one is set (matching the
finance layer, which resets the capacity fade and charges the replacement
CAPEX in the same year), so the plot is consistent with the cashflow
regardless of how lightly the battery cycles.  When no replacement year is
configured the pack is instead swapped the first year SOH falls to its
end-of-life threshold (80 %).

Sheet ``economics``
-------------------

* ``discount_rate_pct``: WACC.
* ``opex_inflation_pct``: annual OPEX escalation.
* ``retail_inflation_pct`` / ``dam_inflation_pct``: separate annual
  escalation rates for the retail-indexed revenue stream (load / PPA)
  and the DAM-indexed export stream.
* ``aggregator_fee_pct_revenue`` (default 0 %, fee-free — opt-in;
  Gridcog convention): energy-aggregator fee on gross DAM + retail
  revenue post-solve.  Surfaces as a signed ``aggregator_fee_eur``
  column on ``cashflow_yearly``.  Does **not** apply to balancing or
  PPA revenue.  Real-world route-to-market charges are typically a few
  EUR/MWh of *sold* energy or a share of *market* revenue only, so the
  template no longer pre-fills a flat percentage of all revenue.
* ``route_to_market_fee_eur_per_mwh`` (default 0, off): route-to-market /
  representation fee per MWh of grid-**exported** energy (PV + BESS) —
  the charge a cumulative-representation aggregator (Greek FoSE, or the
  last-resort FoSETeK under regulated charges; German Direktvermarkter)
  levies for scheduling, forecasting, balancing responsibility and market
  access.  Charged on *sold* energy only: never on self-consumption
  savings, balancing or PPA revenue, and the PPA-covered PV export share
  is exempt while a physical (sleeved) contract is in term.  Typical
  0.5–5 EUR/MWh (Greek examples ~1–3.5).  Flat over the project life.
  Surfaces as a signed ``route_to_market_fee_eur`` cashflow column;
  excluded from LCOE/LCOS.  See Eq. E13c in ``docs/economics_design.md``.
* ``optimizer_revenue_share_pct`` (default 0 %, off): battery optimizer /
  trading-services revenue share on the **positive** annual BESS
  wholesale trading margin (DAM export revenue minus grid-charging
  cost); nothing is charged in years where the margin is negative.
  Mirrors the merchant revenue-share / floor+share structures of BESS
  optimizers; typical 10–25 %.  Applies only to the battery's wholesale
  stream.  Surfaces as a signed ``optimizer_fee_eur`` cashflow column;
  excluded from LCOE/LCOS.  A warning fires when combined with
  ``aggregator_fee_pct_revenue`` (double-charging the battery's
  wholesale stream).  See Eq. E13d in ``docs/economics_design.md``.
* ``balancing_aggregator_fee_pct_revenue`` (default 0 %): optional,
  separate route-to-market (BSP / balancing-aggregator) fee on **gross**
  balancing revenue (capacity + activation), for assets that participate
  through an aggregator that keeps a share (~5-20 % typical
  behind-the-meter).  Surfaces as a signed
  ``balancing_aggregator_fee_eur`` column; the default 0 keeps results
  bit-identical and the column all-zero.  Range-validated ``[0, 100]``,
  like ``aggregator_fee_pct_revenue``.  Excluded from LCOE/LCOS.
* ``bess_toll_eur_per_mw_year`` (default 0, off): BESS tolling
  agreement — a fixed annual payment per MW of BESS power for dispatch
  rights over a phase window (``bess_toll_year_from`` /
  ``bess_toll_year_to``, inclusive; ``year_to = 0`` = end of life).
  Availability-scaled, contractually indexed
  (``bess_toll_indexation_pct``), with **no** capacity-fade scaling
  (the payment is on the power block).  Under the default
  ``bess_toll_merchant_treatment = zeroed`` every BESS-origin merchant
  stream (BESS DAM margin, balancing legs and their BSP fee, the BESS
  route-to-market fee share, the optimizer share, the charging-side
  grid fee) is zeroed in toll years — the toller keeps them;
  ``retained`` stacks the toll on top instead (warns:
  double-monetises the same MW).  Surfaces as a ``toll_revenue_eur``
  cashflow column (flat 1/12 monthly); excluded from LCOE/LCOS; not
  scaled by the Revenue tornado driver.  See Eqs. E29/E29a in
  ``docs/economics_design.md``.
* ``state_support_eur_per_mw_year`` (default 0, off) /
  ``state_support_year_from`` / ``state_support_year_to`` (defaults
  1 / 0 = end of life) /
  ``state_support_clawback_threshold_eur_per_mw_year`` (default 0) /
  ``state_support_clawback_share_pct`` (default 0 %) /
  ``state_support_indexation_pct`` (default 0): fixed annual state
  support per MW of BESS power with a TWO-WAY netting against realised
  market revenue (Eqs. E31/E31a) — the RRF-style settlement of Greek
  storage-support auctions (Tameio Anakampsis / TAA reference; the
  mechanism is neutral).  Realised market revenue (the E25a base, plus
  capacity-market revenue when present) above the threshold is clawed
  back, below it is compensated, both at the share; no floor — a net
  repayment year is flagged in the run log.  Surfaces as
  ``state_support_eur`` (flat 1/12 monthly) and the signed
  ``state_support_clawback_eur`` (month-12 ex-post booking).  Excluded
  from LCOE/LCOS; the gross support is not scaled by the Revenue
  tornado driver while the netting is recomputed against the un-scaled
  threshold (revenue-stabilising).
* ``sensitivity_tax_rate_delta_pp`` (default 5): TaxRate tornado
  driver +/- in percentage points, active only while
  ``corporate_tax_rate_pct`` > 0.  Each leg is a full cashflow +
  tax-layer rebuild (taxes are nonlinear) and the driver reports
  POST-TAX deltas in dedicated sensitivity columns; the pre-tax
  tornado is untouched.  The cumulative-cashflow figure gains a
  dashed post-tax line while the rate is on.
* ``go_price_eur_per_mwh`` (default 0 = off): guarantees-of-origin
  sale price applied to the PV grid-export volume (the eligible
  renewable injection; BESS discharge and self-consumed energy
  excluded).  Flat over the horizon; the eligible MWh fade on the PV
  degradation curve.  Fee-exempt and excluded from LCOE.
* ``revenue_levy_pct`` (default 0, off): levy on gross MARKET
  turnover (Eq. E33) - wholesale DAM export revenue gross of the
  aggregator fee, balancing capacity + activation revenue and the PPA
  contract leg; self-consumption savings, the contracted streams
  (toll / state support / capacity market) and the imbalance
  settlement are excluded by construction, and negative turnover
  never yields a rebate (clamp).  The 3 % special RES turnover levy
  applied in Greece is the reference example.  Surfaces as a signed
  ``revenue_levy_eur`` column inside ``net_cashflow_eur``
  (revenue-share monthly weights); excluded from LCOE/LCOS; SCALES
  with the Revenue tornado driver (a price-proportional base).
* ``capacity_market_eur_per_mw_year`` (default 0, off) /
  ``capacity_market_derating_pct`` (default 100 %) /
  ``capacity_market_year_from`` / ``capacity_market_year_to``
  (defaults 1 / 0 = end of life) / ``capacity_market_indexation_pct``
  (default 0): capacity-market payment on the DERATED power block
  (Eq. E32).  Enter the auction's published storage class factor as
  the derating (EU mechanisms derate storage by duration vs the
  stress-event window); availability-scaled, no capacity-fade scaling.
  Counts as realised market revenue for the state-support netting
  (Eq. E31a).  Surfaces as ``capacity_market_revenue_eur`` (flat 1/12
  monthly); excluded from LCOE/LCOS; not scaled by the Revenue
  tornado driver (administered price).
* ``optimizer_floor_enabled`` (default FALSE) /
  ``optimizer_floor_eur_per_kw_year`` (default 0) /
  ``optimizer_term_year_from`` / ``optimizer_term_year_to`` (defaults
  1 / 0 = whole life) / ``optimizer_margin_basis`` (default ``dam``):
  the floor+share optimizer structure (Eqs. E30/E30a).  With the
  switch on, ``optimizer_revenue_share_pct`` applies to the margin
  ABOVE the guaranteed floor (availability-scaled EUR/kW/yr on the
  power block) and shortfalls are topped up through the
  ``optimizer_floor_topup_eur`` column (booked in month 12 — annual
  ex-post settlement).  ``dam_plus_balancing`` widens the margin base
  to include balancing net of the BSP fee.  FALSE keeps the plain
  share bit-identical.  Excluded from LCOE/LCOS; the Revenue tornado
  recomputes the fee/top-up pair exactly at the floor kink.
* ``corporate_tax_rate_pct`` (default 0, pre-tax only) /
  ``depreciation_years_pv`` / ``depreciation_years_bess`` /
  ``depreciation_years_site`` (defaults 20 / 10 / 20) /
  ``tax_loss_carryforward_years`` (default 0 = unlimited): the
  depreciation + corporate tax layer (Eqs. E34-E38).  Taxable income
  = EBITDA - straight-line depreciation - debt interest, with FIFO
  loss carry-forward (a positive window expires aged vintages; e.g. 5
  in Greece); a BESS replacement starts its own tranche the year
  after the month-12 booking; tranches truncate at the horizon.  Tax
  is never positive (losses only carry forward).  Appends the
  post-tax column family (``net_cashflow_post_tax_eur``, discounted
  and cumulative variants, month-12 monthly booking) while the
  pre-tax columns and KPIs remain the published baseline; at rate 0
  everything passes through bit-identically.  Reference: 22 %
  corporate rate in Greece (2024).
* ``sensitivity_enabled`` / ``sensitivity_capex_delta_pct`` /
  ``sensitivity_opex_delta_pct`` /
  ``sensitivity_revenue_delta_pct`` /
  ``sensitivity_discount_rate_delta_pp`` /
  ``sensitivity_ppa_price_delta_pct``: tornado configuration (the
  PPA-price driver activates only with an enabled contract).
* ``benchmark_lcoe_low_eur_per_mwh`` / ``benchmark_lcoe_high_eur_per_mwh``
  / ``benchmark_lcos_low_eur_per_mwh`` /
  ``benchmark_lcos_high_eur_per_mwh``: Lazard 2024 band overlays
  drawn on the LCOE / LCOS summary plots (defaults 30 / 85 and
  157 / 274 EUR/MWh); presentation-only, never enter the metric
  computation.

Debt / equity leverage
~~~~~~~~~~~~~~~~~~~~~~~~

Four optional ``economics`` keys turn the all-equity project into a
geared one.  They are inert at their defaults, so an unconfigured run
is bit-identical to the unlevered case:

* ``gearing_pct`` (default 0): debt as a share of Year-0 CAPEX.
  ``0`` keeps the project all-equity and suppresses every leverage
  output.
* ``debt_interest_rate_pct`` (default 5): fixed annual rate on the
  drawn debt.
* ``debt_tenor_years`` (default 15): amortisation horizon in years.
* ``debt_repayment`` ∈ ``annuity | linear | sculpted`` (default
  ``annuity``): ``annuity`` levels the total debt service; ``linear``
  levels the principal repayment; ``sculpted`` shapes the debt
  service proportionally to the yearly cashflow so the coverage ratio
  is level across the tenor instead of binding in one year (the
  profile lenders use for irregular cashflows).  All three fully
  amortise the loan to a zero closing balance by the end of the
  tenor.

When ``gearing_pct > 0`` the run reports three leverage KPIs
alongside the project metrics — ``equity_irr_pct`` (IRR on the equity
cashflow after debt service), ``min_dscr`` and ``avg_dscr`` (the
minimum / average debt-service coverage ratio over the tenor) — and
writes a styled ``debt_schedule``
sheet (year, opening / closing balance, interest, principal, debt
service, equity cashflow, DSCR).  The unlevered metrics
(``npv_eur``, project ``irr_pct``, LCOE, LCOS, …) are computed from
the pre-financing cashflow and are unchanged by gearing.

Target-DSCR debt sizing
~~~~~~~~~~~~~~~~~~~~~~~~

Instead of fixing the debt through ``gearing_pct``, the debt amount
can be **sized to a lender covenant**.  Three ``economics`` keys,
inert at their defaults:

* ``debt_sizing_mode`` ∈ ``manual | target_dscr`` (default
  ``manual``): ``manual`` keeps the ``gearing_pct`` convention
  unchanged.  ``target_dscr`` solves the maximum debt that holds the
  target coverage on the sizing case in closed form per repayment
  profile, caps it at the Year-0 outlay, and reports **gearing as an
  output** (``gearing_sized_pct``); ``gearing_pct`` is then an input
  echo only and the run warns when it is non-zero.
* ``target_dscr`` (default 1.30, must be >= 1.0): the minimum
  (``annuity`` / ``linear``) or level (``sculpted``) debt service
  coverage ratio the sized debt must hold.
* ``debt_sizing_case`` ∈ ``base | p90 | low_price`` (default
  ``base``): the cashflow case the debt is sized against.  ``base``
  is the run's own yearly cashflow; ``p90`` sizes against the
  production-P90 haircut below (a warning flags the degenerate
  combination with a factor of 100); ``low_price`` re-dispatches the
  year with the price deck named by ``debt_sizing_deck`` and sizes
  on that deck's cashflow — a genuine re-solve through the
  multi-deck scenario machinery, so BESS arbitrage adapts to the
  deck's spreads and the run's solve time roughly doubles.
* ``debt_sizing_deck`` (default ``low``): the price deck the
  ``low_price`` case re-dispatches with — the ``<column>__<deck>``
  variant-column suffix on the ``timeseries`` sheet, matched
  lowercase.  Validation requires matching variant columns and lists
  the decks actually available.

The sized run reports ``debt_capacity_eur`` (uncapped),
``sized_debt_eur``, ``gearing_sized_pct``, ``dscr_target_met`` and
the binding DSCR year, renders a "Debt sizing" block in
``SUMMARY.md``, and freezes the sized debt for every downstream
consumer (sensitivity and uncertainty replays never re-size — debt is
committed at financial close).  If the target cannot be held (a
loss-making year inside the tenor under a level-service profile), the
debt capacity is zero and the run completes all-equity with a neutral
message — never an error.

Lender cases (P90 production haircut)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Two ``economics`` keys add the lender's downside-resource view, both
inert at their defaults:

* ``production_p90_factor_pct`` (default 100, validated in
  ``(0, 100]``): the P90-to-P50 annual production ratio in percent
  (e.g. ``92`` = the P90 year delivers 92 % of the modelled energy).
  Applied as a deterministic yearly haircut on the PV-linked revenue
  streams (retail/DAM with the aggregator fee rederived, PPA volume,
  route-to-market fee, imbalance cost); balancing, contracted BESS
  payments, OPEX and CAPEX are deliberately untouched.  This is
  distinct from the forecast-noise Monte Carlo on the ``simulation``
  sheet, which perturbs intra-year dispatch — see the design docs for
  the scope split.  No re-dispatch happens (documented cashflow-level
  approximation).
* ``lender_cases_enabled`` (default FALSE): evaluate the lender case
  table — rows ``base`` and ``p90`` with per-case min/avg DSCR,
  equity IRR, NPV and debt capacity — written to a ``lender_cases``
  sheet in ``03_results.xlsx`` and a "Lender cases" block in
  ``SUMMARY.md``.  The per-case leverage KPIs run on the SAME
  committed debt as the run (frozen under target-DSCR sizing), so the
  table answers "same debt, worse resource year".  LCOE / LCOS are
  deliberately excluded from the table.
* ``plot_dscr_profile`` (default TRUE): render the per-year
  DSCR-profile figure (``dscr_profile.pdf``) when a debt layer is
  active.  All-equity runs emit no figure regardless, so the TRUE
  default changes nothing for unlevered outputs; see
  :doc:`financial_plots`.

In a YAML / JSON config the same settings can be supplied as a
``financing:`` block whose keys are expressed as fractions / years and
mapped onto the ``economics`` keys above::

    financing:
      gearing: 0.70          # → gearing_pct = 70
      interest_rate: 0.05    # → debt_interest_rate_pct = 5
      tenor_years: 15        # → debt_tenor_years
      repayment: annuity     # → debt_repayment

Grid emissions and 24/7 CFE
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Two optional ``economics`` keys add an emissions / carbon-free-energy
report, off by default so an unconfigured run is unchanged:

* ``grid_co2_intensity_kg_per_mwh`` (default 0): grid carbon intensity.
  ``0`` keeps the feature off and suppresses the emissions report.  A
  per-step ``grid_co2_kg_per_mwh`` column on the ``timeseries`` sheet
  overrides this with a time-varying intensity (honest 24/7 accounting on
  a grid whose carbon content moves through the day).
* ``grid_co2_annual_decline_pct`` (default 0): annual decline of the grid
  intensity over the project life, modelling a decarbonising grid; the
  avoided emissions taper accordingly.

When an intensity is configured the run writes a styled ``emissions``
sheet to ``03_results.xlsx`` (per project year: the 24/7 CFE score, load,
carbon-free supply, grid import, clean energy delivered, and avoided /
induced / net / residual emissions in tonnes CO2e) plus the
carbon-free-energy duration curve in ``04_financial_plots/``.  The
Year-1 energy-flow diagram (``05_energy_plots/energy_sankey.pdf``) is a
standard output of every run, with or without emissions accounting.  The **24/7 CFE score** is the
time-coincident match of the load by carbon-free supply (PV direct plus
the PV-sourced share of battery discharge); grid-charged battery energy is
not counted as carbon-free, so the score is stricter than a loose annual
volumetric match.  None of this touches the dispatch or the NPV; it is a
diagnostic on the solved schedule.

In a YAML / JSON config the same settings can be supplied as a ``grid:``
block (``co2_intensity`` in kg/MWh, ``co2_annual_decline`` as a fraction)
mapped onto the ``economics`` keys above::

    grid:
      co2_intensity: 350       # → grid_co2_intensity_kg_per_mwh
      co2_annual_decline: 0.02  # → grid_co2_annual_decline_pct = 2

The four ``imbalance_*`` keys switch on the ex-post imbalance
settlement of forecast-error deviations (requires the rolling-horizon
Monte Carlo and ``uncertainty_window_hours >= 2 x
uncertainty_commit_hours``): ``imbalance_enabled``,
``imbalance_pricing`` (``dual`` settles short/long deviations at their
own prices, cost non-negative under incentive-compatible prices;
``single`` settles both at one price and requires the
``imbalance_price_eur_per_mwh`` timeseries column), and the two
DAM-proxy multipliers used per side when the optional
``imbalance_price_short_eur_per_mwh`` /
``imbalance_price_long_eur_per_mwh`` columns are absent (sign-aware,
so negative-price hours keep the spread ordering).

Sheet ``balancing``
-------------------

Optional FCR / aFRR / mFRR balancing-market block, gated by
``balancing_enabled``.  The block is **BESS-only**: every reservation
cap is a share of ``bess_power_kw`` and every revenue KPI is zero
whenever ``bess_power_kw == 0`` or ``balancing_enabled`` is FALSE,
regardless of PV nameplate or load profile.  See
:mod:`pvbess_opt.balancing` for the per-product configuration and
``docs/balancing_market_design.md`` for the formal contract.

The 34 keys (defaults in the design doc's Inputs table):

* ``balancing_enabled``: master switch (FALSE).
* Capacity shares, % of ``bess_power_kw``, sum across all six ≤ 100:
  ``dam_capacity_share_pct`` (declarative, share-sum validation only),
  ``fcr_capacity_share_pct``, ``afrr_up_capacity_share_pct``,
  ``afrr_dn_capacity_share_pct``, ``mfrr_up_capacity_share_pct``,
  ``mfrr_dn_capacity_share_pct``.
* Bid-acceptance probabilities (%): ``fcr_bid_acceptance_pct``,
  ``afrr_up_bid_acceptance_pct``, ``afrr_dn_bid_acceptance_pct``,
  ``mfrr_up_bid_acceptance_pct``, ``mfrr_dn_bid_acceptance_pct``.
* Activation probabilities (%): ``fcr_activation_probability_pct``
  (informational only; FCR carries no activation payment),
  ``afrr_up_activation_probability_pct``,
  ``afrr_dn_activation_probability_pct``,
  ``mfrr_up_activation_probability_pct``,
  ``mfrr_dn_activation_probability_pct``.
* Fallback capacity prices (EUR/MWh, used when the timeseries column
  is absent): ``fcr_default_capacity_price_eur_per_mwh``,
  ``afrr_up_default_capacity_price_eur_per_mwh``,
  ``afrr_dn_default_capacity_price_eur_per_mwh``,
  ``mfrr_up_default_capacity_price_eur_per_mwh``,
  ``mfrr_dn_default_capacity_price_eur_per_mwh``.
* Fallback activation prices (EUR/MWh; FCR has none):
  ``afrr_up_default_activation_price_eur_per_mwh``,
  ``afrr_dn_default_activation_price_eur_per_mwh``,
  ``mfrr_up_default_activation_price_eur_per_mwh``,
  ``mfrr_dn_default_activation_price_eur_per_mwh``.
* ``fcr_required_duration_hours``: FCR sustained-output requirement.
* ``bm_settlement_minutes``: must equal the timeseries cadence
  (validated; the runtime uses the auto-detected ``dt_minutes``).
* ``bm_merit_order_enabled`` (default ``FALSE``): enable the
  merit-order activation-probability curve read from the optional
  ``bm_merit_order`` sheet (columns ``product``,
  ``price_eur_per_mwh``, ``activation_probability_pct``; aFRR/mFRR
  products only, monotone non-increasing in price).  The per-step
  activation probability is then interpolated at each step's
  activation price — expensive bids activate less.  ``FALSE`` keeps
  the scalar ``*_activation_probability_pct`` path, bit-identical.
* ``bm_block_hours``: reservation block length in hours (default 0 =
  reservations may vary per settlement period, bit-identical).  With
  a positive value (e.g. 4, the common European capacity-auction
  block) the reserved capacity per product is held constant across
  each block, anchored on hour-of-year multiples so rolling-horizon
  windows stay aligned; must be a whole multiple of the dispatch
  step and divide 24 evenly.  Blocking restricts the solver's
  choices, so expected balancing revenue can only stay equal or
  fall — the realistic auction granularity avoids overstating it.
* ``bm_soc_headroom_pct``: SOC safety buffer on the worst-case
  activation reservation.
* ``bm_inflation_pct``: yearly indexation of the balancing revenue
  lines in the multi-year cashflow.
* ``bm_price_sigma_capacity_pct`` / ``bm_price_sigma_activation_pct``:
  log-normal Monte Carlo price sigmas.
* ``bm_mc_scenarios`` / ``bm_random_seed``: Monte Carlo size and
  seed.

The Year-1 balancing capacity + activation revenues flow into the
cashflow as ``balancing_revenue_eur`` and are then escalated by
``bm_inflation_pct``.  They enter NPV / IRR / ROI / BCR / payback via
``net_cashflow_eur`` in ``cashflow_yearly``.  They are **excluded**
from LCOE and LCOS by Lazard convention: both metrics measure cost
per delivered MWh and balancing is a revenue, not a cost.  Toggling
``balancing_enabled`` with identical capacities and price inputs
leaves LCOE and LCOS bit-identical.  The Revenue tornado driver
sweeps the full Year-1+ income stream including balancing, so a
"+10 % Revenue" scenario produces a strictly higher NPV than the
base case under any positive cashflow configuration.

Sheet ``ppa``
-------------

PPA contract engine (design note: ``docs/ppa_design.md``) —
pay-as-produced on a share of the PV export, or a baseload band
settled against the plant's total export.  Master-switch pattern like
the ``balancing`` sheet: disabled (the shipped default) leaves every
output bit-identical to a build without the feature.

* ``ppa_enabled``: master switch (default FALSE).
* ``ppa_structure``: ``pay_as_produced`` (as-generated offtake on a
  share of the PV export) or ``baseload`` — a contracted flat band of
  ``ppa_baseload_mw`` settles a fixed per-step volume financially
  against the plant's total export (PV + BESS): shortfall is
  implicitly bought at spot, excess sold at spot.  Baseload is
  cfd-only (a physical sleeved variant totals identically under
  symmetric spot settlement and is deferred) and provably
  dispatch-neutral: the fixed-volume leg has no decision variables,
  so merchant-optimal dispatch is already baseload-optimal.
* ``ppa_settlement``: ``physical`` (sleeved: the covered volume is
  paid the strike and never touches the DAM) or ``cfd`` (full DAM
  exposure plus a two-way strike-minus-DAM leg, negative whenever the
  DAM exceeds the strike).  Both total share × export × strike on the
  covered volume, so the dispatch is identical (the MILP prices PV
  export at ``(1 − s)·DAM + s·strike``) and only the revenue
  decomposition differs.
* ``ppa_price_eur_per_mwh``: the contract strike.
* ``ppa_volume_share_pct``: covered share of the PV **export**,
  pro-rata per step (self-consumed PV is settled at retail; BESS
  export is not covered).  ``pay_as_produced`` only — the baseload
  band is absolute and a non-100 share is warned as ignored.
* ``ppa_baseload_mw``: the contracted flat band for the baseload
  structure (must be > 0 there; ignored for ``pay_as_produced``).
  The per-step volume honours the timeseries resolution
  (``MW × dt``).  Two raw diagnostics report physical coverage:
  ``ppa_baseload_shortfall_mwh`` / ``ppa_baseload_excess_mwh``
  (never availability-derated).  In ``self_consumption`` mode
  delivered energy is export only, so a band above typical surplus
  export produces a permanently shortfall-heavy contract — check the
  shortfall KPI.
* ``ppa_term_years``: operating years 1..term under contract; after
  the term the stream ends and, under physical settlement, the covered
  volume's DAM value rejoins the DAM revenue stream (where the
  aggregator fee applies to it as market revenue).
* ``ppa_inflation_pct``: yearly indexation of the strike,
  independent of ``retail_inflation_pct`` and ``dam_inflation_pct``.
* ``ppa_negative_price_rule``: ``none`` (default: the covered volume
  settles through negative hours unchanged) or ``suspend`` — the
  contract pauses in every step with DAM < 0 (strict: a zero price is
  not suspended).  Under ``physical`` the covered volume is not paid
  the strike and faces spot; under ``cfd`` the difference leg is
  suspended while the market leg keeps selling.  The dispatch reacts:
  covered PV curtails or charges the BESS instead of exporting at a
  loss (near-zero negative prices below the solver's tiebreak
  curtailment weight may still export).  With the clause on, the
  route-to-market exemption uses the exact per-step covered export
  (KPI ``ppa_fee_exempt_export_mwh``) instead of the share-based
  approximation.

While under contract the PPA stream carries **no aggregator fee**
(bilateral offtake, the same convention as balancing/TSO settlement) and
stays out of LCOE/LCOS.  The ``sensitivity_ppa_price_delta_pct``
economics key adds a PPA-price tornado driver when the contract is on.

* ``support_scheme`` (default ``none``) /
  ``support_strike_eur_per_mwh`` / ``support_term_years`` (default
  20) / ``support_ref_period`` (``monthly`` default | ``hourly``) /
  ``support_negative_hour_suspension`` (default ``FALSE``):
  reference-period state-support settlement on the eligible PV
  export — ``sliding_fip`` pays ``max(strike - reference, 0)`` per
  month on the volume-weighted monthly DAM reference price (the
  Greek DAPEEP sliding Feed-in-Premium; the strike is the reference
  tariff), ``cfd_two_way`` settles ``strike - reference`` both ways.
  The premium is a settlement overlay (dispatch still sells at the
  DAM); negative-DAM hours can be excluded from the eligible volume;
  mutually exclusive with ``ppa_enabled``.

Sheet ``intraday``
------------------

Intraday (IDA) participation as a second wholesale venue (design
note: ``docs/intraday_design.md``) — the committed day-ahead dispatch
is re-optimised against the ``ida_price_eur_per_mwh`` timeseries
column in a second solve with the day-ahead net position pinned.
Master-switch pattern like the ``balancing`` sheet: disabled (the
shipped default) leaves every output bit-identical to a build without
the feature.

* ``id_enabled``: master switch (default FALSE).  Requires the
  ``ida_price_eur_per_mwh`` timeseries column, ``mode = merchant``
  and a finite ``p_grid_export_max_kw``; mutually exclusive with
  ``balancing_enabled``, ``ppa_enabled``, the support schemes,
  ``uncertainty_enabled`` and ``midlife_resolve_year`` (the v1 scope
  gates — see ``docs/intraday_design.md``).
* ``id_max_deviation_frac_of_cap`` (default 0.25, validated in
  ``[0, 1]``): per-step bound on the total traded intraday volume as
  a fraction of ``p_grid_export_max_kw`` x dt — a liquidity and
  nomination-change proxy (Eq. I2).  ``0`` disables trading.
* ``id_allow_purchases`` (default TRUE): allow IDA buys.  Purchases
  are physical only — a buy reduces the PV export or charges the
  BESS in the same step (Eq. I5); BESS charging from IDA purchases
  additionally requires ``allow_bess_grid_charging = TRUE``.
* ``id_fee_eur_per_mwh`` (default 0, non-negative): venue trading
  fee per traded MWh, charged on both buy and sell volume (Eq. E59).
  Excluded from LCOE/LCOS per the market-fee convention.
* ``id_inflation_pct`` (default 0): yearly indexation of the
  intraday margin in the multi-year cashflow (mirrors
  ``dam_inflation_pct``).

Sheet ``simulation``
--------------------

* The 12 ``uncertainty_*`` keys driving the rolling-horizon Monte
  Carlo: ``uncertainty_enabled``, ``uncertainty_compare_sources``,
  ``uncertainty_n_seeds``, ``uncertainty_window_hours``,
  ``uncertainty_commit_hours``, ``uncertainty_dam_enabled``,
  ``uncertainty_pv_enabled``, ``uncertainty_load_enabled``,
  ``uncertainty_sigma_dam``, ``uncertainty_sigma_pv``,
  ``uncertainty_sigma_load``, ``uncertainty_diagnostics_enabled``.
  Their defaults are tabulated in
  :doc:`/technical.documentation/uncertainty_modelling`.
* ``plot_daily_scope`` / ``plot_monthly_scope`` /
  ``plot_yearly_scope`` ∈ ``none | year1_only | all``.
* ``uncertainty_diagnostics_enabled`` (default ``TRUE``): render the
  forecast-calibration diagnostic plots (coverage-by-horizon plus the
  per-source PIT histogram, CRPS timeline and residual Q-Q figures)
  into ``06_uncertainty_plots/`` alongside the input forecast bands.
  Set ``FALSE`` to emit only the per-source
  ``inputs_forecast_band_*.pdf`` and the seasonal / heatmap figures.
* ``risk_metrics_enabled`` (default ``FALSE``) / ``risk_alpha_pct``
  (default 5, range (0, 50]): VaR/CVaR of NPV over the rolling-horizon
  Monte Carlo seeds.  Each seed's realised Year-1 profit maps onto an
  NPV by a pro-rata rescale of the Year-1 revenue base (documented
  approximation); the empirical alpha-quantile and its tail mean land
  in the ``risk_metrics`` results sheet and the SUMMARY rolling
  section.  Requires ``uncertainty_enabled`` with seeds; with a
  scenario deck the same estimators are appended to the comparison
  workbook (equal-weight scenarios).
* ``midlife_resolve_year`` (default 0 = off): re-solve the dispatch
  at the given project year with degraded parameters (BESS energy
  scaled by its capacity factor, PV by its production factor, power
  and prices at Year-1 levels) to validate the analytic lifetime
  scaling.  Must lie in ``2..project_lifecycle_years``.  Adds one
  extra solve; the scaled-vs-resolved delta table is diagnostic only
  (results-workbook sheet ``midlife_resolve`` and a ``SUMMARY.md``
  section) and never alters any financial output.

Sheet ``max_injection_profile``
-------------------------------

Hour-of-day cap profile expressing the share of
``p_grid_export_max_kw`` available for export.  Two supported shapes
(auto-detected by the loader from the column names):

* **24 × 1**: column ``hour_of_day`` (an integer ``0..23`` or a
  24-hour interval label such as ``00:00-01:00``, as shipped) plus
  ``max_injection_pct`` (0..100); applied to every day of the year.
* **24 × 13**: ``hour_of_day`` plus 12 monthly columns
  ``max_injection_pct_jan`` … ``max_injection_pct_dec``; the cell at
  ``(hour_of_day, month - 1)`` is the cap for that hour-of-day in
  that calendar month.

If the sheet is missing the loader logs an INFO message and falls
back to a flat 100 % cap (no curtailment).  Curtailed energy is
reported as an output (``pv_curtail_kwh`` / ``pv_energy_curtailed_mwh``).

Optional per-source sub-cap sheets
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Two optional sheets, ``max_injection_profile_pv`` and
``max_injection_profile_bess``, carry the identical hour-of-day schema
(24 × 1 or 24 × 13) and split the injection cap by origin: the PV sheet
limits PV-originated injection, the BESS sheet limits battery-originated
injection, each as a share of the **same** ``p_grid_export_max_kw``.  They
bind *on top of* the combined ``max_injection_profile`` cap, so PV plus
BESS injection together still cannot exceed the connection nameplate.
Either sheet may be omitted: an absent sheet means no sub-cap for that
source (only the combined cap binds), so existing workbooks are unaffected.

Under ``grid_cap_includes_load = TRUE`` (Virtual Net-Billing) the sub-cap
counts the load-serving flow too, so e.g. ``max_injection_profile_bess``
= 0 at midday forbids the battery from discharging at all in those hours;
under the default co-located cap it limits only the surplus exported to the
grid.  Both apply in ``self_consumption`` and ``merchant`` modes.

The canonical defaults live in
:data:`pvbess_opt.io.PROJECT_SHEET_DEFAULTS`,
:data:`pvbess_opt.io.PV_SHEET_DEFAULTS`,
:data:`pvbess_opt.io.BESS_SHEET_DEFAULTS`,
:data:`pvbess_opt.io.ECONOMICS_SHEET_DEFAULTS`,
:data:`pvbess_opt.io.BALANCING_SHEET_DEFAULTS`,
:data:`pvbess_opt.io.PPA_SHEET_DEFAULTS`, and
:data:`pvbess_opt.io.SIMULATION_SHEET_DEFAULTS`.

The shipped ``inputs/input.xlsx`` is the single source of truth for
the PV shape: 35 040 fifteen-minute rows.  The ``pv_kwh`` column is the
absolute PV generation per step and is consumed verbatim;
``pv_nameplate_kwp`` is metadata (per-kW CAPEX / OPEX and the
sizing-sweep axis).  See ``inputs/input.xlsx`` for the as-shipped
nameplate and profile.

PV source and location
----------------------

``pv_source`` (on the ``pv`` sheet) makes the PV-profile origin explicit:
``auto`` (the default, and what a blank cell means), ``file`` or ``pvgis``.
One presence-aware rule (shared by the Excel reader and the YAML / JSON
loader, so a workbook and the equivalent config resolve identically)
decides the source from ``pv_source``, whether the ``pv_kwh`` column (or a
``timeseries_path`` file) carries data, and whether a ``latitude`` +
``longitude`` is set:

.. list-table::
   :header-rows: 1
   :widths: 12 26 22 40

   * - ``pv_source``
     - ``pv_kwh`` / ``timeseries_path``
     - ``latitude`` + ``longitude``
     - Result
   * - ``auto``
     - has data
     - (any)
     - **file**: the column / path wins; a location set as well is
       ignored (a warning is logged)
   * - ``auto``
     - empty
     - present
     - **pvgis** fetch
   * - ``auto``
     - empty
     - missing
     - **error**
   * - ``file``
     - has data
     - (any)
     - **file**
   * - ``file``
     - empty
     - (any)
     - **error**
   * - ``pvgis``
     - (any)
     - present
     - **pvgis**: any workbook PV data — a filled ``pv_kwh`` column
       and/or a ``timeseries_path`` file — is ignored (a warning is
       logged); price columns are consumed as usual
   * - ``pvgis``
     - (any)
     - missing
     - **error**

The empty-and-no-location case and the two explicit mismatches raise a
clear, actionable error rather than returning a partial profile.

In ``file`` mode the profile is the ``timeseries`` ``pv_kwh`` column (or
an external ``timeseries_path`` CSV / Parquet), consumed verbatim as the
absolute PV generation per step (``pv_nameplate_kwp`` is metadata, not a
rescale target).  The legacy
``pv_kwh_override`` column is **deprecated**: it is read only as a
fallback when ``pv_kwh`` is empty (and emits a one-time deprecation
warning), so older workbooks keep loading without losing their data.

YAML / JSON config
------------------

Instead of the Excel workbook the optimiser also accepts a YAML or JSON
config whose sections mirror the workbook sheets, with the time-series
referenced by ``timeseries_path`` (a CSV / Parquet file) rather than a
35 040-row inline column::

    pv:
      pv_source: file
      pv_nameplate_kwp: 15000
    bess:
      bess_power_kw: 15000
      bess_capacity_kwh: 30000
    timeseries_path: my_timeseries.csv

Run it with ``pvbess --config run.yaml``.  A structured config and the
equivalent workbook parse to the same typed dict and produce identical
results: every section accepts exactly the keys of the matching workbook
sheet, an unknown or misplaced key is warned about and ignored (the same
semantics as the workbook loader), and a ``bess`` section that omits
``bess_degradation_pct_per_cycle`` runs calendar-only fade exactly like a
workbook that omits the row.  :func:`pvbess_opt.io_read.config_json_schema`
emits a JSON Schema for external validation and
:func:`pvbess_opt.io_read.validate_config` checks a config against it.

PVGIS PV profiles (location-sourced)
------------------------------------

Setting ``latitude`` / ``longitude`` (and leaving ``pv_kwh`` empty, or
forcing ``pv_source: pvgis``) fetches the PV profile automatically; no
hand-built ``pv_kwh`` column is needed.  This works from the Excel
workbook **and** from a YAML / JSON config; both funnel through the same
resolver, so the results are identical.  In a config it reads::

    pv:
      pv_source: pvgis
      pv_nameplate_kwp: 10000     # scaling quantity (= PVGIS peakpower)
      latitude: 37.98
      longitude: 23.73
      tilt: optimal               # or a number in degrees
      azimuth: 0                  # 0 = south
      losses_pct: 14
      weather_year: 2019          # non-leap year for a clean 8760
      # raddatabase: PVGIS-SARAH3 # optional
    project:
      mode: merchant
    timeseries_path: prices.csv   # timestamp + dam_price (+ load)

The loader fetches a **per-kWp** profile once (PVGIS ``peakpower=1``),
caches it on disk keyed on the request geometry, scales it by
``pv_nameplate_kwp``, upsamples it onto the 15-minute grid and writes
``ts['pv_kwh']``; a second run reuses the cache.  Latitude, longitude and
``pv_nameplate_kwp`` are required; the rest default as shown.

**Timezone.** PVGIS data is fetched in UTC and shifted by a **fixed**
``+2`` hours (Europe/Athens standard time, no DST) so the uniform
35 040-step grid is preserved.  A DST-aware conversion would create
23h/25h transition days that break that grid; if you need wall-clock DST
alignment, re-grid the transition days first.

From an Excel workbook the same applies: fill ``latitude`` / ``longitude``
on the ``pv`` sheet and clear the ``pv_kwh`` column.  The fetched profile
is scaled by ``pv_nameplate_kwp`` and used verbatim (the realised PVGIS
yield is kept), so the timeseries must span a whole number of hours
(e.g. the 35 040-row 15-minute grid).

Capacity sizing sweep (``sizing`` sheet / ``sizing:`` block)
------------------------------------------------------------

Sweep capacities instead of running a single size.  The Excel workbook
carries a ``sizing`` sheet for this; a YAML / JSON config uses an
equivalent ``sizing:`` block.

In the **Excel workbook** the ``sizing`` sheet is columnar (one column
per grid axis, one value per row), gated by an ``enabled`` TRUE / FALSE
toggle read from the first data row.  It ships **disabled** with a worked
example, so a normal run is untouched until you set ``enabled`` to
``TRUE``.  Leave a cell blank to drop that value; ``bess_capacity_kwh``
takes precedence over ``bess_duration_hours`` (capacity = power x
duration) when both columns carry values:

.. list-table::
   :header-rows: 1

   * - ``enabled``
     - ``pv_nameplate_kwp``
     - ``bess_power_kw``
     - ``bess_duration_hours``
   * - ``TRUE``
     - 10000
     - 10000
     - 2
   * -
     - 15000
     - 15000
     - 4
   * -
     - 20000
     - 20000
     -

In a **YAML / JSON config** the same sweep is a ``sizing:`` block; each
axis is an explicit list or a ``{min, max, step}`` mapping::

    sizing:
      pv_nameplate_kwp: [8000, 10000, 12000]
      bess_power_kw: [2000, 4000]
      bess_capacity_kwh: {min: 4000, max: 12000, step: 4000}

Either way the optimiser re-runs the dispatch solve at every
``(pv, power, capacity)`` point, ranks an **efficient frontier** by NPV,
and writes ``sizing.xlsx`` (frontier + marginal value + summary, styled
like every other workbook) plus two plots: the NPV-vs-IRR frontier
scatter and the NPV-vs-capacity curve marking the **oversizing
break-even**: the BESS energy where the marginal value of storage
(dNPV/dMWh) crosses zero.  The PV profile is scaled to each
``pv_nameplate_kwp`` by the nameplate ratio off the base column.  With the
sheet disabled (or no ``sizing:`` block) the run is a single size,
unchanged.

Scenario batches (``scenarios`` sheet / ``--scenarios``)
--------------------------------------------------------

Run many named variants in one invocation and emit a comparison.  The
Excel workbook carries a ``scenarios`` sheet for this; a YAML / JSON file
passed with ``--scenarios`` is the equivalent.

In the **Excel workbook** the ``scenarios`` sheet is tidy / long (one
override per row, grouped by ``name``; blank ``name`` cells inherit the
row above), gated by an ``enabled`` TRUE / FALSE toggle in the first
data row.  It ships **disabled** with a worked example.  The ``target``
cell is a dotted path (``project.mode``, ``bess.power_kw``; short aliases
such as ``pv.nameplate_kwp`` / ``bess.power_kw`` are accepted) or one of
the bare specials ``balancing`` (``on`` / ``off``) and
``capex_multiplier``; ``inherits`` clones another scenario:

.. list-table::
   :header-rows: 1

   * - ``enabled``
     - ``name``
     - ``inherits``
     - ``target``
     - ``value``
   * - ``TRUE``
     - Merchant hybrid
     -
     - project.mode
     - merchant
   * -
     - Merchant hybrid + balancing
     - Merchant hybrid
     - balancing
     - on
   * -
     - Cheap CAPEX
     - Merchant hybrid
     - capex_multiplier
     - 0.8

A **YAML / JSON file** passed with ``pvbess inputs/input.xlsx --scenarios
examples/scenarios.yaml`` lists the same overrides as nested mappings::

    scenarios:
      - name: "Merchant hybrid"
        project: { mode: merchant }
      - name: "Merchant hybrid + balancing"
        inherits: "Merchant hybrid"
        balancing: on
      - name: "Cheap CAPEX case"
        inherits: "Merchant hybrid"
        capex_multiplier: 0.8

**Price decks** let a scenario swap the price timeseries itself
(Central / High / Low fundamentals) before its re-solve.  Add variant
columns to the base ``timeseries`` sheet named ``<column>__<deck>``
(double underscore reserved; e.g. ``dam_price_eur_per_mwh__high``) for
any recognised price column — DAM, retail, and the balancing
capacity / activation price columns — then select the deck with the
bare ``price_deck`` target (``target=price_deck``, ``value=high``), or
``price_deck: high`` in a YAML scenario.  Variant columns are inert in
a normal run; a partial deck keeps base values for columns it does not
carry; a deck name matching no variant column fails before any solver
time.  A YAML / JSON config can keep decks in external files with a
top-level ``price_decks: {high: high.csv}`` mapping (canonical price
column names, row count matching the grid).  The comparison workbook
and bars gain a deck column / ``[deck]`` tick-label suffix only when a
deck is used.  Combine with trajectories: the deck sets the Year-1
price LEVEL (dispatch re-solves), the trajectory sets the years-2+
SHAPE.

A YAML scenarios file may also carry a ``trajectories:`` section per
scenario (same shape as the top-level block; an overridden stream
replaces the base workbook's vector wholesale).  The Excel scenarios
sheet cannot — a single cell cannot carry a per-year vector, and the
loader says so.

Each scenario runs through the same path as a standalone run, so its
results match running it alone.  Every override target must name a real
workbook key: any ``<sheet>.<key>`` from the seven parameter sheets, the
short aliases above, or the bare specials.  An unknown target raises a
``ValueError`` naming the scenario and the offending key *before* any
solver time is spent: a typo'd override would otherwise silently produce
a comparison row identical to the base case.  The batch writes a styled
``scenario_comparison.xlsx`` (one row per scenario: NPV / IRR / payback /
LCOE / LCOS + revenue by stream) plus a comparison-bars plot and a
revenue bridge between the first two scenarios.  Scenarios vary on a
shared base PV profile; use separate inputs for different sites.  The
``scenarios`` and ``sizing`` sheets are mutually exclusive; enabling both
raises a clear error.

Per-year stream trajectories (``trajectories`` sheet / ``trajectories:`` block)
--------------------------------------------------------------------------------

Shape a revenue or cost stream year by year instead of the flat
``(1 + inflation)^(y-1)`` index — a declining DAM capture rate as PV
build-out compresses solar-hour prices, an ancillary-services price
decay as the balancing fleet saturates, or a stepped OPEX profile
(post-warranty LTSA step-up, an insurance line).  The Excel workbook
carries a ``trajectories`` sheet for this; a YAML / JSON config uses an
equivalent ``trajectories:`` block.

In the **Excel workbook** the sheet is tidy / long (one row per
``(stream, year)`` multiplier; blank ``stream`` / ``mode`` cells inherit
the row above), gated by an ``enabled`` TRUE / FALSE toggle in the first
data row and shipped **disabled** with a worked example.  Streams:
``revenue_dam``, ``revenue_retail``, ``balancing_capacity``,
``balancing_activation``, ``opex``, or the per-asset split ``opex_pv`` /
``opex_bess`` (the shared ``opex`` stream and the split streams are
mutually exclusive).  ``mode`` is ``replace`` (the vector substitutes the
stream's inflation index; the loader warns when the matching
``*_inflation_pct`` is also non-zero) or ``overlay`` (the vector
multiplies on top of it):

.. list-table::
   :header-rows: 1

   * - ``enabled``
     - ``stream``
     - ``mode``
     - ``year``
     - ``value``
   * - ``TRUE``
     - revenue_dam
     - overlay
     - 1
     - 1.0
   * -
     -
     -
     - 2
     - 0.99
   * -
     -
     -
     - 3
     - 0.98

Every enabled stream must cover **all** operating years
``1..project_lifecycle_years`` contiguously and anchor at ``1.0`` in
year 1 (multipliers are relative to the Year-1 base, which stays equal
to the dispatch result).  The PPA strike deliberately takes no
trajectory (it escalates contractually via ``ppa_inflation_pct``), and
so does the per-MWh route-to-market fee (a flat volume charge).

In a **YAML / JSON config** the same block is a mapping of stream name
to either a ``{mode, values}`` block or a plain list (``replace``
mode)::

    trajectories:
      revenue_dam:
        mode: overlay
        values: [1.0, 0.99, 0.98, 0.97, 0.96]
      opex: [1.0, 1.0, 1.02, 1.02, 1.10]

With the sheet disabled (or no ``trajectories:`` block) every stream
keeps its flat scalar index and the run is bit-identical to before.  The
multi-year application itself (equations E24 / E24a) is described in the
economics guide.
