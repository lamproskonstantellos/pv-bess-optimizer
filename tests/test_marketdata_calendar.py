"""Market-data calendar engine: resample, stitch, DST, leap, correctness.

Pure unit tests of :mod:`pvbess_opt.marketdata.base` — no network, no
workbook.  The three mandatory correctness cases of the design contract
are here: (a) hourly→15-min step-hold preserves revenue exactly for a
fixed dispatch, (b) a one-day known dispatch × known prices reproduces a
hand-computed revenue to the cent, (c) a reference year straddling the
2025-10-01 SDAC 15-minute MTU go-live stitches PT60M and PT15M months
with exact continuity.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.marketdata import (
    MarketDataError,
    PriceSegment,
    sample_local_year,
    stitch_segments_utc,
    validate_model_year_grid,
)

# The Europe/Athens local year 2025 starts 2024-12-31 22:00 UTC (EET,
# UTC+2) and spans exactly 8 760 UTC hours (the DST offsets cancel).
_ATHENS_2025_START_UTC = datetime(2024, 12, 31, 22, 0, tzinfo=UTC)
_ATHENS_2024_START_UTC = datetime(2023, 12, 31, 22, 0, tzinfo=UTC)


def _athens_year_series(
    values: np.ndarray, resolution_minutes: int = 60,
) -> pd.Series:
    seg = PriceSegment(
        start_utc=_ATHENS_2025_START_UTC,
        resolution_minutes=resolution_minutes,
        values=[float(v) for v in values],
    )
    series, _notes = stitch_segments_utc(
        [seg], resolution_minutes, column="dam_price_eur_per_mwh",
    )
    return series


def _grid_pos(local: str, dt_minutes: int) -> int:
    """Grid index of a naive local timestamp inside its calendar year."""
    stamp = pd.Timestamp(local)
    origin = pd.Timestamp(year=stamp.year, month=1, day=1)
    return int((stamp - origin) // pd.Timedelta(minutes=dt_minutes))


# ---------------------------------------------------------------------------
# Intensive-quantity resampling
# ---------------------------------------------------------------------------


def test_hourly_to_15min_step_holds_never_divides():
    hourly = np.arange(8760, dtype=float)
    series = _athens_year_series(hourly)
    seg = PriceSegment(_ATHENS_2025_START_UTC, 60, hourly.tolist())
    stitched, notes = stitch_segments_utc([seg], 15, column="dam")
    assert len(stitched) == 8760 * 4
    # Each hourly price repeats over its four quarters at full level.
    np.testing.assert_array_equal(
        stitched.to_numpy()[:8], [0, 0, 0, 0, 1, 1, 1, 1],
    )
    assert notes == []
    assert float(series.iloc[0]) == 0.0


def test_15min_to_hourly_takes_arithmetic_mean_with_note():
    quarters = np.array([10.0, 20.0, 30.0, 40.0] * 24)
    seg = PriceSegment(_ATHENS_2025_START_UTC, 15, quarters.tolist())
    stitched, notes = stitch_segments_utc([seg], 60, column="dam")
    assert len(stitched) == 24
    assert float(stitched.iloc[0]) == pytest.approx(25.0)
    assert any("averaged" in note for note in notes)


def test_incommensurable_resolution_rejected():
    seg = PriceSegment(_ATHENS_2025_START_UTC, 45, [1.0] * 8)
    with pytest.raises(MarketDataError, match="incommensurable"):
        stitch_segments_utc([seg], 60, column="dam")


def test_nan_prices_rejected():
    seg = PriceSegment(_ATHENS_2025_START_UTC, 60, [1.0, float("nan")])
    with pytest.raises(MarketDataError, match="NaN"):
        stitch_segments_utc([seg], 60, column="dam")


# ---------------------------------------------------------------------------
# Stitch continuity (SDAC 15-minute MTU go-live)
# ---------------------------------------------------------------------------

# Local hours from 2025-01-01 00:00 to 2025-10-01 00:00 Europe/Athens:
# 273 wall-clock days minus the spring-forward hour.
_HOURS_JAN_TO_OCT = 273 * 24 - 1


def _mtu_straddle_segments() -> list[PriceSegment]:
    """PT60M at 100 EUR until 2025-10-01 local, PT15M at 200 EUR after."""
    boundary_utc = _ATHENS_2025_START_UTC + pd.Timedelta(
        hours=_HOURS_JAN_TO_OCT,
    )
    hours_after = 8760 - _HOURS_JAN_TO_OCT
    return [
        PriceSegment(
            _ATHENS_2025_START_UTC, 60, [100.0] * _HOURS_JAN_TO_OCT,
        ),
        PriceSegment(
            boundary_utc, 15, [200.0] * (hours_after * 4),
        ),
    ]


def test_mtu_straddle_stitches_and_samples_exactly():
    stitched, _ = stitch_segments_utc(
        _mtu_straddle_segments(), 15, column="dam",
    )
    values = sample_local_year(
        stitched, tz_name="Europe/Athens", year=2025, dt_minutes=15,
        column="dam",
    )
    assert len(values) == 35040
    boundary = _grid_pos("2025-10-01 00:00", 15)
    assert values[boundary - 1] == 100.0
    assert values[boundary] == 200.0
    # No double-counted or lost step anywhere: every step carries one of
    # the two levels and the split point is exactly the boundary.
    assert int((values == 100.0).sum()) == boundary
    assert int((values == 200.0).sum()) == 35040 - boundary


def test_stitch_gap_is_a_hard_error():
    segs = _mtu_straddle_segments()
    segs[1].start_utc += pd.Timedelta(hours=1)
    with pytest.raises(MarketDataError, match="gap"):
        stitch_segments_utc(segs, 15, column="dam")


def test_stitch_overlap_is_a_hard_error():
    segs = _mtu_straddle_segments()
    segs[1].start_utc -= pd.Timedelta(hours=1)
    with pytest.raises(MarketDataError, match="overlap"):
        stitch_segments_utc(segs, 15, column="dam")


# ---------------------------------------------------------------------------
# DST transition days (Europe/Athens 2025)
# ---------------------------------------------------------------------------


def test_spring_forward_fills_by_repeating_previous_value():
    series = _athens_year_series(np.arange(8760, dtype=float))
    values = sample_local_year(
        series, tz_name="Europe/Athens", year=2025, dt_minutes=60,
        column="dam",
    )
    assert len(values) == 8760
    # 2025-03-30 03:00 local does not exist (clocks jump 03:00→04:00):
    # the grid keeps the step and repeats the 02:00 value.
    pos = _grid_pos("2025-03-30 03:00", 60)
    assert values[pos] == values[pos - 1]
    assert values[pos + 1] == values[pos - 1] + 1.0


def test_fall_back_drops_the_repeated_hour():
    series = _athens_year_series(np.arange(8760, dtype=float))
    values = sample_local_year(
        series, tz_name="Europe/Athens", year=2025, dt_minutes=60,
        column="dam",
    )
    # 2025-10-26 03:00 local occurs twice; the grid samples its FIRST
    # (summer-time) occurrence and drops the repeat, so stepping from
    # 03:00 to 04:00 skips exactly one UTC hour.
    pos = _grid_pos("2025-10-26 03:00", 60)
    assert values[pos] == values[pos - 1] + 1.0
    assert values[pos + 1] == values[pos] + 2.0


def test_transition_days_keep_full_grid_length():
    series = _athens_year_series(np.arange(8760, dtype=float))
    values = sample_local_year(
        series, tz_name="Europe/Athens", year=2025, dt_minutes=60,
        column="dam",
    )
    for day, first_hour in (("2025-03-30", 0), ("2025-10-26", 0)):
        pos = _grid_pos(f"{day} {first_hour:02d}:00", 60)
        assert len(values[pos:pos + 24]) == 24


# ---------------------------------------------------------------------------
# Leap reference year
# ---------------------------------------------------------------------------


def test_leap_year_drops_feb_29():
    hours_2024 = 8784
    seg = PriceSegment(
        _ATHENS_2024_START_UTC, 60,
        list(np.arange(hours_2024, dtype=float)),
    )
    stitched, _ = stitch_segments_utc([seg], 60, column="dam")
    values = sample_local_year(
        stitched, tz_name="Europe/Athens", year=2024, dt_minutes=60,
        column="dam",
    )
    assert len(values) == 8760
    # Feb 28 23:00 is followed by Mar 1 00:00 — the 24 Feb-29 hourly
    # values are gone from the grid (position math on the non-leap
    # output uses the non-leap day count: Jan 31 + Feb 28 = 59 days).
    feb28_last = 59 * 24 - 1
    assert values[feb28_last + 1] - values[feb28_last] == pytest.approx(25.0)


def test_missing_coverage_is_a_hard_error():
    seg = PriceSegment(
        _ATHENS_2025_START_UTC, 60, [50.0] * 8000,  # 760 hours short
    )
    stitched, _ = stitch_segments_utc([seg], 60, column="dam")
    with pytest.raises(MarketDataError, match="does not cover"):
        sample_local_year(
            stitched, tz_name="Europe/Athens", year=2025, dt_minutes=60,
            column="dam",
        )


# ---------------------------------------------------------------------------
# Model-grid validation
# ---------------------------------------------------------------------------


def _grid_frame(start: str, periods: int, freq: str) -> pd.DataFrame:
    return pd.DataFrame(
        {"timestamp": pd.date_range(start, periods=periods, freq=freq)},
    )


def test_grid_validation_accepts_full_non_leap_year():
    ts = _grid_frame("2026-01-01", 35040, "15min")
    assert validate_model_year_grid(ts, 15, context="test") == 35040


def test_grid_validation_rejects_partial_year():
    ts = _grid_frame("2026-01-01", 96, "15min")
    with pytest.raises(MarketDataError, match="full non-leap model year"):
        validate_model_year_grid(ts, 15, context="test")


def test_grid_validation_rejects_midyear_start():
    ts = _grid_frame("2026-06-01", 35040, "15min")
    with pytest.raises(MarketDataError, match="Jan 1 00:00"):
        validate_model_year_grid(ts, 15, context="test")


# ---------------------------------------------------------------------------
# Correctness: revenue preservation (design contract §resolution safety)
# ---------------------------------------------------------------------------


def test_hourly_prices_on_15min_grid_preserve_revenue_exactly():
    """(a) Fixed dispatch: hourly-grid revenue == 15-min-grid revenue.

    24 distinct hourly prices repeat daily; a constant 1 kW dispatch is
    0.25 kWh per quarter.  Step-hold makes each hour contribute
    ``price_h × 1 kWh`` on both grids — a divide-by-4 bug would cut the
    15-min revenue to a quarter, so equality here is the guard.
    """
    day_prices = np.linspace(10.0, 240.0, 24)
    hourly = np.tile(day_prices, 365)
    seg = PriceSegment(_ATHENS_2025_START_UTC, 60, hourly.tolist())
    stitched, _ = stitch_segments_utc([seg], 15, column="dam")
    quarter_prices = sample_local_year(
        stitched, tz_name="Europe/Athens", year=2025, dt_minutes=15,
        column="dam",
    )
    hourly_local = sample_local_year(
        stitch_segments_utc([seg], 60, column="dam")[0],
        tz_name="Europe/Athens", year=2025, dt_minutes=60, column="dam",
    )
    revenue_hourly = float((hourly_local * 1.0).sum() / 1000.0)
    revenue_quarter = float((quarter_prices * 0.25).sum() / 1000.0)
    assert revenue_quarter == pytest.approx(revenue_hourly, abs=1e-9)


def test_one_day_dispatch_reproduces_hand_computed_revenue():
    """(b) Known one-day dispatch × known prices, to the cent.

    Prices 10, 20, …, 240 EUR/MWh over 24 h; export 2 kWh in hours
    0-11 and 0.5 kWh in hours 12-23.  By hand:
    sum(h=1..12) of h*10 * 2 kWh   = 780 * 2 / 1000  = 1.56 EUR
    sum(h=13..24) of h*10 * 0.5 kWh = 2220 * 0.5 / 1000 = 1.11 EUR
    total = 2.67 EUR exactly.
    """
    day_prices = np.arange(10.0, 250.0, 10.0)
    dispatch_kwh = np.array([2.0] * 12 + [0.5] * 12)
    revenue = float((day_prices * dispatch_kwh).sum() / 1000.0)
    assert revenue == pytest.approx(2.67, abs=5e-3)
    assert round(revenue, 2) == 2.67
    # The same day priced through the calendar engine (step-hold onto
    # the 15-min grid, kWh split evenly across quarters) must agree to
    # the cent.
    hourly = np.tile(day_prices, 365)
    seg = PriceSegment(_ATHENS_2025_START_UTC, 60, hourly.tolist())
    stitched, _ = stitch_segments_utc([seg], 15, column="dam")
    quarter_prices = sample_local_year(
        stitched, tz_name="Europe/Athens", year=2025, dt_minutes=15,
        column="dam",
    )
    day_q = quarter_prices[:96]
    dispatch_q = np.repeat(dispatch_kwh, 4) / 4.0
    revenue_q = float((day_q * dispatch_q).sum() / 1000.0)
    assert round(revenue_q, 2) == 2.67
