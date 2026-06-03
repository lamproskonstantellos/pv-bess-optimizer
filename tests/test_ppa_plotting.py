"""Plotting + results surfacing for the PPA premium and zero feed-in.

Covers the config label/colour registration, the signed PPA-premium
segment on the yearly revenue stack (reconciling to the net line) and on
the Year-1 monthly cash-flow bars, and that the energy plots build under
zero feed-in (no export series).
"""

from __future__ import annotations

import tempfile
import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pytest
from matplotlib.colors import to_hex

import pvbess_opt.plotting.daily as daily_mod
import pvbess_opt.plotting.financial as fin_mod
import pvbess_opt.plotting.lifecycle as life_mod
from pvbess_opt.config import (
    FINANCIAL_COLORS,
    assert_financial_label_color_coverage,
    assert_unique_financial_colors,
    financial_color,
)
from pvbess_opt.economics import build_yearly_cashflow, derive_monthly_cashflow
from pvbess_opt.io import (
    BESS_SHEET_DEFAULTS,
    ECONOMICS_SHEET_DEFAULTS,
    PPA_SHEET_DEFAULTS,
    PROJECT_SHEET_DEFAULTS,
    PV_SHEET_DEFAULTS,
)
from pvbess_opt.kpis import add_economic_columns
from pvbess_opt.optimization import run_scenario


def _econ() -> dict:
    econ: dict = {}
    for d in (
        PROJECT_SHEET_DEFAULTS, PV_SHEET_DEFAULTS, BESS_SHEET_DEFAULTS,
        ECONOMICS_SHEET_DEFAULTS, PPA_SHEET_DEFAULTS,
    ):
        econ.update(d)
    econ["project_lifecycle_years"] = 6
    econ["project_start_year"] = 2026
    return econ


_CAP = {"pv_kwp": 6000.0, "bess_kw": 2000.0, "bess_kwh": 8000.0}


def _y1(ppa_pv: float, ppa_bess: float) -> dict:
    return {
        "profit_total_eur": 100000.0,
        "profit_load_from_pv_eur": 40000.0,
        "profit_load_from_bess_eur": 10000.0,
        "profit_export_from_pv_eur": 35000.0,
        "profit_export_from_bess_eur": 20000.0,
        "expense_charge_bess_grid_eur": 5000.0,
        "bm_total_capacity_revenue_eur": 0.0,
        "bm_total_activation_revenue_eur": 0.0,
        "bess_total_discharge_mwh": 1500.0,
        "pv_generation_mwh": 9000.0,
        "ppa_premium_pv_eur": ppa_pv,
        "ppa_premium_bess_eur": ppa_bess,
        "revenue_bess_fcr_eur": 0.0,
        "revenue_bess_afrr_up_eur": 0.0,
        "revenue_bess_afrr_dn_eur": 0.0,
        "revenue_bess_mfrr_up_eur": 0.0,
        "revenue_bess_mfrr_dn_eur": 0.0,
    }


def _synthetic_res(n: int = 8760) -> pd.DataFrame:
    ts = pd.date_range("2026-01-01", periods=n, freq="h")
    rng = np.random.default_rng(0)
    return pd.DataFrame({
        "timestamp": ts,
        "profit_load_from_pv_eur": rng.random(n),
        "profit_load_from_bess_eur": rng.random(n) * 0.3,
        "profit_export_from_pv_eur": rng.random(n) * 0.5,
        "profit_export_from_bess_eur": rng.random(n) * 0.4,
        "expense_charge_bess_grid_eur": rng.random(n) * 0.1,
        "pv_kwh": rng.random(n) * 100,
    })


# ---------------------------------------------------------------------------
# Config registration
# ---------------------------------------------------------------------------


def test_ppa_premium_label_color_registered():
    assert_financial_label_color_coverage()
    assert_unique_financial_colors()
    assert financial_color("PPA premium") == FINANCIAL_COLORS["ppa_premium"]


# ---------------------------------------------------------------------------
# Yearly revenue-stack signed PPA segment
# ---------------------------------------------------------------------------


def _render_stack(cf: pd.DataFrame, y1: dict):
    plt.close("all")
    captured: dict = {}
    original = life_mod.save_figure

    def keep_open(out):
        captured["fig"] = plt.gcf()
        return Path(out)

    life_mod.save_figure = keep_open
    try:
        life_mod.plot_revenue_stack_yearly(
            cf, y1, Path(tempfile.gettempdir()) / "stk.pdf", econ=_econ(),
        )
    finally:
        life_mod.save_figure = original
    return captured["fig"]


