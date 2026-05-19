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
  PDFs.  Each resolution is gated by its scope flag on the
  ``economic`` sheet (``plot_daily_scope`` /
  ``plot_monthly_scope`` / ``plot_yearly_scope``); each accepts
  ``none`` | ``year1_only`` | ``all``.

The monthly and yearly SOC plots draw each aggregate period as a
vertical range bar (daily / monthly min→max) with a short horizontal
tick at the mean — there is no connecting line and no point markers,
since the data is an aggregate rather than an instantaneous reading.
The daily SOC plot is point-in-time at 15-minute resolution and keeps
its stepped-line form.

All plots are PDF-only (IEEE preset) and titles are off by default;
toggle with ``show_titles`` in the ``economic`` sheet.
