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

from .balancing import (
    PRODUCTS_ALL,
    PRODUCTS_DN,
    PRODUCTS_UP,
    PRODUCTS_WITH_ACTIVATION,
    BalancingConfig,
    acceptance_probability,
    activation_probability,
    resolve_balancing_config,
)
from .kpis import add_economic_columns, compute_kpis
from .optimization import run_scenario

logger = logging.getLogger(__name__)

__all__ = [
    "add_forecast_noise",
    "monte_carlo_balancing",
    "monte_carlo_rolling",
    "realise_balancing_scenario",
    "rolling_horizon_dispatch",
]


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
        # Clip to the per-window PV max as a proxy for nameplate — this
        # function has no access to pv_nameplate_kwp.
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

    # BESS energy capacity is pinned to params['bess_capacity_kwh']
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
    _MC_COLUMNS = [
        "seed", "profit_total_eur", "grid_export_mwh", "grid_import_mwh",
        "pv_curtailed_mwh", "bess_cycles_total", "foresight_gap_pct",
    ]
    seeds = [int(base_seed) + i for i in range(int(n_seeds))]
    if not seeds:
        # n_seeds == 0 — return a column-shaped empty frame so downstream
        # consumers (e.g. foresight_gap_pct readers) see a stable schema.
        return pd.DataFrame(columns=_MC_COLUMNS)
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
# Balancing-market Monte Carlo realisation
# ---------------------------------------------------------------------------


def _lognormal_unit_mean(
    rng: np.random.Generator, sigma: float, shape: tuple[int, ...],
) -> np.ndarray:
    """Sample log-normal multipliers with E[X] = 1 and the given sigma."""
    if sigma <= 0.0:
        return np.ones(shape, dtype=float)
    mu = -0.5 * sigma * sigma
    return rng.lognormal(mean=mu, sigma=sigma, size=shape)


