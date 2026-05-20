Multi-year financial pipeline
=============================

The ``economic`` sheet drives the project-finance pipeline:

* **CAPEX** is paid in Year 0 (calendar
  ``project_start_year - 1``); operating Years 1..N cover
  ``project_start_year .. project_start_year + N - 1``.
* **OPEX** scales by ``(1 + opex_inflation_pct/100)^(y-1)``.
* **Revenue** uses the Year-1 ``profit_total_eur`` from the dispatch
  KPIs as the base.  Revenue is split into a retail-indexed stream
  (load offset / PPA) and a DAM-indexed stream (wholesale exports);
  each is scaled by the PV degradation curve and its own inflation
  rate:
  ``rev_retail_y = rev_retail_1 * pv_factor * (1 + retail_infl)^(y-1)``
  and
  ``rev_dam_y = rev_dam_1 * pv_factor * (1 + dam_infl)^(y-1)``.
* **BESS replacement** is optional (``bess_replacement_year > 0``).

BESS capacity fade — calendar plus cycle
----------------------------------------

The BESS capacity factor combines two terms:

* an **unchanged multiplicative calendar fade**
  ``(1 - bess_degradation_annual_pct/100)^years_since_install``; and
* an **additive linear cycle fade** proportional to the full
  equivalent cycles the battery has accumulated.

.. math::

   \text{bess\_factor}(y) = \max\!\left(0,\;
   (1 - d_{\text{annual}})^{\text{years\_since}}
   - d_{\text{per\_cycle}} \cdot \text{cumulative\_cycles}\right)

The cycle term is driven by the ``bess`` sheet key
``bess_degradation_pct_per_cycle`` — the capacity lost per full
equivalent cycle, in percent.  The LFP default is ``0.008`` (typical
range 0.005–0.010; NMC chemistries sit higher, ~0.010–0.020).  A more
heavily cycled battery therefore degrades faster than an idle one.

Setting ``bess_degradation_pct_per_cycle = 0`` removes the cycle term
entirely and recovers the pre-v0.8.8 calendar-only behaviour exactly.
Workbooks that predate the key load unchanged and default it to 0.

``compute_financial_kpis`` reports the year-N decomposition as
``bess_calendar_fade_pct_y_final``, ``bess_cycle_fade_pct_y_final`` and
``bess_total_fade_pct_y_final``; the first two sum to the third.

Why analytical scaling instead of solving N MILPs?
--------------------------------------------------

Industry tools — Gridcog, Aurora Energy Research, HOMER Pro — use a
pragmatic recipe: solve the dispatch optimisation **once** for a
representative Year 1, then derive Years 2..N analytically.  Re-solving
25 MILPs would be ~25× slower for negligible accuracy gain in financial
planning — the noise from price-curve forecasts dwarfs the numerical
difference between an analytically-scaled Year 5 and a freshly re-solved
Year 5.

Sign convention
---------------

* CAPEX rows are stored as **negative** numbers (cash outflow).
* DEVEX rows are stored as **negative** numbers (cash outflow).
* OPEX rows are stored as **negative** numbers (cash outflow).
* Revenue rows are stored as **positive** numbers (cash inflow).
* ``net_cashflow_eur = revenue_eur + opex_eur + capex_eur + devex_eur``.

Default values
--------------

Default values come from the public literature:

* PV CAPEX ~525 EUR/kWp — IRENA *Renewable Power Generation Costs in
  2023* (2024).
* BESS CAPEX ~200 EUR/kW — Lazard *Levelized Cost of Storage v9* (2024).
* PV degradation 2.5 % LID + 0.55 %/yr linear — Tier-1 module warranty
  terms.
* BESS degradation 2 %/yr linear — typical Tier-1 LFP cell warranty.
* Discount rate 7 % — typical EU renewable WACC band 6-8 %.
* Retail / DAM indexation — user-supplied annual percentages.  The
  workbook ships with both indexation rates set to 0 (no indexation)
  so the user has to opt in explicitly.

KPI keys (lowercase snake_case)
-------------------------------

Headline financial KPIs returned by
:func:`pvbess_opt.economics.compute_financial_kpis`:

* ``npv_eur``
* ``irr_pct``
* ``roi_pct``
* ``bcr``
* ``simple_payback_years``
* ``discounted_payback_years``
* ``total_capex_eur``
* ``total_opex_eur_lifecycle``
* ``total_revenue_eur_lifecycle``
* ``project_start_year`` / ``project_end_year``
* ``bess_calendar_fade_pct_y_final`` / ``bess_cycle_fade_pct_y_final`` /
  ``bess_total_fade_pct_y_final`` — year-N BESS capacity-fade split
