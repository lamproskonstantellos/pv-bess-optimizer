"""LCOE / LCOS benchmark-comparison redesign tests (Phase 6)."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402

from pvbess_opt.plotting.lifecycle import (  # noqa: E402
    BENCHMARK_LCOE_PV_UTILITY_EUR_PER_MWH,
    BENCHMARK_LCOS_LITHIUM_ION_EUR_PER_MWH,
    plot_lcoe_lcos_summary,
)


def _econ() -> dict:
    return {
        "sensitivity_capex_delta_pct": 10.0,
        "sensitivity_opex_delta_pct": 10.0,
    }


# ---------------------------------------------------------------------------
# Hybrid: both rows render
# ---------------------------------------------------------------------------


def test_hybrid_renders_two_rows(tmp_path: Path):
    fin = {
        "lcoe_eur_per_mwh": 45.0,
        "lcos_eur_per_mwh": 180.0,
    }
    caps = {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 2000.0}
    out = tmp_path / "summary_hybrid.pdf"
    plot_lcoe_lcos_summary(fin, None, caps, _econ(), out)
    assert out.exists()
    assert out.stat().st_size > 0


# ---------------------------------------------------------------------------
# PV-only / BESS-only — italic N/A line for the missing row
# ---------------------------------------------------------------------------


def test_pv_only_shows_na_for_lcos(tmp_path: Path):
    fin = {
        "lcoe_eur_per_mwh": 60.0,
        "lcos_eur_per_mwh": float("nan"),
    }
    caps = {"pv_kwp": 1000.0, "bess_kw": 0.0, "bess_kwh": 0.0}
    out = tmp_path / "summary_pv_only.pdf"
    plot_lcoe_lcos_summary(fin, None, caps, _econ(), out)
    assert out.exists()


def test_bess_only_shows_na_for_lcoe(tmp_path: Path):
    fin = {
        "lcoe_eur_per_mwh": float("nan"),
        "lcos_eur_per_mwh": 200.0,
    }
    caps = {"pv_kwp": 0.0, "bess_kw": 500.0, "bess_kwh": 2000.0}
    out = tmp_path / "summary_bess_only.pdf"
    plot_lcoe_lcos_summary(fin, None, caps, _econ(), out)
    assert out.exists()


# ---------------------------------------------------------------------------
# Benchmark constants exposed at the documented values
# ---------------------------------------------------------------------------


def test_benchmark_constants_exposed():
    # v0.8.1: bands tightened to the Lazard 2024 EUR-equivalent range
    # (LCOE+ v17 utility-scale PV; LCOS v9 4-hour Li-ion utility-scale).
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
# Round 3: every numeric value is reported in the legend
# ---------------------------------------------------------------------------


def _render_lcoe_lcos(tmp_path: Path, fin: dict, caps: dict):
    """Render plot_lcoe_lcos_summary and return the live figure."""
    plt.close("all")
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
    return captured["fig"]


def test_legend_carries_lazard_band_with_numeric_range(tmp_path: Path):
    """Round 3: the Lazard band's numeric range lives in the legend,
    not in a free-floating italic caption beneath the bar."""
    fin = {"lcoe_eur_per_mwh": 45.0, "lcos_eur_per_mwh": 200.0}
    caps = {"pv_kwp": 1000.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    fig = _render_lcoe_lcos(tmp_path, fin, caps)
    for row_idx, ax in enumerate(fig.axes):
        legend = ax.get_legend()
        assert legend is not None, f"row {row_idx}: missing legend"
        labels = [t.get_text() for t in legend.get_texts()]
        assert any("Lazard" in lab and "EUR/MWh" in lab for lab in labels), (
            f"row {row_idx}: legend missing Lazard band entry; got {labels}"
        )


def test_legend_carries_base_value(tmp_path: Path):
    """Round 3: the base LCOE / LCOS value is reported in the legend."""
    fin = {"lcoe_eur_per_mwh": 45.0, "lcos_eur_per_mwh": 200.0}
    caps = {"pv_kwp": 1000.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    fig = _render_lcoe_lcos(tmp_path, fin, caps)
    for row_idx, (ax, want) in enumerate(zip(fig.axes, ("LCOE", "LCOS"))):
        legend = ax.get_legend()
        labels = [t.get_text() for t in legend.get_texts()]
        assert any(
            f"Base {want}" in lab and "EUR/MWh" in lab for lab in labels
        ), f"row {row_idx}: legend missing 'Base {want}' entry; got {labels}"


def test_no_diamond_marker_drawn(tmp_path: Path):
    """Round 3: the base value is drawn as a vertical line, not a
    diamond marker — no scatter artist sits on the row centreline."""
    fin = {"lcoe_eur_per_mwh": 45.0, "lcos_eur_per_mwh": 200.0}
    caps = {"pv_kwp": 1000.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    fig = _render_lcoe_lcos(tmp_path, fin, caps)
    for ax in fig.axes:
        for coll in ax.collections:
            assert coll.__class__.__name__ != "PathCollection" or (
                len(coll.get_offsets()) == 0
            ), "Round-3 redesign forbids scatter / diamond markers"