def realise_balancing_scenario(
    reservations: dict[str, np.ndarray],
    cfg: BalancingConfig,
    prices: dict[str, np.ndarray],
    *,
    dt_hours: float,
    rng: np.random.Generator,
    soc_path_kwh: np.ndarray | None = None,
    soc_min_kwh: float | None = None,
    soc_max_kwh: float | None = None,
    eta_charge: float = 1.0,
    eta_discharge: float = 1.0,
) -> dict[str, Any]:
    """Realise one Monte Carlo scenario of balancing revenue.

    Parameters
    ----------
    reservations
        Per-product per-step kW reservation arrays from the MILP.
    cfg
        Parsed balancing configuration (probabilities and sigmas).
    prices
        Per-product per-step capacity and activation prices in EUR/MWh.
        Capacity keys ``<product>_capacity_price_eur_per_mwh`` for every
        product in :data:`PRODUCTS_ALL`; activation keys
        ``<product>_activation_price_eur_per_mwh`` for the products in
        :data:`PRODUCTS_WITH_ACTIVATION`.
    dt_hours
        Length of a settlement period (hours).
    rng
        Pre-seeded numpy random generator.
    soc_path_kwh
        Optional planned SOC trajectory from the MILP. When supplied,
        the realisation tracks the realised SOC against the bounds and
        flags ``soc_constrained`` when a step would violate them; the
        revenue accrued before the violation is still reported.
    soc_min_kwh, soc_max_kwh
        SOC bounds in kWh. Required when ``soc_path_kwh`` is supplied.
    eta_charge, eta_discharge
        BESS efficiencies used to convert reserved kW into SOC drift.
    """
    sigma_cap = float(cfg.bm_price_sigma_capacity_pct) / 100.0
    sigma_act = float(cfg.bm_price_sigma_activation_pct) / 100.0

    per_product_capacity: dict[str, float] = {}
    per_product_activation: dict[str, float] = {}
    total_capacity = 0.0
    total_activation = 0.0
    soc_constrained = False

    # Capture per-product activation realisations so the optional SOC
    # trajectory check below stays coupled to the revenue pass — a single
    # Monte Carlo scenario must not report revenue from activation events
    # that never happened in its SOC trace, nor "SOC OK" on a trace that
    # never generated revenue.
    activated_by_product: dict[str, np.ndarray] = {}

    for product in PRODUCTS_ALL:
        r = np.asarray(
            reservations.get(product, np.zeros(0, dtype=float)),
            dtype=float,
        )
        n = r.shape[0]
        alpha = acceptance_probability(cfg, product)
        cleared = rng.random(n) < alpha
        cap_price_col = f"{product}_capacity_price_eur_per_mwh"
        cap_price = np.asarray(prices[cap_price_col], dtype=float)
        cap_noise = _lognormal_unit_mean(rng, sigma_cap, (n,))
        cap_revenue = float(
            (cleared.astype(float) * r * dt_hours * cap_price * cap_noise).sum()
            / 1000.0
        )
        per_product_capacity[product] = cap_revenue
        total_capacity += cap_revenue

        # Activation realisation, conditional on being cleared.
        if product not in PRODUCTS_WITH_ACTIVATION:
            continue
        beta = activation_probability(cfg, product)
        activated = cleared & (rng.random(n) < beta)
        activated_by_product[product] = activated
        act_price_col = f"{product}_activation_price_eur_per_mwh"
        act_price = np.asarray(prices[act_price_col], dtype=float)
        act_noise = _lognormal_unit_mean(rng, sigma_act, (n,))
        act_revenue = float(
            (
                activated.astype(float) * r * dt_hours
                * act_price * act_noise
            ).sum() / 1000.0
        )
        per_product_activation[product] = act_revenue
        total_activation += act_revenue

    # Optional SOC trajectory check — uses the activation arrays already
    # sampled above so the SOC view of a scenario is bit-consistent with
    # the revenue view of the same scenario.
    if soc_path_kwh is not None:
        if soc_min_kwh is None or soc_max_kwh is None:
            raise ValueError(
                "soc_min_kwh and soc_max_kwh must be provided alongside "
                "soc_path_kwh."
            )
        n = len(soc_path_kwh)
        realised_soc = np.array(soc_path_kwh, dtype=float)
        for product in PRODUCTS_UP + PRODUCTS_DN:
            r = np.asarray(reservations.get(product, np.zeros(n)), dtype=float)
            activated = activated_by_product.get(product)
            if activated is None or activated.shape[0] != n:
                continue
            if product in PRODUCTS_UP:
                realised_soc -= activated.astype(float) * r * dt_hours / eta_discharge
            else:
                realised_soc += activated.astype(float) * r * dt_hours * eta_charge
        if np.any(realised_soc < soc_min_kwh - 1e-6) or np.any(
            realised_soc > soc_max_kwh + 1e-6,
        ):
            soc_constrained = True

    return {
        "per_product_capacity_revenue_eur": per_product_capacity,
        "per_product_activation_revenue_eur": per_product_activation,
        "total_capacity_revenue_eur": total_capacity,
        "total_activation_revenue_eur": total_activation,
        "total_balancing_revenue_eur": total_capacity + total_activation,
        "soc_constrained": soc_constrained,
    }


