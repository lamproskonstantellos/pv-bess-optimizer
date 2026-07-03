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
    │   ├── sensitivity_npv_tornado.pdf
    │   ├── sensitivity_irr_tornado.pdf
    │   ├── balancing_reservation_profile.pdf   # balancing on
    │   ├── balancing_mc_distribution.pdf       # balancing on
    │   └── rolling_horizon_distribution.pdf
    ├── 05_energy_plots/
    │   ├── energy_sankey.pdf                 # Year-1 energy-flow diagram
    │   └── <calendar_year>/{daily,monthly,yearly}/...
    └── 06_uncertainty_plots/
        ├── inputs_forecast_band.pdf
        ├── inputs_seasonal_boxplot.pdf
        ├── dam_intraday_heatmap.pdf
        └── rolling_horizon_foresight_gap_comparison.pdf
