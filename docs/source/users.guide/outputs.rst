Output artifacts
================

A run produces a single result directory under ``results/``.

* ``00_summary/`` — ``run_log.txt`` (full stdout + stderr capture).
* ``01_inputs/`` — ``input_snapshot.xlsx`` (a verbatim copy of the
  workbook used) and ``assumptions_summary.txt`` (a flat dump of the
  parsed parameters and economic assumptions).
* ``02_dispatch/dispatch_hourly.xlsx`` — one sheet per calendar year
  with the per-step dispatch (lowercase snake_case columns).
* ``03_results.xlsx`` — KPIs, monthly KPIs, cashflows (yearly /
  quarterly / monthly), financial KPIs, sensitivity, lifetime yearly
  aggregates, rolling-horizon MC distribution (when active),
  economic assumptions echo.
* ``04_financial_plots/`` — cumulative cashflow, yearly bars, NPV
  waterfall, payback visualisation, monthly cashflow Year-1, NPV /
  IRR tornados, rolling-horizon distribution (when active).
* ``05_energy_plots/<calendar_year>/`` — daily / monthly / yearly
  PDFs.  Year-1 daily plots are gated by the ``plot_daily_year1``
  flag in the ``economic`` sheet.

All plots are PDF-only (IEEE preset) and titles are off by default;
toggle with ``show_titles`` in the ``economic`` sheet.
