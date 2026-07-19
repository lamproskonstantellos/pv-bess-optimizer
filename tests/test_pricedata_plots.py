"""Price-scenario figures — fan chart and capture KPIs.

Rendering smoke, placeholder gating and the theme registrations for
``plot_price_path_fan`` / ``plot_capture_kpis``.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from pvbess_opt.plotting import plot_capture_kpis, plot_price_path_fan
from pvbess_opt.theme import (
    FINANCIAL_COLORS,
    FINANCIAL_LABELS,
    FINANCIAL_LEGEND_ORDER,
    SCENARIO_PATH_COLORS,
    financial_color,
    scenario_path_color,
)


def _paths(n_years: int = 10, level: float = 80.0) -> pd.DataFrame:
    years = np.arange(1, n_years + 1)
    mean = level * 0.99 ** (years - 1)
    return pd.DataFrame({
        "project_year": years,
        "dam_mean_price_eur_per_mwh": mean,
        "pv_capture_price_eur_per_mwh": mean * (1.0 - 0.02 * (years - 1)),
        "bess_realized_spread_eur_per_mwh": 40.0 + 0.5 * (years - 1),
    })


def test_theme_registrations():
    assert FINANCIAL_COLORS["pv_capture_price_line"] == "#F9A825"
    assert FINANCIAL_COLORS["dam_baseload_line"] == "#546E7A"
    assert FINANCIAL_COLORS["bess_spread_line"] == "#7B1FA2"
    for label in (
        "PV capture price", "DAM baseload price", "Realized BESS spread",
    ):
        assert label in FINANCIAL_LABELS
        assert label in FINANCIAL_LEGEND_ORDER
        assert financial_color(label).startswith("#")
    # The multi-scenario palette extends past four entries and cycles.
    assert len(SCENARIO_PATH_COLORS) >= 8
    assert scenario_path_color(0) == SCENARIO_PATH_COLORS[0]
    assert scenario_path_color(len(SCENARIO_PATH_COLORS)) == (
        SCENARIO_PATH_COLORS[0]
    )


def test_fan_chart_renders_beyond_four_scenarios(tmp_path):
    paths_by_scenario = {
        f"Scenario {i}": _paths(level=60.0 + 5.0 * i) for i in range(6)
    }
    out = plot_price_path_fan(paths_by_scenario, tmp_path / "fan.pdf")
    assert out.exists() and out.stat().st_size > 0


def test_fan_chart_gates_on_empty_input(tmp_path):
    out = plot_price_path_fan({}, tmp_path / "fan_empty.pdf")
    assert out.exists()  # the empty_placeholder file


def test_capture_figure_renders_with_canonical_labels(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.theme"):
        out = plot_capture_kpis(_paths(), tmp_path / "capture.pdf")
    assert out.exists() and out.stat().st_size > 0
    assert not [
        r for r in caplog.records
        if "Non-canonical" in r.message
    ]


def test_capture_figure_skips_all_nan_spread(tmp_path):
    paths = _paths()
    paths["bess_realized_spread_eur_per_mwh"] = float("nan")
    out = plot_capture_kpis(paths, tmp_path / "capture_no_bess.pdf")
    assert out.exists() and out.stat().st_size > 0


def test_capture_figure_gates_on_missing_columns(tmp_path):
    out = plot_capture_kpis(
        pd.DataFrame({"project_year": [1]}), tmp_path / "gate.pdf",
    )
    assert out.exists()
