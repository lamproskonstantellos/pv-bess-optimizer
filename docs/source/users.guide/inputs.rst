Input workbook
==============

The optimiser consumes a single Excel workbook with three sheets:
``timeseries``, ``project``, ``economic``.  All keys use lowercase
snake_case.

Sheet ``timeseries``
--------------------

Per-step data (one row per timestep; the timestep is auto-detected).

==========================  =========  ====================================
Column                      Required   Notes
==========================  =========  ====================================
``timestamp``               yes        Datetime; regular cadence required.
``pv_kwh``                  yes        PV production per step.
``load_kwh``                vnb only   Required when ``mode=vnb``.  In
                                       ``mode=merchant`` the column is
                                       ignored if present (an INFO log
                                       message is emitted) and
                                       ``pv_to_load`` / ``bess_dis_load`` /
                                       ``grid_to_load`` are pinned to 0.
``dam_price_eur_per_mwh``   no         Day-ahead price per step.  Negative
                                       prices are accepted and preserved
                                       sign-aware in the rolling-horizon
                                       noise model.
``retail_price_eur_per_mwh``  no       Time-varying retail tariff.  Falls
                                       back to the scalar
                                       ``retail_tariff_eur_per_mwh`` from
                                       the ``project`` sheet.
==========================  =========  ====================================

Sheet ``project``
-----------------

Three logical groups (separator rows starting with ``#`` are allowed and
ignored by the loader):

**system**
    ``pv_nameplate_kwp``, ``bess_power_kw``, ``bess_capacity_kwh``,
    ``efficiency_charge``, ``efficiency_discharge``, ``soc_min_frac``, ``soc_max_frac``,
    ``initial_soc_frac``, ``terminal_soc_equal``, ``p_charge_max_kw``,
    ``p_dis_max_kw``, ``battery_hours``, ``max_cycles_per_day``,
    ``p_grid_export_max_kw``.

**regulatory**
    ``mode`` (``vnb`` | ``merchant``), ``retail_tariff_eur_per_mwh``,
    ``curtailment_pct``, ``allow_bess_grid_charging``,
    ``settlement_minutes``.

**optimization**
    ``weight_curtail_tiebreak``, ``weight_cycles_term``,
    ``solver_mip_gap``, ``solver_time_limit_seconds``.

Sheet ``economic``
------------------

Six logical groups (separator rows allowed):

**horizon**
    ``project_lifecycle_years``, ``project_start_year``,
    ``discount_rate_pct``, ``opex_inflation_pct``,
    ``revenue_inflation_pct``.

**capex**
    ``capex_pv_eur_per_kw``, ``capex_bess_eur_per_kw``,
    ``capex_licenses_eur_per_kw``.

**opex**
    ``opex_pv_eur_per_kwp``, ``opex_bess_eur_per_kw``.

**degradation_replacement**
    ``pv_degradation_year1_pct``, ``pv_degradation_annual_pct``,
    ``bess_degradation_annual_pct``, ``bess_replacement_year``,
    ``bess_replacement_cost_pct``.

**sensitivity**
    ``sensitivity_enabled``, ``sensitivity_capex_delta_pct``,
    ``sensitivity_opex_delta_pct``, ``sensitivity_revenue_delta_pct``,
    ``sensitivity_discount_rate_delta_pp``.

**output**
    ``show_titles``, ``currency_format``, ``plot_daily_year1``,
    ``plot_monthly_scope``, ``plot_yearly_scope``.

The canonical defaults live in :data:`pvbess_opt.io.PROJECT_DEFAULTS`
and :data:`pvbess_opt.io.ECON_DEFAULTS`.  Run::

    python scripts/build_input_xlsx.py

to regenerate the case-study ``inputs/input.xlsx`` from the defaults
(8 760 hourly rows for 2026, 4 500 kWp PV, 5 MW / 20 MWh BESS, ``vnb``
mode, 27 % curtailment cap).
