MIP formulation
===============

The MILP is formulated in `Pyomo <https://pyomo.readthedocs.io/>`_ and
lives in :mod:`pvbess_opt.optimization`.

Decision variables (per timestep, kWh)
--------------------------------------

* ``pv_to_load[t]``, ``pv_to_bess[t]``, ``pv_to_grid[t]``,
  ``pv_curtail[t]`` — PV split.
* ``bess_dis_load[t]``, ``bess_dis_grid[t]`` — BESS discharge.
* ``grid_to_load[t]``, ``grid_to_bess[t]`` — grid-bound flows.
* ``soc[t]`` — state-of-charge (kWh).
* ``y_charge[t]``, ``y_dis[t]``, ``y_grid_io[t]``, ``z_pv_active[t]``
  — binary indicators.

The BESS energy capacity ``e_cap`` is a fixed parameter pinned to
``bess_capacity_kwh`` from the workbook — no longer a decision
variable.

Constraints
-----------

PV split (always active):

.. math::

   p^{\text{pv}}_t = p^{\text{pv→load}}_t + p^{\text{bess←pv}}_t
                  + p^{\text{pv→grid}}_t + p^{\text{curtail}}_t

Load balance (self_consumption only):

.. math::

   l_t = p^{\text{pv→load}}_t + p^{\text{bess→load}}_t + p^{\text{grid→load}}_t

SOC dynamics:

.. math::

   \text{soc}[t+1] = \text{soc}[t] + \eta_{\text{ch}} \cdot
       (p^{\text{bess←pv}}_t + p^{\text{grid→bess}}_t)
       - \frac{p^{\text{bess→load}}_t + p^{\text{bess→grid}}_t}{\eta_{\text{dis}}}

Charge / discharge power limits — a single symmetric per-step energy
cap derived from ``bess_power_kw``:

.. math::

   \text{bess\_step\_lim} = p^{\text{bess}} \cdot \Delta t

   p^{\text{bess←pv}}_t + p^{\text{grid→bess}}_t
   \le \text{bess\_step\_lim} \cdot y^{\text{charge}}_t

   p^{\text{bess→load}}_t + p^{\text{bess→grid}}_t
   \le \text{bess\_step\_lim} \cdot y^{\text{dis}}_t

   y^{\text{charge}}_t + y^{\text{dis}}_t \le 1

Charge and discharge power are both driven by the symmetric
``bess_power_kw`` key; the schema does not carry separate per-direction
limits.

Static max-injection cap (BOTH modes):

.. math::

   p^{\text{pv→grid}}_t + p^{\text{bess→grid}}_t
   \le p^{\text{export\_max}} \cdot \Delta t \cdot
       \text{max\_injection\_frac}

In ``self_consumption`` mode additionally:

* **PV→Load priority (Section 2, hard)** — pinned exactly:

  .. math::

     p^{\text{pv→load}}_t \ge \min(\text{pv}_t, l_t) \quad \forall t

  Combined with the PV-split and load-balance equalities this forces
  ``pv_to_load[t] == min(pv[t], load[t])`` exactly, so all available
  PV (up to the load) is consumed by the load.

* **Surplus-only export (Section 5)** — binary-free slack formulation:

  .. math::

     s_t \ge p^{\text{pv}}_t + p^{\text{bess→load}}_t
            + p^{\text{bess→grid}}_t - l_t

     p^{\text{pv→grid}}_t + p^{\text{bess→grid}}_t \le s_t

* **No simultaneous grid I/O** — strict, tight big-M:

  .. math::

     p^{\text{grid→load}}_t + p^{\text{grid→bess}}_t
     \le M^{\text{imp}} \cdot y^{\text{grid\_io}}_t

     p^{\text{pv→grid}}_t + p^{\text{bess→grid}}_t
     \le M^{\text{exp}} \cdot (1 - y^{\text{grid\_io}}_t)

In ``merchant`` mode the load-coverage flows are pinned to zero:

.. math::

   p^{\text{pv→load}}_t = p^{\text{bess→load}}_t = p^{\text{grid→load}}_t = 0

Tight big-M values
------------------

Big-M values are derived per-instance by
:func:`pvbess_opt.optimization.derive_tight_big_m`:

.. math::

   M^{\text{imp}} = (l^{\text{max}} + \text{bess\_step\_lim}) \cdot 1.001

   M^{\text{exp}} = p^{\text{export\_max}} \cdot \Delta t \cdot
                    \text{max\_injection\_frac} \cdot 1.001

   M^{\text{ch}} = \text{bess\_step\_lim} \cdot 1.001

   M^{\text{pv}} = \max_t p^{\text{pv}}_t \cdot 1.001

Audit invariants
----------------

After every solve :func:`pvbess_opt.optimization.verify_dispatch_invariants`
checks nine invariants:

1. **PV balance** — ``pv = pv_to_load + pv_to_bess + pv_to_grid + pv_curtail``.
2. **Load balance** — self_consumption only; 0 in merchant.
3. **SOC dynamics** — per-step continuity of ``soc[t+1] - soc[t]``
   against the charge/discharge expression.
4. **RTE bound** — ``Σ discharge ≤ η_ch × η_dis × Σ charge + η_dis ×
   (soc[0] - final_state)``.
5. **No-sim grid I/O** — self_consumption only; max product of grid-import × grid-
   export across all timesteps.
6. **Load priority (Section 5)** — self_consumption only; count of timesteps with
   simultaneous export > 0 and grid_to_load > 0.
7. **Curtail behavior** — cap not binding ⇒ curtail = 0.  Checked in
   **both** modes.
8. **Closed-cycle SOC** — when ``terminal_soc_equal=True``, ``final_state
   = soc[0]``.
9. **PV→Load priority (Section 2)** — self_consumption only; max absolute deviation
   of ``pv_to_load[t]`` from ``min(pv[t], load[t])``.

The ``--strict`` CLI flag turns invariant violations into errors.