def _stack_sums_to_net(fig) -> bool:
    ax = fig.axes[0]
    sums: dict[int, float] = {}
    for patch in ax.patches:
        x = round(patch.get_x() + patch.get_width() / 2.0)
        sums[x] = sums.get(x, 0.0) + patch.get_height()
    target = financial_color("Net revenue").lower()
    net_line = None
    for line in ax.get_lines():
        c = line.get_color()
        c_hex = c.lower() if isinstance(c, str) else to_hex(c).lower()
        if c_hex == target and line.get_linestyle() == "-":
            net_line = line
            break
    assert net_line is not None
    for x, y in zip(net_line.get_xdata(), net_line.get_ydata(), strict=False):
        if abs(sums[round(float(x))] - float(y)) >= 1e-6:
            return False
    return True


def _legend_labels(fig) -> list[str]:
    leg = fig.axes[0].get_legend()
    return [t.get_text() for t in leg.get_texts()] if leg else []


@pytest.mark.parametrize("ppa_pv,ppa_bess", [(8000.0, 4000.0), (-6000.0, -2000.0)])
def test_revenue_stack_sums_to_net_with_ppa(ppa_pv, ppa_bess):
    cf = build_yearly_cashflow(_y1(ppa_pv, ppa_bess), _econ(), _CAP)
    fig = _render_stack(cf, _y1(ppa_pv, ppa_bess))
    assert _stack_sums_to_net(fig)
    assert "PPA premium" in _legend_labels(fig)


def test_revenue_stack_no_ppa_label_when_off():
    cf = build_yearly_cashflow(_y1(0.0, 0.0), _econ(), _CAP)
    fig = _render_stack(cf, _y1(0.0, 0.0))
    assert _stack_sums_to_net(fig)
    assert "PPA premium" not in _legend_labels(fig)


# ---------------------------------------------------------------------------
# Year-1 monthly cash-flow signed PPA segment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ppa_pv,ppa_bess", [(8000.0, 4000.0), (-6000.0, -2000.0), (0.0, 0.0)])
def test_monthly_cashflow_builds_with_ppa(ppa_pv, ppa_bess):
    cf = build_yearly_cashflow(_y1(ppa_pv, ppa_bess), _econ(), _CAP)
    mcf, _ = derive_monthly_cashflow(_synthetic_res(), cf, _econ())
    plt.close("all")
    captured: dict = {}
    original = fin_mod.save_figure

    def keep_open(out):
        captured["fig"] = plt.gcf()
        return Path(out)

    fin_mod.save_figure = keep_open
    try:
        # Must not raise.
        fin_mod.plot_monthly_cashflow_year1(
            mcf, Path(tempfile.gettempdir()) / "mcf.pdf", econ=_econ(),
        )
    finally:
        fin_mod.save_figure = original
    leg = captured["fig"].axes[0].get_legend()
    labels = [t.get_text() for t in leg.get_texts()] if leg else []
    if abs(ppa_pv) + abs(ppa_bess) > 0.0:
        assert "PPA premium" in labels
    else:
        assert "PPA premium" not in labels


# ---------------------------------------------------------------------------
# Zero feed-in energy plots build (no export series)
# ---------------------------------------------------------------------------


def test_zero_feed_in_energy_plots_build():
    t = pd.date_range("2026-06-01", periods=48, freq="h")
    h = np.arange(48) % 24
    pv = 8000.0 * np.where((h >= 6) & (h <= 18),
                           np.sin(np.pi * (h - 6) / 12.0), 0.0)
    ts = pd.DataFrame({
        "timestamp": t,
        "pv_kwh": np.maximum(pv, 0.0),
        "load_kwh": np.full(48, 1000.0),
        "dam_price_eur_per_mwh": 50.0 - 15.0 * np.sin(np.pi * (h - 6) / 12.0),
    })
    params = dict(
        dt_minutes=60, efficiency_charge=0.97, efficiency_discharge=0.97,
        soc_min_frac=0.20, soc_max_frac=0.95, initial_soc_frac=0.50,
        terminal_soc_equal=True, max_cycles_per_day=1.0,
        p_grid_export_max_kw=5000.0, retail_tariff_eur_per_mwh=120.0,
        settlement_minutes=15, mode="self_consumption",
        allow_bess_grid_charging=False, show_titles=False,
        pv_nameplate_kwp=6000.0, bess_power_kw=2000.0, bess_capacity_kwh=8000.0,
        zero_feed_in=True,
    )
    res, _ = run_scenario(params, ts, solver_name="highs", mip_gap=0.0)
    add_economic_columns(res, params)
    assert float((res["pv_to_grid_kwh"] + res["bess_dis_grid_kwh"]).sum()) == (
        pytest.approx(0.0, abs=1e-6)
    )
    out_dir = Path(tempfile.mkdtemp())
    date_str = str(pd.to_datetime(res["timestamp"].iloc[0]).date())
    plt.close("all")
    # No export series; the zero-series filtering must drop the empty
    # export bars and the plots must build without error.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        daily_mod.plot_daily_combined(res, date_str, out_dir)
        daily_mod.plot_daily_combined_with_soc(res, date_str, out_dir)
