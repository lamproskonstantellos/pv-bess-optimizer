"""``add_forecast_noise`` PV clip must be at nameplate, not per-window max.

Clipping at the per-window observed maximum biases the realised mean
downward: samples already at the peak only see noise that pushes them
lower (the upper clip is the peak itself), while samples below the peak
see two-sided noise.  At ``sigma_pv=0.12`` the noon-peak bias is around
−5% and grows to ~−12% at ``sigma_pv=0.3``.

The fix clips at the nameplate (kWp × dt_h), which is the true
physical ceiling, and leaves the realised mean unbiased.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from pvbess_opt.rolling_horizon import add_forecast_noise


def _bell_pv_profile() -> np.ndarray:
    """24-step bell-shaped daytime PV profile, peak at hour 12."""
    hours = np.arange(24, dtype=float)
    peak = 100.0
    profile = peak * np.exp(-0.5 * ((hours - 12.0) / 3.0) ** 2)
    profile[profile < 0.5] = 0.0
    return profile


def _measure_peak_realised_mean(
    sigma_pv: float,
    *,
    n_trials: int = 1000,
    pv_nameplate_kwp: float = 200.0,
    dt_h: float = 1.0,
    seed: int = 0,
) -> float:
    profile = _bell_pv_profile()
    peak_idx = int(np.argmax(profile))
    expected_peak = float(profile[peak_idx])

    rng = np.random.default_rng(seed)
    peak_samples = np.empty(n_trials, dtype=float)
    nameplate_kwh = pv_nameplate_kwp * dt_h
    for i in range(n_trials):
        ts = pd.DataFrame({"pv_kwh": profile.copy()})
        # commit_steps=0 → every row perturbed.
        out = add_forecast_noise(
            ts,
            commit_steps=0,
            rng=rng,
            sigma_dam=0.0,
            sigma_pv=sigma_pv,
            sigma_load=0.0,
            enable_dam=False,
            enable_pv=True,
            enable_load=False,
            pv_nameplate_kwh_per_step=nameplate_kwh,
        )
        peak_samples[i] = float(out["pv_kwh"].iloc[peak_idx])

    bias = (peak_samples.mean() - expected_peak) / expected_peak
    return float(bias)


def test_pv_noise_peak_unbiased_at_default_sigma():
    """Peak-hour realised mean within ±0.5% of true peak at sigma=0.12."""
    bias = _measure_peak_realised_mean(sigma_pv=0.12, n_trials=2000)
    assert abs(bias) < 0.005, f"Peak bias {bias:+.4%} exceeds ±0.5%"


def test_pv_noise_peak_unbiased_at_high_sigma():
    """At sigma=0.3 the realised mean stays within ±1.5%."""
    bias = _measure_peak_realised_mean(sigma_pv=0.3, n_trials=2000)
    assert abs(bias) < 0.015, f"Peak bias {bias:+.4%} exceeds ±1.5%"


def test_pv_noise_legacy_path_still_biased_and_warns(caplog):
    """Without a nameplate the legacy per-window-max clip is used,
    which is biased.  Confirms the fallback path is wired and that the
    function still produces values (no NaNs)."""
    import logging
    caplog.set_level(logging.WARNING, logger="pvbess_opt.rolling_horizon")

    # Reset the module-level "warned once" flag so this test reliably
    # observes the warning on first entry.
    import pvbess_opt.rolling_horizon as rh_mod
    rh_mod._NAMEPLATE_FALLBACK_WARNED = False

    profile = _bell_pv_profile()
    ts = pd.DataFrame({"pv_kwh": profile.copy()})
    rng = np.random.default_rng(42)
    out = add_forecast_noise(
        ts,
        commit_steps=0,
        rng=rng,
        sigma_dam=0.0,
        sigma_pv=0.12,
        sigma_load=0.0,
        enable_dam=False,
        enable_pv=True,
        enable_load=False,
        # pv_nameplate_kwh_per_step intentionally omitted.
    )
    assert not out["pv_kwh"].isna().any()
    # One warning should have been recorded the first time.
    fallback_warnings = [
        rec for rec in caplog.records
        if "pv_nameplate_kwh_per_step" in rec.getMessage()
    ]
    assert len(fallback_warnings) >= 1, (
        "Expected a one-time warning when nameplate is not supplied"
    )
