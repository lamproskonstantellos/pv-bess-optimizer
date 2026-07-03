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
zero forecast noise it reduces to the pure horizon-truncation cost
(~0 on short single-window fixtures; measured 0.32 % on the shipped
full-year workbook, see below).  That bound is enforced at
runtime: a seed whose profit exceeds
``pf_profit + 2 * mip_gap * |pf_profit| + 1 EUR`` triggers a prominent
warning, or a hard error under ``--strict``.  The percentage formula
assumes a positive benchmark; for a non-positive ``pf_profit`` the sign
meaning inverts, a warning is emitted, and the absolute profit column
should be read instead.

Validation of the observed gap magnitude
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The shipped self-consumption case study shows a median foresight gap of
roughly half a percent of annual profit.  Two checks establish that
this is a genuine cost of imperfect information rather than solver
noise (measured on the shipped workbook, 48 h window / 24 h commit):

* **Tight-gap re-run.**  At ``mip_gap = 1e-5`` (PF benchmark
  2,849,785 EUR) a 5-seed ensemble produced gaps of 0.445 to 0.481 %
  with a median of 0.464 % — the same magnitude as at the default
  ``mip_gap = 0.001`` (3 seeds: 0.440 / 0.462 / 0.476 %).  The gap is
  roughly 50x the combined solver slack, so it is not an optimality
  artifact.
* **Sigma-to-zero collapse.**  With all noise sigmas at 0 the gap drops
  to 0.324 %: this residual is the pure horizon-truncation cost of
  re-optimising 48 h at a time (a window cannot position SOC for
  opportunities beyond its lookahead, and the year-end windows discover
  the closed-cycle SOC condition only 48 h before year end).  On short
  single-window fixtures, where truncation cannot bite, the same
  collapse lands at ~0 (see ``tests/test_rolling_horizon_scope.py``).

The decomposition is therefore: about 0.32 pp of the ~0.46 % median
gap is horizon truncation, and forecast noise adds the remaining
~0.14 pp.  The noise contribution is small because self-consumption
profit is dominated by retail-priced avoided cost, and the retail
tariff is never perturbed (load noise is small, sigma 0.05, and
irrelevant to the tariff), so only the DAM-exposed export and arbitrage
slice degrades under the sigma 0.20 DAM noise.

PV-only plants: rolling horizon equals perfect foresight
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

On a plant without storage there are no look-ahead-dependent decisions:
the committed slice of every window is solved on noise-free data (noise
only perturbs rows beyond the commit horizon), and PV dispatch inside
the commit window does not depend on anything the window cannot see.
Every Monte Carlo seed therefore reproduces the perfect-foresight
dispatch exactly — verified on a PV-only copy of the shipped workbook,
where all seeds land on the same profit to the cent and the foresight
gap is 0.00 %.  This identity is expected behaviour, not a bug.  The
``rolling_horizon_distribution`` plot detects the degenerate ensemble
(seed spread below max(1 EUR, 1e-6 x |P50|)) and renders a dedicated
layout: one narrow bar at the common value, a readable x-window,
whole-euro tick labels, a collapsed legend and an annotation stating
that forecast noise has no effect on the configuration.

Year-close SOC shortfall
~~~~~~~~~~~~~~~~~~~~~~~~

The year-close SOC condition is enforced on the final window as a
target relaxed by a heavily penalised shortfall variable (10 EUR/kWh,
far above any energy price).  The relaxation exists because a hard
equality can be physically unreachable: on the shipped workbook the
last 48 hours of December carry only ~3.9 MWh of PV surplus above
load, and with surplus-only charging the battery cannot climb from a
drained state back to the 30 MWh target, which would make the final
window infeasible and abort the run.  When the shortfall activates the
run completes, ends the year at the highest reachable SOC, logs a
prominent warning, and reports ``year_close_soc_shortfall_kwh`` in the
rolling-horizon KPIs.  The perfect-foresight bound check widens by the
shortfall's maximum energy value so the "no seed beats PF" guard stays
sound.  In the deterministic (sigma-zero) run the shortfall is
14,188 kWh; the noisy seeds close the cycle fully (shortfall 0).

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
* **Per-window cycle cap** — ``max_cycles_per_day`` binds inside each
  window slice, not on the stitched year.  A window sees at most
  ``window_hours`` of horizon, so the cap it enforces is the pro-rata
  share of the daily budget for that slice; across the stitched year
  the realised cycles can differ slightly from what the annual
  perfect-foresight MILP would allow under the same cap.  The effect is
  small at the default 48 h window and shows up in the
  ``bess_cycles_total`` column of the MC sheet.
* **Integer step counts required** — ``window_hours`` and
  ``commit_hours`` must be an integer number of steps at the input
  cadence; non-divisible combinations raise a ``ValueError`` instead of
  silently truncating the horizon.

These limitations are deliberate.  Adding cross-asset correlation
without a defensible empirical basis would create an illusion of
precision; future work could lift them with a copula-based forecast model.
