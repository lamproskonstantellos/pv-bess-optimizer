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
  multiplied by

  .. math::

     \text{bess\_factor}(y) = (1 - d_{\text{bess}})^{y-1}

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

Calendar mapping
----------------

HOMER / Gridcog / Aurora convention: Year 0 (CAPEX paid at COD) and
Year 1 (first operating year) share the same calendar year
(``project_start_year``).  Year :math:`y` for :math:`y \ge 1` maps to
``project_start_year + (y - 1)``.
