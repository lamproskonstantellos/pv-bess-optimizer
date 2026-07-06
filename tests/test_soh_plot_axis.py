"""SOH trajectory plot: fixed 0..100 percentage axis with headroom."""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
import pytest

from pvbess_opt.plotting.degradation import plot_soh_trajectory


def _frame(replacement_year: int) -> pd.DataFrame:
    rows = []
    soh = 100.0
    for y in range(1, 16):
        if replacement_year and y == replacement_year:
            soh = 100.0
        rows.append({
            "project_year": y,
            "calendar_year": 2025 + y,
            "soh_pct": soh,
            "capacity_fade_pct": 100.0 - soh,
            "replacement": bool(replacement_year and y == replacement_year),
        })
        soh -= 2.5
    return pd.DataFrame(rows)


@pytest.mark.parametrize("replacement_year", [10, 0])
def test_soh_axis_fixed_zero_to_hundred_with_headroom(
    tmp_path, replacement_year,
):
    """y-limits are exactly (0, 105) and ticks run 0..100 step 10, for
    both a scheduled-replacement frame and a no-replacement frame."""
    out = tmp_path / f"soh_{replacement_year}.pdf"
    plt.close("all")
    plot_soh_trajectory(_frame(replacement_year), out)
    assert out.exists()
    # save_figure closes the figure; re-render to inspect the axes state.
    plt.close("all")
    fig, ax = plt.subplots()
    try:
        import pvbess_opt.plotting.degradation as deg_mod

        # Reproduce the exact axis-styling calls on a live axes object.
        frame = _frame(replacement_year)
        ax.plot(frame["calendar_year"], frame["soh_pct"])
        deg_mod.apply_universal_margins(ax, skip_y=True)
        ax.set_ylim(*deg_mod._SOH_YLIM)
        ax.set_yticks(deg_mod._SOH_YTICKS)
        assert ax.get_ylim() == (0.0, 105.0)
        assert list(ax.get_yticks()) == [float(v) for v in range(0, 101, 10)]
    finally:
        plt.close(fig)


@pytest.mark.parametrize("replacement_year", [10, 0])
def test_soh_plot_axes_state_before_save(monkeypatch, tmp_path, replacement_year):
    """Capture the real axes at save time: fixed limits and ticks, and
    the shared project-window year axis (every year labelled)."""
    captured: dict = {}

    import pvbess_opt.plotting.degradation as deg_mod

    real_save = deg_mod.save_figure

    def _spy(out_path):
        ax = plt.gcf().axes[0]
        captured["ylim"] = ax.get_ylim()
        captured["yticks"] = list(ax.get_yticks())
        captured["xticks"] = list(ax.get_xticks())
        captured["xminor"] = list(ax.get_xticks(minor=True))
        captured["xlim"] = ax.get_xlim()
        return real_save(out_path)

    monkeypatch.setattr(deg_mod, "save_figure", _spy)
    out = tmp_path / "soh.pdf"
    plot_soh_trajectory(_frame(replacement_year), out)
    assert captured["ylim"] == (0.0, 105.0)
    assert captured["yticks"] == [float(v) for v in range(0, 101, 10)]
    # Every project year is labelled (2026..2040 for this frame), no
    # minor ticks (labelled-ticks-only convention).
    assert captured["xticks"], "no major x ticks captured"
    assert captured["xticks"] == [float(t) for t in range(2026, 2041)]
    assert not captured["xminor"], captured["xminor"]
    # Operational frame (starts at Year 1): a line plot spans the year
    # axis edge-to-edge — the window opens exactly at the first year and
    # closes exactly at the final year (no empty half-slots).
    assert captured["xlim"] == (2026.0, 2040.0)
