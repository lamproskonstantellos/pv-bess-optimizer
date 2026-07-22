"""Tests for the BESS-specific revenue decomposition.

Covers:

* The 8 canonical revenue aggregate keys emitted by
  :func:`pvbess_opt.kpis.compute_kpis`.
* Internal consistency: BESS-DAM + Σ balancing-product = total BESS
  revenue (no double-counting); PV-DAM + BESS-DAM = the underlying DAM
  revenue without the balancing layer.
* Smoke rendering of :func:`plot_bess_revenue_waterfall`,
  :func:`plot_bess_capacity_vs_activation_split`, and
  :func:`plot_bess_revenue_by_month`.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.kpis import _compute_canonical_revenue_aggregates
from pvbess_opt.plotting.bess_revenue import (
    plot_bess_capacity_vs_activation_split,
    plot_bess_revenue_by_month,
    plot_bess_revenue_waterfall,
)


def _kpis_self_consumption_with_balancing() -> dict[str, float]:
    return {
        "profit_load_from_pv_eur": 15_000.0,
        "profit_load_from_bess_eur": 8_000.0,
        "profit_export_from_pv_eur": 30_000.0,
        "profit_export_from_bess_eur": 20_000.0,
        "expense_charge_bess_grid_eur": 4_000.0,
        "bm_fcr_capacity_revenue_eur": 6_000.0,
        "bm_afrr_up_capacity_revenue_eur": 3_000.0,
        "bm_afrr_up_activation_revenue_eur": 1_500.0,
        "bm_afrr_dn_capacity_revenue_eur": 2_500.0,
        "bm_afrr_dn_activation_revenue_eur": 900.0,
        "bm_mfrr_up_capacity_revenue_eur": 1_200.0,
        "bm_mfrr_up_activation_revenue_eur": 400.0,
        "bm_mfrr_dn_capacity_revenue_eur": 900.0,
        "bm_mfrr_dn_activation_revenue_eur": 250.0,
    }


def test_canonical_aggregates_exposes_eight_keys():
    out = _compute_canonical_revenue_aggregates(
        _kpis_self_consumption_with_balancing(), "self_consumption",
    )
    expected = {
        "revenue_pv_dam_eur",
        "revenue_bess_dam_eur",
        "revenue_self_consumption_eur",
        "revenue_bess_fcr_eur",
        "revenue_bess_afrr_up_eur",
        "revenue_bess_afrr_dn_eur",
        "revenue_bess_mfrr_up_eur",
        "revenue_bess_mfrr_dn_eur",
    }
    assert set(out) == expected


def test_pv_and_bess_dam_match_underlying_kpis():
    kpis = _kpis_self_consumption_with_balancing()
    agg = _compute_canonical_revenue_aggregates(kpis, "self_consumption")
    assert agg["revenue_pv_dam_eur"] == kpis["profit_export_from_pv_eur"]
    # BESS-DAM is the net arbitrage: exports minus grid-charging.
    assert agg["revenue_bess_dam_eur"] == (
        kpis["profit_export_from_bess_eur"]
        - kpis["expense_charge_bess_grid_eur"]
    )


def test_self_consumption_aggregate_is_zero_in_merchant():
    kpis = _kpis_self_consumption_with_balancing()
    agg_self = _compute_canonical_revenue_aggregates(kpis, "self_consumption")
    agg_mer = _compute_canonical_revenue_aggregates(kpis, "merchant")
    assert agg_self["revenue_self_consumption_eur"] > 0.0
    assert agg_mer["revenue_self_consumption_eur"] == 0.0


def test_balancing_aggregates_sum_capacity_and_activation():
    kpis = _kpis_self_consumption_with_balancing()
    agg = _compute_canonical_revenue_aggregates(kpis, "self_consumption")
    # FCR is capacity-only.
    assert agg["revenue_bess_fcr_eur"] == kpis["bm_fcr_capacity_revenue_eur"]
    # aFRR / mFRR pair capacity + activation.
    assert agg["revenue_bess_afrr_up_eur"] == (
        kpis["bm_afrr_up_capacity_revenue_eur"]
        + kpis["bm_afrr_up_activation_revenue_eur"]
    )
    assert agg["revenue_bess_afrr_dn_eur"] == (
        kpis["bm_afrr_dn_capacity_revenue_eur"]
        + kpis["bm_afrr_dn_activation_revenue_eur"]
    )
    assert agg["revenue_bess_mfrr_up_eur"] == (
        kpis["bm_mfrr_up_capacity_revenue_eur"]
        + kpis["bm_mfrr_up_activation_revenue_eur"]
    )
    assert agg["revenue_bess_mfrr_dn_eur"] == (
        kpis["bm_mfrr_dn_capacity_revenue_eur"]
        + kpis["bm_mfrr_dn_activation_revenue_eur"]
    )


def test_total_bess_revenue_no_double_counting():
    """BESS-DAM + Σ balancing-product = canonical total BESS revenue.

    Mirrors the per-stream economics: capacity and activation appear in
    exactly one canonical aggregate per product, so summing the five
    product aggregates plus the BESS-DAM segment must equal the total
    derived directly from the underlying KPIs.
    """
    kpis = _kpis_self_consumption_with_balancing()
    agg = _compute_canonical_revenue_aggregates(kpis, "self_consumption")
    total_via_aggregates = (
        agg["revenue_bess_dam_eur"]
        + agg["revenue_bess_fcr_eur"]
        + agg["revenue_bess_afrr_up_eur"]
        + agg["revenue_bess_afrr_dn_eur"]
        + agg["revenue_bess_mfrr_up_eur"]
        + agg["revenue_bess_mfrr_dn_eur"]
    )
    bm_total = sum(
        kpis.get(f"bm_{p}_capacity_revenue_eur", 0.0)
        for p in ("fcr", "afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn")
    ) + sum(
        kpis.get(f"bm_{p}_activation_revenue_eur", 0.0)
        for p in ("afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn")
    )
    expected = (
        kpis["profit_export_from_bess_eur"]
        - kpis["expense_charge_bess_grid_eur"]
        + bm_total
    )
    assert total_via_aggregates == expected


def test_plot_bess_revenue_waterfall_smoke(tmp_path: Path):
    out = plot_bess_revenue_waterfall(
        _kpis_self_consumption_with_balancing(),
        tmp_path / "waterfall.pdf",
        econ={"currency_format": "auto"},
    )
    assert out.exists() and out.stat().st_size > 0


def test_plot_bess_revenue_waterfall_skips_when_zero(tmp_path: Path):
    out = plot_bess_revenue_waterfall(
        {}, tmp_path / "waterfall.pdf",
        econ={"currency_format": "auto"},
    )
    # Returns a placeholder PDF rather than raising.
    assert out.exists()


def test_plot_bess_capacity_vs_activation_split_smoke(tmp_path: Path):
    out = plot_bess_capacity_vs_activation_split(
        _kpis_self_consumption_with_balancing(),
        tmp_path / "split.pdf",
        econ={"currency_format": "auto"},
    )
    assert out.exists() and out.stat().st_size > 0


def test_monthly_energy_fee_uses_net_dam_base_like_waterfall(
    tmp_path: Path, monkeypatch,
):
    """The monthly plot's energy-aggregator-fee bars must reconcile to the
    waterfall's step, which is charged on the BESS DAM margin NET of grid
    charging (profit_export_from_bess - expense_charge_bess_grid), not on
    gross export.  A gross base disagrees with both the waterfall and the
    cashflow whenever grid charging is non-zero."""
    import matplotlib.pyplot as plt

    import pvbess_opt.plotting.bess_revenue as br

    captured: dict[str, float] = {}

    def _spy(out_path):
        ax = plt.gca()
        for container in ax.containers:
            if container.get_label() == "Energy aggregator fee":
                captured["energy_fee"] = float(
                    sum(b.get_height() for b in container)
                )
        return out_path

    monkeypatch.setattr(br, "save_figure", _spy)

    n = 96 * 30  # one month at 15 min
    ts = pd.date_range("2026-01-01", periods=n, freq="15min").append(
        pd.date_range("2026-02-01", periods=n, freq="15min")
    )
    total = len(ts)
    # Annual gross BESS export 100k, grid charging 60k => net DAM 40k.
    res = pd.DataFrame({
        "timestamp": ts,
        "profit_export_from_bess_eur": np.full(total, 100_000.0 / total),
        "expense_charge_bess_grid_eur": np.full(total, 60_000.0 / total),
        "bess_dis_grid_kwh": np.full(total, 0.0),
    })
    br.plot_bess_revenue_by_month(
        res, {}, tmp_path / "m.pdf",
        econ={"currency_format": "auto",
              "aggregator_fee_pct_revenue": 5.0},
    )
    # Waterfall/cashflow value: -5% x 40k NET = -2000 (not -5% x 100k gross).
    assert captured["energy_fee"] == pytest.approx(-2_000.0, abs=1e-6)
    assert abs(captured["energy_fee"] - (-5_000.0)) > 1.0


def _month_bar_spy(monkeypatch, br):
    """Install a save_figure spy that sums each stacked container's heights
    by legend label; returns the captured dict."""
    import matplotlib.pyplot as plt

    captured: dict[str, float] = {}

    def _spy(out_path):
        ax = plt.gca()
        for container in ax.containers:
            lbl = container.get_label()
            captured[lbl] = captured.get(lbl, 0.0) + float(
                sum(b.get_height() for b in container)
            )
        return out_path

    monkeypatch.setattr(br, "save_figure", _spy)
    return captured


def test_monthly_dam_bars_reconcile_to_derated_waterfall_base(
    tmp_path: Path, monkeypatch,
):
    """The dispatch frame is un-derated but the waterfall and KPI totals use
    the availability-derated year1_kpis.  The monthly DAM bars must sum to the
    canonical ``revenue_bess_dam_eur`` KPI the waterfall draws (here equal to
    profit_export_from_bess − charge under availability), not the raw dispatch
    sum, so the monthly figure and the waterfall agree."""
    import pvbess_opt.plotting.bess_revenue as br

    captured = _month_bar_spy(monkeypatch, br)

    n = 96 * 30
    ts = pd.date_range("2026-01-01", periods=n, freq="15min").append(
        pd.date_range("2026-02-01", periods=n, freq="15min")
    )
    total = len(ts)
    # Raw dispatch: gross export 100k, grid charge 40k => raw net DAM 60k.
    res = pd.DataFrame({
        "timestamp": ts,
        "profit_export_from_bess_eur": np.full(total, 100_000.0 / total),
        "expense_charge_bess_grid_eur": np.full(total, 40_000.0 / total),
        "bess_dis_grid_kwh": np.full(total, 0.0),
    })
    # Availability-only derate (4 % on the 60k net): revenue_bess_dam_eur and
    # profit_export − charge coincide, both 57_600.
    year1_kpis = {
        "revenue_bess_dam_eur": 57_600.0,
        "profit_export_from_bess_eur": 96_000.0,
        "expense_charge_bess_grid_eur": 38_400.0,
    }
    br.plot_bess_revenue_by_month(
        res, year1_kpis, tmp_path / "m.pdf",
        econ={"currency_format": "auto"},
    )
    # DAM bars sum to the derated base 57_600, not the raw 60_000.
    assert captured.get(br._BESS_DAM_LABEL) == pytest.approx(
        57_600.0, abs=1.0,
    )
    assert abs(captured.get(br._BESS_DAM_LABEL, 0.0) - 60_000.0) > 1.0


def test_monthly_dam_bars_track_dam_kpi_not_fee_base_under_curtailment(
    tmp_path: Path, monkeypatch,
):
    """Under the exogenous-curtailment derate the canonical DAM KPI
    (``revenue_bess_dam_eur``, scaled monolithically by 1−q) diverges from the
    energy/optimizer fee base (``profit_export_from_bess − expense_charge``,
    whose withdrawal leg is curtailment-exempt).  The waterfall draws its DAM
    bar from ``revenue_bess_dam_eur`` (03_results.xlsx / SUMMARY.md), so the
    monthly DAM bars must reconcile to THAT — not to the fee base and not to
    the raw dispatch sum."""
    import pvbess_opt.plotting.bess_revenue as br

    captured = _month_bar_spy(monkeypatch, br)

    n = 96 * 30
    ts = pd.date_range("2026-01-01", periods=n, freq="15min").append(
        pd.date_range("2026-02-01", periods=n, freq="15min")
    )
    total = len(ts)
    # Raw dispatch: gross export 100k, grid charge 40k.
    res = pd.DataFrame({
        "timestamp": ts,
        "profit_export_from_bess_eur": np.full(total, 100_000.0 / total),
        "expense_charge_bess_grid_eur": np.full(total, 40_000.0 / total),
        "bess_dis_grid_kwh": np.full(total, 0.0),
    })
    # Availability a=0.99, curtailment q=0.10:
    #   profit_export = a·(1−q)·100k = 89_100  (curtailment-derated)
    #   expense_charge = a·40k        = 39_600  (curtailment-EXEMPT withdrawal)
    #   revenue_bess_dam = a·(1−q)·(100k−40k) = 53_460  (monolithic)
    #   fee base = profit_export − expense_charge      = 49_500  (diverges)
    year1_kpis = {
        "revenue_bess_dam_eur": 53_460.0,
        "profit_export_from_bess_eur": 89_100.0,
        "expense_charge_bess_grid_eur": 39_600.0,
    }
    br.plot_bess_revenue_by_month(
        res, year1_kpis, tmp_path / "m.pdf",
        econ={"currency_format": "auto"},
    )
    dam_bar = captured.get(br._BESS_DAM_LABEL, 0.0)
    # DAM bars reconcile to the canonical DAM KPI (53_460), NOT the fee base
    # (49_500) and NOT the raw dispatch net (60_000).
    assert dam_bar == pytest.approx(53_460.0, abs=1.0)
    assert abs(dam_bar - 49_500.0) > 1.0
    assert abs(dam_bar - 60_000.0) > 1.0


def test_plot_bess_revenue_by_month_smoke(tmp_path: Path):
    n = 96 * 30  # one month at 15 min cadence
    rng = pd.date_range("2026-01-01", periods=n, freq="15min")
    rng2 = pd.date_range("2026-02-01", periods=n, freq="15min")
    ts = rng.append(rng2)
    res = pd.DataFrame({
        "timestamp": ts,
        "profit_export_from_bess_eur": np.full(len(ts), 1.0),
        "expense_charge_bess_grid_eur": np.full(len(ts), 0.25),
        "bm_reservation_fcr_kw": np.full(len(ts), 50.0),
        "bm_reservation_afrr_up_kw": np.full(len(ts), 40.0),
        "bm_reservation_afrr_dn_kw": np.full(len(ts), 35.0),
        "bm_reservation_mfrr_up_kw": np.full(len(ts), 15.0),
        "bm_reservation_mfrr_dn_kw": np.full(len(ts), 10.0),
    })
    out = plot_bess_revenue_by_month(
        res,
        _kpis_self_consumption_with_balancing(),
        tmp_path / "monthly.pdf",
        econ={"currency_format": "auto"},
    )
    assert out.exists() and out.stat().st_size > 0
