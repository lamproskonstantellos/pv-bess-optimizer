"""Option-B SOC plot tests (Phase 4, v0.8.8).

Monthly and yearly SOC plots render the aggregate as a vertical range
bar (min->max) per period with a short horizontal mean tick — no
connecting line, no point markers.  The daily SOC plot is unchanged.
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

_DAYS_IN_MONTH = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
                   7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}


def _soc_fixture(month: int) -> pd.DataFrame:
    n_days = _DAYS_IN_MONTH[month]
    n = n_days * 24
    timestamps = pd.date_range(f"2026-{month:02d}-01 00:00", periods=n, freq="h")
    capacity_kwh = 50_000.0
    h = np.arange(n).astype(float) % 24
    soc_pct = 20.0 + (95.0 - 20.0) * (h / 23.0)
    return pd.DataFrame({
        "timestamp": timestamps,
        "soc_kwh": soc_pct * capacity_kwh / 100.0,
        "soc_pct": soc_pct,
        "pv_kwh": np.zeros(n),
        "load_kwh": np.zeros(n),
    })


def _year_soc_fixture() -> pd.DataFrame:
    return pd.concat([_soc_fixture(m) for m in range(1, 13)], ignore_index=True)


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


def _capture_daily(fn, date_str: str, df: pd.DataFrame) -> plt.Figure:
    captured: dict = {}

    def keep_open(out, _date_str):
        captured["fig"] = plt.gcf()
        return Path(out)

    original = daily_mod.save_figure_daily
    daily_mod.save_figure_daily = keep_open
    try:
        fn(df, date_str, Path("/tmp"))
    finally:
        daily_mod.save_figure_daily = original
    return captured["fig"]


@pytest.fixture(autouse=True)
def _close_figures():
    plt.close("all")
    yield
    plt.close("all")


def _line_collections(ax, label: str):
    return [
        c for c in ax.collections
        if "LineCollection" in type(c).__name__ and c.get_label() == label
    ]


def test_monthly_renders_range_bars():
    fig = _capture_save(
        monthly_mod, monthly_mod.plot_monthly_soc, _soc_fixture(1), 1,
        Path("/tmp"),
    )
    ax = fig.axes[0]
    bars = _line_collections(ax, "SOC range (min-max)")
    ticks = _line_collections(ax, "Mean SOC")
    assert len(bars) == 1 and len(ticks) == 1
    assert len(bars[0].get_segments()) == 31
    assert len(ticks[0].get_segments()) == 31


def test_yearly_renders_range_bars():
    fig = _capture_save(
        yearly_mod, yearly_mod.plot_yearly_soc, _year_soc_fixture(), 2026,
        Path("/tmp"),
    )
    ax = fig.axes[0]
    bars = _line_collections(ax, "SOC range (min-max)")
    ticks = _line_collections(ax, "Mean SOC")
    assert len(bars) == 1 and len(ticks) == 1
    assert len(bars[0].get_segments()) == 12
    assert len(ticks[0].get_segments()) == 12


@pytest.mark.parametrize(
    "module,fn,args",
    [
        ("monthly", "plot_monthly_soc", (1,)),
        ("yearly", "plot_yearly_soc", (2026,)),
    ],
)
def test_no_markers_present(module, fn, args):
    """No Line2D with a visible marker is drawn for the SOC trace —
    catches an accidental reintroduction of point markers."""
    if module == "monthly":
        fig = _capture_save(
            monthly_mod, monthly_mod.plot_monthly_soc, _soc_fixture(1), *args,
            Path("/tmp"),
        )
    else:
        fig = _capture_save(
            yearly_mod, yearly_mod.plot_yearly_soc, _year_soc_fixture(), *args,
            Path("/tmp"),
        )
    ax = fig.axes[0]
    for line in ax.get_lines():
        marker = line.get_marker()
        assert marker in (None, "", "None"), (
            f"SOC trace must not draw markers; found {marker!r}"
        )


def test_daily_plot_unchanged():
    """plot_daily_soc still renders its single stepped SOC Line2D."""
    ts = pd.date_range("2026-04-20", "2026-04-20 23:45", freq="15min")
    n = len(ts)
    soc_kwh = np.linspace(5_000.0, 45_000.0, n)
    df = pd.DataFrame({
        "timestamp": ts,
        "soc_kwh": soc_kwh,
        "soc_pct": soc_kwh / 50_000.0 * 100.0,
    })
    fig = _capture_daily(daily_mod.plot_daily_soc, "2026-04-20", df)
    ax = fig.axes[0]
    assert len(ax.get_lines()) == 1, "daily SOC plot must keep its step line"
