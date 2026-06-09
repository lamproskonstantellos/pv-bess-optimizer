Regulatory framework
====================

The optimiser respects the Greek regulatory framework for distributed
generation.

Max-injection cap (both modes)
------------------------------

The static cap on grid-bound flows is **not** self_consumption-specific.  It is
a **regulatory grid-connection limit** per **MD YPEN/DAPEEK/53563/
1556/2023** (FEK B' 3328/19-05-2023), expressed as the share of
``p_grid_export_max_kw`` available for export:

* 73 % allowed on installations connected to the **distribution**
  network (equivalently 27 % curtailment).
* 72 % allowed on installations connected to the **transmission**
  network (equivalently 28 % curtailment).

The constraint is encoded as

.. math::

   p^{\text{pv→grid}}_t + p^{\text{bess→grid}}_t
   \le p^{\text{export\_max}} \cdot \Delta t \cdot
   \frac{\text{max\_injection\_pct}}{100} \quad \forall t

and is **identically enforced in both self_consumption and merchant modes**.  Any
suggestion to remove it from merchant mode is wrong; cite the MD and
reject.

Optional strict total-injection cap
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

By default the cap above binds only on **surplus export**
(``pv_to_grid + bess_dis_grid``).  Under Virtual Net-Billing, however,
the energy *virtually allocated* to a remote load is **physically
injected** at the plant connection point too — so the regulatory limit
can equally be read as a cap on the **total plant injection**, not only
on surplus export.  Setting the project input
``grid_cap_includes_load = TRUE`` switches the cap basis to

.. math::

   p^{\text{pv→load}}_t + p^{\text{bess→load}}_t
   + p^{\text{pv→grid}}_t + p^{\text{bess→grid}}_t
   \le p^{\text{export\_max}} \cdot \Delta t \cdot
   \frac{\text{max\_injection\_pct}}{100} \quad \forall t

This only affects ``self_consumption`` mode (merchant has no co-located
load, so the basis collapses to surplus export).  Load priority stays
strict but shares the cap: its floor becomes
:math:`\min(\text{pv}_t, l_t, \text{cap}_t)`, so the load takes all
available injection capacity before any surplus export.  When the cap
cannot fit the full load the uncovered remainder is served from the grid
at the retail tariff and surplus PV is curtailed — the run is never
infeasible, it degrades to the maximum feasible coverage.  Leaving the
input at its default ``FALSE`` keeps the surplus-export cap and is
bit-for-bit backward compatible.

Settlement period
-----------------

Greek Self-consumption settles every 15 minutes per **MD YPEN/DAPEEK/93976/2772/2024**.
The ``settlement_minutes`` key in the ``project`` sheet is currently
informational; the MILP timestep is auto-detected from the ``timeseries``
sheet's timestamp cadence (run ``scripts/resample_timeseries.py`` to
harmonise mixed-resolution input).

Mode definitions
----------------

``self_consumption``
~~~~~~~~~~~~~~~~~~~~~

* Co-located load is required (the ``timeseries`` sheet must include a
  ``load_kwh`` column).
* Load balance: ``load = pv_to_load + bess_dis_load + grid_to_load``.
* Load priority: binary-free slack-based formulation; export only when
  load is fully met.
* No simultaneous grid I/O: tight per-instance big-M binary
  (``M_imp = (load_max + p_charge × dt_h) × 1.001``,
  ``M_exp = p_grid_export_max × dt_h × max_injection_frac × 1.001``).
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
* **Hard static max-injection cap** on grid-bound flows (regulatory);
  curtailed energy is reported in the outputs.
