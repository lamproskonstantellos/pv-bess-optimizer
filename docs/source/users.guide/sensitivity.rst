Tornado sensitivity
===================

The sensitivity module varies one driver at a time around the base
case and records the change in NPV / IRR / payback.

Drivers
-------

Four canonical drivers (all default to symmetric ±10 % envelopes
except the discount rate, which uses ±2 percentage points):

* **Total CAPEX** — symmetric +/-10 % on the Year-0 CAPEX line.
* **Total annual OPEX** — symmetric +/-10 % on the Year-1 OPEX line.
* **Year-1 revenue base** — symmetric +/-10 % on the Year-1 revenue.
* **Discount rate** — symmetric +/-2 pp on ``discount_rate_pct``.

The IRR tornado drops the discount-rate row (the IRR is by definition
the rate that zeros the NPV, so varying the discount rate does not
move the IRR).

Variable spec
-------------

* :func:`pvbess_opt.sensitivity.variables_for_npv_sensitivity` — the
  four drivers above.
* :func:`pvbess_opt.sensitivity.variables_for_irr_sensitivity` — the
  three drivers minus the discount rate.

Output DataFrame columns
------------------------

Long-form ("tidy") DataFrame so plotting code can ``groupby`` on
``variable`` and pivot ``scenario`` without further reshaping:

============================  =====================================
Column                        Notes
============================  =====================================
``variable``                  ``CAPEX`` | ``OPEX`` | ``Revenue`` |
                              ``DiscountRate``.
``label``                     Human-readable label for plot legend.
``scenario``                  ``low`` | ``base`` | ``high``.
``delta_value``               Perturbation in natural unit
                              (relative = fraction; absolute = pp).
``value``                     Perturbed driver value (e.g.
                              base CAPEX × 1.10).
``npv_eur``                   Perturbed NPV.
``irr_pct``                   Perturbed IRR.
``payback_years``             Perturbed simple payback.
``delta_npv_eur``             Signed delta vs base NPV.
``delta_irr_pp``              Signed delta vs base IRR.
``delta_payback_years``       Signed delta vs base payback.
============================  =====================================
