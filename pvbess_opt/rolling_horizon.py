"""Rolling-horizon dispatch with imperfect foresight + Monte Carlo.

A single annual MILP with full visibility into every hour's DAM price,
PV output, and load is a **perfect-foresight** model — it produces an
upper bound on achievable profit, not a realistic operating result.
Industry tools (Aurora Chronos, Gridcog, Plexos with look-ahead) handle
this via **rolling-horizon dispatch with imperfect foresight + Monte
Carlo over forecast scenarios**.

Three public functions:

* :func:`add_forecast_noise` applies log-normal multiplicative noise
  beyond the commit horizon.
* :func:`rolling_horizon_dispatch` runs a sliding-window MILP solve.
* :func:`monte_carlo_rolling` runs N seeds and returns the distribution.

Default forecast-noise sigmas (defensible from literature):

================ =========== =====================================================
Variable         sigma       Source
================ =========== =====================================================
DAM price        0.20 (MAPE) ENTSO-E D+1 benchmark for volatile markets
PV generation    0.12 (RMSE) NREL day-ahead PV forecast study
Load             0.05 (MAPE) Predictable-customer benchmark (booking horizon)
================ =========== =====================================================
"""

from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np
import pandas as pd

from .kpis import add_economic_columns, compute_kpis
from .optimization import run_scenario, verify_dispatch_invariants

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Time-axis helper
# ---------------------------------------------------------------------------


def _hours_to_steps(hours: int, dt_minutes: int) -> int:
    """Convert a duration in real hours to the equivalent row count.

    The rolling-horizon kwargs (``window_hours``, ``commit_hours``) and
    the workbook keys (``uncertainty_window_hours``,
    ``uncertainty_commit_hours``) are expressed in real hours.  Internal
    arithmetic against the timeseries DataFrame needs row counts.  This
    helper bridges the two so a documented 48-hour window is genuinely
    48 hours on every supported cadence (15-min, 30-min, hourly).

    Raises
    ------
    ValueError
        If ``dt_minutes`` is non-positive, or if the resulting step
        count is non-positive (e.g. requesting fewer than one full step
        at the configured cadence).
    """
    if dt_minutes <= 0:
        raise ValueError(
            f"dt_minutes must be > 0, got {dt_minutes!r}"
        )
    steps = int(hours) * 60 // int(dt_minutes)
    if steps <= 0:
        raise ValueError(
            f"window of {hours}h at dt={dt_minutes}min yields "
            f"{steps} steps; increase the horizon."
        )
    return steps


# ---------------------------------------------------------------------------
# Forecast noise
# ---------------------------------------------------------------------------


def _lognormal_multiplier(rng: np.random.Generator, sigma: float, n: int) -> np.ndarray:
    """Return n samples of a log-normal multiplier with mean 1."""
    if sigma <= 0.0 or n == 0:
        return np.ones(n, dtype=float)
    # Reparameterise so E[X] = 1: mu = -sigma^2 / 2.
    mu = -0.5 * sigma * sigma
    return rng.lognormal(mean=mu, sigma=sigma, size=n)


