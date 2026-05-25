Objective: profit (single)
==========================

The optimiser ships with a **single objective** — profit maximisation.

Why not "max-green" or a blended objective?
-------------------------------------------

When the user's retail tariff exceeds the DAM price in the majority
of hours (the typical case for ``self_consumption`` projects with a co-located
load) the ``profit`` objective maximises self-consumption
emergently via the load-priority slack and produces the same
dispatch a green objective would.  Self-consumption falls out of
the economics; it is not encoded as a hard preference in the
objective.

In ``merchant`` mode there is no co-located load to "be green about" in
the first place.

Adding multiple objectives only burdens the user with a meaningless
choice.  The optimiser ships with one objective.

Profit-maximisation expression
------------------------------

In ``self_consumption`` mode the objective is:

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
under degeneracy (multiple optima).  The weight is a private
module-level constant in :mod:`pvbess_opt.optimization` (set to 0 to
disable); it is not exposed in the workbook.
