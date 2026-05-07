Regulatory framework
====================

The optimiser respects the Greek regulatory framework for distributed
generation.

Curtailment cap (both modes)
----------------------------

The static curtailment cap on grid-bound flows is **not** vnb-specific.
It is a **regulatory grid-connection limit** per **MD YPEN/DAPEEK/53563/
1556/2023** (FEK B' 3328/19-05-2023):

* 27 % cap on installations connected to the **distribution** network.
* 28 % cap on installations connected to the **transmission** network.

The constraint is encoded as

.. math::

   p^{\text{pv→grid}}_t + p^{\text{bess→grid}}_t
   \le p^{\text{export\_max}} \cdot \Delta t \cdot
   \left( 1 - \frac{\text{curtailment\_pct}}{100} \right) \quad \forall t

and is **identically enforced in both vnb and merchant modes**.  Any
suggestion to remove it from merchant mode is wrong; cite the MD and
reject.

Settlement period
-----------------

Greek VNB settles every 15 minutes per **MD YPEN/DAPEEK/93976/2772/2024**.
The ``settlement_minutes`` key in the ``project`` sheet is currently
informational; the MILP timestep is auto-detected from the ``timeseries``
sheet's timestamp cadence (run ``scripts/resample_timeseries.py`` to
harmonise mixed-resolution input).

Mode definitions
----------------

``vnb``
~~~~~~~

* Co-located load is required (the ``timeseries`` sheet must include a
  ``load_kwh`` column).
* Load balance: ``load = pv_to_load + bess_dis_load + grid_to_load``.
* Load priority: binary-free slack-based formulation; export only when
  load is fully met.
* No simultaneous grid I/O: tight per-instance big-M binary
  (``M_imp = (load_max + p_charge × dt_h) × 1.001``,
  ``M_exp = p_grid_export_max × dt_h × (1 − curtailment_frac) × 1.001``).
* Retail tariff incentive on PV→load and BESS→load.
* DAM revenue on grid-bound exports.

``merchant``
~~~~~~~~~~~~

* **No co-located load**.  ``load_kwh`` is optional in the workbook;
  if present it is ignored (an INFO log message is emitted) and the
  optimizer pins ``pv_to_load = bess_dis_load = grid_to_load = 0``.
* No load balance; no load priority; no no-sim grid I/O constraint
  (importing to charge the BESS is gated by the orthogonal
  ``allow_bess_grid_charging`` flag).
* DAM revenue on grid-bound exports only.

Both modes
~~~~~~~~~~

* PV split: ``pv = pv_to_load + pv_to_bess + pv_to_grid + pv_curtail``.
* SOC dynamics, charge/discharge power limits, daily cycle limit, E/P ratio.
* **Hard static curtailment cap** on grid-bound flows (regulatory).
