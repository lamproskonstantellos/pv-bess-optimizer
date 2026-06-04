Input workbook
==============

The optimiser consumes a single Excel workbook with **eight sheets**:
``timeseries``, ``project``, ``pv``, ``bess``, ``economics``,
``simulation``, ``balancing``, ``max_injection_profile``.  All keys use
lowercase snake_case.

Sheet ``timeseries``
--------------------

Per-step data (one row per timestep; the timestep is auto-detected).
The case-study workbook ships at 15-minute cadence (35 040 rows for one
year) per MD YPEN/DAPEEK/93976/2772/2024.

==============================  =========  ====================================
Column                          Required   Notes
==============================  =========  ====================================
``timestamp``                   yes        Datetime; regular cadence required.
``pv_kwh``                      yes        PV production per step.
``load_kwh``                    self_consumption only   Required when ``mode=self_consumption``.  In
                                           ``mode=merchant`` the column is
                                           ignored if present (an INFO log
                                           message is emitted) and
                                           ``pv_to_load`` / ``bess_dis_load`` /
                                           ``grid_to_load`` are pinned to 0.
``dam_price_eur_per_mwh``       no         Day-ahead price per step.  Negative
                                           prices are accepted and preserved
                                           sign-aware in the rolling-horizon
                                           noise model.
``retail_price_eur_per_mwh``    no         Time-varying retail tariff.  Falls
                                           back to the scalar
                                           ``retail_tariff_eur_per_mwh`` from
                                           the ``project`` sheet.
==============================  =========  ====================================

Sheet ``project``
-----------------

High-level run configuration:

* ``project_lifecycle_years`` — total project horizon (years).
* ``project_start_year`` — calendar year of Year 1 (first operating
  year).  CAPEX is paid in Year 0 (``project_start_year - 1``).
* ``mode`` — ``self_consumption`` | ``merchant``.
* ``settlement_minutes`` — informational; the MILP timestep is
  auto-detected from the timeseries.
* ``p_grid_export_max_kw`` — grid-connection export limit (kW).  A
  positive number caps the combined PV + BESS export flow.  Leave the
  cell empty, or set it to ``inf`` / ``infinity`` / ``unlimited`` /
  ``disabled`` / ``none`` (case-insensitive), to remove the cap; no
  injection limit is applied in that case.  Internally a finite
  Big-M is substituted for the disabled cap so the MILP stays
  solver-agnostic (HiGHS, Gurobi, CBC) — the constraint itself is never
  removed.  A negative number or ``0`` remains a validation error.
* ``retail_tariff_eur_per_mwh`` — retail tariff used in self_consumption mode.
* ``allow_bess_grid_charging`` — TRUE → BESS may charge from grid in
  PV-zero periods.
* ``unavailability_pct`` — annual outage / maintenance factor
  (default 1 %).  Applied as a post-solve derate on PV generation,
  BESS discharge, and revenue.
* ``site_capex_eur`` (default 0) — site-wide lump-sum CAPEX in
  absolute EUR for items that are not naturally per-kWp / per-kW
  (substation construction, MV/HV grid upgrades, interconnection
  works, …).  Paid in Year 0; folded into the Year-0 ``capex_eur``
  cash-flow row and reflected in NPV / IRR / ROI / BCR / payback.
  **Excluded** from LCOE / LCOS (Lazard convention — see below).
* ``site_devex_eur`` (default 0) — site-wide lump-sum DEVEX in
  absolute EUR (environmental impact studies, land acquisition fees,
  permits not expressed per-kW, …).  Paid in Year 0; folded into the
  Year-0 ``devex_eur`` row.  Also excluded from LCOE / LCOS.
* ``currency_format`` — ``auto`` | ``millions`` | ``raw`` for
  financial-axis labels.
* ``show_titles`` — TRUE → render plot titles.

Sheet ``pv``
------------

