MIP formulation
===============

The MILP is formulated in `Pyomo <https://pyomo.readthedocs.io/>`_ and
lives in :mod:`pvbess_opt.optimization`.

.. note::

   The **authoritative formulation** — every constraint as a numbered
   equation with its implementing symbol — lives in the domain design
   documents ``docs/self_consumption_design.md`` (equations S1–S34),
   ``docs/merchant_design.md`` (M1–M3) and
   ``docs/balancing_market_design.md`` (B1–B8), indexed by
   ``docs/README.md``.  This page is a one-stop summary kept verbatim-
   consistent with those documents.

Decision variables (per timestep, kWh)
--------------------------------------

* ``pv_to_load[t]``, ``pv_to_bess[t]``, ``pv_to_grid[t]``,
  ``pv_curtail[t]`` — PV split.
* ``bess_dis_load[t]``, ``bess_dis_grid[t]`` — BESS discharge.
* ``grid_to_load[t]``, ``grid_to_bess[t]`` — grid-bound flows.
* ``soc[t]`` — state-of-charge (kWh).
* ``slack[t]`` — surplus-only-export slack
  (``self_consumption`` only).
* ``y_charge[t]``, ``y_dis[t]``, ``y_grid_io[t]``, ``z_pv_active[t]``
  — binary indicators (``y_grid_io`` exists in ``self_consumption``
  only; ``z_pv_active`` only when grid charging is enabled).
* ``r_balancing[k, t]`` — per-product reserved kW
  (``balancing_enabled`` only; see
  ``docs/balancing_market_design.md``).

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
       + \delta^{\text{ch}}_t - \delta^{\text{dis}}_t

where the expected-activation drift terms
:math:`\delta^{\text{ch}}_t, \delta^{\text{dis}}_t` are nonzero only
when ``balancing_enabled`` (equation B6 in
``docs/balancing_market_design.md``).  SOC is bounded inside
``[soc_min_frac, soc_max_frac] × e_cap``; ``soc[0]`` is pinned to
``initial_soc_frac × e_cap``; with ``terminal_soc_equal = TRUE`` the
post-final-step SOC closes the cycle back to ``soc[0]``
(``SOC_TERM``), otherwise it is only kept within the SOC bounds.

Daily cycle cap — per calendar day :math:`d`:

.. math::

   \sum_{t \in d} \left(p^{\text{bess→load}}_t + p^{\text{bess→grid}}_t\right)
   \le \text{max\_cycles\_per\_day} \cdot e_{\text{cap}}

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

   g_t \le p^{\text{export\_max}} \cdot \Delta t \cdot
       \text{max\_injection\_frac}

The cap basis :math:`g_t` (``grid_injection_total``) is selected by the
optional ``grid_cap_includes_load`` project input:

* **Default** (``grid_cap_includes_load = FALSE``) — binds on surplus
  export only,

  .. math::

     g_t = p^{\text{pv→grid}}_t + p^{\text{bess→grid}}_t

  which is bit-for-bit backward compatible with earlier releases.

* **Strict** (``grid_cap_includes_load = TRUE``, ``self_consumption``
  only) — binds on the **total plant injection** at the connection point,

  .. math::

     g_t = p^{\text{pv→load}}_t + p^{\text{bess→load}}_t
           + p^{\text{pv→grid}}_t + p^{\text{bess→grid}}_t

  Under Virtual Net-Billing the energy virtually allocated to a remote
  load is physically injected at the plant connection point too, so the
  regulatory limit is a **physical plant-injection cap**, not merely a
  surplus-export cap.  Load priority stays strict but shares the cap: its
  floor becomes :math:`\min(\text{pv}_t, l_t, \text{cap}_t)` — and when a
  PV-source sub-cap sheet is supplied the floor is additionally bounded
  by the per-step PV sub-cap — so the load takes all available injection
  capacity before any surplus export.  When
  the cap cannot fit the full load the uncovered remainder is grid-served
  at the retail tariff and surplus PV is curtailed — the run is never
  infeasible, it degrades to the maximum feasible coverage.  In
  ``merchant`` mode there is no
  co-located load, so :math:`g_t` collapses to surplus export and the
  flag is a no-op.

Optional per-source injection sub-caps — when the
``max_injection_profile_pv`` / ``max_injection_profile_bess`` sheets
are supplied, the PV-origin and BESS-origin injections are additionally
capped per step (``EXPORT_CAP_PV`` / ``EXPORT_CAP_BESS``); the combined
cap above still binds.

Grid-charging gates — only when ``allow_bess_grid_charging = TRUE``
and a BESS is present (both modes):

.. math::

   p^{\text{grid→bess}}_t \le M^{\text{ch}} \cdot (1 - z^{\text{pv}}_t),
   \qquad
   p^{\text{pv}}_t \le M^{\text{pv}} \cdot z^{\text{pv}}_t

so the BESS charges from the grid only in steps where PV is effectively
zero.  With the flag off, ``grid_to_bess[t] = 0`` is pinned.

Balancing extension — with ``balancing_enabled = TRUE`` the model
additionally carries the per-product reservation bounds, the
per-direction power budgets (``BM_POWER_DN`` / ``BM_POWER_UP``), the
SOC headroom constraints (``BM_SOC_UP`` / ``BM_SOC_DN``) and the
expected-revenue objective terms — equations B1–B7 in
``docs/balancing_market_design.md`` (this page does not duplicate
them).

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
