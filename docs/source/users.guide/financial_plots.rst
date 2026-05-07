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
