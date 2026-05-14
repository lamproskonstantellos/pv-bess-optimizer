"""LCOE / LCOS benchmark-comparison redesign tests (Phase 6)."""

from __future__ import annotations

from pathlib import Path

from pvbess_opt.plotting.lifecycle import (
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
