"""Intraday venue inside the rolling-horizon Monte Carlo (Eq. U12).

Covers the sign-aware IDA forecast noise, the rng-stream bit-identity
of runs without the venue, the actuals-restore of the IDA price, the
post-stitch annual Stage-2 pass, the two-stage foresight benchmark and
the conditional ``id_net_revenue_eur`` Monte Carlo column.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.rolling_horizon import (
    PRICE_COLUMNS,
    add_forecast_noise,
    monte_carlo_rolling,
    rolling_horizon_dispatch,
)
from tests.conftest import _make_short_ts, _short_params


def _highs_available() -> bool:
    try:
        import highspy  # noqa: F401
    except ImportError:
        return False
    return True


def _id_params(**overrides) -> dict:
    params = _short_params("merchant")
    params["intraday"] = {
        "id_enabled": True,
        "id_max_deviation_frac_of_cap": 0.25,
        "id_allow_purchases": True,
        "id_fee_eur_per_mwh": 0.0,
        "id_inflation_pct": 0.0,
    }
    params["intraday"].update(overrides)
    return params


def _ts_with_ida(n_hours: int = 48) -> pd.DataFrame:
    ts = _make_short_ts(n_hours, with_load=False)
    h = np.arange(n_hours) % 24
    ts["ida_price_eur_per_mwh"] = (
        ts["dam_price_eur_per_mwh"].to_numpy(dtype=float)
        + np.where(h >= 12, 30.0, -20.0)
    )
    return ts


# ---------------------------------------------------------------------------
# Forecast noise (no solver needed)
# ---------------------------------------------------------------------------


def test_ida_noise_is_sign_aware_and_unbiased():
    n = 4000
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "pv_kwh": 0.0,
        "ida_price_eur_per_mwh": np.where(np.arange(n) % 7 == 0, -80.0, 90.0),
    })
    rng = np.random.default_rng(11)
    out = add_forecast_noise(
        ts, commit_steps=0, rng=rng,
        enable_dam=False, enable_pv=False, enable_load=False,
        sigma_ida=0.15, enable_ida=True,
    )
    noised = out["ida_price_eur_per_mwh"].to_numpy(dtype=float)
    orig = ts["ida_price_eur_per_mwh"].to_numpy(dtype=float)
    # Sign preserved everywhere (negative auction prices stay negative).
    assert (np.sign(noised) == np.sign(orig)).all()
    # Unit-mean multiplier: the realised mean magnitude stays within
    # 2 % of the input at n = 4000.
    ratio = np.abs(noised).mean() / np.abs(orig).mean()
    assert abs(ratio - 1.0) < 0.02
    # Committed rows are untouched.
    out2 = add_forecast_noise(
        ts, commit_steps=24, rng=np.random.default_rng(3),
        enable_dam=False, enable_pv=False, enable_load=False,
        enable_ida=True,
    )
    assert (
        out2["ida_price_eur_per_mwh"].iloc[:24].to_numpy()
        == orig[:24]
    ).all()


def test_ida_noise_flag_and_column_gating():
    """enable_ida=False leaves the column byte-identical, and a frame
    without the column draws nothing extra from the rng stream (the
    DAM multipliers of pre-existing seeds stay bit-identical)."""
    ts = _make_short_ts(48, with_load=False)
    base = add_forecast_noise(
        ts, commit_steps=0, rng=np.random.default_rng(5),
    )
    with_ida_col = ts.copy()
    with_ida_col["ida_price_eur_per_mwh"] = 55.0
    off = add_forecast_noise(
        with_ida_col, commit_steps=0, rng=np.random.default_rng(5),
        enable_ida=False,
    )
    # Flag off: IDA column untouched, DAM draws identical to a run
    # without the column at the same seed.
    assert (off["ida_price_eur_per_mwh"] == 55.0).all()
    pd.testing.assert_series_equal(
        off["dam_price_eur_per_mwh"], base["dam_price_eur_per_mwh"],
    )
    on = add_forecast_noise(
        with_ida_col, commit_steps=0, rng=np.random.default_rng(5),
        enable_ida=True,
    )
    # Flag on: the IDA draw happens LAST, so the DAM stream is STILL
    # identical (rng-order contract).
    pd.testing.assert_series_equal(
        on["dam_price_eur_per_mwh"], base["dam_price_eur_per_mwh"],
    )
    assert not (on["ida_price_eur_per_mwh"] == 55.0).all()


def test_ida_price_registered_and_restored():
    assert "ida_price_eur_per_mwh" in PRICE_COLUMNS


# ---------------------------------------------------------------------------
# Rolling-horizon two-stage pass (solver required)
# ---------------------------------------------------------------------------


pytestmark_solver = pytest.mark.skipif(
    not _highs_available(), reason="HiGHS solver not installed",
)


@pytestmark_solver
def test_rolling_horizon_runs_annual_stage2_pass():
    params = _id_params()
    ts = _ts_with_ida(48)
    full, kpis = rolling_horizon_dispatch(
        params, ts,
        window_hours=24, commit_hours=12,
        forecast_seed=7,
        mip_gap=0.01, time_limit_seconds=120,
    )
    # The Stage-2 pass ran: the stitched frame carries the intraday
    # trade columns and the realised KPIs the venue keys.
    assert "id_sell_pv_kwh" in full.columns
    assert "id_net_revenue_eur" in kpis
    # Actuals-restore: the frame's IDA prices equal the noise-free ts.
    assert np.allclose(
        full["ida_price_eur_per_mwh"].to_numpy(dtype=float),
        ts["ida_price_eur_per_mwh"].to_numpy(dtype=float),
    )


@pytestmark_solver
def test_rolling_horizon_id_off_bit_identical():
    """id_enabled=FALSE with an idle ida column: stitched frame and
    KPIs identical to a run without the column (same seed)."""
    params = _short_params("merchant")
    ts_plain = _make_short_ts(48, with_load=False)
    ts_ida = _ts_with_ida(48)
    full_plain, kpis_plain = rolling_horizon_dispatch(
        params, ts_plain,
        window_hours=24, commit_hours=12, forecast_seed=7,
        mip_gap=0.01, time_limit_seconds=120,
    )
    full_ida, kpis_ida = rolling_horizon_dispatch(
        params, ts_ida,
        window_hours=24, commit_hours=12, forecast_seed=7,
        mip_gap=0.01, time_limit_seconds=120,
    )
    pd.testing.assert_frame_equal(
        full_ida.drop(columns=["ida_price_eur_per_mwh"]), full_plain,
    )
    assert kpis_ida == kpis_plain


@pytestmark_solver
def test_monte_carlo_two_stage_benchmark_and_column():
    """Seeds carry id_net_revenue_eur; against the two-stage benchmark
    the strict PF-bound guard holds on a short synthetic year."""
    import pvbess_opt.pipeline as pipeline_mod
    from pvbess_opt.intraday import redispatch_intraday
    from pvbess_opt.kpis import compute_kpis
    from pvbess_opt.optimization import run_scenario

    params = _id_params()
    ts = _ts_with_ida(48)
    # Two-stage perfect-foresight benchmark: deterministic Stage 1 +
    # Stage 2 on actual prices (what pipeline._run_one feeds the MC).
    _res1, _s1, res1_full = run_scenario(
        params, ts, return_unrounded=True, mip_gap=0.001,
    )
    res2, _s2, _res2_full = redispatch_intraday(params, ts, res1_full)
    pf_kpis = compute_kpis(res2.copy(), params, verify_balance=False)
    pf_profit = float(pf_kpis["profit_total_eur"])

    mc = monte_carlo_rolling(
        params, ts,
        n_seeds=2, base_seed=3,
        pf_profit_eur=pf_profit,
        window_hours=24, commit_hours=12,
        strict=True,
        mip_gap=0.01, time_limit_seconds=120,
    )
    assert "id_net_revenue_eur" in mc.columns
    assert len(mc) == 2
    # Bit-identity of the MC schema without the venue.
    mc_off = monte_carlo_rolling(
        _short_params("merchant"), _make_short_ts(48, with_load=False),
        n_seeds=1, base_seed=3,
        window_hours=24, commit_hours=12,
        mip_gap=0.01, time_limit_seconds=120,
    )
    assert "id_net_revenue_eur" not in mc_off.columns
    # pipeline resolver: the ida flag is forced off without the venue.
    cfg_off = pipeline_mod._resolve_uncertainty_config(
        pipeline_mod.RunConfig(excel="x.xlsx"),
        {"uncertainty_ida_enabled": True, "id_enabled": False},
        mode="merchant",
    )
    assert cfg_off["enable_ida"] is False
    cfg_on = pipeline_mod._resolve_uncertainty_config(
        pipeline_mod.RunConfig(excel="x.xlsx"),
        {"uncertainty_ida_enabled": True, "id_enabled": True},
        mode="merchant",
    )
    assert cfg_on["enable_ida"] is True
    assert cfg_on["sigma_ida"] == 0.15
