"""Uncertainty-plot consistency + diagnostic regressions.

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
    ticklabels = [lbl for lbl in ax.get_xticklabels() if lbl.get_text()]
    labels = [lbl.get_text() for lbl in ticklabels]
    assert labels, "no tick labels rendered"
    assert all(_DDMMYYYY.match(lbl) for lbl in labels), labels
    # The formatter must render a known date as DD-MM-YYYY.
    fmt = ax.xaxis.get_major_formatter()
    rendered = fmt(mdates.date2num(pd.Timestamp("2027-04-02")))
    assert rendered == "02-04-2027", rendered
    # House rotation: rotated right-anchored like every other dense
    # axis in the report (year, month and energy date axes).
    from pvbess_opt.theme import XTICK_ROT
    for lbl in ticklabels:
        assert lbl.get_rotation() == float(XTICK_ROT)
        assert lbl.get_horizontalalignment() == "right"
    plt.close(fig)


def test_legend_below_loc_constant():
    # The house rule anchors every legend below the axes from the
    # "upper center" corner of the legend box.
    from pvbess_opt.plotting.style import legend_below

    fig, ax = plt.subplots()
    ax.plot([0, 1], [0, 1], label="x")
    leg = legend_below(ax)
    assert leg is not None
    plt.close(fig)


# ---------------------------------------------------------------------------
# Plot rendering + legend placement
# ---------------------------------------------------------------------------


def _legend_loc_code(loc: str = "upper center"):
    """Resolve the integer loc code matplotlib assigns to a legend loc
    (the house below-the-axes placement anchors from "upper center")."""
    fig, ax = plt.subplots()
    ax.plot([0, 1], [0, 1], label="x")
    leg = ax.legend(loc=loc)
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


# Every diagnostic hangs its legend below the axes (house rule).
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
    # Per-source writers return the list of written paths; the
    # single-figure coverage plot returns one path.
    outs = out if isinstance(out, list) else [out]
    assert outs and all(p.exists() and p.stat().st_size > 0 for p in outs)
    assert locs, f"{name}: no legend captured"
    assert all(code == expected_loc for code in locs), (name, locs)


def test_forecast_band_legend_below(tmp_path, monkeypatch):
    ts = _fixture_ts()
    expected_loc = _legend_loc_code()
    outs, locs = _capture_legend_locs(
        monkeypatch,
        lambda: plot_input_forecast_band(
            ts, tmp_path / "fb.pdf", week_start_doy=92,
        ),
    )
    assert outs and all(p.exists() and p.stat().st_size > 0 for p in outs)
    assert locs, "no legend captured on forecast band"
    assert all(loc == expected_loc for loc in locs), locs


# ---------------------------------------------------------------------------
# Rolling-horizon distribution — degenerate-ensemble rendering
# ---------------------------------------------------------------------------


def _capture_distribution_axes(monkeypatch):
    """Spy on save_figure to capture the axes state at save time."""
    import pvbess_opt.plotting.uncertainty as unc

    captured: dict = {}
    real_save = unc.save_figure

    def _spy(out_path):
        ax = plt.gcf().axes[0]
        captured["xlim"] = ax.get_xlim()
        captured["texts"] = [t.get_text() for t in ax.texts]
        legend = ax.get_legend()
        captured["legend"] = (
            [t.get_text() for t in legend.get_texts()] if legend else []
        )
        captured["tick_labels"] = [
            ax.xaxis.get_major_formatter()(loc)
            for loc in ax.xaxis.get_majorticklocs()
        ]
        return real_save(out_path)

    monkeypatch.setattr(unc, "save_figure", _spy)
    return captured


def test_degenerate_ensemble_renders_dedicated_layout(tmp_path, monkeypatch):
    """Constant-profit frame: collapsed legend, readable x-window,
    whole-euro tick labels, PF marker present, no annotation box."""
    from pvbess_opt.plotting.uncertainty import plot_rolling_horizon_distribution

    captured = _capture_distribution_axes(monkeypatch)
    value = 768_584.32
    mc = pd.DataFrame({
        "seed": range(8),
        "profit_total_eur": [value + i * 0.05 for i in range(8)],  # 0.35 EUR spread
        "foresight_gap_pct": [0.0] * 8,
    })
    out = tmp_path / "degenerate.pdf"
    plt.close("all")
    plot_rolling_horizon_distribution(
        mc, out, pf_profit_eur=value, currency_format="raw",
    )
    assert out.exists() and out.stat().st_size > 0
    # Paper-ready axes: no explanatory annotation boxes.
    assert not captured["texts"]
    # Legend collapsed: the all-equal entry plus the PF marker, both
    # value-free.
    assert any("MC seeds (all equal)" in t for t in captured["legend"])
    assert any(t == "Perfect foresight" for t in captured["legend"])
    assert sum("P50" in t for t in captured["legend"]) == 0
    assert not any("€" in t or "=" in t for t in captured["legend"])
    # Readable x-window: at least +/- ~2 % of the value.
    lo, hi = captured["xlim"]
    assert hi - lo >= 0.03 * value
    # Whole-euro tick labels: no sub-euro decimals on a raw-format axis.
    for label in captured["tick_labels"]:
        digits = label.replace("€", "").replace(",", "").strip()
        assert "." not in digits, f"sub-euro tick label {label!r}"


def test_degenerate_ensemble_without_pf_renders_clean(
    tmp_path, monkeypatch,
):
    """No PF benchmark: the collapsed bar stands alone, still without
    any annotation box or PF marker."""
    from pvbess_opt.plotting.uncertainty import plot_rolling_horizon_distribution

    captured = _capture_distribution_axes(monkeypatch)
    mc = pd.DataFrame({
        "seed": range(5),
        "profit_total_eur": [1_000.0] * 5,
        "foresight_gap_pct": [0.0] * 5,
    })
    out = tmp_path / "degenerate_no_pf.pdf"
    plt.close("all")
    plot_rolling_horizon_distribution(mc, out, currency_format="raw")
    assert not captured["texts"]
    assert any("MC seeds (all equal)" in t for t in captured["legend"])
    assert not any("Perfect foresight" in t for t in captured["legend"])


def test_normal_spread_keeps_histogram_layout(tmp_path, monkeypatch):
    """A healthy ensemble keeps the P10/P50/P90 histogram presentation."""
    from pvbess_opt.plotting.uncertainty import plot_rolling_horizon_distribution

    captured = _capture_distribution_axes(monkeypatch)
    rng = np.random.default_rng(7)
    mc = pd.DataFrame({
        "seed": range(30),
        "profit_total_eur": 2_800_000.0 + rng.normal(0.0, 15_000.0, 30),
        "foresight_gap_pct": rng.normal(0.5, 0.1, 30),
    })
    out = tmp_path / "normal.pdf"
    plt.close("all")
    plot_rolling_horizon_distribution(
        mc, out, pf_profit_eur=2_850_000.0, currency_format="raw",
    )
    labels = captured["legend"]
    assert any("P10" in t for t in labels)
    assert any("P50" in t for t in labels)
    assert any("P90" in t for t in labels)
    assert any("Perfect foresight" in t for t in labels)
    assert not any("MC seeds" in t for t in labels)
    # Legend entries carry series names only, never computed values.
    assert not any("€" in t or "=" in t for t in labels)


def test_compare_sources_degenerate_collapses(tmp_path, monkeypatch):
    from pvbess_opt.plotting.uncertainty import plot_rolling_horizon_distribution

    captured = _capture_distribution_axes(monkeypatch)
    frames = []
    for src in ("dam", "pv", "load", "all"):
        frames.append(pd.DataFrame({
            "source_set": src,
            "seed": range(5),
            "profit_total_eur": [500_000.0] * 5,
            "foresight_gap_pct": [0.0] * 5,
        }))
    mc = pd.concat(frames, ignore_index=True)
    out = tmp_path / "compare_degenerate.pdf"
    plt.close("all")
    plot_rolling_horizon_distribution(
        mc, out, pf_profit_eur=500_000.0, currency_format="raw",
    )
    assert any("MC seeds (all equal)" in t for t in captured["legend"])
    assert any("Perfect foresight" in t for t in captured["legend"])
    assert not captured["texts"]
