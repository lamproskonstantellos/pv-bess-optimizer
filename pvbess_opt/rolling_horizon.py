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
from typing import Any

import numpy as np
import pandas as pd

from .kpis import add_economic_columns, compute_kpis
from .optimization import run_scenario, verify_dispatch_invariants

logger = logging.getLogger(__name__)


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
    commit_hours: int,
    rng: np.random.Generator,
    sigma_dam: float = 0.20,
    sigma_pv: float = 0.12,
    sigma_load: float = 0.05,
) -> pd.DataFrame:
    """Apply log-normal multiplicative noise BEYOND the commit horizon.

    Rows ``[0, commit_hours)`` are byte-identical to the input — those
    are the committed decisions for the current window.  Rows
    ``[commit_hours, len(ts))`` get independent multiplicative log-normal
    noise on ``dam_price_eur_per_mwh``, ``pv_kwh``, ``load_kwh``.

    Negative DAM prices are sign-aware: noise is applied to the absolute
    value and the sign is restored.  ``load_kwh`` is skipped when absent
    (merchant mode).
    """
    if commit_hours < 0:
        raise ValueError(f"commit_hours must be non-negative, got {commit_hours!r}")
    out = ts.copy()
    n = len(out)
    if commit_hours >= n:
        return out

    n_perturb = n - commit_hours

    if "dam_price_eur_per_mwh" in out.columns:
        prices = out["dam_price_eur_per_mwh"].to_numpy(dtype=float).copy()
        sign = np.where(prices < 0, -1.0, 1.0)
        magnitude = np.abs(prices)
        mult = _lognormal_multiplier(rng, sigma_dam, n_perturb)
        magnitude[commit_hours:] = magnitude[commit_hours:] * mult
        out["dam_price_eur_per_mwh"] = sign * magnitude

    if "pv_kwh" in out.columns:
        pv = out["pv_kwh"].to_numpy(dtype=float).copy()
        mult = _lognormal_multiplier(rng, sigma_pv, n_perturb)
        pv[commit_hours:] = np.maximum(pv[commit_hours:] * mult, 0.0)
        out["pv_kwh"] = pv

    if "load_kwh" in out.columns:
        load = out["load_kwh"].to_numpy(dtype=float).copy()
        mult = _lognormal_multiplier(rng, sigma_load, n_perturb)
        load[commit_hours:] = np.maximum(load[commit_hours:] * mult, 0.0)
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
    evaluate_with_actuals: bool = True,
    solver_name: str = "highs",
    **solve_kwargs: Any,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Sliding-window MILP solve with imperfect foresight.

    For each window starting at hour t in {0, commit_hours, 2*commit_hours, ...}:

        1. Slice ts[t : t + window_hours].
        2. Apply forecast noise beyond commit_hours
           (skipped if forecast_seed is None — gives deterministic RH).
        3. Solve the MILP with the noisy window; pin initial_soc to the
           SOC carried over from the previous window.
        4. Keep the first commit_hours of the dispatch.
        5. Pass SOC[commit_hours] as initial_soc to the next window.

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

    rng = (
        np.random.default_rng(int(forecast_seed))
        if forecast_seed is not None else None
    )

    # Pin the BESS energy capacity after the first window so each window
    # operates against the same physical asset (the optimizer can't size
    # up "for free" mid-year).  Implemented by inflating ``soc_min_frac``
    # on the params copy used in subsequent windows.
    initial_soc_kwh: float | None = None
    fixed_e_cap_kwh: float | None = None

    committed_chunks: list[pd.DataFrame] = []
    last_e_cap_kwh: float | None = None

    cursor = 0
    while cursor < n:
        win_end = min(cursor + window_hours, n)
        commit_end_global = min(cursor + commit_hours, n)
        window_ts = _slice_window(ts, cursor, win_end)

        if rng is not None:
            # Local commit horizon = global commit horizon truncated to
            # window length; noise beyond that.
            local_commit = min(commit_hours, len(window_ts))
            window_noisy = add_forecast_noise(
                window_ts,
                commit_hours=local_commit,
                rng=rng,
                sigma_dam=sigma_dam,
                sigma_pv=sigma_pv,
                sigma_load=sigma_load,
            )
        else:
            window_noisy = window_ts

        # Pin the BESS energy capacity from the first window.  We pass
        # the fixed e_cap by tightening the bounds: SOC_MIN ≥ soc_min_frac
        # × e_cap and SOC_MAX ≤ soc_max_frac × e_cap, plus E_P (e_cap ≤
        # p_dis × battery_hours).  Setting battery_hours = e_cap / p_dis
        # forces the upper E/P bound to the desired value; combined with
        # SOC_MIN/SOC_MAX, the optimizer has a single feasible e_cap.
        win_params = dict(params)
        if fixed_e_cap_kwh is not None and float(params.get("p_dis_max_kw", 0.0)) > 0.0:
            win_params["battery_hours"] = (
                fixed_e_cap_kwh / float(params["p_dis_max_kw"])
            )

        res_window, e_cap_kwh, _solver = run_scenario(
            win_params, window_noisy,
            solver_name=solver_name,
            initial_soc_kwh=initial_soc_kwh,
            terminal_soc_free=True,  # do not close the cycle within a window
            **solve_kwargs,
        )
        if fixed_e_cap_kwh is None:
            fixed_e_cap_kwh = e_cap_kwh
        last_e_cap_kwh = e_cap_kwh

        # Keep the first ``commit_hours`` slice of the solved dispatch.
        local_commit_n = commit_end_global - cursor
        committed = res_window.iloc[:local_commit_n].copy()
        # Re-attach the original (un-noised) timestamps so the year-long
        # frame lines up with ``ts``.
        committed["timestamp"] = ts["timestamp"].iloc[cursor:commit_end_global].values
        committed_chunks.append(committed)

        # SOC carryover: take the SOC at the end of the committed slice,
        # which is the "starting SOC" for the next window.  When we
        # commit n hours, the SOC at hour n (the start of hour n) is
        # res_window["soc_kwh"].iloc[local_commit_n] if the window is
        # longer; if not (last window) we walk one step from
        # iloc[-1] using the dynamics.
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

    full = pd.concat(committed_chunks, ignore_index=True)

    # Re-evaluate against actuals if requested: replace the price columns
    # carried in the noisy result with the original prices, then re-run
    # ``add_economic_columns`` and ``compute_kpis``.
    if evaluate_with_actuals:
        if "dam_price_eur_per_mwh" in ts.columns:
            full["dam_price_eur_per_mwh"] = (
                ts["dam_price_eur_per_mwh"].iloc[: len(full)].values
            )
        if "retail_price_eur_per_mwh" in ts.columns:
            full["retail_price_eur_per_mwh"] = (
                ts["retail_price_eur_per_mwh"].iloc[: len(full)].values
            )
        # Drop any prior eur columns and recompute.
        price_cols = ("retail_price_eur_per_mwh", "dam_price_eur_per_mwh")
        eur_cols = [
            c for c in full.columns
            if c.endswith("_eur") and c not in price_cols
        ]
        if eur_cols:
            full = full.drop(columns=eur_cols)
        full = add_economic_columns(full, params)

    if last_e_cap_kwh is None:
        last_e_cap_kwh = 0.0
    kpis = compute_kpis(full, params, float(last_e_cap_kwh), verify_balance=False)
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
    for seed in seeds:
        _full, kpis = rolling_horizon_dispatch(
            params, ts,
            window_hours=window_hours,
            commit_hours=commit_hours,
            forecast_seed=seed,
            sigma_dam=sigma_dam,
            sigma_pv=sigma_pv,
            sigma_load=sigma_load,
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
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Helper for callers wanting per-window invariants
# ---------------------------------------------------------------------------


def verify_window_invariants(
    res: pd.DataFrame, params: dict[str, Any],
) -> dict[str, float]:
    """Run the 8 audit invariants on a single committed window."""
    return verify_dispatch_invariants(res, params, mode=str(params.get("mode", "vnb")))
