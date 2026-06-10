"""Adaptive EUR tick precision — narrow axes must not collapse labels.

The rolling-horizon Monte-Carlo profit histogram spans a few hundred
EUR around ~€1.18M; with the fixed 1-decimal millions format every tick
rendered as ``€1.2M``.  The axis formatter now escalates precision from
the tick spacing so neighbouring ticks stay distinct, while wide axes
keep the historical ``€12.3M`` style and plain :func:`format_eur` calls
are byte-identical to before.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np

from pvbess_opt.plotting._currency import euro_axis_formatter, format_eur


def _tick_labels(xlim: tuple[float, float], mode: str = "auto") -> list[str]:
    fig, ax = plt.subplots()
    try:
        ax.set_xlim(*xlim)
        ax.xaxis.set_major_formatter(euro_axis_formatter(mode))
        fig.canvas.draw()
        labels = [t.get_text() for t in ax.get_xticklabels()]
    finally:
        plt.close(fig)
    return [t for t in labels if t]


def test_narrow_million_axis_ticks_are_distinct():
    # The regression case: MC profits spanning ~600 EUR around 1.18M.
    labels = _tick_labels((1_181_700.0, 1_182_300.0))
    assert len(labels) >= 3
    assert len(set(labels)) == len(labels), f"duplicate tick labels: {labels}"
    assert all(label.endswith("M") for label in labels)


def test_narrow_axis_distinct_in_millions_and_raw_modes():
    for mode in ("millions", "raw"):
        labels = _tick_labels((1_181_700.0, 1_182_300.0), mode)
        assert len(set(labels)) == len(labels), (mode, labels)


def test_narrow_thousands_axis_ticks_are_distinct():
    labels = _tick_labels((45_010.0, 45_090.0))
    assert len(set(labels)) == len(labels), labels
    assert all(label.endswith("k") for label in labels)


def test_wide_axis_keeps_historical_one_decimal_style():
    labels = _tick_labels((0.0, 30_000_000.0))
    # Nice ticks every 5M -> one decimal suffices and must not escalate.
    assert any(label == "€5.0M" for label in labels), labels
    assert all(label.count(".") <= 1 for label in labels)
    for label in labels:
        if "." in label and label.endswith("M"):
            assert len(label.split(".")[1]) == 2  # "xM" -> 1 digit + 'M'


def test_format_eur_defaults_unchanged():
    assert format_eur(12_345_678) == "€12.3M"
    assert format_eur(45_000) == "€45k"
    assert format_eur(850) == "€850"
    assert format_eur(-3_200_000) == "-€3.2M"
    assert format_eur(12_345_678, "millions") == "€12.3M"
    assert format_eur(12_345_678, "raw") == "€12,345,678"
    assert format_eur(float("nan")) == ""


def test_detached_formatter_falls_back_to_default_precision():
    fmt = euro_axis_formatter("auto")
    assert fmt(12_345_678, 0) == "€12.3M"
    assert fmt(45_000, 0) == "€45k"


def test_escalation_is_capped():
    # A pathologically narrow axis (1 EUR span at 1.18M) must not blow
    # past default+6 decimals.
    labels = _tick_labels((1_181_816.0, 1_181_817.0))
    for label in labels:
        if "." in label and label.endswith("M"):
            assert len(label.split(".")[1]) <= 8  # <= 7 digits + 'M'
    assert np.all([label.startswith("€") for label in labels])
