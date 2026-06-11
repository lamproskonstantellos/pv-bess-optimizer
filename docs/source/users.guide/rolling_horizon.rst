Rolling-horizon dispatch with imperfect foresight
==================================================

Why rolling horizon
-------------------

A single annual MILP with full visibility into every hour's DAM price,
PV output, and load is a **perfect-foresight** model — it produces an
upper bound on achievable profit, not a realistic operating result.

Industry tools (Aurora Chronos, Gridcog, Plexos with look-ahead) handle
this via **rolling-horizon dispatch with imperfect foresight + Monte
Carlo over forecast scenarios** — not full stochastic programming with
explicit scenario trees, which is overkill for a single-asset dispatch
problem and harder to defend operationally.

For a typical PV + BESS project the dominant uncertainty is **DAM price
forecast error**.  PV uncertainty is second-order when the input profile
is TMY-based (TMY is the long-term mean).  Load uncertainty is third-
order for predictable customers (e.g. hotel bookings known D-2).

Forecast-noise sigmas (defensible from literature):

================ =========== =====================================================
Variable         sigma       Source
================ =========== =====================================================
DAM price        0.20 (MAPE) ENTSO-E D+1 benchmark for volatile markets
PV generation    0.12 (RMSE) NREL day-ahead PV forecast study
Load             0.05 (MAPE) Predictable-customer benchmark (booking horizon)
================ =========== =====================================================

These are **defaults**; the workbook and the CLI both expose them as
overridable parameters.

How the algorithm works
-----------------------

For each window starting at hour :math:`t \in \{0, c, 2c, \ldots\}` where
:math:`c` is ``commit_hours``:

1. Slice ``ts[t : t + window_hours]``.
2. Apply forecast noise beyond ``commit_hours`` (skipped if
   ``forecast_seed=None`` — gives a deterministic rolling horizon).
3. Solve the MILP with the noisy window; pin ``initial_soc`` to the
   SOC carried over from the previous window.
4. Keep the first ``commit_hours`` of the dispatch as the committed
   slice.
5. Pass ``soc_kwh[commit_hours]`` as ``initial_soc`` to the next window.

The MILP's ``terminal_soc_equal`` constraint is **disabled** *within*
rolling-horizon windows (a window should not close its own cycle), but
when the workbook sets ``terminal_soc_equal`` every window that reaches
the end of the horizon pins its post-final-step SOC to the
**year-initial** SOC.  The stitched dispatch then honours the same
closed-cycle condition as the annual perfect-foresight benchmark —
without it the final window would drain the battery for profit the
benchmark is not allowed to take, biasing the foresight comparison.
The BESS energy capacity ``e_cap`` is pinned at workbook
load (the per-window MILP reads ``e_cap`` as a constant from the
start) so every window operates against the same physical asset.

Re-evaluation with actuals
~~~~~~~~~~~~~~~~~~~~~~~~~~

When ``evaluate_with_actuals=True`` (default) the returned KPIs are
recomputed against the **original** (noise-free) timeseries — this
reflects realised performance.  Otherwise KPIs reflect what the solver
thought it was getting given the forecast.

The implementation runs ``add_economic_columns`` and ``compute_kpis`` on
the committed dispatch with the original prices, then applies the same
post-solve unavailability derate as the pipeline's headline Year-1 KPIs
so the rolling-horizon and perfect-foresight numbers share one scope.
It does **not** re-solve.

Foresight gap
~~~~~~~~~~~~~

.. math::

   \text{foresight\_gap\_pct} = 100 \cdot \left( 1 - \frac{\text{rh\_profit}}{\text{pf\_profit}} \right)

where :math:`\text{pf\_profit}` is the perfect-foresight benchmark
computed from the existing single-MILP solve (the headline,
unavailability-derated ``profit_total_eur``) and
:math:`\text{rh\_profit}` carries the identical derate, so the gap is
derate-invariant.  Positive values mean imperfect foresight reduces
profit.  Because every seed's stitched dispatch — including the
year-close SOC condition — is feasible for the perfect-foresight MILP,
the gap cannot go negative beyond the solver's ``mip_gap`` slack; with
zero forecast noise it collapses to ~0.

CLI examples
------------

.. code-block:: bash

   # Single deterministic noiseless rolling horizon
   python main.py inputs/input.xlsx \
       --rolling-horizon \
       --window-hours 48 \
       --commit-hours 24 \
       --solver highs

   # Full Monte Carlo (30 seeds)
   python main.py inputs/input.xlsx \
       --rolling-horizon \
       --window-hours 48 \
       --commit-hours 24 \
       --monte-carlo 30 \
       --seed 42 \
       --solver highs

   # Merchant mode rolling horizon
   python main.py inputs/input.xlsx \
       --mode merchant \
       --rolling-horizon \
       --monte-carlo 30 \
       --solver highs

Output artifacts
----------------

* ``03_results.xlsx`` gains a ``rolling_horizon_mc`` sheet with one row
  per seed: ``seed``, ``profit_total_eur``, ``grid_export_mwh``,
  ``grid_import_mwh``, ``pv_curtailed_mwh``, ``bess_cycles_total``,
  ``foresight_gap_pct``.
* ``04_financial_plots/rolling_horizon_distribution.pdf`` — histogram
  of MC profit values with vertical lines at P10 / P50 / P90 and a
  separate dashed marker at the perfect-foresight benchmark.
* New KPI keys (only populated when ``--rolling-horizon`` is active):
  ``foresight_gap_pct_p50``, ``foresight_gap_pct_p10``,
  ``foresight_gap_pct_p90``, ``mc_n_seeds``, ``mc_window_hours``,
  ``mc_commit_hours``.

Limitations
-----------

* **i.i.d. noise** — no temporal correlation in forecast errors
  (consecutive hours are independently perturbed).
* **Log-normal assumption** — multiplicative noise with an asymmetric
  long tail; preserves positivity for PV and load and the sign for DAM
  prices (sign-aware).
* **No inter-asset correlation** — a high-PV hour does not depress the
  DAM price in the noise model.

These limitations are deliberate.  Adding cross-asset correlation
without a defensible empirical basis would create an illusion of
precision; future work could lift them with a copula-based forecast model.
