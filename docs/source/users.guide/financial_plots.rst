Financial plots
===============

All financial plots are IEEE-styled PDFs.  Compact EUR formatter
(``EUR 12.3M``, ``EUR 45k``) on every EUR axis via
:func:`pvbess_opt.plotting._currency.euro_axis_formatter`.

A family of plots is produced when the financial pipeline runs
(the exact count depends on which of sensitivity / lifecycle / LCOE
/ LCOS are active for the run):

1. ``cumulative_cashflow_<start>-<end>.pdf``: cumulative undiscounted
   + discounted cash-flow over the project horizon (both solid data
   curves, distinguished by colour).
2. ``yearly_cashflow_bars_<start>-<end>.pdf``: stacked yearly bars for
   revenue (+), OPEX (-), CAPEX (-), with the net line overlaid.
3. ``npv_waterfall_<start>-<end>.pdf``: yearly contribution to total
   NPV (waterfall stacked bar).
4. ``cumulative_cashflow_with_payback_{start}-{end}.pdf``: cumulative
   cash-flow with vertical markers at the simple and discounted
   payback years.
5. ``monthly_cashflow_<start>.pdf``: Year-1 monthly stacked bars
   (seasonality of cash-flows).
6. ``sensitivity_npv_tornado.pdf``: sorted NPV tornado, four drivers.
7. ``sensitivity_irr_tornado.pdf``: sorted IRR tornado.  The
   ``Discount rate`` driver is filtered out of the IRR tornado (and
   only the IRR tornado), because the discount rate is the divisor in
   the IRR calculation: varying it would be a circular sensitivity.

   Both tornados annotate each bar end with the absolute driver value
   that produced it (CAPEX / OPEX / revenue in EUR, the discount rate
   as a percentage), placed strictly outside the bar and anchored on
   the row centerline.  The metric itself is read off the x-axis.  The
   base case is marked once, by a dashed vertical line with the bare
   ``Base`` legend entry (the base value is read off the x-axis and
   quoted in SUMMARY.md).  Each y-axis label carries the ± sensitivity
   range used for that driver.
8. ``rolling_horizon_distribution.pdf``: Monte Carlo profit histogram
   with vertical markers at P10 / P50 / P90 and a dashed marker at the
   perfect-foresight benchmark (only when ``--rolling-horizon
   --monte-carlo`` is active).
9. ``balancing_reservation_profile.pdf`` /
   ``balancing_mc_distribution.pdf``: 24-hour average per-product
   reservation stack and the realised balancing-revenue Monte Carlo
   histogram (``bm_mc_scenarios`` draws, P10 / P50 / P90 markers).
   Written only when ``balancing_enabled`` is on; the distribution
   shares the headline availability-derated scope of the ``bm_*`` KPIs.
10. The lifecycle set, produced on every default run into the same
    folder: ``revenue_stack_yearly_<start>-<end>.pdf`` (per-year
    revenue build-up with fees as negative bars),
    ``bess_revenue_waterfall.pdf`` /
    ``bess_revenue_capacity_vs_activation.pdf`` /
    ``bess_revenue_by_month.pdf`` (BESS-scoped revenue views),
    ``lifetime_cycles_<start>-<end>.pdf`` (equivalent cycles per
    year), ``lcoe_summary.pdf`` / ``lcos_summary.pdf`` (Lazard
    benchmark strips) and ``soh_trajectory.pdf`` (BESS projects).
    ``cfe_duration_curve.pdf`` joins them when emissions accounting
    is enabled.

11. ``dscr_profile.pdf``: per-year debt-service coverage over the
    tenor.  Only for levered runs (``gearing_pct > 0`` or
    ``debt_sizing_mode = target_dscr``; all-equity runs emit no
    file), gated by the ``plot_dscr_profile`` key (default TRUE).
    The base-case DSCR line comes from the ``debt_schedule`` sheet; a
    ``DSCR P90 case`` companion line joins it when
    ``production_p90_factor_pct < 100`` (same committed debt, haircut
    cashflow), and in target-DSCR sizing mode the target is drawn as
    a dashed reference line carried in the legend — never as text
    inside the axes.

When a PPA contract is enabled, the yearly revenue stack gains a
``PPA revenue`` bar drawn straight from the cashflow's
``ppa_revenue_eur`` column (term cutoff, escalation and the post-term
reversion included), and the tornado gains the ``PPA price`` driver.

Input-uncertainty plots
-----------------------

Additional PDFs land under ``06_uncertainty_plots/``.  They make the
rolling-horizon forecast model visible without running the Monte
Carlo.  Every per-source view renders one figure per source on the
standard 7x4 canvas (``_dam`` / ``_pv`` / ``_load`` file suffixes; the
``load`` variant exists only when the timeseries carries a
``load_kwh`` column):

* ``inputs_forecast_band_{dam,pv,load}.pdf``: one representative week
  per source with the actual line overlaid on the analytical P10-P90
  envelope.  The envelope width is derived directly from the
  log-normal noise sigmas used by ``add_forecast_noise``:

  .. math::

     \text{P10/P90} = \text{actual} \cdot \exp\!\left(-\tfrac{\sigma^2}{2}
                       \pm \Phi^{-1}(0.90)\,\sigma\right)

  with :math:`\Phi^{-1}(0.90) \approx 1.2816`.  DAM uses a sign-aware
  band so negative-price hours preserve their sign.
* ``inputs_seasonal_boxplot_{dam,pv,load}.pdf``: monthly boxplot per
  source (outliers hidden for readability).
* ``dam_intraday_heatmap.pdf``: DAM by hour-of-day (y) × day-of-year
  (x).  At 15-minute cadence the four sub-hourly samples per cell are
  averaged before plotting, so the heatmap stays clean.
