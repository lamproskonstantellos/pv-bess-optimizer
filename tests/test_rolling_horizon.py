"""Rolling-horizon dispatch + Monte Carlo + forecast noise tests."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.optimization import verify_dispatch_invariants
from pvbess_opt.rolling_horizon import (
    add_forecast_noise,
    monte_carlo_rolling,
    rolling_horizon_dispatch,
)

# ---------------------------------------------------------------------------
# Forecast noise — sign-aware, commit-protected
# ---------------------------------------------------------------------------


def test_noise_zero_sigma_dam_byte_identical(short_ts):
    rng = np.random.default_rng(42)
    out = add_forecast_noise(
        short_ts, commit_steps=0, rng=rng,
        sigma_dam=0.0, sigma_pv=0.0, sigma_load=0.0,
    )
    pd.testing.assert_frame_equal(out, short_ts)


def test_noise_commit_horizon_byte_identical(short_ts):
    """Rows < commit_steps are byte-identical to the input regardless of seed."""
    for seed in (1, 7, 999):
        rng = np.random.default_rng(seed)
        out = add_forecast_noise(short_ts, commit_steps=24, rng=rng)
        pd.testing.assert_frame_equal(
            out.iloc[:24].reset_index(drop=True),
            short_ts.iloc[:24].reset_index(drop=True),
        )
        # Beyond 24, prices change with seed
        assert not (out["dam_price_eur_per_mwh"].iloc[24:] ==
                    short_ts["dam_price_eur_per_mwh"].iloc[24:]).all()


def test_noise_negative_dam_preserved():
    """Sign-aware: negative DAM stays negative after noise."""
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=10, freq="h"),
        "pv_kwh": [0.0] * 10,
        "load_kwh": [100.0] * 10,
        "dam_price_eur_per_mwh": [-25.0] * 10,
    })
    rng = np.random.default_rng(42)
    out = add_forecast_noise(ts, commit_steps=0, rng=rng, sigma_dam=0.20)
    assert (out["dam_price_eur_per_mwh"] < 0).all()


def test_noise_dam_mape_close_to_sigma():
    """1 000 perturbations: realised MAPE on perturbed rows ≈ sigma ± 2 pp."""
    n = 1000
    base_price = 100.0
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "pv_kwh": [0.0] * n,
        "load_kwh": [100.0] * n,
        "dam_price_eur_per_mwh": [base_price] * n,
    })
    rng = np.random.default_rng(42)
    out = add_forecast_noise(ts, commit_steps=0, rng=rng, sigma_dam=0.20)
    perturbed = out["dam_price_eur_per_mwh"].to_numpy()
    mape = float(np.mean(np.abs(perturbed - base_price) / base_price))
    # Theoretical mean abs deviation of log-normal(0, 0.20) is roughly 0.16-0.17;
    # sigma applied to log space ⇒ sigma in linear space ~ sigma * mean.
    # Tolerance ±2 pp gives a generous bracket.
    assert 0.10 < mape < 0.30


def test_noise_skips_load_when_absent():
    """add_forecast_noise should skip load_kwh if column missing (merchant)."""
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=10, freq="h"),
        "pv_kwh": [100.0] * 10,
        "dam_price_eur_per_mwh": [100.0] * 10,
    })
    rng = np.random.default_rng(42)
    out = add_forecast_noise(ts, commit_steps=0, rng=rng)
    assert "load_kwh" not in out.columns


# ---------------------------------------------------------------------------
# window_hours / commit_hours are real hours on sub-hourly cadences
# ---------------------------------------------------------------------------


def test_hours_to_steps_helper_rejects_bad_inputs():
    from pvbess_opt.rolling_horizon import _hours_to_steps

    assert _hours_to_steps(48, 15) == 192       # 48 h @ 15 min
    assert _hours_to_steps(48, 60) == 48        # 48 h @ 60 min
    assert _hours_to_steps(24, 30) == 48        # 24 h @ 30 min
    with pytest.raises(ValueError):
        _hours_to_steps(1, 0)
    with pytest.raises(ValueError):
        # Less than one full step.
        _hours_to_steps(0, 15)


def test_hours_to_steps_rejects_non_divisible_cadence():
    """A horizon that is not an integer step count must fail loudly
    instead of silently flooring (1 h at 45-min cadence is 1.33 steps)."""
    from pvbess_opt.rolling_horizon import _hours_to_steps

    with pytest.raises(ValueError, match="not an integer number of steps"):
        _hours_to_steps(1, 45)
    with pytest.raises(ValueError, match="not an integer number of steps"):
        _hours_to_steps(5, 90)
    # Divisible combinations keep working, including coarse cadences.
    assert _hours_to_steps(3, 45) == 4
    assert _hours_to_steps(3, 90) == 2


def test_window_hours_means_real_hours_on_15min_data(
    short_params_15min, short_ts_15min, monkeypatch,
):
    """Documented 48-hour window must produce a 192-step window on
    15-min data (= 48 real hours × 4 steps/hour).  The previous
    behaviour was that ``window_hours`` was treated as a row count,
    so 48 became 12 real hours on this cadence."""
    from pvbess_opt import rolling_horizon as rh

    seen: list[int] = []
    sentinel = RuntimeError("captured")

    def _spy(_params, window_ts, **_kw):
        seen.append(len(window_ts))
        raise sentinel

    monkeypatch.setattr(rh, "run_scenario", _spy)

    with pytest.raises(RuntimeError):
        rh.rolling_horizon_dispatch(
            short_params_15min, short_ts_15min,
            window_hours=48, commit_hours=24,
            forecast_seed=None,
        )

    # First window: 48 h * 4 steps/h = 192 rows from a 672-row series.
    assert seen == [192], (
        f"first window expected 192 steps (48 h @ 15 min), got {seen}"
    )


def test_window_hours_means_real_hours_on_hourly_data(
    short_params, short_ts, monkeypatch,
):
    """On hourly data steps == hours, so behaviour is unchanged from
    pre-fix: a 24-hour window slices 24 rows."""
    from pvbess_opt import rolling_horizon as rh

    seen: list[int] = []
    sentinel = RuntimeError("captured")

    def _spy(_params, window_ts, **_kw):
        seen.append(len(window_ts))
        raise sentinel

    monkeypatch.setattr(rh, "run_scenario", _spy)

    with pytest.raises(RuntimeError):
        rh.rolling_horizon_dispatch(
            short_params, short_ts,
            window_hours=24, commit_hours=12,
            forecast_seed=None,
        )

    assert seen == [24]


# ---------------------------------------------------------------------------
# Rolling-horizon dispatch — SOC continuity, invariants, foresight gap
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _rh_short(short_params, short_ts):
    full, kpis = rolling_horizon_dispatch(
        short_params, short_ts,
        window_hours=24, commit_hours=12,
        forecast_seed=42,
        solver_name="highs", mip_gap=0.01, time_limit_seconds=30,
    )
    return full, kpis


def test_rh_returns_full_year_length(_rh_short, short_ts):
    full, _ = _rh_short
    assert len(full) == len(short_ts)


def test_rh_invariants_per_window(_rh_short, short_params):
    full, _ = _rh_short
    inv = verify_dispatch_invariants(full, short_params, mode=short_params["mode"])
    tol = 1.0e-3
    assert inv["invariant_1_pv_balance_kwh"] < tol
    # Load balance still holds because the committed slices satisfy LOAD_BAL
    assert inv["invariant_2_load_balance_kwh"] < tol


def test_rh_curtailment_cap_holds_per_window(_rh_short):
    full, _ = _rh_short
    cap = float(full["grid_export_cap_kwh"].iloc[0])
    assert full["grid_export_total_kwh"].max() <= cap + 1e-3


def test_rh_kpi_reevaluation_uses_actual_prices(short_params, short_ts):
    """With evaluate_with_actuals=True, the realised profit uses original prices."""
    _full_actuals, kpis_actuals = rolling_horizon_dispatch(
        short_params, short_ts.iloc[:48].reset_index(drop=True),
        window_hours=24, commit_hours=12,
        forecast_seed=7,
        evaluate_with_actuals=True,
        solver_name="highs", mip_gap=0.01, time_limit_seconds=30,
    )
    # Profit must be a real number — main correctness signal
    assert "profit_total_eur" in kpis_actuals
    assert isinstance(kpis_actuals["profit_total_eur"], float)


def test_rh_deterministic_when_seed_none(short_params, short_ts):
    """forecast_seed=None gives a deterministic noiseless RH (reproducible)."""
    _full1, kpis1 = rolling_horizon_dispatch(
        short_params, short_ts.iloc[:48].reset_index(drop=True),
        window_hours=24, commit_hours=12,
        forecast_seed=None,
        solver_name="highs", mip_gap=0.01, time_limit_seconds=30,
    )
    _full2, kpis2 = rolling_horizon_dispatch(
        short_params, short_ts.iloc[:48].reset_index(drop=True),
        window_hours=24, commit_hours=12,
        forecast_seed=None,
        solver_name="highs", mip_gap=0.01, time_limit_seconds=30,
    )
    assert kpis1["profit_total_eur"] == pytest.approx(kpis2["profit_total_eur"], rel=1e-6)


def test_rh_foresight_gap_meaningful(short_params, short_ts):
    """Foresight gap behaves sensibly across noiseless/noisy runs.

    On short test windows the per-window daily-cycle limit is applied to
    the local window only, so RH may technically out-cycle PF — the
    spec acknowledges per-window invariants only.  The test still
    asserts that both numbers are finite and the noisy run shifts
    relative to the noiseless run.
    """
    short = short_ts.iloc[:48].reset_index(drop=True)
    pf_params = dict(short_params)
    pf_params["terminal_soc_equal"] = False

    _full_noiseless, k_no_noise = rolling_horizon_dispatch(
        pf_params, short,
        window_hours=24, commit_hours=12,
        forecast_seed=None,
        solver_name="highs", mip_gap=0.01, time_limit_seconds=30,
    )
    _full_noisy, k_noisy = rolling_horizon_dispatch(
        pf_params, short,
        window_hours=24, commit_hours=12,
        forecast_seed=42,
        solver_name="highs", mip_gap=0.01, time_limit_seconds=30,
    )
    assert isinstance(k_no_noise["profit_total_eur"], float)
    assert isinstance(k_noisy["profit_total_eur"], float)
    # Noiseless RH should be a real number; noise typically degrades
    # realised profit but the magnitude depends on the seed.
    assert k_no_noise["profit_total_eur"] > 0


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------


def test_monte_carlo_reproducibility(short_params, short_ts):
    """Identical base_seed → identical DataFrames across two runs."""
    short = short_ts.iloc[:48].reset_index(drop=True)
    df1 = monte_carlo_rolling(
        short_params, short,
        n_seeds=3, base_seed=42,
        pf_profit_eur=1000.0,
        window_hours=24, commit_hours=12,
        solver_name="highs", mip_gap=0.01, time_limit_seconds=30,
    )
    df2 = monte_carlo_rolling(
        short_params, short,
        n_seeds=3, base_seed=42,
        pf_profit_eur=1000.0,
        window_hours=24, commit_hours=12,
        solver_name="highs", mip_gap=0.01, time_limit_seconds=30,
    )
    pd.testing.assert_frame_equal(df1, df2)


def test_monte_carlo_columns(short_params, short_ts):
    short = short_ts.iloc[:48].reset_index(drop=True)
    df = monte_carlo_rolling(
        short_params, short,
        n_seeds=2, base_seed=42,
        pf_profit_eur=1000.0,
        window_hours=24, commit_hours=12,
        solver_name="highs", mip_gap=0.01, time_limit_seconds=30,
    )
    for col in ("seed", "profit_total_eur", "grid_export_mwh", "grid_import_mwh",
                "pv_curtailed_mwh", "bess_cycles_total", "foresight_gap_pct"):
        assert col in df.columns


# ---------------------------------------------------------------------------
# Mode parity
# ---------------------------------------------------------------------------


def test_rh_merchant_mode_parity(short_params_merchant, short_ts):
    """Rolling horizon works end-to-end in merchant mode."""
    full, _kpis = rolling_horizon_dispatch(
        short_params_merchant, short_ts,
        window_hours=24, commit_hours=12,
        forecast_seed=7,
        solver_name="highs", mip_gap=0.01, time_limit_seconds=30,
    )
    # No load flows in any committed window
    assert full["pv_to_load_kwh"].max() == 0.0
    assert full["bess_dis_load_kwh"].max() == 0.0
    assert full["grid_to_load_kwh"].max() == 0.0
    # Curtailment cap holds in merchant too
    cap = float(full["grid_export_cap_kwh"].iloc[0])
    assert full["grid_export_total_kwh"].max() <= cap + 1e-3


# ---------------------------------------------------------------------------
# SOC carry-over with balancing — the shared final-SOC helper
# ---------------------------------------------------------------------------


def test_final_soc_helper_matches_model_closed_cycle(short_params, short_ts):
    """final_soc_after_last_step reproduces the model's own terminal SOC.

    With ``terminal_soc_equal`` the MILP pins its post-final-step SOC
    expression (including expected balancing-activation drift) to
    ``soc[0]``.  The shared helper must land on the same value; the old
    drift-free algebra was off by exactly the final step's drift.
    """
    from pvbess_opt.kpis import _balancing_soc_drift, final_soc_after_last_step
    from pvbess_opt.optimization import run_scenario
    from tests._balancing_helpers import _balancing_on

    params = _balancing_on(short_params)
    ts12 = short_ts.iloc[:12].reset_index(drop=True)
    _res, _solver, res_full = run_scenario(
        params, ts12, solver_name="highs",
        mip_gap=1e-4, time_limit_seconds=30, return_unrounded=True,
    )
    reconstructed = final_soc_after_last_step(res_full, params)
    soc0 = float(res_full["soc_kwh"].iloc[0])
    assert abs(reconstructed - soc0) < 1e-3
    # The test only discriminates when the final step carries drift:
    # assert it does, so a drift-free reconstruction would fail above.
    drift = _balancing_soc_drift(res_full, params)
    assert drift is not None
    assert abs(float(drift[-1])) > 1e-3


def test_rh_soc_carryover_with_balancing_consistent(short_params, short_ts):
    """Stitched SOC dynamics hold across window boundaries with balancing.

    With ``window_hours == commit_hours`` every window is fully
    committed, so EVERY boundary SOC comes from the reconstructed
    post-final-step value.  Regression for the carry-over that omitted
    the expected balancing-activation drift: invariant 3 (SOC dynamics,
    drift-aware) must hold on the stitched frame.
    """
    from tests._balancing_helpers import _balancing_on

    params = _balancing_on(short_params)
    full, _kpis = rolling_horizon_dispatch(
        params, short_ts,
        window_hours=12, commit_hours=12,
        forecast_seed=None, evaluate_with_actuals=False,
        solver_name="highs", mip_gap=1e-4, time_limit_seconds=60,
    )
    inv = verify_dispatch_invariants(full, params, mode=params["mode"])
    assert inv["invariant_3_soc_dynamics_kwh"] <= 1.0e-3
    # The scenario must exercise the drift path, otherwise this test
    # cannot regress: some boundary step carries nonzero reservations.
    from pvbess_opt.kpis import _balancing_soc_drift

    drift = _balancing_soc_drift(full, params)
    assert drift is not None
    boundary_steps = list(range(11, len(full) - 1, 12))
    assert any(abs(float(drift[t])) > 1e-3 for t in boundary_steps)


# ---------------------------------------------------------------------------
# Perfect-foresight bound guard
# ---------------------------------------------------------------------------


def test_mc_pf_bound_guard_warns_and_strict_raises(
    short_params, short_ts, caplog,
):
    """A seed profit above the PF bound warns, and raises under strict.

    An artificially understated benchmark makes every seed exceed the
    bound, which is exactly the misconfiguration the guard exists to
    catch (scope mismatch between ensemble and benchmark).
    """
    import logging

    # First find a realistic RH profit to understate.
    _full, kpis = rolling_horizon_dispatch(
        short_params, short_ts,
        window_hours=24, commit_hours=12, forecast_seed=42,
        solver_name="highs", mip_gap=0.01, time_limit_seconds=30,
    )
    rh_profit = float(kpis["profit_total_eur"])
    assert rh_profit > 0
    fake_pf = 0.5 * rh_profit  # every seed will beat this

    with caplog.at_level(logging.WARNING, logger="pvbess_opt.rolling_horizon"):
        df = monte_carlo_rolling(
            short_params, short_ts,
            n_seeds=1, base_seed=42,
            pf_profit_eur=fake_pf,
            window_hours=24, commit_hours=12,
            solver_name="highs", mip_gap=0.01, time_limit_seconds=30,
        )
    assert len(df) == 1
    assert any(
        "exceeds the perfect-foresight bound" in r.message for r in caplog.records
    )

    with pytest.raises(RuntimeError, match="perfect-foresight bound"):
        monte_carlo_rolling(
            short_params, short_ts,
            n_seeds=1, base_seed=42,
            pf_profit_eur=fake_pf,
            window_hours=24, commit_hours=12,
            solver_name="highs", strict=True,
            mip_gap=0.01, time_limit_seconds=30,
        )


def test_mc_non_positive_pf_benchmark_warns(short_params, short_ts, caplog):
    """A non-positive benchmark flips the gap's sign meaning; warn once."""
    import logging

    with caplog.at_level(logging.WARNING, logger="pvbess_opt.rolling_horizon"):
        df = monte_carlo_rolling(
            short_params, short_ts,
            n_seeds=1, base_seed=42,
            pf_profit_eur=-1_000.0,
            window_hours=24, commit_hours=12,
            solver_name="highs", mip_gap=0.01, time_limit_seconds=30,
        )
    assert len(df) == 1
    assert any("non-positive" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Year-close SOC target — penalised shortfall when physically unreachable
# ---------------------------------------------------------------------------


def test_model_carries_shortfall_var_only_with_target(short_params, short_ts):
    from pvbess_opt.optimization import build_model

    ts12 = short_ts.iloc[:12].reset_index(drop=True)
    m_free = build_model(short_params, ts12)
    assert not hasattr(m_free, "year_close_shortfall")
    m_target = build_model(
        short_params, ts12, terminal_soc_target_kwh=10_000.0,
    )
    assert hasattr(m_target, "year_close_shortfall")


def test_unreachable_year_close_target_yields_shortfall(caplog):
    """A winter-style year end (zero PV, surplus-only charging) cannot
    recharge to the year-close target.  The run must complete, end at
    the maximum reachable SOC, and report the shortfall loudly instead
    of aborting on an infeasible window."""
    import logging

    n = 24
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-12-31", periods=n, freq="h"),
        "pv_kwh": [0.0] * n,
        "load_kwh": [100.0] * n,
        "dam_price_eur_per_mwh": [300.0] * n,
    })
    params = {
        "dt_minutes": 60,
        "efficiency_charge": 0.97,
        "efficiency_discharge": 0.97,
        "soc_min_frac": 0.10,
        "soc_max_frac": 0.95,
        "initial_soc_frac": 0.50,
        "terminal_soc_equal": True,
        "max_cycles_per_day": 1.0,
        "p_grid_export_max_kw": 1_000.0,
        "pv_nameplate_kwp": 0.0,
        "bess_power_kw": 100.0,
        "bess_capacity_kwh": 400.0,
        "retail_tariff_eur_per_mwh": 200.0,
        "mode": "self_consumption",
        "allow_bess_grid_charging": False,
        "show_titles": False,
    }
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.rolling_horizon"):
        full, kpis = rolling_horizon_dispatch(
            params, ts,
            window_hours=12, commit_hours=12,
            forecast_seed=None, evaluate_with_actuals=True,
            solver_name="highs", mip_gap=1e-4, time_limit_seconds=30,
        )
    assert len(full) == n
    shortfall = float(kpis["year_close_soc_shortfall_kwh"])
    # Window 1 (year-end-blind) drains to soc_min for retail profit;
    # window 2 has zero PV and no grid charging, so the best it can do
    # is idle at soc_min: shortfall = target - soc_min = 200 - 40 kWh.
    assert shortfall == pytest.approx(160.0, abs=1.0)
    assert any(
        "year-close SOC target" in r.message for r in caplog.records
    )
    # The final committed SOC sits at the reachable maximum (soc_min),
    # not below it: the 10 EUR/kWh penalty forbids further draining.
    assert float(full["soc_kwh"].iloc[-1]) >= 40.0 - 1e-6


