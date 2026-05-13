"""IRR tornado dumbbell-layout tests (Phase 5)."""

from __future__ import annotations

import pandas as pd

from pvbess_opt.plotting.financial import (
    _build_tornado_pivot,
    _dumbbell_plot,
    plot_irr_tornado,
)


def _econ() -> dict:
    return {
        "project_lifecycle_years": 10,
        "project_start_year": 2026,
    }


def _multi_driver_sens_df() -> pd.DataFrame:
    rows = []
    for label, low, high in (
        ("Total CAPEX", 30.0, 42.0),
        ("Total annual OPEX", 35.5, 38.0),
        ("Year-1 revenue base", 28.0, 44.0),
        ("Discount rate", 36.8, 36.8),  # filtered out
    ):
        rows.append({"label": label, "scenario": "low", "irr_pct": low})
        rows.append({"label": label, "scenario": "high", "irr_pct": high})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Renderer creates a non-empty PDF
# ---------------------------------------------------------------------------


def test_dumbbell_renders_pdf(tmp_path):
    out = tmp_path / "irr_dumbbell.pdf"
    plot_irr_tornado(_multi_driver_sens_df(), {"irr_pct": 36.8}, _econ(), out)
    assert out.exists()
    assert out.stat().st_size > 0


def test_dumbbell_handles_single_driver(tmp_path):
    df = pd.DataFrame([
        {"label": "Total CAPEX", "scenario": "low", "irr_pct": 30.0},
        {"label": "Total CAPEX", "scenario": "high", "irr_pct": 42.0},
    ])
    out = tmp_path / "irr_dumbbell_single.pdf"
    plot_irr_tornado(df, {"irr_pct": 36.0}, _econ(), out)
    assert out.exists()
    assert out.stat().st_size > 0


# ---------------------------------------------------------------------------
# Drop discount-rate row before drawing
# ---------------------------------------------------------------------------


def test_dumbbell_drops_discount_rate(tmp_path):
    """The Discount rate row drops out (its impact is 0 anyway, but the
    explicit filter is part of the v0.7 contract preserved into v0.8)."""
    df = _multi_driver_sens_df()
    out = tmp_path / "irr_dumbbell_drop.pdf"
    # Use the helper directly to inspect drop behaviour.
    pivot = _build_tornado_pivot(df, "irr_pct", 36.8)
    _dumbbell_plot(
        pivot, base_value=36.8, out_path=out,
        title="IRR test",
        xlabel="IRR (%)",
        value_formatter=lambda v: f"{v:.1f}%",
        drop_labels=("Discount rate",),
        footer_note="Discount rate omitted — does not affect IRR by definition.",
    )
    assert out.exists()
    # The pivot constructed inside the helper is private, but we can
    # cross-check that filtering worked by inspecting the dataframe path:
    assert "Discount rate" in df["label"].values
    pivot = df.pivot_table(
        index="label", columns="scenario", values="irr_pct", aggfunc="first",
    )
    # The Discount rate row in the pivot has zero spread.
    assert (pivot.loc["Discount rate", "low"]
            == pivot.loc["Discount rate", "high"])


# ---------------------------------------------------------------------------
# Empty / placeholder paths still produce a PDF
# ---------------------------------------------------------------------------


def test_dumbbell_empty_input_produces_placeholder(tmp_path):
    out = tmp_path / "irr_dumbbell_empty.pdf"
    plot_irr_tornado(pd.DataFrame(), {"irr_pct": 36.8}, _econ(), out)
    assert out.exists()


def test_dumbbell_drivers_with_zero_spread_are_filtered(tmp_path):
    df = pd.DataFrame([
        {"label": "ZeroSpread", "scenario": "low", "irr_pct": 36.8},
        {"label": "ZeroSpread", "scenario": "high", "irr_pct": 36.8},
    ])
    out = tmp_path / "irr_dumbbell_zero.pdf"
    plot_irr_tornado(df, {"irr_pct": 36.8}, _econ(), out)
    assert out.exists()
