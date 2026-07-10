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

from .availability import apply_unavailability_derate
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
from .kpis import add_economic_columns, compute_kpis, final_soc_after_last_step
from .optimization import run_scenario
from .timeutils import dt_hours_from

logger = logging.getLogger(__name__)

__all__ = [
    "PRICE_COLUMNS",
    "add_forecast_noise",
    "monte_carlo_balancing",
    "monte_carlo_rolling",
    "realise_balancing_scenario",
    "resolve_imbalance_prices",
    "rolling_horizon_dispatch",
    "settle_imbalance",
]


# Canonical list of every price column the rolling-horizon engine must
# treat as a "noise-free input" when running with
# ``evaluate_with_actuals=True``.  Includes DAM, retail, and every
# balancing capacity / activation price column.  This is the single
# source of truth used by both :func:`add_forecast_noise` (when deciding
# which columns are eligible for forecast noise) and
# :func:`rolling_horizon_dispatch` (when restoring noise-free prices
# before re-deriving the economic columns).  Adding a new noisable
# price -- e.g. a balancing capacity price variant -- only requires
# extending this list; the actuals-restore path will pick it up
# automatically and the realised KPIs will not absorb the noise.
PRICE_COLUMNS: tuple[str, ...] = (
    "dam_price_eur_per_mwh",
    "retail_price_eur_per_mwh",
    "fcr_capacity_price_eur_per_mwh",
    "afrr_up_capacity_price_eur_per_mwh",
    "afrr_dn_capacity_price_eur_per_mwh",
    "mfrr_up_capacity_price_eur_per_mwh",
    "mfrr_dn_capacity_price_eur_per_mwh",
    "afrr_up_activation_price_eur_per_mwh",
    "afrr_dn_activation_price_eur_per_mwh",
    "mfrr_up_activation_price_eur_per_mwh",
    "mfrr_dn_activation_price_eur_per_mwh",
)


# ---------------------------------------------------------------------------
# Imbalance settlement (Eqs. U6-U9)
# ---------------------------------------------------------------------------

# The three optional imbalance price columns are actuals-only inputs:
# they are never forecast-noised (deviation settlement prices are not
# known day-ahead by construction), so they deliberately stay OUT of
# PRICE_COLUMNS — the settlement always reads them from the original
# noise-free timeseries.
IMBALANCE_PRICE_COLUMNS: tuple[str, ...] = (
    "imbalance_price_eur_per_mwh",           # single-price regime
    "imbalance_price_short_eur_per_mwh",     # dual regime, short side
    "imbalance_price_long_eur_per_mwh",      # dual regime, long side
)

_IMBALANCE_PROXY_WARNED = False