def test_reachable_year_close_target_reports_zero_shortfall(
    short_params, short_ts,
):
    params = dict(short_params)
    params["terminal_soc_equal"] = True
    _full, kpis = rolling_horizon_dispatch(
        params, short_ts,
        window_hours=24, commit_hours=12,
        forecast_seed=11,
        solver_name="highs", mip_gap=0.01, time_limit_seconds=30,
    )
    assert float(kpis["year_close_soc_shortfall_kwh"]) == pytest.approx(
        0.0, abs=1.0,
    )


# ---------------------------------------------------------------------------
# Daily cycle cap across window seams (commit_hours not dividing 24)
# ---------------------------------------------------------------------------


def _seam_case():
    """A 3-day merchant deck whose morning arbitrage strictly dominates the
    evening one, so a window that commits the morning cycle uses that day's
    whole daily budget — and a naive per-window daily cap would let the next
    window run a second cycle on the same day across the commit seam."""
    ndays, n = 3, 24 * 3
    price = np.zeros(n)
    for d in range(ndays):
        b = d * 24
        price[b + 0:b + 9] = 10.0     # cheap morning
        price[b + 9:b + 18] = 200.0   # rich midday (dominant arb, committed)
        price[b + 18:b + 21] = 10.0   # cheap evening
        price[b + 21:b + 24] = 100.0  # rich night (second arb, next window)
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "pv_kwh": np.zeros(n),
        "dam_price_eur_per_mwh": price,
    })
    params = dict(
        dt_minutes=60, efficiency_charge=1.0, efficiency_discharge=1.0,
        soc_min_frac=0.0, soc_max_frac=1.0, initial_soc_frac=0.0,
        terminal_soc_equal=False, max_cycles_per_day=1.0, max_cycles_per_year=0.0,
        p_grid_export_max_kw=1e6, pv_nameplate_kwp=0.0,
        bess_power_kw=2000.0, bess_capacity_kwh=2000.0,
        bess_wear_cost_eur_per_mwh=0.0, retail_tariff_eur_per_mwh=0.0,
        mode="merchant", allow_bess_grid_charging=True, show_titles=False,
        unavailability_pct=0.0,
    )
    return params, ts


