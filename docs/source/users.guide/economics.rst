Multi-year financial pipeline
=============================

The ``economics`` sheet drives the project-finance pipeline:

* **CAPEX** is paid in Year 0 (calendar
  ``project_start_year - 1``); operating Years 1..N cover
  ``project_start_year .. project_start_year + N - 1``.
* **OPEX** scales by ``(1 + opex_inflation_pct/100)^(y-1)``.
* Any stream's flat index can be reshaped year-by-year with the
  optional ``trajectories`` input (equations E24/E24a): a
  ``replace``-mode vector substitutes the index, ``overlay``
  multiplies on top of it, and the per-asset ``opex_pv`` /
  ``opex_bess`` streams shape each OPEX leg (the LCOE / LCOS
  numerators follow the same series, so metric and cashflow OPEX
  never diverge).  See the inputs guide for the sheet format.
* **Revenue** uses the Year-1 per-stream KPI breakdown as the base.
  Revenue is split into a retail-indexed stream (load offset / PPA) and
  a DAM-indexed stream (wholesale exports); within each stream the
  PV-origin component degrades on the PV curve and the BESS-origin
  component on the BESS capacity-fade curve, then the stream's own
  inflation index applies:
  ``rev_retail_y = (retail_pv_1 * pv_factor + retail_bess_1 *
  bess_factor) * (1 + retail_infl)^(y-1)`` and
  ``rev_dam_y = (dam_pv_1 * pv_factor + dam_bess_1 * bess_factor) *
  (1 + dam_infl)^(y-1)``.  The grid-charging expense is bundled into
  the BESS-DAM component by convention (``pvbess_opt/conventions.md``).
* **BESS replacement** is optional (``bess_replacement_year > 0``).

BESS capacity fade: calendar plus cycle
---------------------------------------

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
``bess_degradation_pct_per_cycle``: the capacity lost per full
equivalent cycle, in percent.  The LFP default is ``0.008`` (typical
range 0.005-0.010; NMC chemistries sit higher, ~0.010-0.020).  A more
heavily cycled battery therefore degrades faster than an idle one.

Setting ``bess_degradation_pct_per_cycle = 0`` removes the cycle term
entirely, leaving the calendar-only fade.  Workbooks that omit the key
load unchanged and default it to 0.

``compute_financial_kpis`` reports the year-N decomposition as
``bess_calendar_fade_pct_y_final``, ``bess_cycle_fade_pct_y_final`` and
``bess_total_fade_pct_y_final``; the first two sum to the third.

Tax, depreciation and the revenue levy
--------------------------------------

Three opt-in fiscal layers ride the ``economics`` sheet, all
default-off and bit-identical when unset:

* ``revenue_levy_pct`` charges gross MARKET turnover (DAM export
  revenue gross of the aggregator fee, balancing gross of the BSP fee,
  the PPA contract leg) - the 3 % special RES turnover levy applied in
  Greece is the reference.  It is a pre-tax operating cost inside
  ``net_cashflow_eur`` and therefore deductible from taxable income by
  construction.
* ``corporate_tax_rate_pct`` with the three straight-line lives
  (``depreciation_years_pv`` / ``depreciation_years_bess`` /
  ``depreciation_years_site``) and ``tax_loss_carryforward_years``
  computes taxable income = EBITDA - depreciation - debt interest with
  FIFO loss carry-forward, and appends the post-tax cashflow column
  family (``net_cashflow_post_tax_eur`` and friends; the tax books in
  month 12 on the monthly sheet).  Tax is never positive - losses only
  carry forward.
* The post-tax KPIs (``npv_post_tax_eur``, ``irr_post_tax_pct``,
  ``equity_irr_post_tax_pct``, the post-tax paybacks and the lifetime
  tax/depreciation totals) report alongside the pre-tax baseline and
  show "n/a" while the rate is 0; the pre-tax KPIs never change under
  any tax setting.  A worked mini-example: at a 22 % rate with 20/10/20
  lives, early years typically run at a taxable loss (high
  depreciation), the carry-forward absorbs the first profitable years,
  and cash tax starts once the pool is consumed - NPV post-tax then
  sits below the pre-tax NPV by the discounted tax stream.

Why analytical scaling instead of solving N MILPs?
--------------------------------------------------

Industry tools (Gridcog, Aurora Energy Research, HOMER Pro) use a
pragmatic recipe: solve the dispatch optimisation **once** for a
representative Year 1, then derive Years 2..N analytically.  Re-solving
25 MILPs would be ~25× slower for negligible accuracy gain in financial
planning, because the noise from price-curve forecasts dwarfs the numerical
difference between an analytically-scaled Year 5 and a freshly re-solved
Year 5.