def monte_carlo_balancing(
    res: pd.DataFrame,
    params: dict[str, Any],
    *,
    n_scenarios: int = 200,
    seed: int | None = None,
) -> dict[str, Any]:
    """Run the balancing-market Monte Carlo realisation across scenarios.

    Pulls the per-product reservation columns and per-step price columns
    from ``res`` (populated by :func:`pvbess_opt.optimization.run_scenario`
    when the balancing block fired). Returns aggregated P10/P50/P90 of
    total balancing revenue plus per-product breakdowns and the fraction
    of scenarios that hit a realised SOC bound.

    When ``balancing_enabled`` is FALSE the function short-circuits and
    returns an empty dict so the rolling-horizon path stays unchanged.
    """
    raw_cfg = params.get("balancing") or {}
    cfg = resolve_balancing_config(raw_cfg)
    if not cfg.balancing_enabled:
        return {}

    missing = [
        f"bm_reservation_{p}_kw" for p in PRODUCTS_ALL
        if f"bm_reservation_{p}_kw" not in res.columns
    ]
    if missing:
        logger.warning(
            "monte_carlo_balancing: dispatch frame is missing %s; "
            "Monte Carlo skipped.", missing,
        )
        return {}

    dt_h = float(params.get("dt_minutes", 0) or 0) / 60.0
    if dt_h <= 0.0:
        return {}

    reservations = {
        p: res[f"bm_reservation_{p}_kw"].to_numpy(dtype=float)
        for p in PRODUCTS_ALL
    }
    price_cols = [
        f"{p}_capacity_price_eur_per_mwh" for p in PRODUCTS_ALL
    ] + [
        f"{p}_activation_price_eur_per_mwh" for p in PRODUCTS_WITH_ACTIVATION
    ]
    prices = {
        c: res[c].to_numpy(dtype=float)
        for c in price_cols if c in res.columns
    }

    bess_capacity_kwh = float(params.get("bess_capacity_kwh", 0.0) or 0.0)
    soc_path = (
        res["soc_kwh"].to_numpy(dtype=float)
        if "soc_kwh" in res.columns else None
    )
    soc_min = (
        float(params.get("soc_min_frac", 0.0) or 0.0) * bess_capacity_kwh
    )
    soc_max = (
        float(params.get("soc_max_frac", 1.0) or 1.0) * bess_capacity_kwh
    )
    eta_c = float(params.get("efficiency_charge", 1.0) or 1.0)
    eta_d = float(params.get("efficiency_discharge", 1.0) or 1.0)

    seed_base = (
        int(cfg.bm_random_seed) if seed is None else int(seed)
    )
    totals = np.zeros(int(n_scenarios), dtype=float)
    per_product_totals: dict[str, np.ndarray] = {
        p: np.zeros(int(n_scenarios), dtype=float) for p in PRODUCTS_ALL
    }
    per_product_activation_totals: dict[str, np.ndarray] = {
        p: np.zeros(int(n_scenarios), dtype=float)
        for p in PRODUCTS_WITH_ACTIVATION
    }
    constrained_count = 0
    for s in range(int(n_scenarios)):
        rng = np.random.default_rng(seed_base + s)
        outcome = realise_balancing_scenario(
            reservations, cfg, prices,
            dt_hours=dt_h, rng=rng,
            soc_path_kwh=soc_path,
            soc_min_kwh=soc_min if bess_capacity_kwh > 0.0 else None,
            soc_max_kwh=soc_max if bess_capacity_kwh > 0.0 else None,
            eta_charge=eta_c, eta_discharge=eta_d,
        )
        totals[s] = outcome["total_balancing_revenue_eur"]
        for p, val in outcome["per_product_capacity_revenue_eur"].items():
            per_product_totals[p][s] = val
        for p, val in outcome["per_product_activation_revenue_eur"].items():
            per_product_activation_totals[p][s] = val
        if outcome["soc_constrained"]:
            constrained_count += 1

    quantiles = np.quantile(totals, [0.10, 0.50, 0.90])
    aggregated: dict[str, Any] = {
        "bm_total_balancing_revenue_p10_eur": float(round(quantiles[0], 2)),
        "bm_total_balancing_revenue_p50_eur": float(round(quantiles[1], 2)),
        "bm_total_balancing_revenue_p90_eur": float(round(quantiles[2], 2)),
        "bm_soc_constrained_scenarios_pct": float(round(
            100.0 * constrained_count / max(1, int(n_scenarios)), 4,
        )),
        "bm_mc_total_realised_eur": [float(v) for v in totals],
    }
    for product, arr in per_product_totals.items():
        q = np.quantile(arr, [0.10, 0.50, 0.90])
        aggregated[f"bm_{product}_capacity_revenue_p10_eur"] = float(round(q[0], 2))
        aggregated[f"bm_{product}_capacity_revenue_p50_eur"] = float(round(q[1], 2))
        aggregated[f"bm_{product}_capacity_revenue_p90_eur"] = float(round(q[2], 2))
    for product, arr in per_product_activation_totals.items():
        q = np.quantile(arr, [0.10, 0.50, 0.90])
        aggregated[f"bm_{product}_activation_revenue_p10_eur"] = float(round(q[0], 2))
        aggregated[f"bm_{product}_activation_revenue_p50_eur"] = float(round(q[1], 2))
        aggregated[f"bm_{product}_activation_revenue_p90_eur"] = float(round(q[2], 2))
    return aggregated
