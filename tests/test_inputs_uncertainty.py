"""Input-uncertainty plotting tests."""

from __future__ import annotations

from pvbess_opt.plotting.inputs_uncertainty import (
    _lognormal_band,
    plot_dam_intraday_heatmap,
    plot_input_forecast_band,
    plot_input_seasonal_boxplot,
)


def test_lognormal_band_zero_sigma_collapses(short_ts):
    actual = short_ts["pv_kwh"].to_numpy()
    low, high = _lognormal_band(actual, 0.0)
    assert (low == actual).all()
    assert (high == actual).all()


def test_lognormal_band_p10_below_p90(short_ts):
    actual = short_ts["pv_kwh"].to_numpy()
    low, high = _lognormal_band(actual, 0.20)
    nonzero = actual > 0
    assert (low[nonzero] < actual[nonzero]).all()
    assert (high[nonzero] > actual[nonzero]).all()


def test_forecast_band_writes_pdf_per_source(short_ts, tmp_path):
    outs = plot_input_forecast_band(
        short_ts, tmp_path / "fb.pdf", week_start_doy=152,
    )
    assert [p.name for p in outs] == [
        "fb_dam.pdf", "fb_pv.pdf", "fb_load.pdf",
    ]
    assert all(p.exists() for p in outs)


def test_seasonal_boxplot_writes_pdf_per_source(short_ts, tmp_path):
    outs = plot_input_seasonal_boxplot(short_ts, tmp_path / "sb.pdf")
    assert [p.name for p in outs] == [
        "sb_dam.pdf", "sb_pv.pdf", "sb_load.pdf",
    ]
    assert all(p.exists() for p in outs)


def test_dam_heatmap_writes_pdf(short_ts, tmp_path):
    out = plot_dam_intraday_heatmap(short_ts, tmp_path / "hm.pdf")
    assert out.exists()
