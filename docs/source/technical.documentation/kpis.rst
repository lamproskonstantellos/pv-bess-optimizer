KPI dictionary
==============

The headline KPI dictionary returned by
:func:`pvbess_opt.kpis.compute_kpis` uses lowercase snake_case keys
throughout.  Selected keys:

Dispatch metrics
----------------

* ``e_cap_mwh`` — BESS energy capacity (MWh), pinned from
  ``bess_capacity_kwh`` at workbook load.
* ``system_total_import_mwh`` / ``system_total_export_mwh``.
* ``bess_total_charge_mwh`` / ``bess_total_discharge_mwh``.
* ``pv_to_bess_mwh`` / ``bess_charge_grid_mwh``.
* ``pv_generation_mwh`` / ``load_energy_mwh`` (load is 0 in merchant).
* ``pv_direct_to_load_mwh`` / ``bess_to_load_mwh``.
* ``bess_green_to_load_mwh`` / ``system_green_to_load_mwh`` —
  PV-origin discharge attribution via FIFO-like running balance.
* ``pv_energy_curtailed_mwh``.
* ``soc_initial_pct`` / ``soc_min_pct`` / ``soc_max_pct`` /
  ``soc_avg_pct``.
* ``bess_equivalent_cycles_total`` /
  ``bess_equivalent_cycles_per_day``.
* ``bess_roundtrip_eff_est`` / ``bess_roundtrip_eff_theoretical``.
* ``bess_net_soc_change_mwh``.

Self-consumption / coverage ratios (0 in merchant for load coverage)
--------------------------------------------------------------------

* ``pv_direct_self_consumption_frac``.
* ``bess_from_pv_self_consumption_frac``.
* ``system_pv_self_consumption_frac``.
* ``load_coverage_from_pv_frac`` / ``load_coverage_from_bess_frac`` /
  ``load_coverage_from_bess_total_frac``.
* ``system_load_green_coverage_frac``.

EUR metrics
-----------

* ``profit_load_from_pv_eur`` / ``profit_load_from_bess_eur``.
* ``profit_export_from_pv_eur`` / ``profit_export_from_bess_eur``.
* ``expense_charge_bess_grid_eur``.
* ``profit_total_eur``.

Rolling-horizon metrics (only when ``--rolling-horizon`` is active)
-------------------------------------------------------------------

* ``foresight_gap_pct_p10`` / ``foresight_gap_pct_p50`` /
  ``foresight_gap_pct_p90`` — Monte Carlo distribution percentiles.
* ``mc_n_seeds`` / ``mc_window_hours`` / ``mc_commit_hours``.
