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

In ``self_consumption`` mode the objective is (equation S1 in
``docs/self_consumption_design.md``):

.. math::

   \max \quad \sum_{t} \frac{r_t \cdot (p^{\text{pv→load}}_t + p^{\text{bess→load}}_t)}{1000}
            + \sum_{t} \frac{p^{\text{eff}}_t \cdot p^{\text{pv→grid}}_t
                            + d_t \cdot p^{\text{bess→grid}}_t}{1000}
            - \sum_{t} \frac{d_t \cdot p^{\text{grid→bess}}_t}{1000}
            - c^{w} \sum_{t} \frac{p^{\text{bess→load}}_t + p^{\text{bess→grid}}_t}{1000}
            + R^{\text{bm}}
            - \epsilon \cdot \sum_{t} p^{\text{curtail}}_t

where :math:`r_t` is the retail tariff (EUR/MWh), :math:`d_t` the DAM
price (EUR/MWh), and

* :math:`p^{\text{eff}}_t = (1-s) \cdot d_t + s \cdot \text{strike}`
  is the **PPA-adjusted PV export price** when a pay-as-produced
  contract is active (``docs/ppa_design.md``, equation P4); without a
  contract :math:`p^{\text{eff}}_t = d_t`;
* :math:`c^{w}` = ``bess_wear_cost_eur_per_mwh`` is the
  discharge-throughput wear cost (default 0; a dispatch shadow price,
  never added to the reported cashflow);
* :math:`R^{\text{bm}}` is the expected balancing revenue, present
  only when ``balancing_enabled``
  (``docs/balancing_market_design.md``, equation B7);
* :math:`\epsilon` is the curtailment tiebreaker (default
  :math:`10^{-5}` EUR/kWh).

In ``merchant`` mode the avoided-cost term is dropped (load flows are
pinned to zero; equation M3 in ``docs/merchant_design.md``):

.. math::

   \max \quad \sum_{t} \frac{p^{\text{eff}}_t \cdot p^{\text{pv→grid}}_t
                            + d_t \cdot p^{\text{bess→grid}}_t}{1000}
            - \sum_{t} \frac{d_t \cdot p^{\text{grid→bess}}_t}{1000}
            - c^{w} \sum_{t} \frac{p^{\text{bess→load}}_t + p^{\text{bess→grid}}_t}{1000}
            + R^{\text{bm}}
            - \epsilon \cdot \sum_{t} p^{\text{curtail}}_t

The curtailment tiebreaker is a *tiny* term that adds determinism
under degeneracy (multiple optima).  The weight is a private
module-level constant in :mod:`pvbess_opt.optimization` (set to 0 to
disable); it is not exposed in the workbook.
