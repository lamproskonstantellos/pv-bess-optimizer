Output layout
=============

See :doc:`outputs` for the full reference.  Quick summary::

    results/<input>_<scenario>_<timestamp>/
    ├── 00_summary/
    │   └── run_log.txt
    ├── 01_inputs/
    │   ├── input_snapshot.xlsx
    │   └── assumptions_summary.txt
    ├── 02_dispatch/
    │   └── dispatch_hourly.xlsx          # one sheet per calendar year
    ├── 03_results.xlsx
    ├── 04_financial_plots/
    │   ├── cumulative_cashflow_*.pdf
    │   ├── yearly_cashflow_bars_*.pdf
    │   ├── npv_waterfall_*.pdf
    │   ├── payback_visualization.pdf
    │   ├── sensitivity_npv_tornado.pdf
    │   ├── sensitivity_irr_tornado.pdf
    │   └── rolling_horizon_distribution.pdf
    └── 05_energy_plots/
        └── <calendar_year>/{daily,monthly,yearly}/...
