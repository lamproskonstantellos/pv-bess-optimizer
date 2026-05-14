Changelog
=========

0.8.1 (2026-05-14)
------------------

Patch round — see ``docs/v0.8_changelog.md`` for the per-task
breakdown.

* **Revenue inflation split** — ``revenue_inflation_pct`` is replaced
  by ``retail_inflation_pct`` (load coverage / PPA, default 2 %) and
  ``dam_inflation_pct`` (wholesale exports, default 0 %).  Legacy
  workbooks load with a WARNING and the value is mapped to
  ``retail_inflation_pct``.
* **LCOE / LCOS audit** — LCOE numerator now isolates PV CAPEX,
  PV DEVEX and PV OPEX (Lazard / IRENA / NREL ATB convention).
  Benchmark bands updated to the Lazard 2024 EUR-equivalent: LCOE
  30–85 EUR/MWh, LCOS 157–274 EUR/MWh, both overridable via
  ``benchmark_lcoe_*`` / ``benchmark_lcos_*`` workbook keys.  A
  single INFO log line in ``run_log.txt`` records the computed
  LCOE / LCOS next to the bands.
* **NPV waterfall redesign** — morphology matches
  ``yearly_cashflow_bars`` (five-entry legend, no in-axis CAPEX /
  DEVEX captions, padded y-axis).
* **cumulative_cashflow / payback dedup** —
  ``plot_cumulative_cashflow`` no longer draws payback verticals;
  ``plot_payback`` is renamed to
  ``cumulative_cashflow_with_payback_<start>-<end>.pdf`` for
  naming parity.
* **LCOS annotation placement** — when the project bar undershoots
  the Lazard band the summary annotation lifts above the bar.
* **Net-revenue line contrast** — Net revenue / Real-EUR net lines
  in ``revenue_stack_yearly`` switch to magenta
  (``FINANCIAL_COLORS["net_revenue_line"]``) so they stay visible
  over the dark blue BESS-export stack.

0.8.0 (2026-05-08)
------------------

Feature pack — see ``docs/v0.8_changelog.md`` for the full as-built
diff and the five-line v0.7 → v0.8 migration summary.

* **Workbook schema (breaking)** — the two-sheet ``project +
  economic`` layout is replaced by seven themed sheets:
  ``project``, ``pv``, ``bess``, ``economics``, ``simulation``,
  ``curtailment_profile`` (plus the existing ``timeseries``).  The
  loader reads legacy v0.7 workbooks with a single migration WARNING.
* **BESS spec rationalisation** — ``battery_hours``,
  ``p_charge_max_kw``, ``p_dis_max_kw`` are dropped.
  ``bess_power_kw`` is the symmetric charge / discharge limit and
  ``bess_capacity_kwh`` pins the energy capacity (industry standard
  for sizing-as-input projects).  ``e_cap`` is no longer a decision
  variable; ``run_scenario`` returns
  ``(res, resolved_solver_name)`` and the KPI key
  ``e_cap_opt_mwh`` is renamed to ``e_cap_mwh``.
* **Hourly curtailment cap profile** — the v0.7 scalar
  ``curtailment_pct`` becomes a 24-row hour-of-day profile, optionally
  with one column per calendar month.  Missing sheet ⇒ flat 27 %.
  Implemented in :mod:`pvbess_opt.curtailment`.
* **DEVEX (NEW)** — per-asset ``devex_pv_eur_per_kw`` (default 60
  EUR/kWp) and ``devex_bess_eur_per_kw`` (default 30 EUR/kW) replace
  ``capex_licenses_eur_per_kw``.  Surfaces as a ``devex_eur`` column
  on ``cashflow_yearly`` and as ``total_devex_eur`` /
  ``total_capex_devex_eur`` financial KPIs.
* **Unavailability (NEW)** — ``unavailability_pct`` (default 1 %)
  applies a post-solve derate to PV generation, BESS discharge, and
  revenue.  Implemented in :mod:`pvbess_opt.availability`.
* **Aggregator fee (NEW)** — ``aggregator_fee_pct_revenue`` (default
  10 %, Gridcog convention) reduces gross revenue.
