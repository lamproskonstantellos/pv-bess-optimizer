Multi-year lifetime scaling
===========================

The MILP is solved **once** for Year 1; Years 2..N are derived from
the Year-1 dispatch by applying:

* a PV degradation curve (initial light-induced + linear),
* a BESS capacity-fade curve (linear),
* a revenue-side inflation index, and
* an OPEX inflation index.

Scaling rules (per year :math:`y`)
----------------------------------

* **PV-origin flows** (``pv_kwh``, ``pv_to_load_kwh``, ``pv_to_grid_kwh``,
  ``pv_curtail_kwh``, ``pv_to_bess_kwh``) are multiplied by

  .. math::

     \text{pv\_factor}(y) =
     \begin{cases}
       1 & y = 1 \\
       (1 - \text{lid}) \cdot (1 - d_{\text{annual}})^{y-2} & y \ge 2
     \end{cases}

* **BESS-origin flows** (``bess_dis_load_kwh``, ``bess_dis_grid_kwh``,
  ``bess_charge_grid_kwh``, ``soc_kwh``, etc.) and the SOC trace are
  multiplied by :func:`pvbess_opt.lifetime._bess_factor`, which combines
  an unchanged multiplicative **calendar fade** with an additive linear
  **cycle fade**:

  .. math::

     \text{bess\_factor}(y) = \max\!\left(0,\;
     (1 - d_{\text{annual}})^{\text{years\_since}}
     - d_{\text{per\_cycle}} \cdot \text{cumulative\_cycles}(y-1)\right)

  where :math:`\text{years\_since}` is :math:`y - 1` before any
  replacement and :math:`y - \text{replacement\_year}` once a
  replacement has occurred (the calendar term resets to 1.0 at
  ``bess_replacement_year``).  :math:`d_{\text{annual}}` and
  :math:`d_{\text{per\_cycle}}` are the fractional forms of
  ``bess_degradation_annual_pct`` and ``bess_degradation_pct_per_cycle``.

  ``cumulative_cycles(y-1)`` is the full equivalent cycles accrued
  **through year y − 1**; the lag avoids a circular dependency, since
  year-:math:`y` dispatch is what determines year-:math:`y` cycles.
  Full equivalent cycles use the discharge-only convention
  (``discharge_mwh / capacity_mwh``) shared with
  :func:`pvbess_opt.economics.compute_financial_kpis`.  The counter
  resets to 0 at ``bess_replacement_year``.

  When :math:`d_{\text{per\_cycle}} = 0` the second term vanishes and
  ``bess_factor`` is the calendar-only formula; workbooks that omit
  ``bess_degradation_pct_per_cycle`` load unchanged and behave
  identically.

  The replacement CAPEX line is added separately by
  :func:`pvbess_opt.economics.build_yearly_cashflow` at the EFFECTIVE
  replacement year: ``bess_replacement_year`` resolves once via
  :func:`pvbess_opt.lifetime.resolve_bess_replacement_year` (N =
  scheduled year, blank / ``auto`` = the first year the analytic SOH
  curve falls to ``bess_eol_soh_pct``, 0 = never), and the fade
  sequence itself comes from the shared
  :func:`pvbess_opt.lifetime.bess_capacity_factors` accumulator used by
  the cashflow, the lifetime projection and the degradation report.

* **Load** and **grid prices** are unchanged across years.
* **Mixed flows** (``grid_to_load_kwh``) pass through Year-1 values;
  their financial scaling lives in :mod:`pvbess_opt.economics`.

Reconciliation invariant
------------------------

The lifetime test asserts:

.. math::

   \frac{\sum \text{pv\_kwh}_y}{\sum \text{pv\_kwh}_1}
   \approx \text{pv\_factor}(y)

within 0.1 % for every year.

The BESS capacity-fade decomposition reconciles as well: the calendar
fade and the cycle fade sum to the total fade,

.. math::

   \underbrace{(1 - (1 - d_{\text{annual}})^{\text{years\_since}})}
       _{\text{calendar fade}}
   + \underbrace{d_{\text{per\_cycle}} \cdot \text{cumulative\_cycles}}
       _{\text{cycle fade}}
   = \underbrace{1 - \text{bess\_factor}(y)}_{\text{total fade}}

with exact equality whenever the :math:`\max(0, \cdot)` floor in
``bess_factor`` is inactive (the normal case).  Equality only breaks
when pathological cycling would otherwise drive the factor negative,
where the floor clamps the total fade at 100 %.

Calendar mapping
----------------

* **Year 0** carries CAPEX only; its calendar value is
  ``project_start_year - 1`` (CAPEX is paid the year before
  commercial-operations date).
* **Year 1** is the first operating year, calendar
  ``project_start_year``.
* **Year N** is the last operating year, calendar
  ``project_start_year + N - 1``.

A 20-year run with ``project_start_year = 2026`` therefore produces
21 yearly cashflow rows: Year 0 = 2025 (CAPEX only); Years 1..20 =
2026..2045.  ``02_dispatch/dispatch_timeseries.xlsx`` covers operating
years only (2026..2045); there is no 2025 sheet.
