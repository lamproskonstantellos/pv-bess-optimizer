Input workbook
==============

The optimiser consumes a single Excel workbook with **seven sheets**
(v0.8): ``timeseries``, ``project``, ``pv``, ``bess``, ``economics``,
``simulation``, ``max_injection_profile``.  All keys use lowercase
snake_case.

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
``load_kwh``                    vnb only   Required when ``mode=vnb``.  In
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
* ``mode`` — ``vnb`` | ``merchant``.
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
* ``retail_tariff_eur_per_mwh`` — retail tariff used in vnb mode.
* ``allow_bess_grid_charging`` — TRUE → BESS may charge from grid in
  PV-zero periods.
* ``unavailability_pct`` (NEW in v0.8) — annual outage / maintenance
  factor (default 1 %).  Applied as a post-solve derate on PV
  generation, BESS discharge, and revenue.
* ``currency_format`` — ``auto`` | ``millions`` | ``raw`` for
  financial-axis labels.
* ``show_titles`` — TRUE → render plot titles.

Sheet ``pv``
------------

* ``pv_nameplate_kwp`` — PV nameplate.  ``0`` ⇒ no PV in this project.
* ``specific_production_kwh_per_kwp`` — annual specific production
  (informational; the MILP consumes the timeseries directly).
* ``pv_degradation_year1_pct`` — initial light-induced degradation
  (LID) applied at start of Year 2.
* ``pv_degradation_annual_pct`` — linear PV degradation after Year 1.
* ``capex_pv_eur_per_kw`` — per-kWp PV CAPEX.
* ``devex_pv_eur_per_kw`` (NEW in v0.8) — per-kWp PV DEVEX
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
* ``capex_bess_eur_per_kw`` / ``devex_bess_eur_per_kw`` (NEW in v0.8;
  default 30 EUR/kW) / ``opex_bess_eur_per_kw``.
* ``bess_replacement_year`` / ``bess_replacement_cost_pct`` —
  Year-N replacement (0 disables).
* ``bess_degradation_annual_pct`` — linear calendar BESS capacity fade.
* ``bess_degradation_pct_per_cycle`` — cycle-based capacity fade per
  full equivalent cycle, in percent (LFP default 0.008, range
  0.005–0.010; NMC ~0.010–0.020).  Layered additively on the calendar
  fade.  Set to 0 — or omit the row entirely on an older workbook — to
  recover pre-v0.8.8 calendar-only behaviour.

Sheet ``economics``
-------------------

* ``discount_rate_pct`` — WACC.
* ``opex_inflation_pct`` — annual OPEX escalation.
* ``retail_inflation_pct`` / ``dam_inflation_pct`` — separate annual
  escalation rates for the retail-indexed revenue stream (load / PPA)
  and the DAM-indexed export stream.  The legacy
  ``revenue_inflation_pct`` key is still accepted but is auto-mapped
  to ``retail_inflation_pct`` with a ``DeprecationWarning`` (the rename
  table lives in ``pvbess_opt.io._LEGACY_RENAMED``); new workbooks
  should use the split keys directly.
* ``aggregator_fee_pct_revenue`` (NEW in v0.8; default 10 %, Gridcog
  convention) — reduces gross revenue post-solve.  Surfaces as a
  signed ``aggregator_fee_eur`` column on ``cashflow_yearly``.
* ``sensitivity_enabled`` / ``sensitivity_capex_delta_pct`` /
  ``sensitivity_opex_delta_pct`` /
  ``sensitivity_revenue_delta_pct`` /
  ``sensitivity_discount_rate_delta_pp`` — tornado configuration.

Sheet ``simulation``
--------------------

* The 11 ``uncertainty_*`` keys driving the rolling-horizon Monte
  Carlo (see
  ``docs/technical.documentation/uncertainty_modelling.md``).
* ``plot_daily_scope`` / ``plot_monthly_scope`` /
  ``plot_yearly_scope`` ∈ ``none | year1_only | all``.

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
back to a flat 73 % cap.  Workbooks still using the legacy
``curtailment_profile`` schema (with ``curtailment_pct`` columns)
continue to load with a ``DeprecationWarning`` and are auto-converted
via ``100 - x``; the legacy schema will be removed in a future release.
Curtailed energy is reported as an output
(``pv_curtail_kwh`` / ``pv_energy_curtailed_mwh``).

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
