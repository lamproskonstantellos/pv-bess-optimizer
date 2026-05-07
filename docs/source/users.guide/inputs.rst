Input workbook
==============

The optimiser consumes a single Excel workbook with three sheets:
``timeseries``, ``project``, ``economic``.  All keys use lowercase
snake_case.

Sheet ``timeseries``
--------------------

Per-step data (one row per timestep; the timestep is auto-detected).
The case-study workbook ships at 15-minute cadence (35 040 rows for one
year) per MD YPEN/DAPEEK/93976/2772/2024.

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

Sheet ``project`` (v0.6)
------------------------

Three logical groups (separator rows starting with ``#`` are allowed and
ignored by the loader).  v0.6 splits the v0.5 ``# system`` group into
sizing-only and BESS-internal-behaviour halves and removes the
``# optimization`` group entirely (solver gap / time limit are CLI-
only via ``--mip-gap`` / ``--time-limit``; the curtail-tiebreaker and
cycles-bonus weights are private constants in
:mod:`pvbess_opt.optimization`):

**system_sizing**
    ``pv_nameplate_kwp``, ``bess_power_kw``, ``bess_capacity_kwh``,
    ``battery_hours``, ``p_charge_max_kw``, ``p_dis_max_kw``,
    ``p_grid_export_max_kw``.

**bess_operation**
    ``efficiency_charge``, ``efficiency_discharge``, ``soc_min_frac``,
    ``soc_max_frac``, ``initial_soc_frac``, ``terminal_soc_equal``,
    ``max_cycles_per_day``.

**regulatory**
    ``mode`` (``vnb`` | ``merchant``), ``retail_tariff_eur_per_mwh``,
    ``curtailment_pct``, ``allow_bess_grid_charging``,
    ``settlement_minutes``.

A zero ``pv_nameplate_kwp`` means **no PV in the project**; a zero
``bess_power_kw`` means **no BESS in the project**.  Setting both to
zero raises ``ValueError`` from :func:`pvbess_opt.io.read_inputs`.

Sheet ``economic`` (v0.6)
-------------------------

Eight logical groups (separator rows allowed):

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

**uncertainty** (new in v0.6)
    ``uncertainty_enabled``, ``uncertainty_compare_sources``,
    ``uncertainty_n_seeds``, ``uncertainty_window_hours``,
    ``uncertainty_commit_hours``, ``uncertainty_dam_enabled``,
    ``uncertainty_pv_enabled``, ``uncertainty_load_enabled``,
    ``uncertainty_sigma_dam``, ``uncertainty_sigma_pv``,
    ``uncertainty_sigma_load``.  See
    ``docs/technical.documentation/uncertainty_modelling.md``.

**output**
    ``show_titles``, ``currency_format``, ``plot_daily_scope``,
    ``plot_monthly_scope``, ``plot_yearly_scope``.  All three plot
    scope flags share the same vocabulary in v0.6:
    ``none`` | ``year1_only`` | ``all``.

The canonical defaults live in :data:`pvbess_opt.io.PROJECT_DEFAULTS`
and :data:`pvbess_opt.io.ECON_DEFAULTS`.  Run::

    python scripts/build_input_xlsx.py

to regenerate the case-study ``inputs/input.xlsx`` from the defaults
(35 040 fifteen-minute rows for 2026, 4 500 kWp PV, 5 MW / 20 MWh BESS,
``vnb`` mode, 27 % curtailment cap).
