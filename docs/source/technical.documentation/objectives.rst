Objective: profit (single)
==========================

The optimiser ships with a **single objective** — profit maximisation.

Why not "max-green" or a blended objective?
-------------------------------------------

Under Greek VNB economics, retail (132 EUR/MWh) > DAM avg (~100 EUR/MWh)
in **>99 % of hours**.  The ``profit`` objective therefore maximises
self-consumption emergently via the load-priority slack and produces the
same dispatch as a green objective would in this market.

In ``merchant`` mode there is no co-located load to "be green about" in
the first place.

Adding multiple objectives only burdens the user with a meaningless
choice.  The optimiser ships with one objective.

Profit-maximisation expression
------------------------------

In ``vnb`` mode the objective is:

.. math::

   \max \quad \sum_{t} \frac{r_t \cdot (p^{\text{pv→load}}_t + p^{\text{bess→load}}_t)}{1000}
            + \sum_{t} \frac{d_t \cdot (p^{\text{pv→grid}}_t + p^{\text{bess→grid}}_t)}{1000}
            - \sum_{t} \frac{d_t \cdot p^{\text{grid→bess}}_t}{1000}
            - \epsilon \cdot \sum_{t} p^{\text{curtail}}_t

where :math:`r_t` is the retail tariff (EUR/MWh), :math:`d_t` is the
DAM price (EUR/MWh), and :math:`\epsilon` is the curtailment
tiebreaker (default :math:`10^{-5}` EUR/kWh).

In ``merchant`` mode the avoided-cost term is dropped (load flows are
pinned to zero):

.. math::

   \max \quad \sum_{t} \frac{d_t \cdot (p^{\text{pv→grid}}_t + p^{\text{bess→grid}}_t)}{1000}
            - \sum_{t} \frac{d_t \cdot p^{\text{grid→bess}}_t}{1000}
            - \epsilon \cdot \sum_{t} p^{\text{curtail}}_t

The curtailment tiebreaker is a *tiny* term that adds determinism
under degeneracy (multiple optima).  In v0.6 the weight is a private
module-level constant in :mod:`pvbess_opt.optimization` (set to 0 to
disable); it is no longer exposed in the workbook.
