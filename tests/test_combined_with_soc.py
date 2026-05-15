"""Render-time checks for ``plot_daily_combined_with_soc`` (VNB) and
``plot_daily_combined_merchant_with_soc`` (merchant).

The combined-with-SOC plots add a SOC (%) overlay on the right axis to
the existing daily combined energy stacks.  Their axis convention is
intentionally different from the SOC-only plots: Energy lives on the
LEFT, SOC (%) on the RIGHT.  These tests guard:

* PDFs render for both modes when the BESS is present;
* the right-axis SOC range and tick layout match the spec;
* the right axis carries no gridlines;
* the plot collapses to a single Axes (no ``twinx``) when the BESS is
  absent (every SOC value is zero);
* the ``Import→BESS (charge)`` label appears in the legend when
  grid-charging is active in VNB mode.
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


def _make_dispatch_vnb(n_hours: int = 48) -> pd.DataFrame:
    """48-h VNB-flavoured dispatch with non-zero SOC and load."""
    n = n_hours
    timestamps = pd.date_range("2026-06-01 00:00", periods=n, freq="h")
    h = np.arange(n) % 24
    pv = 1000.0 * np.where((h >= 6) & (h <= 18),
                           np.sin(np.pi * (h - 6) / 12.0), 0.0)
    load = np.full(n, 200.0)
    pv_to_load = np.minimum(pv, load)
    grid_to_load = np.maximum(load - pv_to_load, 0.0)
    return pd.DataFrame({
        "timestamp": timestamps,
        "load_kwh": load,
        "pv_kwh": pv,
        "pv_to_load_kwh": pv_to_load,
        "pv_to_bess_kwh": np.maximum((pv - pv_to_load) * 0.4, 0.0),
        "bess_charge_grid_kwh": np.zeros(n),
        "bess_dis_load_kwh": np.zeros(n),
        "bess_dis_grid_kwh": np.maximum(50.0 * np.sin(np.pi * h / 12.0), 0.0),
        "pv_to_grid_kwh": np.maximum((pv - pv_to_load) * 0.5, 0.0),
        "pv_curtail_kwh": np.maximum((pv - pv_to_load) * 0.05, 0.0),
        "grid_to_load_kwh": grid_to_load,
        "soc_kwh": 200.0 + 100.0 * np.sin(np.pi * h / 12.0),
        "soc_pct": 50.0 + 25.0 * np.sin(np.pi * h / 12.0),
    })


def _make_dispatch_merchant(n_hours: int = 48) -> pd.DataFrame:
    """48-h merchant dispatch with zero load and non-zero SOC."""
    df = _make_dispatch_vnb(n_hours)
    df["load_kwh"] = 0.0
    df["pv_to_load_kwh"] = 0.0
    df["grid_to_load_kwh"] = 0.0
    df["bess_dis_load_kwh"] = 0.0
    return df


def _capture_daily_combined(
    fn, date_str: str, df: pd.DataFrame,
) -> plt.Figure:
    """Run ``fn`` against an in-memory ``save_figure_daily`` and return
    the live figure for assertions."""
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


# ---------------------------------------------------------------------------
# 1-2. Render smoke tests
# ---------------------------------------------------------------------------


def test_vnb_combined_with_soc_writes_pdf(tmp_path):
    df = _make_dispatch_vnb()
    daily_mod.plot_daily_combined_with_soc(df, "2026-06-01", tmp_path)
    files = list(tmp_path.rglob("daily_combined_with_soc_2026-06-01.pdf"))
    assert files


def test_merchant_combined_with_soc_writes_pdf(tmp_path):
    df = _make_dispatch_merchant()
    daily_mod.plot_daily_combined_merchant_with_soc(
        df, "2026-06-01", tmp_path,
    )
    files = list(tmp_path.rglob("daily_combined_with_soc_2026-06-01.pdf"))
    assert files


# ---------------------------------------------------------------------------
# 3. BESS-absent collapse — single Axes, no twinx
# ---------------------------------------------------------------------------


def test_vnb_combined_with_soc_skips_overlay_when_no_bess():
    df = _make_dispatch_vnb()
    df["soc_kwh"] = 0.0
    df["soc_pct"] = 0.0
    fig = _capture_daily_combined(
        daily_mod.plot_daily_combined_with_soc, "2026-06-01", df,
    )
    assert len(fig.axes) == 1, (
        "no BESS ⇒ no SOC overlay ⇒ exactly one Axes (no twinx)"
    )


def test_merchant_combined_with_soc_skips_overlay_when_no_bess():
    df = _make_dispatch_merchant()
    df["soc_kwh"] = 0.0
    df["soc_pct"] = 0.0
    fig = _capture_daily_combined(
        daily_mod.plot_daily_combined_merchant_with_soc, "2026-06-01", df,
    )
    assert len(fig.axes) == 1


# ---------------------------------------------------------------------------
# 4. SOC axis range / ticks / labels / no gridlines
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fn_name", [
    "plot_daily_combined_with_soc",
    "plot_daily_combined_merchant_with_soc",
])
def test_soc_overlay_axis_layout(fn_name):
    fn = getattr(daily_mod, fn_name)
    df = (
        _make_dispatch_vnb()
        if fn_name == "plot_daily_combined_with_soc"
        else _make_dispatch_merchant()
    )
    fig = _capture_daily_combined(fn, "2026-06-01", df)
    assert len(fig.axes) >= 2, "SOC overlay must add a twinx right axis"
    ax2 = fig.axes[1]
    assert ax2.get_ylim() == (0.0, 100.0)
    assert list(ax2.get_yticks()) == list(np.arange(0, 101, 10))
    assert ax2.get_ylabel() == "SOC (%)"
    assert not any(g.get_visible() for g in ax2.yaxis.get_gridlines()), (
        "right (SOC %) axis must not draw its own gridlines"
    )


# ---------------------------------------------------------------------------
# 5. Grid-charging visibility — VNB
# ---------------------------------------------------------------------------


def test_vnb_combined_with_soc_legend_shows_grid_charge():
    df = _make_dispatch_vnb()
    # Force grid-charging at night (zero PV) for the first six hours.
    df.loc[:5, "bess_charge_grid_kwh"] = 150.0
    fig = _capture_daily_combined(
        daily_mod.plot_daily_combined_with_soc, "2026-06-01", df,
    )
    legend = fig.axes[0].get_legend()
    assert legend is not None
    labels = [text.get_text() for text in legend.get_texts()]
    assert "Import→BESS (charge)" in labels


# ---------------------------------------------------------------------------
# 6. Dispatcher wiring — both modes render daily_combined_with_soc_*.pdf
# ---------------------------------------------------------------------------


def test_dispatcher_renders_vnb_combined_with_soc(tmp_path):
    from main import _generate_energy_plots_for_year

    df = _make_dispatch_vnb()
    _generate_energy_plots_for_year(
        df, 2026, tmp_path,
        daily=True, monthly=False, yearly=False,
        mode="vnb",
    )
    assert list(tmp_path.rglob("daily_combined_with_soc_*.pdf"))


def test_dispatcher_renders_merchant_combined_with_soc(tmp_path):
    from main import _generate_energy_plots_for_year

    df = _make_dispatch_merchant()
    _generate_energy_plots_for_year(
        df, 2026, tmp_path,
        daily=True, monthly=False, yearly=False,
        mode="merchant",
    )
    assert list(tmp_path.rglob("daily_combined_with_soc_*.pdf"))
