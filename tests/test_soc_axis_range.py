"""Render-time checks for the SOC plot dual-axis convention.

Every SOC plot (daily / monthly / yearly) must render with:

* Left axis pinned to (0, 100) with ticks every 10 %.
* Right axis pinned to (0, bess_capacity_kwh) with 11 evenly spaced
  ticks (so kWh ticks land on the same horizontal lines as the %
  ticks).
* Grid lines drawn only against the left (%) axis — the right axis is
  a pure relabelling and must not draw a second set of horizontal
  rules across the figure.

The tests intercept ``save_figure`` / ``save_figure_daily`` and inspect
the live figure instead of writing to disk.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

import pvbess_opt.plotting.daily as daily_mod  # noqa: E402
import pvbess_opt.plotting.monthly as monthly_mod  # noqa: E402
import pvbess_opt.plotting.yearly as yearly_mod  # noqa: E402

CAPACITY_KWH = 50_000.0
EXPECTED_LEFT_TICKS = list(np.arange(0, 101, 10).astype(float))


def _soc_frame(timestamps: pd.DatetimeIndex) -> pd.DataFrame:
    """Return a frame whose soc_pct ↔ soc_kwh ratio implies CAPACITY_KWH."""
    n = len(timestamps)
    soc_kwh = np.linspace(0.10 * CAPACITY_KWH, 0.90 * CAPACITY_KWH, n)
    return pd.DataFrame({
        "timestamp": timestamps,
        "soc_kwh": soc_kwh,
        "soc_pct": soc_kwh / CAPACITY_KWH * 100.0,
    })


def _capture_daily(date_str: str, df: pd.DataFrame) -> plt.Figure:
    captured: dict = {}

    def keep_open(out, _date_str):
        captured["fig"] = plt.gcf()
        return Path(out)

    original = daily_mod.save_figure_daily
    daily_mod.save_figure_daily = keep_open
    try:
        daily_mod.plot_daily_soc(df, date_str, Path("/tmp"))
    finally:
        daily_mod.save_figure_daily = original
    return captured["fig"]


def _capture_save(module, fn, *args) -> plt.Figure:
    captured: dict = {}

    def keep_open(out):
        captured["fig"] = plt.gcf()
        return Path(out)

    original = module.save_figure
    module.save_figure = keep_open
    try:
        fn(*args)
    finally:
        module.save_figure = original
    return captured["fig"]


@pytest.fixture(autouse=True)
def _close_figures():
    plt.close("all")
    yield
    plt.close("all")


def _assert_soc_axes(fig: plt.Figure, *, expected_left_lines: int = 1) -> None:
    axes = fig.axes
    assert len(axes) >= 2, "SOC plot must expose a twinx right axis"
    ax, ax2 = axes[0], axes[1]

    assert ax.get_ylim() == (0.0, 100.0)
    assert [float(t) for t in ax.get_yticks()] == EXPECTED_LEFT_TICKS

    ax2_ylim = ax2.get_ylim()
    assert ax2_ylim == (0.0, CAPACITY_KWH)
    ax2_ticks = list(ax2.get_yticks())
    assert len(ax2_ticks) == 11
    assert ax2_ticks[0] == pytest.approx(0.0)
    assert ax2_ticks[-1] == pytest.approx(CAPACITY_KWH)

    # Right axis must not draw its own gridlines — the left axis owns
    # the only visible grid on the plot.
    assert not any(g.get_visible() for g in ax2.yaxis.get_gridlines()), (
        "Right (SOC kWh) axis must not draw gridlines"
    )

    # The daily plot carries a single SOC Line2D; the monthly / yearly
    # plots draw the SOC trace as range-bar LineCollections instead, so
    # they expose zero Line2D objects on the left axis.
    assert len(ax.get_lines()) == expected_left_lines, (
        f"Left axis must carry {expected_left_lines} SOC Line2D object(s)"
    )
    assert len(ax2.get_lines()) == 0, (
        "Right axis is a pure relabelling — no line should be drawn on it"
    )


def test_daily_soc_axis_range():
    ts = pd.date_range("2026-04-20", "2026-04-20 23:45", freq="15min")
    fig = _capture_daily("2026-04-20", _soc_frame(ts))
    _assert_soc_axes(fig, expected_left_lines=1)


def test_monthly_soc_axis_range():
    ts = pd.date_range("2026-04-01", "2026-04-30 23:00", freq="1h")
    fig = _capture_save(
        monthly_mod, monthly_mod.plot_monthly_soc,
        _soc_frame(ts), 4, Path("/tmp"),
    )
    _assert_soc_axes(fig, expected_left_lines=0)


def test_yearly_soc_axis_range():
    ts = pd.date_range("2026-01-01", "2026-12-31 23:00", freq="1h")
    fig = _capture_save(
        yearly_mod, yearly_mod.plot_yearly_soc,
        _soc_frame(ts), 2026, Path("/tmp"),
    )
    _assert_soc_axes(fig, expected_left_lines=0)
