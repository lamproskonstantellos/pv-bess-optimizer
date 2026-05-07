Energy balance verification
===========================

The function :func:`pvbess_opt.kpis.verify_energy_balance` checks four
per-step residuals against the dispatch DataFrame and reports the
maximum residual:

* ``max_pv_split_residual_kwh``
  — ``pv_kwh - (pv_to_load_kwh + pv_to_bess_kwh + pv_to_grid_kwh
  + pv_curtail_kwh)``.
* ``max_load_balance_residual_kwh`` (vnb only)
  — ``load_kwh - (pv_to_load_kwh + bess_dis_load_kwh + grid_to_load_kwh)``.
* ``max_export_definition_residual_kwh``
  — ``grid_export_total_kwh - (pv_to_grid_kwh + bess_dis_grid_kwh)``.
* ``max_soc_dynamics_residual_kwh``
  — ``soc[t+1] - soc[t] - (efficiency_charge × charge - discharge / efficiency_discharge)``.

The default tolerance is :data:`pvbess_opt.kpis.ENERGY_TOLERANCE`
= 1e-3 kWh per timestep.  Residuals above this threshold are logged at
WARNING level (or raised when ``raise_on_failure=True``).
