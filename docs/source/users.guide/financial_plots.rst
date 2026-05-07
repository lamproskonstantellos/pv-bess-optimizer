Financial plots
===============

All financial plots are IEEE-styled PDFs.  Compact EUR formatter
(``EUR 12.3M``, ``EUR 45k``) on every EUR axis via
:func:`pvbess_opt.plotting._currency.euro_axis_formatter`.

Eight plots are produced when the financial pipeline runs:

1. ``cumulative_cashflow_<start>-<end>.pdf`` — cumulative undiscounted
   (solid) + discounted (dashed) cash-flow over the project horizon.
2. ``yearly_cashflow_bars_<start>-<end>.pdf`` — stacked yearly bars for
   revenue (+), OPEX (-), CAPEX (-), with the net line overlaid.
3. ``npv_waterfall_<start>-<end>.pdf`` — yearly contribution to total
   NPV (waterfall stacked bar).
4. ``payback_visualization.pdf`` — cumulative cash-flow with vertical
   markers at the simple and discounted payback years.
5. ``monthly_cashflow_<start>.pdf`` — Year-1 monthly stacked bars
   (seasonality of cash-flows).
6. ``sensitivity_npv_tornado.pdf`` — sorted NPV tornado, four drivers.
7. ``sensitivity_irr_tornado.pdf`` — sorted IRR tornado.  Drops the
   ``Discount rate`` row (the IRR is by definition the rate that zeros
   the NPV, so varying the discount rate does not move the IRR).  An
   italic footer note flags the omission.
8. ``rolling_horizon_distribution.pdf`` — Monte Carlo profit histogram
   with vertical markers at P10 / P50 / P90 and a dashed marker at the
   perfect-foresight benchmark (only when ``--rolling-horizon
   --monte-carlo`` is active).

Input-uncertainty plots
-----------------------

Three additional PDFs land under ``06_uncertainty_plots/``.  They make
the rolling-horizon forecast model visible without running the Monte
Carlo:

* ``inputs_forecast_band.pdf`` — three-panel weekly slice (DAM, PV,
  load) with the actual line overlaid on the analytical P10–P90
  envelope.  The envelope width is derived directly from the
  log-normal noise sigmas used by ``add_forecast_noise``:

  .. math::

     \text{P10/P90} = \text{actual} \cdot \exp\!\left(-\tfrac{\sigma^2}{2}
                       \pm \Phi^{-1}(0.90)\,\sigma\right)

  with :math:`\Phi^{-1}(0.90) \approx 1.2816`.  DAM uses a sign-aware
  band so negative-price hours preserve their sign.
* ``inputs_seasonal_boxplot.pdf`` — three-panel monthly boxplot of
  DAM / PV / load (outliers hidden for readability).
* ``dam_intraday_heatmap.pdf`` — DAM by hour-of-day (y) × day-of-year
  (x).  At 15-minute cadence the four sub-hourly samples per cell are
  averaged before plotting, so the heatmap stays clean.
