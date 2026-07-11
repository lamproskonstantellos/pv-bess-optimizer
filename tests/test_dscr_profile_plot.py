"""DSCR-profile figure (Eqs. E20/E41-E44).

The figure exists only for levered runs: an all-equity run produces no
debt schedule and therefore no file, so default output directories
stay bit-identical with the TRUE `plot_dscr_profile` default.  Locked:
the None gates, the canonical labels / colours / legend order (theme
registries), the target line rendered as a plotted series (never axes
text), the P90 companion line, and the calendar-year axis contract.
"""

from __future__ import annotations

import logging

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import build_debt_schedule, build_yearly_cashflow
from pvbess_opt.lender import apply_production_case
from pvbess_opt.plotting import plot_dscr_profile
from pvbess_opt.theme import (
    FINANCIAL_LABEL_TO_COLOR_KEY,
    FINANCIAL_LABELS,
    FINANCIAL_LEGEND_ORDER,
    financial_color,
)

N_YEARS = 8


def _econ(**o) -> dict:
    econ = {
        "project_lifecycle_years": N_YEARS,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 400.0,
        "capex_bess_eur_per_kwh": 100.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 5.0,
        "opex_bess_eur_per_kw": 5.0,
        "pv_degradation_year1_pct": 2.0,
        "pv_degradation_annual_pct": 1.0,
        "bess_degradation_annual_pct": 1.5,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
        "gearing_pct": 60.0,
        "debt_interest_rate_pct": 5.0,
        "debt_tenor_years": 6,
        "debt_repayment": "annuity",
    }
    econ.update(o)
    return econ


def _kpis() -> dict:
    return {
        "profit_load_from_pv_eur": 60_000.0,
        "profit_load_from_bess_eur": 10_000.0,
        "profit_export_from_pv_eur": 40_000.0,
        "profit_export_from_bess_eur": 40_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 145_000.0,
    }


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}


def _schedules(**eo):
    econ = _econ(**eo)
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    base = build_debt_schedule(cf, econ)
    p90 = build_debt_schedule(apply_production_case(cf, 0.9), econ)
    return base, p90, econ


# ---------------------------------------------------------------------------
# Theme registrations
# ---------------------------------------------------------------------------


def test_dscr_labels_are_canonical():
    for label in ("DSCR base case", "DSCR P90 case", "Target DSCR"):
        assert label in FINANCIAL_LABELS
        assert label in FINANCIAL_LEGEND_ORDER
        assert label in FINANCIAL_LABEL_TO_COLOR_KEY
        assert financial_color(label).startswith("#")
    # Three mutually distinct hexes (the registry uniqueness test
    # covers the whole palette; this pins the trio directly).
    hexes = {
        financial_color(label)
        for label in ("DSCR base case", "DSCR P90 case", "Target DSCR")
    }
    assert len(hexes) == 3


# ---------------------------------------------------------------------------
# None gates (all-equity bit-identity)
# ---------------------------------------------------------------------------


def test_returns_none_without_schedule(tmp_path):
    out = tmp_path / "dscr.pdf"
    assert plot_dscr_profile(None, out) is None
    assert plot_dscr_profile(pd.DataFrame(), out) is None
    assert not out.exists()


def test_all_equity_run_emits_no_schedule(tmp_path):
    econ = _econ(gearing_pct=0.0)
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    assert build_debt_schedule(cf, econ) is None


# ---------------------------------------------------------------------------
# Rendered figure
# ---------------------------------------------------------------------------


def _render_open(tmp_path, **kwargs):
    """Render but capture the open figure (save_figure closes it)."""
    import pvbess_opt.plotting.financial as fin_mod

    base, p90, econ = _schedules()
    plt.close("all")
    captured = {}
    original = fin_mod.save_figure

    def keep_open(out):
        captured["fig"] = plt.gcf()
        return out

    fin_mod.save_figure = keep_open
    try:
        plot_dscr_profile(
            base, tmp_path / "dscr.pdf", econ=econ,
            **{"p90_schedule": p90, "target_dscr": 1.3, **kwargs},
        )
    finally:
        fin_mod.save_figure = original
    return captured["fig"]


