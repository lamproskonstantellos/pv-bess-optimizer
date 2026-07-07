Rolling-horizon dispatch with imperfect foresight
==================================================

Why rolling horizon
-------------------

A single annual MILP with full visibility into every hour's DAM price,
PV output, and load is a **perfect-foresight** model: it produces an
upper bound on achievable profit, not a realistic operating result.

Industry tools (Aurora Chronos, Gridcog, Plexos with look-ahead) handle
this via **rolling-horizon dispatch with imperfect foresight + Monte
Carlo over forecast scenarios**, not full stochastic programming with
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
   ``forecast_seed=None``, which gives a deterministic rolling horizon).
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
closed-cycle condition as the annual perfect-foresight benchmark.
Without it the final window would drain the battery for profit the
benchmark is not allowed to take, biasing the foresight comparison.
The BESS energy capacity ``e_cap`` is pinned at workbook
load (the per-window MILP reads ``e_cap`` as a constant from the
start) so every window operates against the same physical asset.

Re-evaluation with actuals
~~~~~~~~~~~~~~~~~~~~~~~~~~

When ``evaluate_with_actuals=True`` (default) the returned KPIs are
recomputed against the **original** (noise-free) timeseries: this
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
profit.  Because every seed's stitched dispatch (including the
year-close SOC condition) is feasible for the perfect-foresight MILP,
the gap cannot go negative beyond the solver's ``mip_gap`` slack; with
zero forecast noise it reduces to the pure horizon-truncation cost
(~0 on short single-window fixtures; measured 0.32 % on the shipped
full-year workbook, see below).  That bound is enforced at
runtime: a seed whose profit exceeds
``pf_profit + 2 * mip_gap * |pf_profit| + 1 EUR`` triggers a prominent
warning, or a hard error under ``--strict``.  Within that slack, the
pipeline removes the artifact automatically by re-solving the
benchmark at a tighter gap (see the next section).  The percentage
formula assumes a positive benchmark; for a non-positive ``pf_profit``
the sign meaning inverts, a warning is emitted, and the absolute
profit column should be read instead.

Benchmark re-tightening: the best case stays the best case
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The benchmark incumbent returned by the annual MILP is only
``mip_gap``-optimal: the solver stops as soon as the incumbent is
within ``mip_gap`` (relative) of its best bound.  Each 48 h window, by
contrast, is a small problem the solver closes essentially to
optimality, so a stitched rolling-horizon dispatch can legitimately
land *above* a loose year-long incumbent — by up to the solver slack —
which would read as a spurious **negative** foresight gap even though
nothing is wrong with the model.  Grid charging makes this visible in
practice: the extra charge/discharge binaries make the annual MILP
markedly harder, so its incumbent tends to sit further from the bound
than the near-exact windows do.

The pipeline therefore enforces the best-case property directly.
After the Monte Carlo ensemble completes, if any realisation's profit
exceeds the benchmark, the benchmark MILP is re-solved at a 10x
tighter ``mip_gap`` (repeating, down to a floor of ``1e-6``) until it
is the best case again.  The foresight-gap column and the percentile
KPIs are then recomputed against the final benchmark, and every
downstream artifact — the financial model, ``03_results.xlsx``, and
all plots — uses the re-tightened solution.  The gap actually used is
recorded as the ``pf_benchmark_mip_gap`` KPI (the gap of the solve
that produced the final benchmark) and each re-solve is logged in
``run_log.txt``.  If a realisation still exceeds the benchmark when
the escalation ends, the residual difference is reported as a warning
and the slightly negative gap is left visible rather than masked.

A tighter gap only helps if the solver has time to use it.  Each
benchmark solve also carries the run's ``--time-limit``; when that
limit terminates the search, a deterministic solver walks the same
branch-and-bound tree in the same budget and returns the *identical*
incumbent no matter how tight the requested gap.  The guard therefore
accepts a re-solve only when it actually improves the incumbent: after
one unimproved probe it keeps the previous benchmark, stops
escalating, and logs that the time limit binds — the remedy is a
higher ``--time-limit`` or a faster solver (``--solver gurobi``
typically closes the hard grid-charging MILPs orders of magnitude
faster than HiGHS).  For publication runs the most economical recipe
is to avoid the escalation entirely by solving the benchmark tightly
up front, e.g. ``--solver gurobi --mip-gap 1e-4`` with a generous
time limit.