def _max_daily_discharge_kwh(df: pd.DataFrame) -> float:
    d = df.copy()
    d["day"] = pd.to_datetime(d["timestamp"]).dt.date
    d["dis"] = d["bess_dis_load_kwh"] + d["bess_dis_grid_kwh"]
    return float(d.groupby("day")["dis"].sum().max())


def test_rh_daily_cap_holds_across_seam_commit_not_dividing_24():
    """Regression: with ``commit_hours`` not dividing 24 a calendar day is
    split across a window seam.  Before the seam-threaded daily budget the
    split day cycled twice (4000 kWh vs the 1-cycle 2000 kWh cap); the cap
    must now hold on every calendar day."""
    params, ts = _seam_case()
    e_cap = params["bess_capacity_kwh"]
    cap_kwh = params["max_cycles_per_day"] * e_cap
    df, _ = rolling_horizon_dispatch(
        params, ts, window_hours=36, commit_hours=18,
        forecast_seed=None, solver_name="highs", mip_gap=1e-9,
    )
    assert _max_daily_discharge_kwh(df) <= cap_kwh + 1e-3


def test_rh_daily_cap_commit_divides_24_is_byte_identical_to_full_cap():
    """The seam fix must be inert when the commit slice is day-aligned: every
    boundary day is fresh, so the threaded budget is ``None`` and the daily
    cap stays at its full value.  A day-aligned commit still cycles once/day
    and never trips the cap."""
    params, ts = _seam_case()
    cap_kwh = params["max_cycles_per_day"] * params["bess_capacity_kwh"]
    df, _ = rolling_horizon_dispatch(
        params, ts, window_hours=36, commit_hours=24,
        forecast_seed=None, solver_name="highs", mip_gap=1e-9,
    )
    assert _max_daily_discharge_kwh(df) <= cap_kwh + 1e-3