def test_figure_written_for_levered_run(tmp_path):
    base, p90, econ = _schedules()
    out = plot_dscr_profile(
        base, tmp_path / "dscr_profile.pdf",
        p90_schedule=p90, target_dscr=1.3, econ=econ,
    )
    assert out is not None and out.exists()


def test_legend_labels_and_target_series(tmp_path):
    fig = _render_open(tmp_path)
    ax = fig.axes[0]
    _, labels = ax.get_legend_handles_labels()
    assert labels == ["DSCR base case", "DSCR P90 case", "Target DSCR"]
    # The target is a plotted dashed series, never an axes text (the
    # no-computed-values-in-axes house rule).
    assert len(ax.texts) == 0
    target_lines = [
        ln for ln in ax.get_lines() if ln.get_label() == "Target DSCR"
    ]
    assert len(target_lines) == 1
    assert target_lines[0].get_linestyle() == "--"
    assert np.allclose(target_lines[0].get_ydata(), 1.3)
    plt.close(fig)


def test_p90_line_sits_below_base(tmp_path):
    fig = _render_open(tmp_path)
    ax = fig.axes[0]
    by_label = {ln.get_label(): ln for ln in ax.get_lines()}
    base_y = np.asarray(by_label["DSCR base case"].get_ydata(), dtype=float)
    p90_y = np.asarray(by_label["DSCR P90 case"].get_ydata(), dtype=float)
    assert p90_y.shape == base_y.shape
    assert np.all(p90_y <= base_y + 1e-12)
    plt.close(fig)


def test_calendar_axis_spans_tenor_edge_to_edge(tmp_path):
    fig = _render_open(tmp_path)
    ax = fig.axes[0]
    # Operating years 1..6 map to calendar 2026..2031; line figures
    # hug the data edge to edge.
    assert ax.get_xlim() == (2026.0, 2031.0)
    ticks = list(ax.get_xticks())
    assert ticks == [float(y) for y in range(2026, 2032)]
    plt.close(fig)


def test_optional_series_are_optional(tmp_path):
    base, _p90, econ = _schedules()
    fig_path = tmp_path / "base_only.pdf"
    out = plot_dscr_profile(base, fig_path, econ=econ)
    assert out is not None and fig_path.exists()


def test_no_non_canonical_label_warnings(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.theme"):
        fig = _render_open(tmp_path)
        plt.close(fig)
    assert not [
        r for r in caplog.records
        if "Non-canonical financial legend label" in r.getMessage()
    ]


def test_pipeline_gate_key_defaults_true():
    from pvbess_opt.io import _BOOL_KEYS, ECONOMICS_SHEET_DEFAULTS

    assert ECONOMICS_SHEET_DEFAULTS["plot_dscr_profile"] is True
    assert "plot_dscr_profile" in _BOOL_KEYS


def test_target_line_skipped_when_nan(tmp_path):
    fig = _render_open(tmp_path, p90_schedule=None,
                       target_dscr=float("nan"))
    ax = fig.axes[0]
    _, labels = ax.get_legend_handles_labels()
    assert labels == ["DSCR base case"]
    plt.close(fig)


def test_dscr_values_match_schedule(tmp_path):
    base, _p90, _econ_kw = _schedules()
    fig = _render_open(tmp_path)
    ax = fig.axes[0]
    by_label = {ln.get_label(): ln for ln in ax.get_lines()}
    plotted = np.asarray(
        by_label["DSCR base case"].get_ydata(), dtype=float,
    )
    expected = base["dscr"].to_numpy(dtype=float)
    assert plotted == pytest.approx(expected[np.isfinite(expected)])
    plt.close(fig)