def add_forecast_noise(
    ts: pd.DataFrame,
    *,
    commit_steps: int,
    rng: np.random.Generator,
    sigma_dam: float = 0.20,
    sigma_pv: float = 0.12,
    sigma_load: float = 0.05,
    enable_dam: bool = True,
    enable_pv: bool = True,
    enable_load: bool = True,
) -> pd.DataFrame:
    """Apply log-normal multiplicative noise BEYOND the commit horizon.

    ``commit_steps`` is the commitment horizon expressed in
    timeseries-row indices (i.e. steps at the workbook's configured
    cadence).  Rows ``[0, commit_steps)`` are byte-identical to the
    input — those are the committed decisions for the current window.
    Rows ``[commit_steps, len(ts))`` get independent multiplicative
    log-normal noise on the enabled source columns.

    The three ``enable_*`` flags toggle each source independently.  A
    disabled source forces its sigma to 0 internally — the column is
    left exactly as in the input.  Negative DAM prices are sign-aware:
    noise is applied to the absolute value and the sign is restored.
    ``load_kwh`` is skipped when absent (merchant mode).
    """
    if commit_steps < 0:
        raise ValueError(
            f"commit_steps must be non-negative, got {commit_steps!r}"
        )
    out = ts.copy()
    n = len(out)
    if commit_steps >= n:
        return out

    n_perturb = n - commit_steps

    eff_sigma_dam = sigma_dam if enable_dam else 0.0
    eff_sigma_pv = sigma_pv if enable_pv else 0.0
    eff_sigma_load = sigma_load if enable_load else 0.0

    if "dam_price_eur_per_mwh" in out.columns:
        prices = out["dam_price_eur_per_mwh"].to_numpy(dtype=float).copy()
        sign = np.where(prices < 0, -1.0, 1.0)
        magnitude = np.abs(prices)
        mult = _lognormal_multiplier(rng, eff_sigma_dam, n_perturb)
        magnitude[commit_steps:] = magnitude[commit_steps:] * mult
        out["dam_price_eur_per_mwh"] = sign * magnitude

    if "pv_kwh" in out.columns:
        pv = out["pv_kwh"].to_numpy(dtype=float).copy()
        mult = _lognormal_multiplier(rng, eff_sigma_pv, n_perturb)
        pv_max = float(pv.max()) if pv.size else 0.0
        pv[commit_steps:] = np.minimum(
            np.maximum(pv[commit_steps:] * mult, 0.0),
            pv_max,
        )
        out["pv_kwh"] = pv

    if "load_kwh" in out.columns:
        load = out["load_kwh"].to_numpy(dtype=float).copy()
        mult = _lognormal_multiplier(rng, eff_sigma_load, n_perturb)
        load[commit_steps:] = np.maximum(load[commit_steps:] * mult, 0.0)
        out["load_kwh"] = load

    return out


# ---------------------------------------------------------------------------
# Rolling-horizon dispatch
# ---------------------------------------------------------------------------


def _slice_window(ts: pd.DataFrame, start: int, end: int) -> pd.DataFrame:
    sub = ts.iloc[start:end].reset_index(drop=True).copy()
    return sub


