Asset modes — PV-only / BESS-only / hybrid
==========================================

The loader reads zero literally — there is no inference from the
timeseries or from a power-key fallback.  The four cases are:

.. list-table::
   :header-rows: 1

   * - ``pv_nameplate_kwp``
     - ``bess_power_kw``
     - Configuration
   * - > 0
     - > 0
     - Hybrid PV+BESS.
   * - > 0
     - = 0
     - PV-only (no battery).
   * - = 0
     - > 0
     - BESS-only (only meaningful with ``allow_bess_grid_charging = TRUE``).
   * - = 0
     - = 0
     - Invalid — ``read_inputs`` raises ``ValueError``.

The chosen mode is reflected as a subtitle on every energy-plot title
("PV-only project" / "BESS-only project" / "Hybrid PV+BESS project")
through the ``set_project_mode_label`` helper in
``pvbess_opt.plotting.style``.

Optimizer behaviour
-------------------

``pvbess_opt.optimization.build_model`` introduces two flags after
parsing ``params``:

.. code-block:: python

   pv_present   = params["pv_nameplate_kwp"] > 0.0
   bess_present = params["bess_power_kw"] > 0.0

When **pv_present is False** the per-step ``pv`` dict is overridden to
zero (the ``pv_kwh`` column is no longer trusted) and four pin
constraints zero out ``pv_to_load``, ``pv_to_bess``, ``pv_to_grid``,
``pv_curtail``.

When **bess_present is False** the optimizer pins ``e_cap = 0``,
zeros out ``soc``, ``pv_to_bess``, ``grid_to_bess``, ``bess_dis_load``,
``bess_dis_grid``, ``y_charge``, ``y_dis`` for all ``t``, and skips the
BESS-only constraints (``EP``, ``CYC``, ``SOC_INIT``, ``SOC_TERM*``,
``MODE_LINK``, ``CH_LIM``, ``DIS_LIM``, ``GRID_CHARGE_GATE``,
``GRID_CHG_PV_GATE``).  The self_consumption-mode ``LOAD_BAL`` constraint stays
active — the load is still served by some combination of PV and grid
even when the BESS is absent.

Capacity helper
---------------

``pvbess_opt.economics.derive_asset_capacities`` does not infer
capacities from the timeseries or from any power-key fallback.
Declared values pass through exactly:

.. code-block:: python

   caps = {
       "pv_kwp":  max(params["pv_nameplate_kwp"], 0.0),
       "bess_kw": max(params["bess_power_kw"], 0.0),
       "bess_kwh": e_cap_kwh if bess_kw > 0 else 0.0,
   }

CAPEX / OPEX rows in ``build_yearly_cashflow`` automatically zero out
when the corresponding capacity is zero, so a PV-only project gets a
clean cashflow with no phantom BESS line items, and vice versa.

Plot behaviour
--------------

* The existing ``plot_stack_filtered`` helper drops zero series, so the
  self_consumption-mode supply / surplus / combined plots naturally hide the
  missing asset's stacks.
* Every energy-plot title carries a project-mode suffix —
  ``(self_consumption; PV-only)``, ``(merchant; BESS-only)``, etc — driven by the
  ``set_project_mode_label`` setter that ``main.py`` calls before
  the plot fan-out.
* The merchant-mode ``plot_*_soc`` helpers skip rendering when the BESS
  is absent (no SOC trajectory worth plotting).
* The lifecycle ``plot_lifetime_cycles``, ``plot_lcoe_summary``, and
  ``plot_lcos_summary`` show explicit "N/A" placeholders for the asset
  that is not part of the project.

Lifetime dispatch
-----------------

``pvbess_opt.lifetime.build_lifetime_dispatch`` works unchanged for the
three modes — when an asset's per-step columns are identically zero,
multiplying by the degradation factor is a no-op.  The timestamp
shift always applies regardless of ``pv_kwp`` or ``bess_kw``, so
``02_dispatch/dispatch_hourly.xlsx`` is internally consistent.

Smoke-test recipe
-----------------

.. code-block:: bash

   # Hybrid (the case-study default)
   python main.py inputs/input.xlsx --solver highs

   # PV-only
   # (edit inputs/input.xlsx so bess_power_kw = 0)
   python main.py inputs/input.xlsx --solver highs

   # BESS-only with grid charging
   # (edit inputs/input.xlsx so pv_nameplate_kwp = 0,
   #  allow_bess_grid_charging = TRUE)
   python main.py inputs/input.xlsx --solver highs

All three runs produce a complete output folder with no zero-
divisions, no NaN headline KPIs, and no empty plots.