* ``pv_source`` — where the PV profile comes from: ``file`` (default,
  today's behaviour — use the ``timeseries`` ``pv_kwh`` column) or
  ``pvgis`` (fetch from latitude / longitude via the resource layer).
* ``pv_nameplate_kwp`` — PV nameplate.  ``0`` ⇒ no PV in this project.
* ``specific_production_kwh_per_kwp`` — annual specific production
  (informational; the MILP consumes the timeseries directly).
* ``pv_degradation_year1_pct`` — initial light-induced degradation
  (LID) applied at start of Year 2.
* ``pv_degradation_annual_pct`` — linear PV degradation after Year 1.
* ``capex_pv_eur_per_kw`` — per-kWp PV CAPEX.
* ``devex_pv_eur_per_kw`` — per-kWp PV DEVEX
  (development / permitting).  Paid in Year 0 alongside CAPEX.
* ``opex_pv_eur_per_kwp`` — annual O&M for PV.

Sheet ``bess``
--------------

* ``bess_power_kw`` — symmetric charge / discharge limit.  ``0`` ⇒ no
  BESS in this project.
* ``bess_capacity_kwh`` — pinned energy capacity (industry standard
  for sizing-as-input projects).
* ``efficiency_charge`` / ``efficiency_discharge`` — one-way
  efficiencies.
* ``soc_min_frac`` / ``soc_max_frac`` / ``initial_soc_frac`` /
  ``terminal_soc_equal`` / ``max_cycles_per_day`` — operating
  envelope.
* ``capex_bess_eur_per_kw`` / ``devex_bess_eur_per_kw``
  (default 30 EUR/kW) / ``opex_bess_eur_per_kw``.
* ``bess_replacement_year`` / ``bess_replacement_cost_pct`` —
  Year-N replacement (0 disables).
* ``bess_degradation_annual_pct`` — linear calendar BESS capacity fade.
* ``bess_degradation_pct_per_cycle`` — cycle-based capacity fade per
  full equivalent cycle, in percent (LFP default 0.008, range
  0.005–0.010; NMC ~0.010–0.020).  Layered additively on the calendar
  fade.  Set to 0 — or omit the row — to use calendar-only fade.
* ``bess_wear_cost_eur_per_mwh`` — cycle wear cost penalised per MWh
  discharged in the dispatch objective (default 0 = off).  When set, the
  optimizer only cycles when the price spread beats the wear cost.  It is
  a behavioural shadow price: it shapes dispatch but is **not** added to
  the reported cashflow / NPV (the replacement CAPEX already charges
  degradation), so the cost is never double-counted.  Derive it from
  replacement cost / cycle-life / usable energy with
  :func:`pvbess_opt.degradation.derive_wear_cost_eur_per_mwh`.

Every run also writes a **degradation** report (a styled ``degradation``
sheet in ``03_results.xlsx`` plus an SOH-trajectory plot): ASTM Rainflow
cycle counting on the SOC trace gives DoD-weighted equivalent full
cycles, projected into a state-of-health / capacity-fade trajectory and
replacement schedule
(:func:`pvbess_opt.degradation.build_degradation_report`).

Sheet ``economics``
-------------------

* ``discount_rate_pct`` — WACC.
* ``opex_inflation_pct`` — annual OPEX escalation.
* ``retail_inflation_pct`` / ``dam_inflation_pct`` — separate annual
  escalation rates for the retail-indexed revenue stream (load / PPA)
  and the DAM-indexed export stream.
* ``aggregator_fee_pct_revenue`` (default 10 %, Gridcog
  convention) — reduces gross revenue post-solve.  Surfaces as a
  signed ``aggregator_fee_eur`` column on ``cashflow_yearly``.
* ``sensitivity_enabled`` / ``sensitivity_capex_delta_pct`` /
  ``sensitivity_opex_delta_pct`` /
  ``sensitivity_revenue_delta_pct`` /
  ``sensitivity_discount_rate_delta_pp`` — tornado configuration.

Debt / equity leverage
~~~~~~~~~~~~~~~~~~~~~~~~

Four optional ``economics`` keys turn the all-equity project into a
geared one.  They are inert at their defaults, so an unconfigured run
is bit-identical to the unlevered case:

* ``gearing_pct`` (default 0) — debt as a share of Year-0 CAPEX.
  ``0`` keeps the project all-equity and suppresses every leverage
  output.
* ``debt_interest_rate_pct`` (default 5) — fixed annual rate on the
  drawn debt.
* ``debt_tenor_years`` (default 15) — amortisation horizon in years.
* ``debt_repayment`` ∈ ``annuity | linear`` (default ``annuity``) —
  ``annuity`` levels the total debt service; ``linear`` levels the
  principal repayment.  Both fully amortise the loan to a zero closing
  balance by the end of the tenor.

When ``gearing_pct > 0`` the run reports two leverage KPIs alongside
the project metrics — ``equity_irr_pct`` (IRR on the equity cashflow
after debt service) and ``min_dscr`` (the minimum debt-service
coverage ratio over the tenor) — and writes a styled ``debt_schedule``
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

Sheet ``balancing``
-------------------

Optional FCR / aFRR / mFRR balancing-market block, gated by
``balancing_enabled``.  The block is **BESS-only**: every reservation
cap is a share of ``bess_power_kw`` and every revenue KPI is zero
whenever ``bess_power_kw == 0`` or ``balancing_enabled`` is FALSE,
regardless of PV nameplate or load profile.  See
:mod:`pvbess_opt.balancing` for the per-product configuration and the
formal contract.

The Year-1 balancing capacity + activation revenues flow into the
cashflow as ``balancing_revenue_eur`` and are then escalated by
``bm_inflation_pct``.  They enter NPV / IRR / ROI / BCR / payback via
``net_cashflow_eur`` in ``cashflow_yearly``.  They are **excluded**
from LCOE and LCOS by Lazard convention — both metrics measure cost
per delivered MWh and balancing is a revenue, not a cost.  Toggling
``balancing_enabled`` with identical capacities and price inputs
leaves LCOE and LCOS bit-identical.  The Revenue tornado driver
sweeps the full Year-1+ income stream including balancing, so a
"+10 % Revenue" scenario produces a strictly higher NPV than the
base case under any positive cashflow configuration.

Sheet ``simulation``
--------------------

* The 11 ``uncertainty_*`` keys driving the rolling-horizon Monte
  Carlo (see :doc:`/technical.documentation/uncertainty_modelling`).
* ``plot_daily_scope`` / ``plot_monthly_scope`` /
  ``plot_yearly_scope`` ∈ ``none | year1_only | all``.
* ``uncertainty_diagnostics_enabled`` (default ``TRUE``) — render the
  forecast-calibration diagnostic plots (coverage-by-horizon, PIT
  histogram, CRPS timeline, residual Q-Q) into ``06_uncertainty_plots/``
  alongside the input forecast band.  Set ``FALSE`` to emit only
  ``inputs_forecast_band.pdf`` and the seasonal / heatmap figures.

Sheet ``max_injection_profile``
-------------------------------

Hour-of-day cap profile expressing the share of
``p_grid_export_max_kw`` available for export.  Two supported shapes
(auto-detected by the loader from the column names):

* **24 × 1** — column ``hour_of_day`` (0..23) plus
  ``max_injection_pct`` (0..100); applied to every day of the year.
* **24 × 13** — ``hour_of_day`` plus 12 monthly columns
  ``max_injection_pct_jan`` … ``max_injection_pct_dec``; the cell at
  ``(hour_of_day, month - 1)`` is the cap for that hour-of-day in
  that calendar month.

If the sheet is missing the loader logs an INFO message and falls
back to a flat 100 % cap (no curtailment).  Curtailed energy is
reported as an output (``pv_curtail_kwh`` / ``pv_energy_curtailed_mwh``).

The canonical defaults live in
:data:`pvbess_opt.io.PROJECT_SHEET_DEFAULTS`,
:data:`pvbess_opt.io.PV_SHEET_DEFAULTS`,
:data:`pvbess_opt.io.BESS_SHEET_DEFAULTS`,
:data:`pvbess_opt.io.ECONOMICS_SHEET_DEFAULTS`, and
:data:`pvbess_opt.io.SIMULATION_SHEET_DEFAULTS`.

The shipped ``inputs/input.xlsx`` is the single source of truth for
the PV shape: 35 040 fifteen-minute rows.  The loader rescales the
workbook PV column to match the user's ``pv_nameplate_kwp`` ×
``specific_production_kwh_per_kwp`` target at run time; every
per-step ratio is preserved.  See ``inputs/input.xlsx`` for the
as-shipped nameplate and yield.

PV source and the two ``file`` sub-modes
----------------------------------------

``pv_source`` makes the PV-profile origin explicit:

* ``file`` (default) — the PV profile is taken from the ``timeseries``
  sheet, in one of two sub-modes:

  * **absolute** — populate ``pv_kwh_override`` for every row and the
    loader uses those kWh **verbatim** (your own measured / modelled
    series).
  * **rescaled shape** — leave ``pv_kwh_override`` empty and supply a
    ``pv_kwh`` shape; the loader rescales it so the annual total matches
    ``pv_nameplate_kwp`` × ``specific_production_kwh_per_kwp`` while
    preserving every per-step ratio
    (:func:`pvbess_opt.io._rescale_pv_to_user_target`).

* ``pvgis`` — the profile is fetched from latitude / longitude by the
  resource layer (no PV column needed).

YAML / JSON config
------------------

Instead of the Excel workbook the optimiser also accepts a YAML or JSON
config whose sections mirror the eight sheets, with the time-series
referenced by ``timeseries_path`` (a CSV / Parquet file) rather than a
35 040-row inline column::

    pv:
      pv_source: file
      pv_nameplate_kwp: 15000
      specific_production_kwh_per_kwp: 1500
    bess:
      bess_power_kw: 15000
      bess_capacity_kwh: 30000
    timeseries_path: my_timeseries.csv

Run it with ``pvbess --config run.yaml``.  A structured config and the
equivalent workbook parse to the same typed dict and produce identical
results.  :func:`pvbess_opt.io_read.config_json_schema` emits a JSON
Schema for external validation and
:func:`pvbess_opt.io_read.validate_config` checks a config against it.

PVGIS PV profiles (``pv_source: pvgis``)
----------------------------------------

In a YAML/JSON config, ``pv_source: pvgis`` fetches the PV profile
automatically from latitude / longitude — no hand-built ``pv_kwh``
column::

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

``pv_source: pvgis`` is resolved by the structured-config loader only; an
Excel workbook with ``pv_source=pvgis`` is rejected with a pointer to use
a YAML/JSON config.

Capacity sizing sweep (``sizing:`` block)
-----------------------------------------

Add a ``sizing:`` block to a YAML/JSON config to sweep capacities instead
of running a single size.  Each axis is an explicit list or a
``{min, max, step}`` mapping; BESS energy may be given as
``bess_capacity_kwh`` or ``bess_duration_hours`` (capacity = power x
duration)::

    sizing:
      pv_nameplate_kwp: [8000, 10000, 12000]
      bess_power_kw: [2000, 4000]
      bess_capacity_kwh: {min: 4000, max: 12000, step: 4000}

``pvbess --config run.yaml`` then re-runs the dispatch solve at every
``(pv, power, capacity)`` point, ranks an **efficient frontier** by NPV,
and writes ``sizing.xlsx`` (frontier + marginal value + summary, styled
like every other workbook) plus two plots: the NPV-vs-IRR frontier
scatter and the NPV-vs-capacity curve marking the **oversizing
break-even** — the BESS energy where the marginal value of storage
(dNPV/dMWh) crosses zero.  With no ``sizing:`` block the run is a single
size, unchanged.

Scenario batches (``--scenarios``)
----------------------------------

Run many named variants in one invocation and emit a comparison::

    pvbess inputs/input.xlsx --scenarios examples/scenarios.yaml

The scenarios file lists overrides on the base input; ``inherits`` clones
another scenario.  Overrides accept the canonical sheet keys or short
aliases (``pv.nameplate_kwp``, ``bess.power_kw`` ...), plus
``balancing: on|off`` and ``capex_multiplier``::

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
results match running it alone.  The batch writes a styled
``scenario_comparison.xlsx`` (one row per scenario: NPV / IRR / payback /
LCOE / LCOS + revenue by stream) plus a comparison-bars plot and a
revenue bridge between the first two scenarios.  Scenarios vary on a
shared base PV shape (rescaled per nameplate); use separate configs for
different sites.