Requested versus proven gap
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``--mip-gap`` is a **target**, not a guarantee: it competes with
``--time-limit``, and whichever fires first stops the solve.  On a hard
year-scale grid-charging MILP the time limit usually binds first, so
the solver returns with a gap looser than requested — e.g. asking for
``1e-5`` but stopping at ``5e-4`` when the 30-minute limit ends the
search.  The run records BOTH numbers so the distinction is explicit:
``pf_benchmark_mip_gap`` is what was requested, and
``pf_benchmark_gap_achieved`` is what the solver actually proved (the
same relative gap it prints in its own log).  **A publication should
quote the achieved gap** as the benchmark's certified optimality.
Because the incumbent can only understate the true optimum, the
reported foresight gap is a conservative (lower-bound) estimate of the
value of perfect information, accurate to within that achieved gap; the
true optimum is bracketed by ``[incumbent, incumbent × (1 + achieved
gap)]``.

Validation of the observed gap magnitude
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The shipped self-consumption case study shows a median foresight gap
of roughly half a percent of annual profit: on the 2-hour
(15 MW / 30 MWh) workbook an 8-seed ensemble at ``mip_gap = 0.002``
produced gaps of 0.471 to 0.495 % with a median of 0.491 % against a
2,563,006 EUR perfect-foresight benchmark (48 h window / 24 h
commit).  Two further checks, measured on the earlier 4-hour
(15 MW / 60 MWh) configuration of the same workbook, establish that a
gap of this magnitude is a genuine cost of imperfect information
rather than solver noise:

* **Tight-gap re-run.**  At ``mip_gap = 1e-5`` (PF benchmark
  2,849,785 EUR) a 5-seed ensemble produced gaps of 0.445 to 0.481 %
  with a median of 0.464 %, the same magnitude as at the default
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

The decomposition on that configuration is therefore: about 0.32 pp
of the ~0.46 % median gap is horizon truncation, and forecast noise
adds the remaining
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
dispatch exactly, as verified on a PV-only copy of the shipped workbook,
where all seeds land on the same profit to the cent and the foresight
gap is 0.00 %.  This identity is expected behaviour, not a bug.  The
``rolling_horizon_distribution`` plot detects the degenerate ensemble
(seed spread below 1 EUR or one millionth of the median profit,
whichever is larger) and renders a dedicated
layout: one narrow bar at the common value, a readable x-window,
whole-euro tick labels and a collapsed ``MC seeds (all equal)``
legend entry next to the perfect-foresight marker.

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
* ``04_financial_plots/rolling_horizon_distribution.pdf``: histogram
  of MC profit values with vertical lines at P10 / P50 / P90 and a
  separate dashed marker at the perfect-foresight benchmark.
* New KPI keys (only populated when ``--rolling-horizon`` is active):
  ``foresight_gap_pct_p50``, ``foresight_gap_pct_p10``,
  ``foresight_gap_pct_p90``, ``mc_n_seeds``, ``mc_window_hours``,
  ``mc_commit_hours``, ``pf_benchmark_mip_gap`` (the ``mip_gap``
  requested for the final perfect-foresight benchmark solve — tighter
  than the configured value when the re-tightening guard fired) and
  ``pf_benchmark_gap_achieved`` (the gap the solver actually proved —
  the number a publication should quote; see "Requested versus proven
  gap" above).  ``SUMMARY.md`` renders both under a "Rolling-horizon
  foresight" section.

Limitations
-----------

* **i.i.d. noise**: no temporal correlation in forecast errors
  (consecutive hours are independently perturbed).
* **Log-normal assumption**: multiplicative noise with an asymmetric
  long tail; preserves positivity for PV and load and the sign for DAM
  prices (sign-aware).
* **No inter-asset correlation**: a high-PV hour does not depress the
  DAM price in the noise model.
* **Per-window cycle cap**: ``max_cycles_per_day`` binds inside each
  window slice, not on the stitched year.  A window sees at most
  ``window_hours`` of horizon, so the cap it enforces is the pro-rata
  share of the daily budget for that slice; across the stitched year
  the realised cycles can differ slightly from what the annual
  perfect-foresight MILP would allow under the same cap.  The effect is
  small at the default 48 h window and shows up in the
  ``bess_cycles_total`` column of the MC sheet.
* **Integer step counts required**: ``window_hours`` and
  ``commit_hours`` must be an integer number of steps at the input
  cadence; non-divisible combinations raise a ``ValueError`` instead of
  silently truncating the horizon.

These limitations are deliberate.  Adding cross-asset correlation
without a defensible empirical basis would create an illusion of
precision; future work could lift them with a copula-based forecast model.
