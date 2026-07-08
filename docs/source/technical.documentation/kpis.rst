KPI dictionary
==============

The headline KPI dictionary returned by
:func:`pvbess_opt.kpis.compute_kpis` uses lowercase snake_case keys
throughout.  Selected keys:

Dispatch metrics
----------------

* ``e_cap_mwh``: BESS energy capacity (MWh), pinned from
  ``bess_capacity_kwh`` at workbook load.
* ``system_total_import_mwh`` / ``system_total_export_mwh``.  Under the
  availability derate export scales down by the availability factor, but
  import scales *up*: the grid covers the full load during plant
  downtime, so ``import = A * import_raw + a * load`` (``a`` the
  unavailability fraction, ``A = 1 - a``).  See the "Availability derate"
  note below and ``docs/economics_design.md`` (Eq. E9).
* ``bess_total_charge_mwh`` / ``bess_total_discharge_mwh``.
* ``pv_to_bess_mwh`` / ``bess_charge_grid_mwh``.
* ``pv_generation_mwh`` / ``load_energy_mwh`` (load is 0 in merchant).
* ``pv_direct_to_load_mwh`` / ``bess_to_load_mwh``.
* ``bess_green_to_load_mwh`` / ``system_green_to_load_mwh``:
  PV-origin discharge attribution via FIFO-like running balance.
* ``pv_energy_curtailed_mwh``.
* ``soc_initial_pct`` / ``soc_min_pct`` / ``soc_max_pct`` /
  ``soc_avg_pct``.
* ``bess_equivalent_cycles_total`` /
  ``bess_equivalent_cycles_per_day``.
* ``bess_roundtrip_eff_est`` / ``bess_roundtrip_eff_theoretical``.
* ``bess_net_soc_change_mwh``.

Cycle-counting convention
-------------------------

The headline convention everywhere in the package is the
**discharge-only equivalent full cycle against nameplate energy**::

    cycles = (bess_dis_load_kwh + bess_dis_grid_kwh) / bess_capacity_kwh

One source of truth, four consumers: ``bess_equivalent_cycles_total`` /
``bess_equivalent_cycles_per_day`` in :mod:`pvbess_opt.kpis`,
``bess_lifetime_cycles`` and the cycle-fade accumulator in
:mod:`pvbess_opt.economics` (via
:func:`pvbess_opt.lifetime.bess_capacity_factors`), the lifetime
projection in :mod:`pvbess_opt.lifetime`, and the degradation report in
:mod:`pvbess_opt.degradation`.  Charging throughput is not counted (a
full cycle is one nameplate energy's worth of discharge), and the
denominator is the NAMEPLATE capacity, not the usable window.

The Rainflow-based
:func:`pvbess_opt.degradation.equivalent_full_cycles` diagnostic in the
degradation report deliberately differs: it counts DoD-weighted swings
of the SOC trace against the USABLE amplitude
(``capacity x (soc_max_frac - soc_min_frac)``), so its value is higher
than the headline count for the same dispatch.  It is a reporting
diagnostic only and never drives the SOH curve or the cashflow.

Both headline cycle KPIs are availability-derated together with
``bess_total_discharge_mwh``, so headline cycles reconcile with
``bess_lifetime_cycles / project_years`` (also derated).  The nested
``bess_utilization_diagnostics`` dict deliberately stays raw (it
reports the Year-1 dispatch as solved, before the derate).

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

The **nine canonical revenue aggregates** consumed by the financial
pipeline and the plot stack (``revenue_pv_dam_eur``,
``revenue_pv_ppa_eur``, ``revenue_bess_dam_eur``,
``revenue_self_consumption_eur``, and the five per-product
``revenue_bess_<product>_eur`` balancing aggregates) are defined with
their construction rules in ``docs/economics_design.md``; the
balancing ``bm_*`` key families are catalogued in
``docs/balancing_market_design.md``.  Every revenue-bearing key is
availability-derated exactly once
(:func:`pvbess_opt.availability.apply_unavailability_derate`).

Availability derate
-------------------

The post-solve availability derate scales every generation-, storage-,
export- and revenue-bearing KPI *down* by the availability factor.
``system_total_import_mwh`` is the sole exception and scales *up*: the
load is fixed exogenous demand that the grid must serve in full while
the plant is offline, so ``import = A * import_raw + a * load`` (Eq. E9
in ``docs/economics_design.md``).  This keeps the derated annual energy
balance closed against the never-derated load and leaves every financial
KPI unchanged (grid import is not a monetised stream).  The annual
energy Sankey (``plotting.emissions.plot_energy_sankey``) is passed
``kpis['availability_factor']`` and applies the identical rule, so its
Load node reads the true demand and its ribbons conserve energy — the
figure and the derated tables agree.

Rolling-horizon metrics (only when ``--rolling-horizon`` is active)
-------------------------------------------------------------------

* ``foresight_gap_pct_p10`` / ``foresight_gap_pct_p50`` /
  ``foresight_gap_pct_p90``: Monte Carlo distribution percentiles.
* ``mc_n_seeds`` / ``mc_window_hours`` / ``mc_commit_hours``.
* ``pf_benchmark_mip_gap``: the ``mip_gap`` **requested** for the solve
  that produced the final perfect-foresight benchmark.  Tighter than the
  configured value when a Monte Carlo realisation beat the initial
  incumbent and the pipeline re-solved the benchmark so it remains the
  best case; it stays at the configured value when a tighter re-solve
  could not improve the incumbent (the time limit binds — see the
  rolling-horizon guide).
* ``pf_benchmark_gap_achieved``: the relative optimality gap the solver
  actually **proved** for that benchmark (``|bound − incumbent| /
  |incumbent|``), i.e. the number the solver prints in its own log.
  This is DISTINCT from the requested ``pf_benchmark_mip_gap``: when the
  ``--time-limit`` binds before the target is reached, the solver
  returns whatever gap it had proven so far (e.g. requesting ``1e-5``
  but proving ``5e-4``).  **Publications should quote this achieved gap**
  as the benchmark's certified optimality, not the requested one.
  Absent when the backend does not report bounds (e.g. a pure LP).
