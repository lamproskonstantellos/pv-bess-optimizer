Output artifacts
================

A run produces a single result directory under ``results/``.

* ``00_summary/``: ``SUMMARY.md`` (the run digest: capacities,
  headline KPIs, artifact map) and ``run_log.txt`` (full stdout +
  stderr capture).
* ``01_inputs/``: ``input_snapshot.xlsx`` (a verbatim copy of the
  workbook used) and ``assumptions_summary.txt`` (a flat dump of the
  parsed parameters and economic assumptions).
* ``02_dispatch/dispatch_timeseries.xlsx``: one sheet per calendar year
  with the per-step dispatch (lowercase snake_case columns).
* ``03_results.xlsx``: KPIs, monthly KPIs, cashflows (yearly /
  quarterly / monthly), financial KPIs, sensitivity, lifetime yearly
  aggregates, rolling-horizon MC distribution (when active),
  economic assumptions echo.
* ``04_financial_plots/``: cumulative cashflow, yearly bars, NPV
  waterfall, payback visualisation, monthly cashflow Year-1, NPV /
  IRR tornados, rolling-horizon distribution (when active).
* ``05_energy_plots/<calendar_year>/``: daily / monthly / yearly
  PDFs.  Each resolution is gated by its scope flag on the
  ``simulation`` sheet (``plot_daily_scope`` /
  ``plot_monthly_scope`` / ``plot_yearly_scope``); each accepts
  ``none`` | ``year1_only`` | ``all``.

The monthly and yearly SOC plots draw a stepped mean line (one step
per day / per month) with a min→max fill envelope.  They carry **no
point markers**: markers on a daily / monthly aggregate misread as an
instantaneous SOC reading, whereas the step already conveys each
period's mean and the envelope shows its range.  The daily SOC plot is
point-in-time at 15-minute resolution and keeps its stepped-line form
unchanged.

The IRR and NPV sensitivity tornados annotate each bar end with the
absolute driver value that produced it, placed strictly outside the
bar and anchored on the row centerline.  The metric itself is read
off the x-axis.  The base case is marked once, by a dashed vertical
line whose legend entry (``Base = 15.9%`` / ``Base = €9.0M``) carries
the formatted base value, and each y-axis label carries the ±
sensitivity range applied to that driver.

All plots are PDF-only (IEEE preset) and titles are off by default;
toggle with ``show_titles`` in the ``project`` sheet.
