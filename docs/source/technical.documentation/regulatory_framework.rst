Regulatory framework
====================

The optimiser models the regulatory mechanics of distributed
generation (injection caps, settlement cadence) with every limit
taken as a plain user input.

Max-injection cap (both modes)
------------------------------

The static cap on grid-bound flows is **not** self_consumption-specific.  It
is a **grid-connection limit** whose allowed-injection percentage is a
plain user input, expressed as the share of
``p_grid_export_max_kw`` available for export (a value of X means
X % allowed injection, equivalently 100 - X % curtailment).  Any
national or contractual curtailment rule is modelled by entering its
percentage on the ``max_injection`` sheets.

The constraint is encoded as

.. math::

   p^{\text{pv→grid}}_t + p^{\text{bess→grid}}_t
   \le p^{\text{export\_max}} \cdot \Delta t \cdot
   \frac{\text{max\_injection\_pct}}{100} \quad \forall t

and is **identically enforced in both self_consumption and merchant
modes**.  Any suggestion to remove it from merchant mode is wrong;
the cap is a property of the grid connection, not of the market
regime.

Optional strict total-injection cap
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

By default the cap above binds only on **surplus export**
(``pv_to_grid + bess_dis_grid``).  Under Virtual Net-Billing, however,
the energy *virtually allocated* to a remote load is **physically
injected** at the plant connection point too, so the regulatory limit
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
at the retail tariff and surplus PV is curtailed; the run is never
infeasible, it degrades to the maximum feasible coverage.  Leaving the
input at its default ``FALSE`` keeps the surplus-export cap and is
bit-for-bit backward compatible.

Per-source injection sub-caps
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The cap can additionally be split by origin via two optional inputs,
``max_injection_profile_pv`` and ``max_injection_profile_bess``, on the
same ``p_grid_export_max_kw`` nameplate.  Each binds the corresponding
origin's injection, PV (``pv→load`` + ``pv→grid``) or BESS
(``bess→load`` + ``bess→grid``), at
:math:`p^{\text{export\_max}} \cdot \Delta t \cdot \text{mi\_pct}_{\text{src}}/100`,
with the load-serving terms counted only under
``grid_cap_includes_load = TRUE`` (surplus export alone otherwise).  Each
sub-cap is attached only when its profile is supplied, applies in both
``self_consumption`` and ``merchant`` modes, and is layered on top of the
combined cap, which continues to bound the total injection.  Under the
strict cap the load-priority floor tightens to
:math:`\min(\text{pv}_t, l_t, \text{cap}_t, \text{cap}^{\text{pv}}_t)`.

Settlement period
-----------------

The ``self_consumption`` regime settles every 15 minutes.
The MILP timestep is auto-detected from the ``timeseries`` sheet's
timestamp cadence (run ``scripts/resample_timeseries.py`` to harmonise
mixed-resolution input), so the canonical workbook ships a 15-minute
grid and there is no separate settlement-period key to configure; the
balancing sheet's ``bm_settlement_minutes`` is validated against the
detected cadence when balancing is enabled.

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