Sign convention
---------------

* CAPEX rows are stored as **negative** numbers (cash outflow).
* DEVEX rows are stored as **negative** numbers (cash outflow).
* OPEX rows are stored as **negative** numbers (cash outflow).
* Revenue rows are stored as **positive** numbers (cash inflow).
* ``net_cashflow_eur = revenue_eur + opex_eur + capex_eur + devex_eur``.

Site-wide lump-sum CAPEX / DEVEX
--------------------------------

Two ``project``-sheet keys capture costs that are not naturally
per-kWp or per-kW:

* ``site_capex_eur``: substation construction, MV/HV grid upgrades,
  interconnection works, and similar absolute-EUR items.
* ``site_devex_eur``: environmental impact studies, land acquisition
  fees, and permits not expressed per-kW.

Both default to 0, are paid in Year 0, and fold straight into the
Year-0 ``capex_eur`` / ``devex_eur`` cash-flow rows.  Because the
headline metrics read ``net_cashflow_eur`` / ``discounted_cf_eur``,
they flow through to NPV, IRR, ROI, BCR and payback with no special
handling.  The CAPEX tornado driver scales them too (it represents the
full Year-0 outlay).

**LCOE / LCOS exclude the site lump sum.**  Per the IEA / IRENA /
NREL ATB / Lazard convention, LCOE is PV-only and LCOS is BESS-only;
their numerators are built from the per-asset CAPEX / DEVEX / OPEX
directly, never from the cash-flow ``capex_eur`` column.  A site-wide
lump sum is neither PV-only nor BESS-only, so it is omitted from both
to keep the values Lazard-comparable.

Default values
--------------

Default values come from the public literature:

* PV CAPEX ~525 EUR/kWp, per IRENA *Renewable Power Generation Costs in
  2023* (2024).
* BESS CAPEX ~250 EUR/kWh of nameplate energy capacity (full installed
  cost, band 215-315 EUR/kWh), per Lazard *Levelized Cost of Storage v9*
  (2024).  BESS DEVEX and OPEX stay per kW of the power block.
* PV degradation 2.5 % LID + 0.55 %/yr linear, per Tier-1 module warranty
  terms.
* BESS degradation 2 %/yr linear, a typical Tier-1 LFP cell warranty.
* Discount rate 7 %, within the typical EU renewable WACC band of 6-8 %.
* Retail / DAM indexation: user-supplied annual percentages.  The
  workbook ships with both indexation rates set to 0 (no indexation)
  so the user has to opt in explicitly.

KPI keys (lowercase snake_case)
-------------------------------

Headline financial KPIs returned by
:func:`pvbess_opt.economics.compute_financial_kpis`:

* ``npv_eur``
* ``irr_pct``
* ``roi_pct``: sum of operating net cashflow (Years 1..N) over the
  initial investment ``|Year-0 CAPEX + DEVEX|``
* ``bcr``
* ``simple_payback_years``
* ``discounted_payback_years``
* ``initial_investment_eur``: the Year-0 outlay only (per-asset CAPEX
  + DEVEX + site lump sums); matches the Year-0 bar in the plots
* ``total_capex_eur``: lifecycle total; includes the BESS replacement
  CAPEX when ``bess_replacement_year > 0``
* ``total_opex_eur_lifecycle``
* ``total_revenue_eur_lifecycle``
* ``total_aggregator_fee_eur_lifecycle`` /
  ``total_route_to_market_fee_eur_lifecycle`` /
  ``total_optimizer_fee_eur_lifecycle``: lifecycle totals of the
  route-to-market fee structures (all ≤ 0; the latter two render in
  ``SUMMARY.md`` only when the corresponding knob is set)
* ``npv_post_tax_eur`` / ``irr_post_tax_pct`` /
  ``equity_irr_post_tax_pct`` / ``simple_payback_post_tax_years`` /
  ``discounted_payback_post_tax_years`` /
  ``total_corporate_tax_eur_lifecycle`` /
  ``total_depreciation_eur_lifecycle``: the post-tax KPI family
  (Eq. E39), additive to the pre-tax baseline; all report NaN
  ("n/a" = tax not modelled) while ``corporate_tax_rate_pct`` is 0
* ``project_start_year`` / ``project_end_year``
* ``bess_calendar_fade_pct_y_final`` / ``bess_cycle_fade_pct_y_final`` /
  ``bess_total_fade_pct_y_final``: year-N BESS capacity-fade split