def resolve_imbalance_prices(
    ts: pd.DataFrame,
    dam: np.ndarray,
    *,
    pricing: str,
    mult_short: float,
    mult_long: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Return per-step (short, long) imbalance price arrays.

    Dual regime: each side reads its own column when present, else the
    sign-aware DAM proxy (Eq. U8a) — ``pi_short = DAM + (m_s - 1)|DAM|``
    and ``pi_long = DAM - (1 - m_l)|DAM|``, so ``long <= DAM <= short``
    holds in negative-price hours too (a naive ``DAM x m`` flips the
    spread sign there).  Single regime: both sides are the mandatory
    ``imbalance_price_eur_per_mwh`` column (the loader validates its
    presence — a lone imbalance price has no canonical DAM relationship
    to proxy).
    """
    global _IMBALANCE_PROXY_WARNED
    n = len(dam)
    if pricing == "single":
        col = ts["imbalance_price_eur_per_mwh"].astype(float).to_numpy()[:n]
        return col, col
    abs_dam = np.abs(dam)
    if "imbalance_price_short_eur_per_mwh" in ts.columns:
        short = (
            ts["imbalance_price_short_eur_per_mwh"]
            .astype(float).to_numpy()[:n]
        )
    else:
        short = dam + (float(mult_short) - 1.0) * abs_dam
        if not _IMBALANCE_PROXY_WARNED:
            logger.warning(
                "imbalance settlement: no imbalance_price_short_eur_per_mwh "
                "column; using the sign-aware DAM proxy (Eq. U8a) with "
                "imbalance_price_mult_short=%.3g.", float(mult_short),
            )
            _IMBALANCE_PROXY_WARNED = True
    if "imbalance_price_long_eur_per_mwh" in ts.columns:
        long_ = (
            ts["imbalance_price_long_eur_per_mwh"]
            .astype(float).to_numpy()[:n]
        )
    else:
        long_ = dam - (1.0 - float(mult_long)) * abs_dam
    return short, long_


def settle_imbalance(
    deviation_mwh: np.ndarray,
    dam: np.ndarray,
    price_short: np.ndarray,
    price_long: np.ndarray,
    *,
    pricing: str,
) -> np.ndarray:
    """Per-step settlement cost relative to the DAM-booked revenue.

    The realised dispatch is already re-priced at DAM by the
    actuals-restore path, so the settlement is the CORRECTION for the
    deviation volume — no double counting.  Dual regime (Eq. U7):

        C_t = max(-D,0)(pi_short - DAM) + max(D,0)(DAM - pi_long)

    non-negative whenever ``long <= DAM <= short`` (incentive-compatible
    dual pricing).  Single regime (Eq. U8): ``C_t = (pi_imb - DAM)(-D)``
    — sign-indefinite, an imbalance can profit; the classic single- vs
    dual-price distinction and the reason for the regime switch.
    ``D > 0`` is long (over-delivery vs nomination), ``D < 0`` short.
    """
    d = np.asarray(deviation_mwh, dtype=float)
    if pricing == "single":
        return (price_short - dam) * (-d)
    short_vol = np.clip(-d, 0.0, None)
    long_vol = np.clip(d, 0.0, None)
    return short_vol * (price_short - dam) + long_vol * (dam - price_long)


def _net_grid_position_kwh(frame: pd.DataFrame) -> np.ndarray:
    """Per-step net grid position (kWh): injection minus offtake.

    ``grid_to_load_kwh`` is absent in merchant mode and the BESS
    grid-charge column can be absent in surplus-only configurations;
    missing columns contribute zero.
    """
    n = len(frame)
    out = np.zeros(n, dtype=float)
    for col, sign in (
        ("pv_to_grid_kwh", 1.0),
        ("bess_dis_grid_kwh", 1.0),
        ("grid_to_load_kwh", -1.0),
        ("bess_charge_grid_kwh", -1.0),
    ):
        if col in frame.columns:
            out = out + sign * frame[col].fillna(0.0).astype(float).to_numpy()
    return out


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
        If ``dt_minutes`` is non-positive, if the requested duration is
        not an integer number of steps at the configured cadence (a
        silent floor would shorten the documented horizon), or if the
        resulting step count is non-positive (e.g. requesting fewer
        than one full step at the configured cadence).
    """
    if dt_minutes <= 0:
        raise ValueError(
            f"dt_minutes must be > 0, got {dt_minutes!r}"
        )
    total_minutes = int(hours) * 60
    if total_minutes % int(dt_minutes) != 0:
        raise ValueError(
            f"a {hours}h horizon is not an integer number of steps at "
            f"dt={dt_minutes}min ({total_minutes} % {dt_minutes} != 0); "
            "choose hours divisible by the cadence."
        )
    steps = total_minutes // int(dt_minutes)
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


_NAMEPLATE_FALLBACK_WARNED = False


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
    pv_nameplate_kwh_per_step: float | None = None,
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

    PV noise is clipped to ``pv_nameplate_kwh_per_step`` (the configured
    PV nameplate in kWp times the timestep in hours) — the true physical
    ceiling of the array.  Clipping at the per-window observed maximum
    instead biases the realised mean downward because samples already
    sitting at the peak can only be pushed lower.  When the caller does
    not supply a nameplate (legacy programmatic path), the previous
    per-window-max behaviour is retained with a one-time
    ``logger.warning`` flagging the bias.
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
        if pv_nameplate_kwh_per_step is not None and pv_nameplate_kwh_per_step > 0.0:
            clip_ceiling = float(pv_nameplate_kwh_per_step)
        else:
            global _NAMEPLATE_FALLBACK_WARNED
            if not _NAMEPLATE_FALLBACK_WARNED:
                logger.warning(
                    "add_forecast_noise called without pv_nameplate_kwh_per_step; "
                    "falling back to clipping at the per-window PV maximum, which "
                    "biases the realised mean downward. Pass the configured PV "
                    "nameplate to remove the bias."
                )
                _NAMEPLATE_FALLBACK_WARNED = True
            clip_ceiling = float(pv.max()) if pv.size else 0.0
        pv[commit_steps:] = np.minimum(
            np.maximum(pv[commit_steps:] * mult, 0.0),
            clip_ceiling,
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
    imbalance_enabled: bool = False,
    imbalance_pricing: str = "dual",
    imbalance_price_mult_short: float = 1.25,
    imbalance_price_mult_long: float = 0.75,
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
    performance.  The actuals-restore path overwrites every price
    column listed in :data:`PRICE_COLUMNS` (DAM, retail, and every
    balancing capacity / activation price) with the noise-free input,
    then drops the per-step EUR columns and re-derives them via
    :func:`pvbess_opt.kpis.add_economic_columns`.  The restore is
    driven by :data:`PRICE_COLUMNS` rather than the
    ``_eur_per_mwh`` suffix so a future addition of a non-conforming
    price column still gets restored and so the EUR-suffix drop cannot
    accidentally remove a restored price.  Otherwise KPIs reflect what
    the solver thought it was getting.

    The MILP's closed-cycle ``terminal_soc_equal`` is **not** enforced
    *within* rolling-horizon windows (a window should not close its own
    cycle), but when ``params['terminal_soc_equal']`` is true every
    window that reaches the end of the horizon pins its post-final-step
    SOC to the **year-initial** SOC.  The stitched dispatch then honours
    the same closed-cycle condition as the annual perfect-foresight
    benchmark; without this the last window drains the battery for free
    profit the benchmark is not allowed to take, and the foresight gap
    goes spuriously negative.

    Scope of the returned KPIs: identical to the pipeline's headline
    Year-1 KPIs — ``compute_kpis`` followed by
    :func:`pvbess_opt.availability.apply_unavailability_derate` using
    ``params['unavailability_pct']``.  ``foresight_gap_pct`` computed
    against the (equally derated) perfect-foresight benchmark is
    therefore derate-invariant.  See ``pvbess_opt/conventions.md``.

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

    # Convert the configured PV nameplate (in kWp) to the per-step
    # energy ceiling (kWh per step) so add_forecast_noise can clip noised
    # PV at the true physical limit instead of the per-window max.
    pv_nameplate_kwp = float(params.get("pv_nameplate_kwp", 0.0) or 0.0)
    dt_h_value = dt_hours_from(params)
    pv_nameplate_kwh_per_step: float | None
    if pv_nameplate_kwp > 0.0 and dt_h_value > 0.0:
        pv_nameplate_kwh_per_step = pv_nameplate_kwp * dt_h_value
    else:
        pv_nameplate_kwh_per_step = None

    # BESS energy capacity is pinned to params['bess_capacity_kwh']
    # in build_model, so every window automatically uses the same asset
    # — no need to plumb a fixed_e_cap_kwh through.
    initial_soc_kwh: float | None = None
    committed_chunks: list[pd.DataFrame] = []

    # Imbalance settlement (Eq. U6): per-step DA nominations for each
    # commit block come from the PREVIOUS window's noisy lookahead slice
    # [commit, 2*commit) — the only forecast-based schedule the
    # machinery produces (committed rows are byte-identical to actuals
    # by design).  NaN marks steps without a nomination (the first
    # block, and tail steps beyond the last lookahead), which settle at
    # zero deviation.  Capture happens AFTER each solve and consumes NO
    # rng draws, so existing seeds reproduce bit-identically.
    nomination_kwh = np.full(n, np.nan, dtype=float)
    nomination_pv_kwh = np.full(n, np.nan, dtype=float)

    # Year-close target: when the annual benchmark closes its SOC cycle
    # (terminal_soc_equal), the window(s) covering the final step must
    # return the battery to the year-initial SOC so the realised profit
    # is comparable with the perfect-foresight profit.
    bess_capacity_kwh = float(params.get("bess_capacity_kwh", 0.0) or 0.0)
    if bool(params.get("terminal_soc_equal", True)) and bess_capacity_kwh > 0.0:
        year_close_soc_kwh: float | None = (
            float(params.get("initial_soc_frac", 0.0) or 0.0)
            * bess_capacity_kwh
        )
    else:
        year_close_soc_kwh = None

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
                pv_nameplate_kwh_per_step=pv_nameplate_kwh_per_step,
            )
        else:
            window_noisy = window_ts

        res_window, _solver = run_scenario(
            params, window_noisy,
            solver_name=solver_name,
            initial_soc_kwh=initial_soc_kwh,
            terminal_soc_free=True,  # do not close the cycle within a window
            # Windows that see the end of the horizon steer toward (and the
            # final one enforces) the year-initial SOC — the same closed
            # cycle the perfect-foresight benchmark must honour.
            terminal_soc_target_kwh=(
                year_close_soc_kwh if win_end == n else None
            ),
            **solve_kwargs,
        )

        # Keep the first ``commit_steps`` slice of the solved dispatch.
        local_commit_n = commit_end_global - cursor
        if imbalance_enabled:
            look_end = min(local_commit_n + commit_steps, len(res_window))
            if look_end > local_commit_n:
                look = res_window.iloc[local_commit_n:look_end]
                g_start = cursor + local_commit_n
                g_end = min(g_start + (look_end - local_commit_n), n)
                span = g_end - g_start
                nomination_kwh[g_start:g_end] = (
                    _net_grid_position_kwh(look)[:span]
                )
                # Noisy PV lookahead for the PV-only counterfactual
                # (Eq. U9) — the SAME seed's forecast, so the hedge
                # value is paired by construction.
                if "pv_kwh" in window_noisy.columns:
                    nomination_pv_kwh[g_start:g_end] = (
                        window_noisy["pv_kwh"].astype(float)
                        .to_numpy()[local_commit_n:look_end][:span]
                    )
        committed = res_window.iloc[:local_commit_n].copy()
        # Re-attach the original (un-noised) timestamps so the year-long
        # frame lines up with ``ts``.
        committed["timestamp"] = ts["timestamp"].iloc[cursor:commit_end_global].values
        committed_chunks.append(committed)

        # SOC carryover.
        if local_commit_n < len(res_window):
            initial_soc_kwh = float(res_window["soc_kwh"].iloc[local_commit_n])
        else:
            # Fully-committed window (window == commit, or the final
            # slice): the next window starts AFTER the last solved step,
            # so reconstruct the post-final-step SOC with the shared
            # helper.  It mirrors the model's final_soc_expr including
            # the expected balancing-activation drift, which the
            # previous hand-rolled algebra omitted.
            initial_soc_kwh = final_soc_after_last_step(res_window, params)
        if bess_capacity_kwh > 0.0 and initial_soc_kwh is not None:
            # The model bounds SOC only up to solver feasibility
            # tolerance, so the carried value can overshoot the envelope
            # by ~1e-6 kWh; pinning soc[0] to such a value makes the
            # next window infeasible.  Clamp away the numerical noise.
            soc_lo = float(params.get("soc_min_frac", 0.0) or 0.0) * bess_capacity_kwh
            soc_hi = float(params.get("soc_max_frac", 1.0) or 1.0) * bess_capacity_kwh
            initial_soc_kwh = min(max(initial_soc_kwh, soc_lo), soc_hi)

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

    # Year-close shortfall: the final window's target is relaxed by a
    # heavily penalised slack in the MILP (see build_model), so a
    # physically unreachable target no longer aborts the run.  Surface
    # the realised shortfall loudly: the stitched year did not close its
    # SOC cycle, so the profit comparison against the closed-cycle
    # perfect-foresight benchmark carries up to the shortfall's energy
    # value of upward bias.
    year_close_shortfall_kwh = 0.0
    if year_close_soc_kwh is not None and initial_soc_kwh is not None:
        year_close_shortfall_kwh = max(
            0.0, float(year_close_soc_kwh) - float(initial_soc_kwh),
        )
        if year_close_shortfall_kwh > 1.0:
            logger.warning(
                "rolling_horizon_dispatch: year-close SOC target %.0f kWh "
                "was physically unreachable; the year ends %.0f kWh short "
                "(surplus-only charging could not refill the battery in "
                "the final windows). The foresight gap may be understated "
                "by up to the shortfall's energy value.",
                float(year_close_soc_kwh), year_close_shortfall_kwh,
            )

    if evaluate_with_actuals:
        # Restore every noise-free price column from the original
        # ``ts``.  PRICE_COLUMNS is the single source of truth -- any
        # new noisable price added there is automatically restored here.
        for col in PRICE_COLUMNS:
            if col in ts.columns:
                full[col] = ts[col].iloc[: len(full)].values
        # Drop every per-step EUR column the noisy solve wrote (revenue,
        # expense, etc.) so add_economic_columns can re-derive them
        # from the restored prices.  PRICE_COLUMNS ends in
        # ``_eur_per_mwh`` rather than ``_eur`` so the suffix filter
        # below cannot accidentally drop a restored price column.
        eur_cols = [c for c in full.columns if c.endswith("_eur")]
        if eur_cols:
            full = full.drop(columns=eur_cols)
        full = add_economic_columns(full, params)

    kpis = compute_kpis(full, params, verify_balance=False)

    if imbalance_enabled:
        # Ex-post deviation settlement (Eqs. U6-U9), always against the
        # ORIGINAL noise-free prices (imbalance prices are actuals; the
        # realised revenue was already re-priced at DAM by the
        # actuals-restore path, so the settlement is exactly the
        # correction for the deviation volume).
        gamma_real = _net_grid_position_kwh(full)[:n]
        nom_mask = ~np.isnan(nomination_kwh)
        deviation_mwh = np.zeros(n, dtype=float)
        deviation_mwh[nom_mask] = (
            gamma_real[nom_mask] - nomination_kwh[nom_mask]
        ) / 1000.0
        dam_actual = (
            ts["dam_price_eur_per_mwh"].astype(float).to_numpy()[:n]
        )
        p_short, p_long = resolve_imbalance_prices(
            ts, dam_actual,
            pricing=imbalance_pricing,
            mult_short=imbalance_price_mult_short,
            mult_long=imbalance_price_mult_long,
        )
        cost = settle_imbalance(
            deviation_mwh, dam_actual, p_short, p_long,
            pricing=imbalance_pricing,
        )
        kpis["imbalance_cost_eur"] = round(float(cost.sum()), 2)
        kpis["imbalance_short_mwh"] = round(
            float(np.clip(-deviation_mwh, 0.0, None).sum()), 4,
        )
        kpis["imbalance_long_mwh"] = round(
            float(np.clip(deviation_mwh, 0.0, None).sum()), 4,
        )
        # PV-only counterfactual (Eq. U9): a plant with zero dispatch
        # freedom nominates min(forecast PV, cap) and delivers
        # min(actual PV, cap) — exact, no extra solve, paired with this
        # seed's noise draws by construction.  Cap approximated flat at
        # p_grid_export_max_kw x dt (hour-profile sub-caps ignored).
        pv_actual = (
            ts["pv_kwh"].astype(float).to_numpy()[:n]
            if "pv_kwh" in ts.columns else np.zeros(n)
        )
        cap_kwh = float(
            params.get("p_grid_export_max_kw", 0.0) or 0.0
        ) * dt_h_value
        if cap_kwh <= 0.0:
            cap_kwh = float("inf")
        pv_mask = ~np.isnan(nomination_pv_kwh)
        dev_pv_mwh = np.zeros(n, dtype=float)
        dev_pv_mwh[pv_mask] = (
            np.minimum(pv_actual[pv_mask], cap_kwh)
            - np.minimum(nomination_pv_kwh[pv_mask], cap_kwh)
        ) / 1000.0
        cost_pv = settle_imbalance(
            dev_pv_mwh, dam_actual, p_short, p_long,
            pricing=imbalance_pricing,
        )
        kpis["imbalance_cost_pv_only_eur"] = round(float(cost_pv.sum()), 2)
        kpis["bess_imbalance_hedge_value_eur"] = round(
            float(cost_pv.sum() - cost.sum()), 2,
        )

    # Identical scope to the pipeline's headline Year-1 KPIs: the same
    # post-solve unavailability derate is applied here so the foresight
    # gap compares derated-vs-derated (the factor cancels in the ratio).
    kpis = apply_unavailability_derate(
        kpis, float(params.get("unavailability_pct", 0.0) or 0.0),
    )
    kpis["year_close_soc_shortfall_kwh"] = round(year_close_shortfall_kwh, 4)
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
    strict: bool = False,
    **solve_kwargs: Any,
) -> pd.DataFrame:
    """Run rolling_horizon_dispatch with N seeds, return distribution.

    ``pf_profit_eur`` is the perfect-foresight benchmark used to compute
    ``foresight_gap_pct = 100 * (1 - rh_profit / pf_profit)``.  When
    ``None`` the gap column is NaN.  The percentage formula assumes a
    positive benchmark; when ``pf_profit_eur <= 0`` the sign of the gap
    inverts (a seed less negative than PF reads as a negative gap), so
    a warning is emitted once and consumers should read the absolute
    profits instead.

    Scope contract: pass the pipeline's headline (unavailability-derated)
    ``profit_total_eur`` — the per-seed profits returned by
    :func:`rolling_horizon_dispatch` carry the identical derate, so the
    benchmark and the ensemble compare like for like and the gap is
    derate-invariant.  Because every seed's dispatch is feasible for the
    perfect-foresight MILP (same constraints, including the year-close
    SOC condition), seeds cannot beat the benchmark beyond solver
    tolerance: the gap is non-negative up to ``mip_gap`` slack.  That
    bound is enforced at runtime: a seed whose profit exceeds
    ``pf_profit_eur + 2 * mip_gap * |pf_profit_eur| + 1`` triggers a
    prominent warning, or a ``RuntimeError`` when ``strict`` is True.

    Returns
    -------
    pandas.DataFrame
        One row per seed (RangeIndex; the seed is the ``seed`` column):
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
    # PF-bound tolerance: each window and the benchmark solve carry up
    # to mip_gap relative optimality slack, so a legitimate seed can
    # exceed the PF profit by at most ~2 x mip_gap x |PF| plus rounding.
    mip_gap = float(solve_kwargs.get("mip_gap", 0.001) or 0.001)
    if pf_profit_eur is not None and float(pf_profit_eur) <= 0.0:
        logger.warning(
            "monte_carlo_rolling: pf_profit_eur=%.2f is non-positive; "
            "foresight_gap_pct = 100*(1 - rh/pf) inverts its sign "
            "meaning for a non-positive benchmark — read the absolute "
            "profit column instead.", float(pf_profit_eur),
        )
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
        if pf_profit_eur is not None:
            # Runtime guard on the perfect-foresight bound: every seed's
            # dispatch is PF-feasible, so beating PF beyond the combined
            # solver slack means a modelling error (scope mismatch,
            # missing constraint in a window, wrong benchmark).  The one
            # legitimate exception is a year-close SOC shortfall (the
            # target was physically unreachable): the seed then carries
            # extra discharged energy the closed-cycle benchmark kept in
            # the battery, worth at most shortfall x the year's highest
            # energy price, so the bound widens by exactly that value.
            pf = float(pf_profit_eur)
            shortfall_kwh = float(
                kpis.get("year_close_soc_shortfall_kwh", 0.0) or 0.0,
            )
            shortfall_allowance = 0.0
            if shortfall_kwh > 0.0:
                max_price = float(
                    ts["dam_price_eur_per_mwh"].abs().max()
                ) if "dam_price_eur_per_mwh" in ts.columns else 0.0
                if "retail_price_eur_per_mwh" in ts.columns:
                    max_price = max(
                        max_price,
                        float(ts["retail_price_eur_per_mwh"].abs().max()),
                    )
                max_price = max(
                    max_price,
                    float(params.get("retail_tariff_eur_per_mwh", 0.0) or 0.0),
                )
                shortfall_allowance = shortfall_kwh / 1000.0 * max_price
            pf_bound = pf + 2.0 * mip_gap * abs(pf) + 1.0 + shortfall_allowance
            if profit > pf_bound:
                msg = (
                    f"monte_carlo_rolling: seed {seed} profit "
                    f"{profit:,.2f} EUR exceeds the perfect-foresight "
                    f"bound {pf_bound:,.2f} EUR (PF {pf:,.2f} EUR, "
                    f"mip_gap {mip_gap}, year-close shortfall allowance "
                    f"{shortfall_allowance:,.2f} EUR). A rolling-horizon "
                    "dispatch is PF-feasible and cannot legitimately beat "
                    "the benchmark beyond solver tolerance; check the KPI "
                    "scope and window constraints."
                )
                if strict:
                    raise RuntimeError(msg)
                logger.warning(msg)
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
            activated_mask = activated_by_product.get(product)
            if activated_mask is None or activated_mask.shape[0] != n:
                continue
            if product in PRODUCTS_UP:
                realised_soc -= activated_mask.astype(float) * r * dt_hours / eta_discharge
            else:
                realised_soc += activated_mask.astype(float) * r * dt_hours * eta_charge
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
    availability_factor: float = 1.0,
) -> dict[str, Any]:
    """Run the balancing-market Monte Carlo realisation across scenarios.

    Pulls the per-product reservation columns and per-step price columns
    from ``res`` (populated by :func:`pvbess_opt.optimization.run_scenario`
    when the balancing block fired). Returns aggregated P10/P50/P90 of
    total balancing revenue plus per-product breakdowns and the fraction
    of scenarios that hit a realised SOC bound.

    ``availability_factor`` scales every realised revenue figure (the
    quantiles, the per-product breakdowns, and the raw realisations) so
    the distribution shares the headline-KPI scope: the pipeline passes
    the same post-solve unavailability factor it applied to the
    deterministic ``bm_*`` revenue KPIs, keeping P50 comparable with
    ``bm_total_balancing_revenue_eur``.

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

    dt_h = dt_hours_from(params)
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
    # Per-scenario progress: emit ~20 INFO lines across the ensemble,
    # mirroring the monte_carlo_rolling cadence. Always log the final
    # scenario so the user sees a terminal "done" marker.
    n_total = int(n_scenarios)
    log_every = max(1, n_total // 20)
    t_mc_start = time.perf_counter()
    for s in range(n_total):
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

        done = s + 1
        if done % log_every == 0 or done == n_total:
            elapsed = time.perf_counter() - t_mc_start
            running_median = float(np.median(totals[:done]))
            eta_s = elapsed / done * (n_total - done) if done else 0.0
            logger.info(
                "[mc-balancing] scenario %d/%d done in %.1fs "
                "(running median = %.0f EUR, ETA %.1fm)",
                done, n_total, elapsed, running_median, eta_s / 60.0,
            )
            for h in logger.handlers + logging.getLogger().handlers:
                h.flush()

    af = max(0.0, min(1.0, float(availability_factor)))
    totals = totals * af
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
        q = np.quantile(arr * af, [0.10, 0.50, 0.90])
        aggregated[f"bm_{product}_capacity_revenue_p10_eur"] = float(round(q[0], 2))
        aggregated[f"bm_{product}_capacity_revenue_p50_eur"] = float(round(q[1], 2))
        aggregated[f"bm_{product}_capacity_revenue_p90_eur"] = float(round(q[2], 2))
    for product, arr in per_product_activation_totals.items():
        q = np.quantile(arr * af, [0.10, 0.50, 0.90])
        aggregated[f"bm_{product}_activation_revenue_p10_eur"] = float(round(q[0], 2))
        aggregated[f"bm_{product}_activation_revenue_p50_eur"] = float(round(q[1], 2))
        aggregated[f"bm_{product}_activation_revenue_p90_eur"] = float(round(q[2], 2))
    return aggregated
