"""Monthly and yearly SOC plots draw a step line with no point markers.

The monthly / yearly SOC traces are daily / monthly aggregates, not
instantaneous readings; point markers on them misread as "SOC at this
instant".  These tests guard against an accidental reintroduction of
markers.  The daily SOC plot is genuine 15-minute point-in-time data
and is left untouched.
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


@pytest.fixture(autouse=True)
def _close_figures():
    plt.close("all")
    yield
    plt.close("all")


def _assert_no_markers(ax, label: str) -> None:
    mean_line = next(
        (ln for ln in ax.get_lines() if ln.get_label() == label), None,
    )
    assert mean_line is not None, f"missing SOC mean line {label!r}"
    marker = mean_line.get_marker()
    assert marker in (None, "", "None"), (
        f"SOC mean trace must not draw markers; found {marker!r}"
    )


def test_monthly_no_markers():
    fig = _capture_save(
        monthly_mod, monthly_mod.plot_monthly_soc, _soc_fixture(1), 1,
        Path("/tmp"),
    )
    _assert_no_markers(fig.axes[0], "Daily mean")


def test_yearly_no_markers():
    fig = _capture_save(
        yearly_mod, yearly_mod.plot_yearly_soc, _year_soc_fixture(), 2026,
        Path("/tmp"),
    )
    _assert_no_markers(fig.axes[0], "Monthly mean")


def test_daily_smoke(tmp_path):
    """plot_daily_soc still produces output without errors."""
    ts = pd.date_range("2026-04-20", "2026-04-20 23:45", freq="15min")
    n = len(ts)
    soc_kwh = np.linspace(5_000.0, 45_000.0, n)
    df = pd.DataFrame({
        "timestamp": ts,
        "soc_kwh": soc_kwh,
        "soc_pct": soc_kwh / 50_000.0 * 100.0,
    })
    daily_mod.plot_daily_soc(df, "2026-04-20", tmp_path)
    # save_figure_daily nests the PDF under a YYYY-MM subdirectory.
    assert (tmp_path / "2026-04" / "daily_soc_2026-04-20.pdf").exists()
