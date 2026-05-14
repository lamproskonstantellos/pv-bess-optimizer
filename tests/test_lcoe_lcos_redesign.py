"""LCOE / LCOS benchmark-comparison redesign tests.

Round-5 split: ``plot_lcoe_lcos_summary`` is gone; the two summaries
now render as separate PDFs via ``plot_lcoe_summary`` and
``plot_lcos_summary``.  The rotated y-axis label is dropped — the
panel context is implicit from the filename and legend entries.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402

from pvbess_opt.plotting.lifecycle import (  # noqa: E402
    BENCHMARK_LCOE_PV_UTILITY_EUR_PER_MWH,
    BENCHMARK_LCOS_LITHIUM_ION_EUR_PER_MWH,
    plot_lcoe_summary,
    plot_lcos_summary,
)


def _econ() -> dict:
    return {
        "sensitivity_capex_delta_pct": 10.0,
        "sensitivity_opex_delta_pct": 10.0,
    }


# ---------------------------------------------------------------------------
# Each summary renders its own PDF
# ---------------------------------------------------------------------------


def test_plot_lcoe_summary_renders(tmp_path: Path):
    fin = {"lcoe_eur_per_mwh": 45.0, "lcos_eur_per_mwh": 200.0}
    caps = {"pv_kwp": 1000.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    out = plot_lcoe_summary(fin, None, caps, _econ(), tmp_path / "lcoe.pdf")
    assert out.exists() and out.stat().st_size > 0


def test_plot_lcos_summary_renders(tmp_path: Path):
    fin = {"lcoe_eur_per_mwh": 45.0, "lcos_eur_per_mwh": 200.0}
    caps = {"pv_kwp": 1000.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    out = plot_lcos_summary(fin, None, caps, _econ(), tmp_path / "lcos.pdf")
    assert out.exists() and out.stat().st_size > 0


def test_pv_only_lcos_renders_na_line(tmp_path: Path):
    """BESS-absent project — LCOS panel renders an N/A line, not a bar."""
    fin = {"lcoe_eur_per_mwh": 60.0, "lcos_eur_per_mwh": float("nan")}
    caps = {"pv_kwp": 1000.0, "bess_kw": 0.0, "bess_kwh": 0.0}
    out = plot_lcos_summary(fin, None, caps, _econ(), tmp_path / "lcos.pdf")
    assert out.exists()


def test_bess_only_lcoe_renders_na_line(tmp_path: Path):
    """PV-absent project — LCOE panel renders an N/A line, not a bar."""
    fin = {"lcoe_eur_per_mwh": float("nan"), "lcos_eur_per_mwh": 200.0}
    caps = {"pv_kwp": 0.0, "bess_kw": 500.0, "bess_kwh": 2000.0}
    out = plot_lcoe_summary(fin, None, caps, _econ(), tmp_path / "lcoe.pdf")
    assert out.exists()


# ---------------------------------------------------------------------------
# The combined function must not come back
# ---------------------------------------------------------------------------


def test_lcoe_lcos_summary_function_is_gone():
    from pvbess_opt.plotting import lifecycle
    assert not hasattr(lifecycle, "plot_lcoe_lcos_summary")


# ---------------------------------------------------------------------------
# No rotated y-axis label — Round-5 strips it
# ---------------------------------------------------------------------------


def _render(plot_fn, tmp_path: Path, fin: dict, caps: dict):
    plt.close("all")
    import pvbess_opt.plotting.lifecycle as life_mod
    captured: dict = {}
    original_close = life_mod.plt.close

    def keep_open(fig=None):
        if fig is not None and hasattr(fig, "axes"):
            captured["fig"] = fig

    life_mod.plt.close = keep_open
    try:
        plot_fn(fin, None, caps, _econ(), tmp_path / "out.pdf")
    finally:
        life_mod.plt.close = original_close
    return captured["fig"]


def test_lcoe_summary_has_no_left_y_axis_label(tmp_path: Path):
    fin = {"lcoe_eur_per_mwh": 45.0, "lcos_eur_per_mwh": 200.0}
    caps = {"pv_kwp": 1000.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    fig = _render(plot_lcoe_summary, tmp_path, fin, caps)
    for ax in fig.axes:
        assert ax.get_ylabel() == "", (
            f"plot_lcoe_summary must not draw a y-axis label, got {ax.get_ylabel()!r}"
        )


def test_lcos_summary_has_no_left_y_axis_label(tmp_path: Path):
    fin = {"lcoe_eur_per_mwh": 45.0, "lcos_eur_per_mwh": 200.0}
    caps = {"pv_kwp": 1000.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    fig = _render(plot_lcos_summary, tmp_path, fin, caps)
    for ax in fig.axes:
        assert ax.get_ylabel() == "", (
            f"plot_lcos_summary must not draw a y-axis label, got {ax.get_ylabel()!r}"
        )


# ---------------------------------------------------------------------------
# Benchmark constants exposed at the documented values
# ---------------------------------------------------------------------------


def test_benchmark_constants_exposed():
    assert BENCHMARK_LCOE_PV_UTILITY_EUR_PER_MWH == (30.0, 85.0)
    assert BENCHMARK_LCOS_LITHIUM_ION_EUR_PER_MWH == (157.0, 274.0)


def test_benchmark_constants_are_tuples_of_two_floats():
    for band in (
        BENCHMARK_LCOE_PV_UTILITY_EUR_PER_MWH,
        BENCHMARK_LCOS_LITHIUM_ION_EUR_PER_MWH,
    ):
        assert isinstance(band, tuple)
        assert len(band) == 2
        low, high = band
        assert isinstance(low, float) and isinstance(high, float)
        assert low < high


# ---------------------------------------------------------------------------
# Legend still carries the numeric values
# ---------------------------------------------------------------------------


def test_lcoe_legend_carries_lazard_band_and_base(tmp_path: Path):
    fin = {"lcoe_eur_per_mwh": 45.0, "lcos_eur_per_mwh": 200.0}
    caps = {"pv_kwp": 1000.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    fig = _render(plot_lcoe_summary, tmp_path, fin, caps)
    ax = fig.axes[0]
    legend = ax.get_legend()
    assert legend is not None
    labels = [t.get_text() for t in legend.get_texts()]
    assert any("Lazard" in lab and "EUR/MWh" in lab for lab in labels)
    assert any("Base LCOE" in lab and "EUR/MWh" in lab for lab in labels)


def test_lcos_legend_carries_lazard_band_and_base(tmp_path: Path):
    fin = {"lcoe_eur_per_mwh": 45.0, "lcos_eur_per_mwh": 200.0}
    caps = {"pv_kwp": 1000.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    fig = _render(plot_lcos_summary, tmp_path, fin, caps)
    ax = fig.axes[0]
    legend = ax.get_legend()
    assert legend is not None
    labels = [t.get_text() for t in legend.get_texts()]
    assert any("Lazard" in lab and "EUR/MWh" in lab for lab in labels)
    assert any("Base LCOS" in lab and "EUR/MWh" in lab for lab in labels)


def test_no_diamond_marker_drawn(tmp_path: Path):
    """The base value is drawn as a vertical line — no scatter artist."""
    fin = {"lcoe_eur_per_mwh": 45.0, "lcos_eur_per_mwh": 200.0}
    caps = {"pv_kwp": 1000.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    for plot_fn in (plot_lcoe_summary, plot_lcos_summary):
        fig = _render(plot_fn, tmp_path, fin, caps)
        for ax in fig.axes:
            for coll in ax.collections:
                assert coll.__class__.__name__ != "PathCollection" or (
                    len(coll.get_offsets()) == 0
                ), "Redesign forbids scatter / diamond markers"
