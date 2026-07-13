"""Intraday-venue figures — duration curves and net position.

Rendering smoke, placeholder gating and the theme registrations for
``plot_da_ida_price_duration`` / ``plot_intraday_position``.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from pvbess_opt.plotting import (
    plot_da_ida_price_duration,
    plot_intraday_position,
)
from pvbess_opt.theme import (
    FINANCIAL_COLORS,
    FINANCIAL_LABELS,
    FINANCIAL_LEGEND_ORDER,
    financial_color,
)


def _res_with_intraday(n: int = 96) -> pd.DataFrame:
    h = np.arange(n) % 24
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=n, freq="h"),
        "dam_price_eur_per_mwh": 80.0
        - 30.0 * np.sin(np.pi * (h - 6) / 12.0),
        "ida_price_eur_per_mwh": 85.0
        + np.where(h >= 12, 25.0, -15.0),
        "id_sell_pv_kwh": np.where(h == 13, 400.0, 0.0),
        "id_sell_bess_kwh": np.where(h == 18, 900.0, 0.0),
        "id_buy_kwh": np.where(h == 3, 700.0, 0.0),
    })


def test_theme_registrations():
    assert FINANCIAL_COLORS["da_price_line"] == "#1E88E5"
    assert FINANCIAL_COLORS["ida_price_line"] == "#8E24AA"
    assert FINANCIAL_COLORS["id_position_line"] == "#00897B"
    for label in (
        "Day-ahead price", "Intraday price", "Intraday net position",
    ):
        assert label in FINANCIAL_LABELS
        assert label in FINANCIAL_LEGEND_ORDER
        assert financial_color(label).startswith("#")


def test_duration_figure_renders_and_gates(tmp_path, caplog):
    res = _res_with_intraday()
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.theme"):
        out = plot_da_ida_price_duration(
            res, tmp_path / "duration.pdf",
        )
    assert out.exists() and out.stat().st_size > 0
    assert not [
        r for r in caplog.records
        if "Non-canonical financial legend label" in r.getMessage()
    ]
    # Venue off: the placeholder renders instead of a stale figure.
    plain = res.drop(
        columns=["ida_price_eur_per_mwh"],
    )
    out_off = plot_da_ida_price_duration(plain, tmp_path / "off.pdf")
    assert out_off.exists()


def test_position_figure_renders_and_gates(tmp_path, caplog):
    res = _res_with_intraday()
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.theme"):
        out = plot_intraday_position(res, tmp_path / "position.pdf")
    assert out.exists() and out.stat().st_size > 0
    assert not [
        r for r in caplog.records
        if "Non-canonical financial legend label" in r.getMessage()
    ]
    plain = res.drop(columns=["id_sell_pv_kwh"])
    out_off = plot_intraday_position(plain, tmp_path / "off.pdf")
    assert out_off.exists()


def test_position_figure_without_timestamp(tmp_path):
    res = _res_with_intraday().drop(columns=["timestamp"])
    out = plot_intraday_position(res, tmp_path / "position_idx.pdf")
    assert out.exists() and out.stat().st_size > 0


def test_position_figure_month_axis_convention(tmp_path, monkeypatch):
    """Full-year time axis: one tick per month, MM-YYYY, rotated 45."""
    import matplotlib.pyplot as plt

    from pvbess_opt.theme import XTICK_ROT

    captured = {}
    orig_save = plt.Figure.savefig

    def _spy(fig, *args, **kwargs):
        ax = fig.axes[0]
        fig.canvas.draw()  # materialise the tick labels
        captured["labels"] = [t.get_text() for t in ax.get_xticklabels()]
        captured["rotations"] = {
            t.get_rotation() for t in ax.get_xticklabels()
        }
        return orig_save(fig, *args, **kwargs)

    monkeypatch.setattr(plt.Figure, "savefig", _spy)
    n = 8760
    res = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "id_sell_pv_kwh": np.zeros(n),
        "id_sell_bess_kwh": np.where(np.arange(n) % 24 == 18, 900.0, 0.0),
        "id_buy_kwh": np.where(np.arange(n) % 24 == 3, 700.0, 0.0),
    })
    out = plot_intraday_position(res, tmp_path / "position_year.pdf")
    assert out.exists()
    labels = [lab for lab in captured["labels"] if lab]
    # One tick per month across the year (01-2026 .. 12-2026).
    assert labels[0] == "01-2026"
    assert "12-2026" in labels
    assert len(labels) == 12
    assert captured["rotations"] == {float(XTICK_ROT)}
