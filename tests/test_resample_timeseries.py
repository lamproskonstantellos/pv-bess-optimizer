"""Unit tests for the mixed-resolution timeseries resampler.

Covers both directions:

* downsampling a flow (15→60 min) must **sum** the sub-intervals;
* upsampling a flow (60→15 min) must **split** the parent value evenly;
* prices (stocks) forward-fill on upsample and average on downsample.
"""

from __future__ import annotations

import pandas as pd

from scripts.resample_timeseries import _resample_column


def _series(values: list[float], start: str, minutes: int) -> tuple[pd.Series, pd.DatetimeIndex]:
    idx = pd.date_range(start, periods=len(values), freq=f"{minutes}min")
    return pd.Series(values, index=idx), idx


def test_energy_downsample_sums_subintervals():
    # The canonical bug case: [10, 20, 30, 40] kWh at 15 min → 100 kWh at 60 min.
    s, idx = _series([10.0, 20.0, 30.0, 40.0], "2026-01-01 00:00", 15)
    out = _resample_column(s, idx, target_minutes=60, kind="energy")
    assert out.iloc[0] == 100.0
    assert out.sum() == 100.0


def test_energy_downsample_conserves_total_across_hours():
    s, idx = _series(
        [10.0, 20.0, 30.0, 40.0, 1.0, 2.0, 3.0, 4.0], "2026-01-01 00:00", 15
    )
    out = _resample_column(s, idx, target_minutes=60, kind="energy")
    assert list(out.values) == [100.0, 10.0]
    assert out.sum() == s.sum()


def test_energy_upsample_splits_evenly():
    # 60→15 min: each hourly kWh is divided into 4 equal quarters.
    s, idx = _series([100.0, 40.0], "2026-01-01 00:00", 60)
    out = _resample_column(s, idx, target_minutes=15, kind="energy")
    # First hour splits into 4 × 25, second into 4 × 10 (last point not padded).
    assert out.iloc[0] == 25.0
    assert out.iloc[1] == 25.0
    assert out.iloc[2] == 25.0
    assert out.iloc[3] == 25.0
    # Total energy of fully-expanded intervals is conserved.
    assert out.loc["2026-01-01 00:00":"2026-01-01 00:45"].sum() == 100.0


def test_price_downsample_takes_mean():
    s, idx = _series([10.0, 20.0, 30.0, 50.0], "2026-01-01 00:00", 15)
    out = _resample_column(s, idx, target_minutes=60, kind="price")
    assert out.iloc[0] == 27.5


def test_price_upsample_forward_fills():
    s, idx = _series([10.0, 20.0], "2026-01-01 00:00", 60)
    out = _resample_column(s, idx, target_minutes=15, kind="price")
    assert out.iloc[0] == 10.0
    assert out.iloc[1] == 10.0
    assert out.iloc[2] == 10.0
    assert out.iloc[3] == 10.0
    assert out.loc["2026-01-01 01:00"] == 20.0


def test_matching_step_passthrough():
    s, idx = _series([1.0, 2.0, 3.0], "2026-01-01 00:00", 15)
    out = _resample_column(s, idx, target_minutes=15, kind="energy")
    pd.testing.assert_series_equal(out, s)
