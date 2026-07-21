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
  economic assumptions echo, the ``degradation`` (SOH / cycle-fade)
  trajectory for BESS projects, the ``debt_schedule`` / ``lender_cases``
  sheets (debt layer active), and — when the market-data / price-scenario
  engine is armed — the ``market_data_provenance``,
  ``scenario_price_paths``, ``scenario_resolve_delta`` and
  ``price_scenario_ensemble`` sheets.
* ``04_financial_plots/``: cumulative cashflow, yearly bars, NPV
  waterfall, payback visualisation, monthly cashflow Year-1, NPV /
  IRR tornados, the yearly revenue stack, the BESS revenue waterfall /
  capacity-vs-activation split / by-month chart, lifetime cycles,
  LCOE / LCOS benchmark strips, the battery SOH trajectory (BESS
  projects), the balancing reservation profile and Monte Carlo
  distribution (balancing on), the DSCR profile (levered runs), the
  DA-vs-intraday price duration curves and the intraday net position
  (intraday venue on), the 24/7-CFE duration curve (emissions
  accounting on), the price-path fan (``price_path_fan.pdf``) and PV
  capture-price KPIs (``capture_kpis.pdf``) when the price-scenario
  engine is armed, and the rolling-horizon distribution (when active).
* ``05_energy_plots/``: the Year-1 energy-flow diagram
  (``energy_sankey.pdf``, every run) and the lifetime summary chart
  (``lifetime_summary_<start>-<end>.pdf``) plus daily / monthly /
  yearly PDFs under ``<calendar_year>/``.  Each resolution is gated by
  its scope flag on the ``simulation`` sheet (``plot_daily_scope`` /
  ``plot_monthly_scope`` / ``plot_yearly_scope``); each accepts
  ``none`` | ``year1_only`` | ``all``.
* ``06_uncertainty_plots/``: one figure per source on the standard
  canvas: the input forecast bands
  (``inputs_forecast_band_{dam,pv,load}.pdf``), the seasonal boxplots
  (``inputs_seasonal_boxplot_{dam,pv,load}.pdf``) and the DAM intraday
  heatmap on every run, plus the forecast-calibration diagnostics
  (``coverage_by_horizon.pdf`` and the per-source
  ``pit_histogram_*`` / ``crps_timeline_*`` / ``residual_qq_*`` files)
  while ``uncertainty_diagnostics_enabled`` stays at its default
  ``TRUE``, and the foresight-gap comparison when
  ``--compare-uncertainty-sources`` runs.  The ``load`` variants exist
  only when the timeseries carries a ``load_kwh`` column.

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
line with the bare ``Base`` legend entry (the base value is read off
the x-axis and quoted in SUMMARY.md), and each y-axis label carries
the ± sensitivity range applied to that driver.

All plots are PDF-only (IEEE preset) and titles are off by default;
toggle with ``show_titles`` in the ``project`` sheet.