* **IRR tornado redesign** — dumbbell layout with above-line endpoint
  labels (no more inline numbers on the central Base line).
* **LCOE/LCOS panel redesign** — single comparison panel showing the
  project sensitivity range overlaid on Lazard 2024 benchmark bands.

0.6.0 (2026-05-07)
------------------

Feature pack — see ``docs/v0.6_changelog.md`` for the full as-built
diff and the five-line v0.5 → v0.6 migration summary.

* **Workbook schema (breaking)** — the ``project`` sheet's
  ``# system`` group splits into ``# system_sizing`` (capacities and
  power limits) and ``# bess_operation`` (efficiency, SOC bounds,
  cycle limit).  The ``# optimization`` group is removed entirely:
  solver gap / time limit are CLI-only (``--mip-gap`` /
  ``--time-limit``); the curtail-tiebreaker and cycles-bonus weights
  become module-level constants in :mod:`pvbess_opt.optimization`.
  The ``economic`` sheet adds an ``# uncertainty`` group (11 keys)
  and renames the v0.5 daily-Year-1 bool to ``plot_daily_scope``
  (``none`` | ``year1_only`` | ``all``).  ``plot_yearly_scope`` is
  widened to the same vocabulary.  Legacy v0.5 keys produce explicit
  WARNINGs and are ignored — no silent translation.
* **Year-0 / Year-1 calendar split** — Year 0 (CAPEX) lands at
  calendar ``project_start_year - 1``; Year 1 at
  ``project_start_year``.  A 20-year run with
  ``project_start_year = 2026`` produces 21 cashflow rows: 2025
  (CAPEX only) + 2026..2045.  New ``capex_year`` financial KPI.
* **Three asset modes** — ``pv_nameplate_kwp = 0`` means PV is not
  part of the project; ``bess_power_kw = 0`` means BESS is not part
  of the project; both zero raises ``ValueError``.  The optimizer
  pins absent-asset variables to zero and skips the corresponding
  constraint blocks.
* **Workbook-driven uncertainty + 4-source compare** — the new
  ``# uncertainty`` group drives rolling-horizon Monte Carlo.  Set
  ``uncertainty_compare_sources = TRUE`` to run four ensembles
  (DAM-only, PV-only, Load-only, All-combined), emit a
  comparison plot plus four P50 KPI keys, and produce the new
  ``rolling_horizon_compare_mc`` sheet on ``03_results.xlsx``.
* **Unified plot scopes** — ``plot_daily_scope``,
  ``plot_monthly_scope``, ``plot_yearly_scope`` all accept
  ``none`` | ``year1_only`` | ``all``.
* **Merchant-mode plot trio** — new dispatch / SOC / revenue plots
  per resolution replace the empty supply / surplus / combined plots
  in merchant mode.
* **New financial KPIs** — ``lcoe_eur_per_mwh``, ``lcos_eur_per_mwh``,
  ``pv_capacity_factor``, ``bess_lifetime_cycles``, plus five Year-1
  revenue-breakdown keys.  Three new lifecycle plots:
  ``revenue_stack_yearly``, ``lifetime_cycles``,
  ``lcoe_lcos_summary``.
* **Documentation** — new
  ``docs/technical.documentation/uncertainty_modelling.md`` and
  ``asset_modes.md``.  README badge row reordered to
  license → version → python → CI.

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
* Shared-calendar convention (Year 0 and Year 1 both at
  ``project_start_year``).  Replaced in v0.6 by the Year-0 / Year-1
  split — see the 0.6.0 entry above.
* Single ``02_dispatch/dispatch_hourly.xlsx`` with one sheet per calendar
  year.
* Rolling-horizon dispatch with imperfect foresight + Monte Carlo over
  forecast scenarios.  Defaults: 48-hour window, 24-hour commit,
  log-normal noise on DAM (sigma=0.20), PV (sigma=0.12), load
  (sigma=0.05).
* IEEE-styled PDF plots (energy + financial + rolling-horizon
  distribution).  Compact EUR formatter (``EUR 12.3M``) on every
  financial axis.
