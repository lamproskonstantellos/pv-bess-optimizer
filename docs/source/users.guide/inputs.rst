Input workbook
==============

The optimiser consumes a single Excel workbook.  Nine core data sheets
(``timeseries``, ``project``, ``pv``, ``bess``, ``economics``,
``balancing``, ``ppa``, ``simulation``, ``max_injection_profile``)
carry the run, plus the optional per-source sub-cap sheets
(``max_injection_profile_pv`` / ``max_injection_profile_bess``) and two
optional sweep sheets, ``sizing`` and ``scenarios`` (each gated by an
``enabled`` toggle and shipped disabled).  All keys use lowercase
snake_case.

Sheet ``timeseries``
--------------------

Per-step data (one row per timestep; the timestep is auto-detected).
The case-study workbook ships at 15-minute cadence (35 040 rows for
one year), matching a 15-minute market settlement period.

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
* ``retail_tariff_eur_per_mwh``: retail tariff used in self_consumption mode.
* ``allow_bess_grid_charging``: TRUE → BESS may charge from grid in
  PV-zero periods.
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
* ``losses_pct``: PVGIS system losses (percent).
* ``weather_year``: PVGIS weather year; use a non-leap year for a clean
  8760-hour profile, or ``tmy``.
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

  For the shipped case study, 250 EUR/kWh x 60,000 kWh x 50 % =
  7,500,000 EUR; over 6,000 cycles x 45 MWh usable that is roughly
  28 EUR/MWh as an upper bound, and LFP packs with higher cycle life
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
* ``aggregator_fee_pct_revenue`` (default 10 %, Gridcog
  convention): energy-aggregator fee on gross DAM + retail revenue
  post-solve.  Surfaces as a signed ``aggregator_fee_eur`` column on
  ``cashflow_yearly``.  Does **not** apply to balancing or PPA revenue.
* ``balancing_aggregator_fee_pct_revenue`` (default 0 %): optional,
  separate route-to-market (BSP / balancing-aggregator) fee on **gross**
  balancing revenue (capacity + activation), for assets that participate
  through an aggregator that keeps a share (~5-20 % typical
  behind-the-meter).  Surfaces as a signed
  ``balancing_aggregator_fee_eur`` column; the default 0 keeps results
  bit-identical and the column all-zero.  Range-validated ``[0, 100]``,
  like ``aggregator_fee_pct_revenue``.  Excluded from LCOE/LCOS.
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
* ``debt_repayment`` ∈ ``annuity | linear`` (default ``annuity``):
  ``annuity`` levels the total debt service; ``linear`` levels the
  principal repayment.  Both fully amortise the loan to a zero closing
  balance by the end of the tenor.

When ``gearing_pct > 0`` the run reports two leverage KPIs alongside
the project metrics, ``equity_irr_pct`` (IRR on the equity cashflow
after debt service) and ``min_dscr`` (the minimum debt-service
coverage ratio over the tenor), and writes a styled ``debt_schedule``
sheet (year, opening / closing balance, interest, principal, debt
service, equity cashflow, DSCR).  The unlevered metrics
(``npv_eur``, project ``irr_pct``, LCOE, LCOS, …) are computed from
the pre-financing cashflow and are unchanged by gearing.

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

Pay-as-produced PPA contract on a share of the PV export
(design note: ``docs/ppa_design.md``).  Master-switch pattern like the
``balancing`` sheet: disabled (the shipped default) leaves every
output bit-identical to a build without the feature.

* ``ppa_enabled``: master switch (default FALSE).
* ``ppa_structure``: ``pay_as_produced`` (implemented); ``baseload``
  is reserved for a future shaped profile and rejected with guidance.
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
  export is not covered).
* ``ppa_term_years``: operating years 1..term under contract; after
  the term the stream ends and, under physical settlement, the covered
  volume's DAM value rejoins the DAM revenue stream (where the
  aggregator fee applies to it as market revenue).
* ``ppa_inflation_pct``: yearly indexation of the strike,
  independent of ``retail_inflation_pct`` and ``dam_inflation_pct``.

While under contract the PPA stream carries **no aggregator fee**
(bilateral offtake, the same convention as balancing/TSO settlement) and
stays out of LCOE/LCOS.  The ``sensitivity_ppa_price_delta_pct``
economics key adds a PPA-price tornado driver when the contract is on.

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
  forecast-calibration diagnostic plots (coverage-by-horizon, PIT
  histogram, CRPS timeline, residual Q-Q) into ``06_uncertainty_plots/``
  alongside the input forecast band.  Set ``FALSE`` to emit only
  ``inputs_forecast_band.pdf`` and the seasonal / heatmap figures.

Sheet ``max_injection_profile``
-------------------------------

Hour-of-day cap profile expressing the share of
``p_grid_export_max_kw`` available for export.  Two supported shapes
(auto-detected by the loader from the column names):

* **24 × 1**: column ``hour_of_day`` (0..23) plus
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
:data:`pvbess_opt.io.ECONOMICS_SHEET_DEFAULTS`, and
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
     - **pvgis**: the column is ignored (a warning is logged if it has
       data)
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
