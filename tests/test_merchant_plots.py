"""Merchant-mode plot tests (Phase 6).

Each merchant-mode resolution (daily / monthly / yearly) gets a
new plot trio: dispatch, SOC, revenue.  The dispatcher in main.py
branches on ``params['mode']`` so vnb runs keep the existing
supply / surplus / combined trio while merchant runs use the new
one.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from pvbess_opt.plotting import (
    plot_daily_combined_merchant,
    plot_daily_dispatch,
    plot_daily_revenue,
    plot_daily_soc,
    plot_monthly_combined_merchant,
    plot_monthly_dispatch,
    plot_monthly_revenue,
    plot_monthly_soc,
    plot_yearly_combined_merchant,
    plot_yearly_dispatch,
    plot_yearly_revenue,
    plot_yearly_soc,
)


def _make_dispatch(n_days: int = 7) -> pd.DataFrame:
    n = n_days * 24
    timestamps = pd.date_range("2026-06-01 00:00", periods=n, freq="h")
    rng = np.random.default_rng(0)
    h = np.arange(n) % 24
    pv = 1000.0 * np.where((h >= 6) & (h <= 18),
                            np.sin(np.pi * (h - 6) / 12.0), 0.0)
    df = pd.DataFrame({
        "timestamp": timestamps,
        "load_kwh": np.zeros(n),
        "pv_kwh": pv,
        "pv_to_load_kwh": np.zeros(n),
        "pv_to_bess_kwh": np.maximum(pv * 0.2, 0.0),
        "bess_charge_grid_kwh": np.zeros(n),
        "bess_dis_load_kwh": np.zeros(n),
        "bess_dis_grid_kwh": np.maximum((1.0 - 0.5 * np.cos(np.pi * h / 12.0)) * 100.0, 0.0),
        "pv_to_grid_kwh": np.maximum(pv * 0.7, 0.0),
        "pv_curtail_kwh": np.maximum(pv * 0.05, 0.0),
        "grid_to_load_kwh": np.zeros(n),
        "grid_export_total_kwh": np.zeros(n),
        "grid_export_cap_kwh": np.full(n, 1500.0),
        "soc_kwh": 200.0 + 100.0 * np.sin(np.pi * h / 12.0),
        "soc_pct": 50.0 + 25.0 * np.sin(np.pi * h / 12.0),
        "dam_price_eur_per_mwh": 80.0 + 30.0 * np.sin(np.pi * h / 12.0),
        "profit_export_from_pv_eur": rng.uniform(0.0, 20.0, size=n),
        "profit_export_from_bess_eur": rng.uniform(0.0, 10.0, size=n),
        "expense_charge_bess_grid_eur": rng.uniform(0.0, 2.0, size=n),
    })
    return df


# ---------------------------------------------------------------------------
# Daily merchant trio
# ---------------------------------------------------------------------------


def test_plot_daily_dispatch_writes_pdf(tmp_path):
    df = _make_dispatch()
    plot_daily_dispatch(df, "2026-06-01", tmp_path)
    files = list(tmp_path.rglob("daily_dispatch_2026-06-01.pdf"))
    assert files


def test_plot_daily_soc_skips_when_no_bess(tmp_path):
    df = _make_dispatch()
    df["soc_kwh"] = 0.0  # no BESS
    plot_daily_soc(df, "2026-06-01", tmp_path)
    files = list(tmp_path.rglob("daily_soc_2026-06-01.pdf"))
    assert not files


def test_plot_daily_soc_renders_when_bess_present(tmp_path):
    df = _make_dispatch()
    plot_daily_soc(df, "2026-06-01", tmp_path)
    files = list(tmp_path.rglob("daily_soc_2026-06-01.pdf"))
    assert files


def test_plot_daily_revenue(tmp_path):
    df = _make_dispatch()
    plot_daily_revenue(df, "2026-06-01", tmp_path)
    files = list(tmp_path.rglob("daily_revenue_2026-06-01.pdf"))
    assert files


def test_plot_daily_combined_merchant_renders(tmp_path):
    df = _make_dispatch()
    plot_daily_combined_merchant(df, "2026-06-01", tmp_path)
    files = list(tmp_path.rglob("daily_combined_2026-06-01.pdf"))
    assert files


# ---------------------------------------------------------------------------
# Monthly merchant trio
# ---------------------------------------------------------------------------


def test_plot_monthly_dispatch(tmp_path):
    df = _make_dispatch(n_days=14)
    plot_monthly_dispatch(df, 6, tmp_path)
    assert (tmp_path / "monthly_dispatch_06.pdf").exists()


def test_plot_monthly_soc(tmp_path):
    df = _make_dispatch(n_days=14)
    plot_monthly_soc(df, 6, tmp_path)
    assert (tmp_path / "monthly_soc_06.pdf").exists()


def test_plot_monthly_revenue(tmp_path):
    df = _make_dispatch(n_days=14)
    plot_monthly_revenue(df, 6, tmp_path)
    assert (tmp_path / "monthly_revenue_06.pdf").exists()


def test_plot_monthly_combined_merchant_renders(tmp_path):
    df = _make_dispatch(n_days=14)
    plot_monthly_combined_merchant(df, 6, tmp_path)
    assert (tmp_path / "monthly_combined_06.pdf").exists()


# ---------------------------------------------------------------------------
# Yearly merchant trio
# ---------------------------------------------------------------------------


def _make_year(n_days: int = 60) -> pd.DataFrame:
    n = n_days * 24
    timestamps = pd.date_range("2026-01-01 00:00", periods=n, freq="h")
    df = _make_dispatch(n_days=n_days)
    df["timestamp"] = timestamps
    return df


def test_plot_yearly_dispatch(tmp_path):
    df = _make_year()
    plot_yearly_dispatch(df, 2026, tmp_path)
    assert (tmp_path / "yearly_dispatch.pdf").exists()


def test_plot_yearly_soc(tmp_path):
    df = _make_year()
    plot_yearly_soc(df, 2026, tmp_path)
    assert (tmp_path / "yearly_soc.pdf").exists()


def test_plot_yearly_revenue(tmp_path):
    df = _make_year()
    plot_yearly_revenue(df, 2026, tmp_path)
    assert (tmp_path / "yearly_revenue.pdf").exists()


def test_plot_yearly_combined_merchant_renders(tmp_path):
    df = _make_year()
    plot_yearly_combined_merchant(df, 2026, tmp_path)
    assert (tmp_path / "yearly_combined.pdf").exists()


# ---------------------------------------------------------------------------
# Dispatcher branches on params['mode']
# ---------------------------------------------------------------------------


def test_dispatcher_renders_merchant_trio(tmp_path):
    from main import _generate_energy_plots_for_year
    df = _make_dispatch()
    _generate_energy_plots_for_year(
        df, 2026, tmp_path,
        daily=True, monthly=True, yearly=True,
        mode="merchant",
    )
    # Should NOT have produced any vnb supply/surplus plots.
    assert not list(tmp_path.rglob("daily_supply_*.pdf"))
    # Should HAVE produced merchant dispatch / soc / revenue plots.
    assert list(tmp_path.rglob("daily_dispatch_*.pdf"))
    assert list(tmp_path.rglob("daily_soc_*.pdf"))
    assert list(tmp_path.rglob("daily_revenue_*.pdf"))


def test_dispatcher_renders_merchant_combined(tmp_path):
    """Round-5: the merchant branch also produces the combined trio."""
    from main import _generate_energy_plots_for_year
    df = _make_dispatch()
    _generate_energy_plots_for_year(
        df, 2026, tmp_path,
        daily=True, monthly=True, yearly=True,
        mode="merchant",
    )
    assert list(tmp_path.rglob("daily_combined_*.pdf"))
    assert list(tmp_path.rglob("monthly_combined_*.pdf"))
    assert list(tmp_path.rglob("yearly_combined.pdf"))


def test_dispatcher_renders_vnb_trio(tmp_path):
    from main import _generate_energy_plots_for_year
    df = _make_dispatch()
    df["load_kwh"] = 200.0  # add load for vnb
    df["pv_to_load_kwh"] = np.minimum(df["pv_kwh"], 200.0)
    df["grid_to_load_kwh"] = np.maximum(200.0 - df["pv_to_load_kwh"], 0.0)
    _generate_energy_plots_for_year(
        df, 2026, tmp_path,
        daily=True, monthly=False, yearly=False,
        mode="vnb",
    )
    assert list(tmp_path.rglob("daily_supply_*.pdf"))
    assert list(tmp_path.rglob("daily_surplus_*.pdf"))
    assert list(tmp_path.rglob("daily_combined_*.pdf"))
    # Merchant trio NOT produced in vnb mode.
    assert not list(tmp_path.rglob("daily_dispatch_*.pdf"))
