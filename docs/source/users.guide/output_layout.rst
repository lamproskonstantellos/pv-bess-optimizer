Output layout
=============

See :doc:`outputs` for the full reference.  Quick summary::

    results/<input>_<scenario>_<timestamp>/
    ├── 00_summary/
    │   ├── SUMMARY.md
    │   └── run_log.txt
    ├── 01_inputs/
    │   ├── input_snapshot.xlsx
    │   └── assumptions_summary.txt
    ├── 02_dispatch/
    │   └── dispatch_timeseries.xlsx          # one sheet per calendar year
    ├── 03_results.xlsx
    ├── 04_financial_plots/
    │   ├── cumulative_cashflow_*.pdf
    │   ├── yearly_cashflow_bars_*.pdf
    │   ├── npv_waterfall_*.pdf
    │   ├── cumulative_cashflow_with_payback_{start}-{end}.pdf
    │   ├── monthly_cashflow_<start>.pdf
    │   ├── revenue_stack_yearly_*.pdf
    │   ├── bess_revenue_waterfall.pdf
    │   ├── bess_revenue_capacity_vs_activation.pdf
    │   ├── bess_revenue_by_month.pdf
    │   ├── lifetime_cycles_*.pdf
    │   ├── lcoe_summary.pdf
    │   ├── lcos_summary.pdf
    │   ├── soh_trajectory.pdf                  # BESS projects
    │   ├── sensitivity_npv_tornado.pdf
    │   ├── sensitivity_irr_tornado.pdf
    │   ├── balancing_reservation_profile.pdf   # balancing on
    │   ├── balancing_mc_distribution.pdf       # balancing on
    │   ├── cfe_duration_curve.pdf              # emissions accounting on
    │   └── rolling_horizon_distribution.pdf    # rolling horizon on
    ├── 05_energy_plots/
    │   ├── energy_sankey.pdf                 # Year-1 energy-flow diagram
    │   ├── lifetime_summary_<start>-<end>.pdf
    │   └── <calendar_year>/{daily,monthly,yearly}/...
    └── 06_uncertainty_plots/
        ├── inputs_forecast_band_{dam,pv,load}.pdf
        ├── inputs_seasonal_boxplot_{dam,pv,load}.pdf
        ├── dam_intraday_heatmap.pdf
        ├── coverage_by_horizon.pdf             # diagnostics on (default)
        ├── pit_histogram_{dam,pv,load}.pdf     # diagnostics on (default)
        ├── crps_timeline_{dam,pv,load}.pdf     # diagnostics on (default)
        ├── residual_qq_{dam,pv,load}.pdf       # diagnostics on (default)
        └── rolling_horizon_foresight_gap_comparison.pdf  # --compare-uncertainty-sources
