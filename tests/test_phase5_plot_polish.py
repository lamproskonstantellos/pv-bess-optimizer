"""Phase-5 plot-polish tests.

Covers four independent correctness/cosmetic fixes:

1. Merchant PV-generation overlay masks the flat-zero night segments
   (``line_masked_zeros`` helper) instead of drawing a horizontal line
   at ``y == 0`` between sunrise and sunset.
2. Monthly / yearly SOC plots aggregate ``soc_pct`` directly, so the
   min / mean / max envelope is correct even when ``soc_pct`` and
   ``soc_kwh`` decouple (e.g. capacity fade across lifetime years).
3. The monthly SOC plot fill + mean line extend through the last day's
   bin to the start of the next month.
4. The yearly SOC plot extends to 01-2027 with a visible tick when the
   data covers 2026.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

import pvbess_opt.plotting.daily as daily_mod  # noqa: E402
import pvbess_opt.plotting.monthly as monthly_mod  # noqa: E402
import pvbess_opt.plotting.yearly as yearly_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_merchant_daily(date_str: str = "2026-06-01") -> pd.DataFrame:
    """Single-day merchant dispatch with PV active only in hours 6-18."""
    n = 24
    timestamps = pd.date_range(f"{date_str} 00:00", periods=n, freq="h")
    h = np.arange(n)
    pv = np.where(
        (h >= 6) & (h <= 18),
        500.0 * np.sin(np.pi * (h - 6) / 12.0),
        0.0,
    )
    return pd.DataFrame({
        "timestamp": timestamps,
        "load_kwh": np.zeros(n),
        "pv_kwh": pv,
        "pv_to_load_kwh": np.zeros(n),
        "pv_to_bess_kwh": pv * 0.2,
        "bess_charge_grid_kwh": np.zeros(n),
        "bess_dis_load_kwh": np.zeros(n),
        "bess_dis_grid_kwh": np.maximum(
            50.0 * np.sin(np.pi * h / 12.0), 0.0,
        ),
        "pv_to_grid_kwh": pv * 0.7,
        "pv_curtail_kwh": pv * 0.05,
        "grid_to_load_kwh": np.zeros(n),
        "soc_kwh": 200.0 + 100.0 * np.sin(np.pi * h / 12.0),
        "soc_pct": 50.0 + 25.0 * np.sin(np.pi * h / 12.0),
    })


def _make_soc_fixture(month: int, *, mode: str) -> pd.DataFrame:
    """Build a known-SOC fixture; SOC ramps daily so min/mean/max
    aggregations have crisp expected values."""
    n_days_map = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
                  7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
    n_days = n_days_map[month]
    n = n_days * 24
    timestamps = pd.date_range(f"2026-{month:02d}-01 00:00", periods=n, freq="h")
    capacity_kwh = 50_000.0
    h = np.arange(n).astype(float) % 24
    soc_pct = 20.0 + (95.0 - 20.0) * (h / 23.0)
    soc_kwh = soc_pct * capacity_kwh / 100.0
    df = pd.DataFrame({
        "timestamp": timestamps,
        "soc_kwh": soc_kwh,
        "soc_pct": soc_pct,
        "pv_kwh": np.zeros(n),
        "dam_price_eur_per_mwh": np.full(n, 80.0),
    })
    if mode == "vnb":
        df["load_kwh"] = np.full(n, 100.0)
    else:
        df["load_kwh"] = np.zeros(n)
    return df


def _make_year_soc_fixture(*, mode: str) -> pd.DataFrame:
    """12-month SOC fixture covering 2026 with known monthly envelope."""
    frames = [_make_soc_fixture(m, mode=mode) for m in range(1, 13)]
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Figure capture helpers
# ---------------------------------------------------------------------------


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


def _find_line(ax, label: str):
    for ln in ax.get_lines():
        if ln.get_label() == label:
            return ln
    return None


# ---------------------------------------------------------------------------
# Item 1 — merchant PV-generation overlay masks zero entries
# ---------------------------------------------------------------------------


def test_merchant_pv_generation_masks_night_zeros():
    df = _make_merchant_daily()
    fig = _capture_daily(
        daily_mod.plot_daily_combined_merchant, "2026-06-01", df,
    )
    ax = fig.axes[0]
    line = _find_line(ax, "PV generation")
    assert line is not None, "'PV generation' line must be drawn"
    y = np.asarray(line.get_ydata(), dtype=float)
    pv_padded = np.append(df["pv_kwh"].to_numpy(dtype=float), 0.0)
    zero_mask = pv_padded <= 1e-9
    assert np.all(np.isnan(y[zero_mask])), (
        "y must be NaN at every input zero (line breaks at zeros)"
    )
    assert np.any(~np.isnan(y)), "non-zero PV samples must remain plotted"


def test_merchant_pv_generation_skipped_when_all_zero():
    df = _make_merchant_daily()
    df["pv_kwh"] = 0.0
    fig = _capture_daily(
        daily_mod.plot_daily_combined_merchant, "2026-06-01", df,
    )
    ax = fig.axes[0]
    assert _find_line(ax, "PV generation") is None, (
        "all-zero PV ⇒ no 'PV generation' line"
    )
    legend = ax.get_legend()
    if legend is not None:
        labels = [t.get_text() for t in legend.get_texts()]
        assert "PV generation" not in labels


# ---------------------------------------------------------------------------
# Item 2 — monthly / yearly SOC plots aggregate soc_pct directly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["vnb", "merchant"])
def test_monthly_soc_min_max_match_soc_pct_aggregation(mode):
    df = _make_soc_fixture(month=1, mode=mode)
    fig = _capture_save(
        monthly_mod, monthly_mod.plot_monthly_soc, df, 1, Path("/tmp"),
    )
    ax = fig.axes[0]

    expected = (
        df.groupby(df["timestamp"].dt.date)["soc_pct"]
        .agg(["min", "mean", "max"])
    )

    mean_line = _find_line(ax, "Daily mean")
    assert mean_line is not None
    mean_y = np.asarray(mean_line.get_ydata())[:-1]
    np.testing.assert_allclose(
        mean_y, expected["mean"].to_numpy(), rtol=1e-9, atol=1e-9,
    )

    poly = next(
        c for c in ax.collections
        if "PolyCollection" in c.__class__.__name__
    )
    verts = poly.get_paths()[0].vertices
    ys = verts[:, 1]
    assert float(np.nanmin(ys)) == pytest.approx(
        float(expected["min"].min()), rel=1e-9, abs=1e-9,
    )
    assert float(np.nanmax(ys)) == pytest.approx(
        float(expected["max"].max()), rel=1e-9, abs=1e-9,
    )


@pytest.mark.parametrize("mode", ["vnb", "merchant"])
def test_yearly_soc_min_max_match_soc_pct_aggregation(mode):
    df = _make_year_soc_fixture(mode=mode)
    fig = _capture_save(
        yearly_mod, yearly_mod.plot_yearly_soc, df, 2026, Path("/tmp"),
    )
    ax = fig.axes[0]

    expected = (
        df.groupby(pd.to_datetime(df["timestamp"]).dt.to_period("M"))["soc_pct"]
        .agg(["min", "mean", "max"])
    )

    mean_line = _find_line(ax, "Monthly mean")
    assert mean_line is not None
    mean_y = np.asarray(mean_line.get_ydata())[:-1]
    np.testing.assert_allclose(
        mean_y, expected["mean"].to_numpy(), rtol=1e-9, atol=1e-9,
    )

    poly = next(
        c for c in ax.collections
        if "PolyCollection" in c.__class__.__name__
    )
    verts = poly.get_paths()[0].vertices
    ys = verts[:, 1]
    assert float(np.nanmin(ys)) == pytest.approx(
        float(expected["min"].min()), rel=1e-9, abs=1e-9,
    )
    assert float(np.nanmax(ys)) == pytest.approx(
        float(expected["max"].max()), rel=1e-9, abs=1e-9,
    )


@pytest.mark.parametrize("mode", ["vnb", "merchant"])
def test_monthly_soc_invariant_when_soc_pct_and_kwh_decouple(mode):
    df = _make_soc_fixture(month=1, mode=mode)
    df["soc_kwh"] = df["soc_kwh"] * 2.0
    fig = _capture_save(
        monthly_mod, monthly_mod.plot_monthly_soc, df, 1, Path("/tmp"),
    )
    ax = fig.axes[0]
    mean_line = _find_line(ax, "Daily mean")
    assert mean_line is not None
    mean_y = np.asarray(mean_line.get_ydata())[:-1]
    expected = (
        df.groupby(df["timestamp"].dt.date)["soc_pct"]
        .agg("mean")
        .to_numpy()
    )
    np.testing.assert_allclose(mean_y, expected, rtol=1e-9, atol=1e-9)


# ---------------------------------------------------------------------------
# Items 3 & 4 — x-axis extends through the last bin
# ---------------------------------------------------------------------------


def test_monthly_soc_xlim_extends_to_next_month_start():
    df = _make_soc_fixture(month=1, mode="merchant")
    fig = _capture_save(
        monthly_mod, monthly_mod.plot_monthly_soc, df, 1, Path("/tmp"),
    )
    ax = fig.axes[0]
    upper = ax.get_xlim()[1]
    expected = mdates.date2num(pd.Timestamp("2026-02-01"))
    assert upper == pytest.approx(expected, rel=0, abs=1e-6)


def test_yearly_soc_xlim_extends_to_next_year_start():
    df = _make_year_soc_fixture(mode="merchant")
    fig = _capture_save(
        yearly_mod, yearly_mod.plot_yearly_soc, df, 2026, Path("/tmp"),
    )
    ax = fig.axes[0]
    upper = ax.get_xlim()[1]
    expected = mdates.date2num(pd.Timestamp("2027-01-01"))
    assert upper == pytest.approx(expected, rel=0, abs=1e-6)


def test_monthly_soc_fill_reaches_right_edge():
    df = _make_soc_fixture(month=1, mode="merchant")
    fig = _capture_save(
        monthly_mod, monthly_mod.plot_monthly_soc, df, 1, Path("/tmp"),
    )
    ax = fig.axes[0]
    poly = next(
        c for c in ax.collections
        if "PolyCollection" in c.__class__.__name__
    )
    verts = poly.get_paths()[0].vertices
    max_x = float(np.max(verts[:, 0]))
    expected = mdates.date2num(pd.Timestamp("2026-02-01"))
    assert max_x == pytest.approx(expected, rel=0, abs=1e-6)
