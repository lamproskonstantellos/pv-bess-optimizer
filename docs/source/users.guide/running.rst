Running the optimiser
======================

CLI entry point
---------------

.. code-block:: bash

   python main.py inputs/input.xlsx --solver highs

The CLI accepts:

===================================  ==========================================
Flag                                 Purpose
===================================  ==========================================
``excel``                            Positional input workbook (default
                                     ``inputs/input.xlsx``).
``--solver``                         ``gurobi`` | ``highs`` | ``cbc``
                                     (default ``highs``).
``--outdir``                         Output base directory
                                     (default ``results``).
``--mode``                           Override regulatory mode at the CLI:
                                     ``self_consumption`` | ``merchant``.  Default: read
                                     from the workbook.
``--strict``                         Turn dispatch-invariant violations from
                                     warnings into errors.
``--mip-gap``                        Solver MIP relative gap (default 0.001).
``--time-limit``                     Solver wall-time limit in seconds
                                     (default 1800).
``--tee``                            Print solver output to stdout.
``--rolling-horizon``                Run a rolling-horizon dispatch with
                                     imperfect foresight.
``--window-hours``                   Rolling-horizon window length in hours.
                                     Defaults to the workbook value
                                     (``window_hours = 48`` in the shipped
                                     ``inputs/input.xlsx``); the argparse
                                     default is a sentinel (``None``).
``--commit-hours``                   Rolling-horizon commit slice in hours.
                                     Defaults to the workbook value
                                     (``commit_hours = 24`` in the shipped
                                     ``inputs/input.xlsx``); the argparse
                                     default is a sentinel (``None``).
``--monte-carlo``                    Number of Monte Carlo seeds for the
                                     rolling-horizon ensemble (0 = single
                                     deterministic noiseless run).
``--seed``                           Base seed for the MC ensemble
                                     (default 42).
``--compare-uncertainty-sources``    Run four MC ensembles (DAM-only,
                                     PV-only, Load-only, All-combined)
                                     and emit a comparison plot
                                     (overrides workbook
                                     ``uncertainty_compare_sources``).
===================================  ==========================================

Output layout
-------------

A run writes to
``results/<input>_<scenario>_<timestamp>/``::

    00_summary/        SUMMARY.md, run_log.txt
    01_inputs/         input_snapshot.xlsx, assumptions_summary.txt
    02_dispatch/       dispatch_timeseries.xlsx (one sheet per calendar year)
    03_results.xlsx    KPIs, cashflows, financial KPIs, sensitivity,
                       rolling-horizon MC distribution
    04_financial_plots/ cumulative, waterfall, payback, tornados,
                       rolling_horizon_distribution
    05_energy_plots/<calendar_year>/{daily,monthly,yearly}/...
                       lifetime_summary_<start>-<end>.pdf
    06_uncertainty_plots/ input forecast band, seasonal boxplot,
                       DAM heatmap, forecast-gap comparison

The folder slug is ``<mode>[_grid_ch]`` (e.g.
``self_consumption`` or ``merchant_grid_ch``).
