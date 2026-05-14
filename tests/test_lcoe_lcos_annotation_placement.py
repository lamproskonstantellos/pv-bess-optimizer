"""v0.8.1: LCOS annotation placement when the project bar is to the
left of the Lazard benchmark band.

Old behaviour (v0.8.0): annotation went to the right of the project bar
at y=0, which placed the bbox INSIDE the grey benchmark band — making
the text unreadable.

New behaviour (v0.8.1): when bar_high < bench_low, the annotation goes
ABOVE the bar centred on its midpoint, with y > 0.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402

from pvbess_opt.plotting.lifecycle import plot_lcoe_lcos_summary


def _econ() -> dict:
    return {
        "sensitivity_capex_delta_pct": 10.0,
        "sensitivity_opex_delta_pct": 10.0,
    }


def _collect_annotations() -> list:
    """Return all text annotations from the current figure's axes."""
    out = []
    for ax in plt.gcf().axes:
        for child in ax.texts:
            out.append(child)
    return out


def test_lcos_annotation_lifted_above_bar_when_left_of_benchmark(tmp_path: Path):
    """When LCOS lands far below the Lazard band the annotation must
    sit above the project bar (y > 0), not at y=0 inside the grey
    band."""
    plt.close("all")
    fin = {"lcoe_eur_per_mwh": 65.0, "lcos_eur_per_mwh": 46.0}
    caps = {"pv_kwp": 1000.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}

    # Patch plt.close inside the lifecycle module so we can inspect
    # the figure after the plot returns.
    import pvbess_opt.plotting.lifecycle as life_mod
    captured: dict = {}
    original_close = life_mod.plt.close

    def keep_open(fig=None):
        if fig is not None and hasattr(fig, "axes"):
            captured["fig"] = fig

    life_mod.plt.close = keep_open
    try:
        plot_lcoe_lcos_summary(
            fin, None, caps, _econ(), tmp_path / "summary.pdf",
        )
    finally:
        life_mod.plt.close = original_close
    fig = captured["fig"]
    # Two axes total (LCOE row, LCOS row).  LCOS row is axes[1].
    lcos_ax = fig.axes[1]
    # Find the summary annotation: the one containing "LCOS base ="
    matches = [t for t in lcos_ax.texts if "LCOS base" in t.get_text()]
    assert matches, "LCOS base annotation missing"
    summary = matches[0]
    xy_y = float(summary.xy[1])
    # New contract: y > 0 (above the project bar centreline at y=0).
    assert xy_y > 0.0, (
        f"LCOS annotation must sit above the bar when the project "
        f"undershoots the Lazard band; got xy_y={xy_y}"
    )


def test_lcoe_annotation_stays_right_when_overlapping_band(tmp_path: Path):
    """When the LCOE bar overlaps the Lazard band the historical
    right-of-bar annotation placement still applies."""
    plt.close("all")
    fin = {"lcoe_eur_per_mwh": 60.0, "lcos_eur_per_mwh": 200.0}
    caps = {"pv_kwp": 1000.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}

    import pvbess_opt.plotting.lifecycle as life_mod
    captured: dict = {}
    original_close = life_mod.plt.close

    def keep_open(fig=None):
        if fig is not None and hasattr(fig, "axes"):
            captured["fig"] = fig

    life_mod.plt.close = keep_open
    try:
        plot_lcoe_lcos_summary(
            fin, None, caps, _econ(), tmp_path / "summary.pdf",
        )
    finally:
        life_mod.plt.close = original_close

    fig = captured["fig"]
    lcoe_ax = fig.axes[0]
    matches = [t for t in lcoe_ax.texts if "LCOE base" in t.get_text()]
    assert matches
    summary = matches[0]
    # Overlapping case: annotation anchored at y=0 (right-of-bar centre).
    assert float(summary.xy[1]) == 0.0
