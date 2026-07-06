"""Cross-start-date axis-contract sweep.

Renders every per-year figure and every categorical monthly figure for
a grid of project start years and horizon lengths, asserting the house
axis contracts hold for ANY project window — not just the shipped
2026-2045 example:

* every project year is labelled, rotated ``XTICK_ROT`` and
  right-anchored like the month and date axes;
* the window hugs the data: cashflow views open at Year 0, operational
  views (SOH, revenue stack, cycles, lifetime summary) open at Year 1
  with no empty Year-0 slot;
* no tick is displayed outside the project window (no phantom years);
* the categorical monthly figures carry the house MM-YYYY labels
  (rotated ``XTICK_ROT``, right-anchored) for the project's Year-1
  calendar year, on a snug symmetric window;
* every drawn legend sits measurably clear of the data artists.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pytest

from pvbess_opt.plotting import bess_revenue as bess_mod
from pvbess_opt.plotting import degradation as deg_mod
from pvbess_opt.plotting import financial as fin_mod
from pvbess_opt.plotting import inputs_uncertainty as unc_mod
from pvbess_opt.plotting import lifecycle as life_mod
from pvbess_opt.plotting import yearly as yearly_mod
from pvbess_opt.plotting.style import legend_overlaps_data
from pvbess_opt.theme import XTICK_ROT

STARTS = [2024, 2027, 2035]
N_YEARS = [3, 8, 20, 25]
SWEEP = [(s, n) for s in STARTS for n in N_YEARS]


# ---------------------------------------------------------------------------
# Parametric frame factories (Year 0 at start_year - 1, Year 1..N operating)
# ---------------------------------------------------------------------------


def _yearly_cf(start_year: int, n_years: int) -> pd.DataFrame:
    rows = [{
        "project_year": 0, "calendar_year": start_year - 1,
        "revenue_eur": 0.0, "revenue_retail_eur": 0.0,
        "revenue_dam_eur": 0.0, "aggregator_fee_eur": 0.0,
        "balancing_capacity_revenue_eur": 0.0,
        "balancing_activation_revenue_eur": 0.0,
        "balancing_revenue_eur": 0.0,
        "opex_eur": 0.0, "devex_eur": -75_000.0, "capex_eur": -600_000.0,
        "discount_factor": 1.0,
        "discounted_cf_eur": -675_000.0, "net_cashflow_eur": -675_000.0,
    }]
    r = 0.07
    for y in range(1, n_years + 1):
        df_y = 1 / (1 + r) ** y
        rev_y = 150_000.0 * (1.0 + 0.01 * (y - 1))
        net = rev_y - 14_000.0 + 6_500.0
        rows.append({
            "project_year": y, "calendar_year": start_year - 1 + y,
            "revenue_eur": rev_y,
            "revenue_retail_eur": rev_y * 0.6,
            "revenue_dam_eur": rev_y * 0.4,
            "aggregator_fee_eur": -rev_y * 0.02,
            "balancing_capacity_revenue_eur": 5_000.0,
            "balancing_activation_revenue_eur": 1_500.0,
            "balancing_revenue_eur": 6_500.0,
            "opex_eur": -14_000.0, "devex_eur": 0.0, "capex_eur": 0.0,
            "discount_factor": df_y,
            "discounted_cf_eur": net * df_y, "net_cashflow_eur": net,
        })
    df = pd.DataFrame(rows)
    df["cumulative_cf_eur"] = df["net_cashflow_eur"].cumsum()
    df["cumulative_dcf_eur"] = df["discounted_cf_eur"].cumsum()
    return df


def _year1_kpis() -> dict:
    return {
        "profit_load_from_pv_eur": 40_000.0,
        "profit_load_from_bess_eur": 25_000.0,
        "profit_export_from_pv_eur": 30_000.0,
        "profit_export_from_bess_eur": 22_000.0,
        "expense_charge_bess_grid_eur": 4_000.0,
        "revenue_bess_fcr_eur": 3_500.0,
        "revenue_bess_afrr_up_eur": 3_700.0,
        "revenue_bess_afrr_dn_eur": 2_700.0,
        "revenue_bess_mfrr_up_eur": 1_150.0,
        "revenue_bess_mfrr_dn_eur": 780.0,
        "revenue_bess_dam_eur": 18_000.0,
    }


def _soh_frame(start_year: int, n_years: int) -> pd.DataFrame:
    rows, soh = [], 100.0
    repl = n_years // 2 if n_years >= 6 else 0
    for y in range(1, n_years + 1):
        if repl and y == repl:
            soh = 100.0
        rows.append({
            "project_year": y, "calendar_year": start_year - 1 + y,
            "soh_pct": soh, "capacity_fade_pct": 100.0 - soh,
            "replacement": bool(repl and y == repl),
        })
        soh -= 2.0
    return pd.DataFrame(rows)


def _lifetime_yearly(start_year: int, n_years: int) -> pd.DataFrame:
    years = np.arange(1, n_years + 1)
    return pd.DataFrame({
        "project_year": years,
        "calendar_year": start_year - 1 + years,
        "bess_discharge_mwh": 20_000.0 * (1 - 0.005 * (years - 1)),
        "pv_generation_mwh": 22_500.0 * (1 - 0.004 * (years - 1)),
        "export_total_mwh": 21_000.0 * (1 - 0.004 * (years - 1)),
        "import_to_load_mwh": 2_400.0 + 10.0 * years,
    })


def _monthly_cf(start_year: int) -> pd.DataFrame:
    seas = 1.0 + 0.4 * np.sin(np.pi * (np.arange(12) - 2) / 6.0)
    rev = 20_000.0 * seas
    return pd.DataFrame({
        "project_year": [1] * 12,
        "calendar_year": [start_year] * 12,
        "period": list(range(1, 13)),
        "revenue_eur": rev,
        "opex_eur": [-2_000.0] * 12,
        "net_cashflow_eur": rev - 2_000.0,
    })


def _res_year1(start_year: int) -> pd.DataFrame:
    t = pd.date_range(f"{start_year}-01-01", periods=365, freq="D")
    rng = np.random.default_rng(11)
    return pd.DataFrame({
        "timestamp": t,
        "profit_export_from_bess_eur": rng.uniform(40.0, 90.0, 365),
        "expense_charge_bess_grid_eur": rng.uniform(0.0, 8.0, 365),
    })


def _input_ts(start_year: int) -> pd.DataFrame:
    t = pd.date_range(f"{start_year}-01-01", periods=365, freq="D")
    rng = np.random.default_rng(13)
    return pd.DataFrame({
        "timestamp": t,
        "dam_price_eur_per_mwh": 80.0 + rng.normal(0.0, 10.0, 365),
        "pv_kwh": np.maximum(rng.normal(1_200.0, 300.0, 365), 0.0),
    })


# ---------------------------------------------------------------------------
# Capture + assertion helpers
# ---------------------------------------------------------------------------


def _capture(module, fn):
    """Run ``fn`` with the module's ``save_figure`` spied so the figure
    stays open for inspection (the spy skips the real write)."""
    captured: dict = {}
    original = module.save_figure

    def _spy(out_path):
        captured["fig"] = plt.gcf()
        return Path(out_path)

    module.save_figure = _spy
    try:
        fn()
    finally:
        module.save_figure = original
    fig = captured.get("fig")
    assert fig is not None, "plot function never reached save_figure"
    return fig


def _assert_year_axis(
    ax, *, year0: int, last: int, first_data: int, pad: float,
) -> None:
    """House contract: line plots span edge-to-edge (pad 0.0); bar plots
    keep half a slot so the first/last bar bodies are not clipped."""
    ticks = list(ax.get_xticks())
    assert ticks == [float(t) for t in range(first_data, last + 1)], ticks
    assert ax.get_xlim() == (first_data - pad, last + pad)
    for t in ax.get_xticklabels():
        assert t.get_rotation() == pytest.approx(float(XTICK_ROT))
        assert t.get_horizontalalignment() == "right"
    lo, hi = ax.get_xlim()
    shown = [t for t in ticks if lo <= t <= hi]
    assert shown, "window displays no tick at all"
    assert all(year0 <= t <= last for t in shown), shown


def _assert_month_axis(ax, *, year: int, positions: np.ndarray) -> None:
    labels = [t.get_text() for t in ax.get_xticklabels()]
    assert labels == [f"{m:02d}-{year}" for m in range(1, 13)], labels
    for t in ax.get_xticklabels():
        assert t.get_rotation() == pytest.approx(float(XTICK_ROT))
        assert t.get_horizontalalignment() == "right"
    assert ax.get_xlim() == (positions.min() - 0.5, positions.max() + 0.5)


def _assert_legend_clear(ax) -> None:
    """House rule: the legend hangs BELOW the axes (and therefore
    cannot intersect any data artist)."""
    legend = ax.get_legend()
    if legend is None:
        return
    fig = ax.figure
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    lbox = legend.get_window_extent(renderer=renderer)
    abox = ax.get_window_extent(renderer=renderer)
    assert lbox.y1 <= abox.y0 + 1e-6, "legend must hang below the axes"
    # Fixed-canvas saves (no tight crop): the legend must sit fully
    # inside the declared figure, or the export would clip it.
    assert lbox.y0 >= -1e-6, "legend clipped off the canvas bottom"
    assert lbox.x0 >= -1e-6 and lbox.x1 <= fig.bbox.width + 1e-6
    assert legend_overlaps_data(ax, renderer=renderer) == []


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("start_year", "n_years"), SWEEP)
def test_year_axis_contract_all_plots(tmp_path, start_year, n_years):
    cf = _yearly_cf(start_year, n_years)
    year0, last = start_year - 1, start_year - 1 + n_years
    kpis = _year1_kpis()

    # pad 0.0 = line plot (edge-to-edge); pad 0.5 = bar plot (half slot).
    cashflow_cases = [
        (fin_mod, 0.0, lambda: fin_mod.plot_cumulative_cashflow(
            cf, tmp_path / "cum.pdf")),
        (fin_mod, 0.5, lambda: fin_mod.plot_yearly_cashflow_bars(
            cf, tmp_path / "bars.pdf")),
        (fin_mod, 0.5, lambda: fin_mod.plot_npv_waterfall(
            cf, tmp_path / "npv.pdf")),
        (fin_mod, 0.0, lambda: fin_mod.plot_payback(
            cf, tmp_path / "pb.pdf",
            simple_payback_years=min(6.4, n_years - 0.5),
            discounted_payback_years=float("nan"))),
    ]
    operational_cases = [
        (life_mod, 0.5, lambda: life_mod.plot_revenue_stack_yearly(
            cf, kpis, tmp_path / "stack.pdf", econ={})),
        (life_mod, 0.5, lambda: life_mod.plot_lifetime_cycles(
            _lifetime_yearly(start_year, n_years), 60_000.0,
            tmp_path / "cyc.pdf")),
        (yearly_mod, 0.0, lambda: yearly_mod.plot_lifetime_summary(
            _lifetime_yearly(start_year, n_years), tmp_path / "sum.pdf")),
        (deg_mod, 0.0, lambda: deg_mod.plot_soh_trajectory(
            _soh_frame(start_year, n_years), tmp_path / "soh.pdf")),
    ]

    for module, pad, fn in cashflow_cases:
        plt.close("all")
        fig = _capture(module, fn)
        ax = fig.axes[0]
        _assert_year_axis(ax, year0=year0, last=last, first_data=year0, pad=pad)
        _assert_legend_clear(ax)
        plt.close(fig)

    for module, pad, fn in operational_cases:
        plt.close("all")
        fig = _capture(module, fn)
        ax = fig.axes[0]
        _assert_year_axis(
            ax, year0=year0, last=last, first_data=year0 + 1, pad=pad,
        )
        _assert_legend_clear(ax)
        plt.close(fig)


@pytest.mark.parametrize("start_year", STARTS)
def test_month_axis_contract_all_plots(tmp_path, start_year):
    plt.close("all")
    fig = _capture(fin_mod, lambda: fin_mod.plot_monthly_cashflow_year1(
        _monthly_cf(start_year), tmp_path / "mcf.pdf"))
    ax = fig.axes[0]
    _assert_month_axis(ax, year=start_year, positions=np.arange(1, 13))
    _assert_legend_clear(ax)
    plt.close(fig)

    plt.close("all")
    fig = _capture(bess_mod, lambda: bess_mod.plot_bess_revenue_by_month(
        _res_year1(start_year), _year1_kpis(), tmp_path / "bym.pdf",
        econ={"aggregator_fee_pct_revenue": 10.0}))
    ax = fig.axes[0]
    _assert_month_axis(ax, year=start_year, positions=np.arange(12))
    _assert_legend_clear(ax)
    plt.close(fig)

    plt.close("all")
    fig = _capture(unc_mod, lambda: unc_mod.plot_input_seasonal_boxplot(
        _input_ts(start_year), tmp_path / "box.pdf"))
    # One single-panel figure per source; every figure carries the full
    # house month axis (the captured figure is the last source's).
    assert len(fig.axes) == 1
    _assert_month_axis(fig.axes[0], year=start_year, positions=np.arange(1, 13))
    plt.close(fig)
