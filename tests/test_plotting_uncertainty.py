"""Phase 8 — uncertainty-plot consistency + diagnostic regressions.

Asserts the house date format (DD-MM-YYYY), the pinned ``upper right``
legend placement, and that the four new diagnostic plots render
non-empty files.
"""

from __future__ import annotations

import re

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pytest

from pvbess_opt.plotting import (
    plot_input_forecast_band,
    plot_uncertainty_coverage_by_horizon,
    plot_uncertainty_crps_timeline,
    plot_uncertainty_pit_histogram,
    plot_uncertainty_residual_qq,
)
from pvbess_opt.plotting._dates import DATE_FMT, apply_house_date_axis
from pvbess_opt.plotting.inputs_uncertainty import (
    LEGEND_KWARGS,
    LEGEND_LOC,
)

_DDMMYYYY = re.compile(r"^\d{2}-\d{2}-\d{4}$")


def _fixture_ts(periods: int = 96 * 7) -> pd.DataFrame:
    """One week of 15-min data starting 02-04-2027."""
    t = pd.date_range("2027-04-02 00:00", periods=periods, freq="15min")
    h = (np.arange(periods) / 4.0) % 24.0
    return pd.DataFrame({
        "timestamp": t,
        "dam_price_eur_per_mwh": 80.0 + 20.0 * np.sin(np.pi * (h - 6) / 12.0),
        "pv_kwh": np.maximum(np.sin(np.pi * (h - 6) / 12.0) * 2000.0, 0.0),
        "load_kwh": np.full(periods, 1500.0),
    })


# ---------------------------------------------------------------------------
# Date format + legend constants
# ---------------------------------------------------------------------------


def test_date_fmt_is_ddmmyyyy():
    assert DATE_FMT == "%d-%m-%Y"


def test_apply_house_date_axis_emits_ddmmyyyy():
    fig, ax = plt.subplots()
    t = pd.date_range("2027-04-02", periods=7, freq="D")
    ax.plot(t, np.arange(7))
    apply_house_date_axis(ax)
    fig.canvas.draw()
    labels = [lbl.get_text() for lbl in ax.get_xticklabels() if lbl.get_text()]
    assert labels, "no tick labels rendered"
    assert all(_DDMMYYYY.match(lbl) for lbl in labels), labels
    # The formatter must render a known date as DD-MM-YYYY.
    fmt = ax.xaxis.get_major_formatter()
    rendered = fmt(mdates.date2num(pd.Timestamp("2027-04-02")))
    assert rendered == "02-04-2027", rendered
    plt.close(fig)


def test_legend_loc_constant():
    assert LEGEND_LOC == "upper right"
    assert LEGEND_KWARGS["loc"] == "upper right"


# ---------------------------------------------------------------------------
# Plot rendering + legend placement
# ---------------------------------------------------------------------------


def _legend_loc_code():
    """Resolve the integer loc code matplotlib assigns to LEGEND_KWARGS."""
    fig, ax = plt.subplots()
    ax.plot([0, 1], [0, 1], label="x")
    leg = ax.legend(**LEGEND_KWARGS)
    code = leg._loc
    plt.close(fig)
    return code


def _capture_legend_locs(monkeypatch, plot_call):
    """Call plot_call(), capturing each Axes' legend loc before figures close."""
    import pvbess_opt.plotting.inputs_uncertainty as iu

    captured: list[int] = []
    real_save = iu.save_figure

    def _spy(figpath):
        for ax in plt.gcf().get_axes():
            leg = ax.get_legend()
            if leg is not None:
                captured.append(leg._loc)
        return real_save(figpath)

    monkeypatch.setattr(iu, "save_figure", _spy)
    out = plot_call()
    return out, captured


_PLOTS = [
    ("coverage_by_horizon", plot_uncertainty_coverage_by_horizon),
    ("pit_histogram", plot_uncertainty_pit_histogram),
    ("crps_timeline", plot_uncertainty_crps_timeline),
    ("residual_qq", plot_uncertainty_residual_qq),
]


@pytest.mark.parametrize("name,fn", _PLOTS)
def test_diagnostic_plot_writes_nonempty(name, fn, tmp_path, monkeypatch):
    ts = _fixture_ts()
    expected_loc = _legend_loc_code()
    out, locs = _capture_legend_locs(
        monkeypatch, lambda: fn(ts, tmp_path / f"{name}.pdf"),
    )
    assert out.exists() and out.stat().st_size > 0
    assert locs, f"{name}: no legend captured"
    assert all(loc == expected_loc for loc in locs), (name, locs)


def test_forecast_band_legend_upper_right(tmp_path, monkeypatch):
    ts = _fixture_ts()
    expected_loc = _legend_loc_code()
    out, locs = _capture_legend_locs(
        monkeypatch,
        lambda: plot_input_forecast_band(
            ts, tmp_path / "fb.pdf", week_start_doy=92,
        ),
    )
    assert out.exists() and out.stat().st_size > 0
    assert locs, "no legend captured on forecast band"
    assert all(loc == expected_loc for loc in locs), locs