def rolling_horizon_dispatch(
    params: dict[str, Any],
    ts: pd.DataFrame,
    *,
    window_hours: int = 48,
    commit_hours: int = 24,
    forecast_seed: int | None = None,
    sigma_dam: float = 0.20,
    sigma_pv: float = 0.12,
    sigma_load: float = 0.05,
    enable_dam: bool = True,
    enable_pv: bool = True,
    enable_load: bool = True,
    evaluate_with_actuals: bool = True,
    solver_name: str = "highs",
    **solve_kwargs: Any,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Sliding-window MILP solve with imperfect foresight.

    ``window_hours`` and ``commit_hours`` are expressed in real hours.
    They are converted to row counts at the cadence given by
    ``params["dt_minutes"]`` before indexing into ``ts``, so a 48-hour
    window is genuinely 48 hours on every supported cadence (15-min,
    30-min, hourly).

    For each window starting at step c in {0, commit_steps, 2*commit_steps, ...}:

        1. Slice ts[c : c + window_steps].
        2. Apply forecast noise beyond commit_steps
           (skipped if forecast_seed is None — gives deterministic RH).
        3. Solve the MILP with the noisy window; pin initial_soc to the
           SOC carried over from the previous window.
        4. Keep the first commit_steps of the dispatch.
        5. Pass SOC[commit_steps] as initial_soc to the next window.

    If ``evaluate_with_actuals`` is True the returned KPIs are recomputed
    against the original (noise-free) ``ts`` — this reflects realised
    performance.  Otherwise KPIs reflect what the solver thought it was
    getting.

    The MILP's closed-cycle ``terminal_soc_equal`` is **not** enforced
    inside rolling-horizon windows (that constraint only makes sense
    for the annual benchmark).

    Returns ``(full_year_dispatch_df, kpis_dict)``.
    """
    if window_hours < 1:
        raise ValueError(f"window_hours must be >= 1, got {window_hours!r}")
    if commit_hours < 1 or commit_hours > window_hours:
        raise ValueError(
            f"commit_hours must be 1..window_hours, got {commit_hours!r}."
        )

    n = len(ts)
    if n == 0:
        raise ValueError("timeseries is empty; nothing to dispatch.")

    dt_minutes = int(params.get("dt_minutes", 60) or 60)
    window_steps = _hours_to_steps(window_hours, dt_minutes)
    commit_steps = _hours_to_steps(commit_hours, dt_minutes)

    rng = (
        np.random.default_rng(int(forecast_seed))
        if forecast_seed is not None else None
    )

    # v0.8: BESS energy capacity is pinned to params['bess_capacity_kwh']
    # in build_model, so every window automatically uses the same asset
    # — no need to plumb a fixed_e_cap_kwh through.
    initial_soc_kwh: float | None = None
    committed_chunks: list[pd.DataFrame] = []

    # Per-window progress: emit ~20 INFO lines per seed (not one per
    # window — 365 lines/seed is too noisy at INFO).
    n_windows = max(1, (n + commit_steps - 1) // commit_steps)
    log_every = max(1, n_windows // 20)
    t_rh_start = time.perf_counter()
    win_idx = 0

    cursor = 0
    while cursor < n:
        win_end = min(cursor + window_steps, n)
        commit_end_global = min(cursor + commit_steps, n)
        window_ts = _slice_window(ts, cursor, win_end)

        if rng is not None:
            # Local commit horizon = global commit horizon truncated to
            # window length; noise beyond that.
            local_commit = min(commit_steps, len(window_ts))
            window_noisy = add_forecast_noise(
                window_ts,
                commit_steps=local_commit,
                rng=rng,
                sigma_dam=sigma_dam,
                sigma_pv=sigma_pv,
                sigma_load=sigma_load,
                enable_dam=enable_dam,
                enable_pv=enable_pv,
                enable_load=enable_load,
            )
        else:
            window_noisy = window_ts

        res_window, _solver = run_scenario(
            params, window_noisy,
            solver_name=solver_name,
            initial_soc_kwh=initial_soc_kwh,
            terminal_soc_free=True,  # do not close the cycle within a window
            **solve_kwargs,
        )

        # Keep the first ``commit_steps`` slice of the solved dispatch.
        local_commit_n = commit_end_global - cursor
        committed = res_window.iloc[:local_commit_n].copy()
        # Re-attach the original (un-noised) timestamps so the year-long
        # frame lines up with ``ts``.
        committed["timestamp"] = ts["timestamp"].iloc[cursor:commit_end_global].values
        committed_chunks.append(committed)

        # SOC carryover.
        if local_commit_n < len(res_window):
            initial_soc_kwh = float(res_window["soc_kwh"].iloc[local_commit_n])
        else:
            # End of horizon — derive the post-final-step SOC.
            eta_c = float(params.get("efficiency_charge", 1.0) or 1.0)
            eta_d = float(params.get("efficiency_discharge", 1.0) or 1.0)
            last = res_window.iloc[-1]
            initial_soc_kwh = float(
                last["soc_kwh"]
                + eta_c * (last["pv_to_bess_kwh"] + last["bess_charge_grid_kwh"])
                - (last["bess_dis_load_kwh"] + last["bess_dis_grid_kwh"]) / eta_d
            )

        cursor = commit_end_global

        win_idx += 1
        if win_idx % log_every == 0 or win_idx == n_windows:
            elapsed = time.perf_counter() - t_rh_start
            eta_s = elapsed / win_idx * (n_windows - win_idx) if win_idx else 0.0
            logger.info(
                "rolling_horizon_dispatch: window %d/%d (elapsed %.1fs, "
                "ETA %.1fs)",
                win_idx, n_windows, elapsed, eta_s,
            )

    full = pd.concat(committed_chunks, ignore_index=True)

    if evaluate_with_actuals:
        if "dam_price_eur_per_mwh" in ts.columns:
            full["dam_price_eur_per_mwh"] = (
                ts["dam_price_eur_per_mwh"].iloc[: len(full)].values
            )
        if "retail_price_eur_per_mwh" in ts.columns:
            full["retail_price_eur_per_mwh"] = (
                ts["retail_price_eur_per_mwh"].iloc[: len(full)].values
            )
        price_cols = ("retail_price_eur_per_mwh", "dam_price_eur_per_mwh")
        eur_cols = [
            c for c in full.columns
            if c.endswith("_eur") and c not in price_cols
        ]
        if eur_cols:
            full = full.drop(columns=eur_cols)
        full = add_economic_columns(full, params)

    kpis = compute_kpis(full, params, verify_balance=False)
    return full, kpis


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------


def monte_carlo_rolling(
    params: dict[str, Any],
    ts: pd.DataFrame,
    *,
    n_seeds: int = 30,
    base_seed: int = 42,
    pf_profit_eur: float | None = None,
    sigma_dam: float = 0.20,
    sigma_pv: float = 0.12,
    sigma_load: float = 0.05,
    enable_dam: bool = True,
    enable_pv: bool = True,
    enable_load: bool = True,
    window_hours: int = 48,
    commit_hours: int = 24,
    solver_name: str = "highs",
    **solve_kwargs: Any,
) -> pd.DataFrame:
    """Run rolling_horizon_dispatch with N seeds, return distribution.

    ``pf_profit_eur`` is the perfect-foresight benchmark used to compute
    ``foresight_gap_pct = 100 * (1 - rh_profit / pf_profit)``.  When
    ``None`` the gap column is NaN.

    Returns
    -------
    pandas.DataFrame
        Indexed by seed; columns:
            ``seed``,
            ``profit_total_eur``,
            ``grid_export_mwh``,
            ``grid_import_mwh``,
            ``pv_curtailed_mwh``,
            ``bess_cycles_total``,
            ``foresight_gap_pct``.
    """
    seeds = [int(base_seed) + i for i in range(int(n_seeds))]
    rows: list[dict[str, Any]] = []
    t_start = time.perf_counter()
    for seed in seeds:
        _full, kpis = rolling_horizon_dispatch(
            params, ts,
            window_hours=window_hours,
            commit_hours=commit_hours,
            forecast_seed=seed,
            sigma_dam=sigma_dam,
            sigma_pv=sigma_pv,
            sigma_load=sigma_load,
            enable_dam=enable_dam,
            enable_pv=enable_pv,
            enable_load=enable_load,
            evaluate_with_actuals=True,
            solver_name=solver_name,
            **solve_kwargs,
        )
        profit = float(kpis.get("profit_total_eur", 0.0))
        if pf_profit_eur is not None and abs(pf_profit_eur) > 1e-9:
            gap = 100.0 * (1.0 - profit / float(pf_profit_eur))
        else:
            gap = float("nan")
        rows.append({
            "seed": seed,
            "profit_total_eur": profit,
            "grid_export_mwh": float(kpis.get("system_total_export_mwh", 0.0)),
            "grid_import_mwh": float(kpis.get("system_total_import_mwh", 0.0)),
            "pv_curtailed_mwh": float(kpis.get("pv_energy_curtailed_mwh", 0.0)),
            "bess_cycles_total": float(kpis.get("bess_equivalent_cycles_total", 0.0)),
            "foresight_gap_pct": gap,
        })
        elapsed = time.perf_counter() - t_start
        done = len(rows)
        eta_s = elapsed / done * (len(seeds) - done) if done else 0.0
        logger.info(
            "monte_carlo_rolling: seed %d/%d done in %.1fs "
            "(profit=%.0f EUR, gap=%.2f%%, ETA %.1f min)",
            done, len(seeds), elapsed, profit, gap, eta_s / 60.0,
        )
        # Force flush so a long-running ensemble shows live progress.
        for h in logger.handlers + logging.getLogger().handlers:
            h.flush()
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Helper for callers wanting per-window invariants
# ---------------------------------------------------------------------------


def verify_window_invariants(
    res: pd.DataFrame, params: dict[str, Any],
) -> dict[str, float]:
    """Run the 9 audit invariants on a single committed window."""
    return verify_dispatch_invariants(res, params, mode=str(params.get("mode", "vnb")))
