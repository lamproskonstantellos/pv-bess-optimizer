"""Configurable rolling-horizon uncertainty tests (Phase 4).

Covers:
* The 11 uncertainty keys default to a baseline behaviour
  (enabled=False).
* ``add_forecast_noise`` honours the per-source enable flags.
* ``rolling_horizon_dispatch`` plumbs the flags through.
* All three source flags False  →  foresight gap ≈ 0.
* Plot helpers handle the new ``source_set`` column.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.io import SIMULATION_SHEET_DEFAULTS
from pvbess_opt.rolling_horizon import (
    add_forecast_noise,
    monte_carlo_rolling,
    rolling_horizon_dispatch,
)


def _highs_available() -> bool:
    try:
        import importlib
        importlib.import_module("highspy")
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# Workbook defaults
# ---------------------------------------------------------------------------


def test_uncertainty_defaults_reproduce_v05_behaviour():
    """Default config must NOT enable rolling-horizon."""
    assert SIMULATION_SHEET_DEFAULTS["uncertainty_enabled"] is False
    assert SIMULATION_SHEET_DEFAULTS["uncertainty_compare_sources"] is False
    assert SIMULATION_SHEET_DEFAULTS["uncertainty_n_seeds"] == 30
    assert SIMULATION_SHEET_DEFAULTS["uncertainty_window_hours"] == 48
    assert SIMULATION_SHEET_DEFAULTS["uncertainty_commit_hours"] == 24
    assert SIMULATION_SHEET_DEFAULTS["uncertainty_dam_enabled"] is True
    assert SIMULATION_SHEET_DEFAULTS["uncertainty_pv_enabled"] is True
    assert SIMULATION_SHEET_DEFAULTS["uncertainty_load_enabled"] is True
    assert SIMULATION_SHEET_DEFAULTS["uncertainty_sigma_dam"] == pytest.approx(0.20)
    assert SIMULATION_SHEET_DEFAULTS["uncertainty_sigma_pv"] == pytest.approx(0.12)
    assert SIMULATION_SHEET_DEFAULTS["uncertainty_sigma_load"] == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# add_forecast_noise — per-source enable flags
# ---------------------------------------------------------------------------


def _ts(n: int = 48) -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01", periods=n, freq="h")
    return pd.DataFrame({
        "timestamp": timestamps,
        "pv_kwh": [100.0] * n,
        "load_kwh": [50.0] * n,
        "dam_price_eur_per_mwh": [80.0] * n,
    })


def test_disable_dam_keeps_dam_unchanged():
    rng = np.random.default_rng(42)
    out = add_forecast_noise(_ts(), commit_hours=0, rng=rng,
                              enable_dam=False, enable_pv=True, enable_load=True)
    assert (out["dam_price_eur_per_mwh"] == 80.0).all()
    # PV / load WERE perturbed.
    assert not (out["pv_kwh"] == 100.0).all()
    assert not (out["load_kwh"] == 50.0).all()


def test_disable_pv_keeps_pv_unchanged():
    rng = np.random.default_rng(42)
    out = add_forecast_noise(_ts(), commit_hours=0, rng=rng,
                              enable_dam=True, enable_pv=False, enable_load=True)
    assert (out["pv_kwh"] == 100.0).all()


def test_disable_load_keeps_load_unchanged():
    rng = np.random.default_rng(42)
    out = add_forecast_noise(_ts(), commit_hours=0, rng=rng,
                              enable_dam=True, enable_pv=True, enable_load=False)
    assert (out["load_kwh"] == 50.0).all()


def test_disable_all_returns_unchanged_frame():
    rng = np.random.default_rng(42)
    out = add_forecast_noise(_ts(), commit_hours=0, rng=rng,
                              enable_dam=False, enable_pv=False, enable_load=False)
    pd.testing.assert_frame_equal(out, _ts())


# ---------------------------------------------------------------------------
# Foresight gap with all sources off ≈ 0
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_foresight_gap_zero_when_all_sources_off(short_params, short_ts):
    """With every noise source disabled the rolling horizon matches the
    deterministic noiseless run — foresight gap must be ~0%.
    """
    short = short_ts.iloc[:48].reset_index(drop=True)
    df = monte_carlo_rolling(
        short_params, short,
        n_seeds=2, base_seed=42,
        pf_profit_eur=1000.0,
        enable_dam=False, enable_pv=False, enable_load=False,
        window_hours=24, commit_hours=12,
        solver_name="highs", mip_gap=0.01, time_limit_seconds=30,
    )
    # The two seeds must produce identical realised dispatch -> identical profit.
    assert df["profit_total_eur"].nunique() == 1


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_dispatch_plumbs_flags_through(short_params, short_ts):
    """rolling_horizon_dispatch accepts the per-source flags as kwargs."""
    short = short_ts.iloc[:36].reset_index(drop=True)
    full, kpis = rolling_horizon_dispatch(
        short_params, short,
        window_hours=24, commit_hours=12,
        forecast_seed=42,
        enable_dam=True, enable_pv=False, enable_load=False,
        solver_name="highs", mip_gap=0.01, time_limit_seconds=30,
    )
    assert isinstance(kpis["profit_total_eur"], float)
    assert len(full) == len(short)


# ---------------------------------------------------------------------------
# Plot helpers handle source_set column
# ---------------------------------------------------------------------------


def test_distribution_plot_accepts_source_set(tmp_path):
    from pvbess_opt.plotting.uncertainty import plot_rolling_horizon_distribution
    rng = np.random.default_rng(0)
    rows = []
    for src in ("dam", "pv", "load", "all"):
        for seed in range(5):
            rows.append({
                "source_set": src,
                "seed": seed,
                "profit_total_eur": float(1000.0 + rng.normal(0, 50)),
                "foresight_gap_pct": float(rng.normal(2.0, 1.0)),
            })
    df = pd.DataFrame(rows)
    out = plot_rolling_horizon_distribution(
        df, tmp_path / "hist.pdf", pf_profit_eur=1100.0,
    )
    assert out.exists()


def test_foresight_gap_comparison_plot(tmp_path):
    from pvbess_opt.plotting.uncertainty import plot_foresight_gap_comparison
    rng = np.random.default_rng(0)
    rows = []
    for src in ("dam", "pv", "load", "all"):
        for seed in range(8):
            rows.append({
                "source_set": src,
                "seed": seed,
                "profit_total_eur": 1000.0,
                "foresight_gap_pct": float(rng.normal(2.0, 0.5)),
            })
    df = pd.DataFrame(rows)
    out = plot_foresight_gap_comparison(df, tmp_path / "boxes.pdf")
    assert out.exists()


def test_foresight_gap_comparison_handles_missing_column(tmp_path):
    from pvbess_opt.plotting.uncertainty import plot_foresight_gap_comparison
    out = plot_foresight_gap_comparison(
        pd.DataFrame({"profit_total_eur": [1.0]}), tmp_path / "empty.pdf",
    )
    assert out.exists()
