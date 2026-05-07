Changelog
=========

0.5.0 (2026-05-06)
------------------

Initial release.

* Three-sheet input schema (``timeseries`` / ``project`` / ``economic``)
  with separator-row support.
* Two regulatory regimes: ``vnb`` (Greek Virtual Net Billing with co-
  located load) and ``merchant`` (pure utility-scale dispatch, no
  co-located load).
* Hard static curtailment cap on grid-bound flows in **both** modes per
  **MD YPEN/DAPEEK/53563/1556/2023** (27 % distribution-connected, 28 %
  transmission-connected).
* Single ``profit`` objective (see ``technical.documentation/objectives``
  for the reasoning).
* Tight per-instance big-M MILP formulation; binary-free slack-based
  load priority in ``vnb``.
* 8 audit invariants exposed via
  :func:`pvbess_opt.optimization.verify_dispatch_invariants`.
* Multi-year project-finance pipeline: cashflow projection, NPV / IRR /
  ROI / BCR / simple+discounted payback, four-driver tornado sensitivity.
* HOMER / Gridcog / Aurora calendar convention (Year 0 and Year 1 share
  the same calendar year).
* Single ``02_dispatch/dispatch_hourly.xlsx`` with one sheet per calendar
  year.
* Rolling-horizon dispatch with imperfect foresight + Monte Carlo over
  forecast scenarios.  Defaults: 48-hour window, 24-hour commit,
  log-normal noise on DAM (sigma=0.20), PV (sigma=0.12), load
  (sigma=0.05).
* IEEE-styled PDF plots (energy + financial + rolling-horizon
  distribution).  Compact EUR formatter (``EUR 12.3M``) on every
  financial axis.
