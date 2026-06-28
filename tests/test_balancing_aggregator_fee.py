"""Balancing-aggregator (BSP / route-to-market) fee — scope-change locks.

The optional ``balancing_aggregator_fee_pct_revenue`` deducts a
non-negative share of GROSS balancing revenue (capacity + activation)
before it enters the cashflow.  It mirrors the energy
``aggregator_fee_pct_revenue`` but applies to balancing only; PPA carries
neither fee.  Default 0.0 keeps every output bit-identical.

These tests pin:

* the per-year deduction equals ``-frac * gross_balancing`` (escalated
  with the gross), gross stays gross, the new
  ``balancing_aggregator_fee_eur`` column appears, and the net cashflow
  drops by exactly the fee;
* default 0.0 ⇒ the column is all-zero and the net is bit-identical to a
  workbook without the key;
* lifecycle KPIs expose the fee and the net while the gross roll-up is
  unchanged, and a non-zero fee lowers NPV;
* monthly / quarterly frames carry the column and reconcile to the yearly
  column (and the yearly net) row-for-row;
* the ``[0, 100]`` range validation fires for BOTH revenue fees;
* the sensitivity revenue driver scales the fee column with the gross;
* the revenue-stack plot draws the fee as its own deduction bar and the
  BESS revenue waterfall steps its total down by it.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import (
    build_yearly_cashflow,
    compute_financial_kpis,
    derive_monthly_cashflow,
)
from pvbess_opt.io import _SHEET_DEFAULTS, validate_workbook_params
from pvbess_opt.lifetime import _bess_factor


def _econ(**overrides) -> dict:
    base: dict = {
        "project_lifecycle_years": 10,
        "project_start_year": 2026,
        "capex_pv_eur_per_kw": 0.0,
        "capex_bess_eur_per_kw": 100.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "site_capex_eur": 0.0,
        "site_devex_eur": 0.0,
        "opex_pv_eur_per_kwp": 0.0,
        "opex_bess_eur_per_kw": 0.0,
        "opex_inflation_pct": 0.0,
        "discount_rate_pct": 7.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "pv_degradation_year1_pct": 0.0,
        "pv_degradation_annual_pct": 0.0,
        "bess_degradation_annual_pct": 2.0,
        "bess_degradation_pct_per_cycle": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
        "bm_inflation_pct": 3.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
    }
    base.update(overrides)
    return base


def _year1_kpis() -> dict:
    return {
        "bm_total_capacity_revenue_eur": 12_000.0,
        "bm_total_activation_revenue_eur": 4_500.0,
        "profit_total_eur": 0.0,
        "bess_total_discharge_mwh": 0.0,
    }


def _capacities() -> dict:
    return {"pv_kwp": 0.0, "bess_kw": 1000.0, "bess_kwh": 4000.0}


# ---------------------------------------------------------------------------
# Yearly cashflow — the deduction itself
# ---------------------------------------------------------------------------


def test_bsp_fee_deducts_from_gross_balancing_per_year():
    frac = 0.20
    econ = _econ(balancing_aggregator_fee_pct_revenue=frac * 100.0)
    df = build_yearly_cashflow(_year1_kpis(), econ, _capacities())

    bm_infl = 0.03
    d_annual = 0.02
    for y in (1, 3, 5, 10):
        bess_f = _bess_factor(y, d_annual)
        gross = (12_000.0 + 4_500.0) * bess_f * (1.0 + bm_infl) ** (y - 1)
        row = df.loc[df["project_year"] == y].iloc[0]
        # Gross stays gross.
        assert row["balancing_revenue_eur"] == pytest.approx(gross, rel=1e-9)
        # Fee is exactly -frac * gross, escalated with the gross.
        assert row["balancing_aggregator_fee_eur"] == pytest.approx(
            -frac * gross, rel=1e-9,
        )


def test_bsp_fee_lowers_net_cashflow_by_exactly_the_fee():
    """The only delta between fee-off and fee-on is the fee itself."""
    off = build_yearly_cashflow(_year1_kpis(), _econ(), _capacities())
    on = build_yearly_cashflow(
        _year1_kpis(),
        _econ(balancing_aggregator_fee_pct_revenue=15.0),
        _capacities(),
    )
    delta = (on["net_cashflow_eur"] - off["net_cashflow_eur"]).to_numpy()
    fee = on["balancing_aggregator_fee_eur"].to_numpy()
    np.testing.assert_allclose(delta, fee, rtol=1e-9, atol=1e-9)
    # Gross balancing column is identical with and without the fee.
    np.testing.assert_allclose(
        on["balancing_revenue_eur"].to_numpy(),
        off["balancing_revenue_eur"].to_numpy(),
        rtol=1e-12, atol=1e-9,
    )


def test_year0_bsp_fee_is_zero():
    df = build_yearly_cashflow(
        _year1_kpis(),
        _econ(balancing_aggregator_fee_pct_revenue=20.0),
        _capacities(),
    )
    row = df.loc[df["project_year"] == 0].iloc[0]
    assert row["balancing_aggregator_fee_eur"] == 0.0


def test_default_zero_is_bit_identical_to_missing_key():
    """Column all-zero and the net frame matches a build that never saw the
    key — the bit-identical guarantee."""
    econ_missing = _econ()
    econ_missing.pop("balancing_aggregator_fee_pct_revenue", None)
    df_missing = build_yearly_cashflow(_year1_kpis(), econ_missing, _capacities())
    df_zero = build_yearly_cashflow(
        _year1_kpis(),
        _econ(balancing_aggregator_fee_pct_revenue=0.0),
        _capacities(),
    )
    assert (df_zero["balancing_aggregator_fee_eur"].abs() < 1e-12).all()
    for col in ("net_cashflow_eur", "discounted_cf_eur", "balancing_revenue_eur"):
        np.testing.assert_allclose(
            df_zero[col].to_numpy(), df_missing[col].to_numpy(),
            rtol=1e-12, atol=1e-12,
        )


# ---------------------------------------------------------------------------
# Lifecycle KPIs
# ---------------------------------------------------------------------------


def test_lifecycle_kpis_expose_fee_and_net_and_lower_npv():
    off = compute_financial_kpis(
        build_yearly_cashflow(_year1_kpis(), _econ(), _capacities()), _econ(),
    )
    econ_on = _econ(balancing_aggregator_fee_pct_revenue=20.0)
    on_cf = build_yearly_cashflow(_year1_kpis(), econ_on, _capacities())
    on = compute_financial_kpis(on_cf, econ_on)

    # Gross roll-up is unchanged by the fee.
    assert on["lifetime_bm_revenue_total_eur"] == pytest.approx(
        off["lifetime_bm_revenue_total_eur"], rel=1e-9,
    )
    # Fee total equals the summed column and is non-positive.
    fee_sum = float(on_cf.loc[on_cf["project_year"] >= 1,
                              "balancing_aggregator_fee_eur"].sum())
    assert on["lifetime_bm_aggregator_fee_total_eur"] == pytest.approx(
        round(fee_sum, 2), rel=1e-9,
    )
    assert on["lifetime_bm_aggregator_fee_total_eur"] < 0.0
    # Net = gross + fee.
    assert on["lifetime_bm_revenue_net_total_eur"] == pytest.approx(
        round(on["lifetime_bm_revenue_total_eur"]
              + on["lifetime_bm_aggregator_fee_total_eur"], 2),
        rel=1e-9,
    )
    # A real cost ⇒ NPV strictly lower than the fee-off case.
    assert on["npv_eur"] < off["npv_eur"]


def test_default_zero_lifecycle_fee_is_zero():
    on = compute_financial_kpis(
        build_yearly_cashflow(_year1_kpis(), _econ(), _capacities()), _econ(),
    )
    assert on["lifetime_bm_aggregator_fee_total_eur"] == 0.0
    assert on["lifetime_bm_revenue_net_total_eur"] == pytest.approx(
        on["lifetime_bm_revenue_total_eur"], rel=1e-9,
    )


# ---------------------------------------------------------------------------
# Monthly / quarterly reconciliation
# ---------------------------------------------------------------------------


def _daily_res(n_days: int = 365) -> pd.DataFrame:
    ts = pd.date_range("2026-01-01", periods=n_days, freq="D")
    n = len(ts)
    return pd.DataFrame({
        "timestamp": ts,
        "pv_kwh": np.ones(n) * 10.0,
        "profit_load_from_pv_eur": np.zeros(n),
        "profit_load_from_bess_eur": np.zeros(n),
        "profit_export_from_pv_eur": np.zeros(n),
        "profit_export_from_bess_eur": np.zeros(n),
        "expense_charge_bess_grid_eur": np.zeros(n),
    })


def test_monthly_and_quarterly_fee_reconcile_to_yearly():
    econ = _econ(balancing_aggregator_fee_pct_revenue=18.0)
    yearly_cf = build_yearly_cashflow(_year1_kpis(), econ, _capacities())
    monthly_cf, quarterly_cf = derive_monthly_cashflow(_daily_res(), yearly_cf, econ)

    assert "balancing_aggregator_fee_eur" in monthly_cf.columns
    assert "balancing_aggregator_fee_eur" in quarterly_cf.columns

    yearly_indexed = yearly_cf.set_index("project_year")
    for frame in (monthly_cf, quarterly_cf):
        by_year = frame.groupby("project_year")["balancing_aggregator_fee_eur"].sum()
        for y, mtot in by_year.items():
            ytot = float(yearly_indexed.loc[y, "balancing_aggregator_fee_eur"])
            assert mtot == pytest.approx(ytot, abs=1e-6), f"year {y}"
        # The net cashflow still reconciles row-for-row.
        net_by_year = frame.groupby("project_year")["net_cashflow_eur"].sum()
        for y, mnet in net_by_year.items():
            ynet = float(yearly_indexed.loc[y, "net_cashflow_eur"])
            assert mnet == pytest.approx(ynet, abs=0.05), f"net year {y}"


def _res_seasonal() -> pd.DataFrame:
    """Daily Year-1 frame where the balancing RESERVATION profile and the
    energy-REVENUE profile concentrate in DIFFERENT halves of the year, so
    the per-month balancing share != the per-month revenue (fee) share.

    Reservations sit in months 1-6; per-step energy revenue sits in
    months 7-12.  This makes the two monthly weightings provably distinct
    (a flat-1/12 fixture cannot tell them apart)."""
    ts = pd.date_range("2026-01-01", periods=365, freq="D")
    n = len(ts)
    month = ts.month.to_numpy()
    first_half = (month <= 6).astype(float)
    second_half = (month >= 7).astype(float)
    df = pd.DataFrame({
        "timestamp": ts,
        "pv_kwh": np.ones(n) * 10.0,
        # Energy revenue only in H2 -> fee_share weights months 7-12.
        "profit_load_from_pv_eur": second_half * 5.0,
        "profit_load_from_bess_eur": np.zeros(n),
        "profit_export_from_pv_eur": np.zeros(n),
        "profit_export_from_bess_eur": np.zeros(n),
        "expense_charge_bess_grid_eur": np.zeros(n),
    })
    # Reservations only in H1 -> balancing_share weights months 1-6.
    for p in ("fcr", "afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn"):
        df[f"bm_reservation_{p}_kw"] = first_half * 100.0
    return df


def test_monthly_fee_follows_balancing_profile_not_revenue():
    """The per-month BSP fee must track the per-month GROSS balancing
    revenue (same reservation weighting), NOT the energy-revenue share.

    Kills the mutant that allocates the fee by ``fee_share`` instead of
    ``balancing_share``: with the two profiles in opposite halves of the
    year, the fee must be zero in every month with no balancing revenue
    and exactly ``-frac`` of the balancing revenue where it is present."""
    frac = 0.18
    econ = _econ(balancing_aggregator_fee_pct_revenue=frac * 100.0)
    yearly_cf = build_yearly_cashflow(_year1_kpis(), econ, _capacities())
    monthly_cf, _ = derive_monthly_cashflow(_res_seasonal(), yearly_cf, econ)

    y1 = monthly_cf[monthly_cf["project_year"] == 1]
    bal = y1["balancing_revenue_eur"].to_numpy()
    fee = y1["balancing_aggregator_fee_eur"].to_numpy()
    # Some months carry balancing revenue and some do not (profiles split).
    assert (bal > 1e-9).any() and (bal <= 1e-9).any()
    # Per month: fee == -frac * gross balancing (so zero where balancing is).
    np.testing.assert_allclose(fee, -frac * bal, rtol=1e-9, atol=1e-9)


# ---------------------------------------------------------------------------
# Range validation — both revenue fees, [0, 100]
# ---------------------------------------------------------------------------


def _typed(**econ_overrides) -> dict:
    typed = {sheet: dict(defaults) for sheet, defaults in _SHEET_DEFAULTS.items()}
    typed["economics"].update(econ_overrides)
    return typed


@pytest.mark.parametrize("bad", [150.0, -5.0])
def test_balancing_fee_out_of_range_rejected(bad):
    with pytest.raises(ValueError, match="balancing_aggregator_fee_pct_revenue"):
        validate_workbook_params(
            _typed(balancing_aggregator_fee_pct_revenue=bad), dt_minutes=15,
        )


@pytest.mark.parametrize("bad", [150.0, -5.0])
def test_energy_aggregator_fee_out_of_range_rejected(bad):
    """The energy fee is now rejected loudly out of range too (was silently
    clamped) — symmetric with gearing_pct and the new BSP fee."""
    with pytest.raises(ValueError, match="aggregator_fee_pct_revenue"):
        validate_workbook_params(
            _typed(aggregator_fee_pct_revenue=bad), dt_minutes=15,
        )


def test_in_range_fees_accepted():
    # 0 and 100 are the inclusive bounds; a mid value also passes.
    for v in (0.0, 50.0, 100.0):
        validate_workbook_params(
            _typed(balancing_aggregator_fee_pct_revenue=v), dt_minutes=15,
        )


# ---------------------------------------------------------------------------
# Sensitivity — the fee scales with the gross
# ---------------------------------------------------------------------------


def test_sensitivity_scales_fee_with_gross_balancing():
    from pvbess_opt.sensitivity import _scale_revenue

    econ = _econ(balancing_aggregator_fee_pct_revenue=20.0)
    base = build_yearly_cashflow(_year1_kpis(), econ, _capacities())
    scaled = _scale_revenue(base, 1.10)
    np.testing.assert_allclose(
        scaled["balancing_aggregator_fee_eur"].to_numpy(),
        base["balancing_aggregator_fee_eur"].to_numpy() * 1.10,
        rtol=1e-9, atol=1e-9,
    )
    # The fee stays -frac * gross after scaling (frac preserved).
    on = scaled.loc[scaled["project_year"] >= 1]
    ratio = (on["balancing_aggregator_fee_eur"]
             / on["balancing_revenue_eur"]).to_numpy()
    np.testing.assert_allclose(ratio, -0.20, rtol=1e-9, atol=1e-9)


def test_scale_revenue_identity_preserves_fee():
    """factor == 1.0 is a no-op on the fee column too."""
    from pvbess_opt.sensitivity import _scale_revenue

    econ = _econ(balancing_aggregator_fee_pct_revenue=12.0)
    base = build_yearly_cashflow(_year1_kpis(), econ, _capacities())
    same = _scale_revenue(base, 1.0)
    np.testing.assert_allclose(
        same["balancing_aggregator_fee_eur"].to_numpy(),
        base["balancing_aggregator_fee_eur"].to_numpy(),
        rtol=1e-12, atol=1e-9,
    )


# ---------------------------------------------------------------------------
# Plots — the fee is its own deduction
# ---------------------------------------------------------------------------


def _capture_bar_labels(monkeypatch) -> list[str]:
    labels: list[str] = []
    orig = matplotlib.axes.Axes.bar

    def spy(self, *args, **kwargs):
        lab = kwargs.get("label")
        if lab:
            labels.append(lab)
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(matplotlib.axes.Axes, "bar", spy)
    return labels


def _stack_year1_kpis() -> dict:
    return {
        "profit_load_from_pv_eur": 60_000.0,
        "profit_load_from_bess_eur": 0.0,
        "profit_export_from_pv_eur": 40_000.0,
        "profit_export_from_bess_eur": 0.0,
        "expense_charge_bess_grid_eur": 0.0,
        "revenue_bess_fcr_eur": 12_000.0,
        "revenue_bess_afrr_up_eur": 4_000.0,
        "revenue_bess_afrr_dn_eur": 0.0,
        "revenue_bess_mfrr_up_eur": 0.0,
        "revenue_bess_mfrr_dn_eur": 0.0,
        "revenue_bess_dam_eur": 8_000.0,
    }


def test_revenue_stack_draws_bsp_fee_bar(monkeypatch, tmp_path: Path):
    from pvbess_opt.plotting.lifecycle import plot_revenue_stack_yearly

    econ = _econ(
        balancing_aggregator_fee_pct_revenue=20.0,
        # Give the stack a retail/DAM split so the per-stream path runs.
        aggregator_fee_pct_revenue=0.0,
    )
    y1 = {
        "bm_total_capacity_revenue_eur": 12_000.0,
        "bm_total_activation_revenue_eur": 4_000.0,
        "profit_load_from_pv_eur": 60_000.0,
        "profit_export_from_pv_eur": 40_000.0,
        "bess_total_discharge_mwh": 0.0,
    }
    yearly_cf = build_yearly_cashflow(y1, econ, _capacities())

    labels = _capture_bar_labels(monkeypatch)
    plot_revenue_stack_yearly(
        yearly_cf, _stack_year1_kpis(), tmp_path / "stack.pdf", econ=econ,
    )
    assert "Balancing aggregator fee" in labels


def test_revenue_stack_no_fee_bar_when_off(monkeypatch, tmp_path: Path):
    from pvbess_opt.plotting.lifecycle import plot_revenue_stack_yearly

    econ = _econ(balancing_aggregator_fee_pct_revenue=0.0)
    y1 = {
        "bm_total_capacity_revenue_eur": 12_000.0,
        "bm_total_activation_revenue_eur": 4_000.0,
        "profit_load_from_pv_eur": 60_000.0,
        "profit_export_from_pv_eur": 40_000.0,
        "bess_total_discharge_mwh": 0.0,
    }
    yearly_cf = build_yearly_cashflow(y1, econ, _capacities())
    labels = _capture_bar_labels(monkeypatch)
    plot_revenue_stack_yearly(
        yearly_cf, _stack_year1_kpis(), tmp_path / "stack.pdf", econ=econ,
    )
    assert "Balancing aggregator fee" not in labels


def test_waterfall_steps_down_by_bsp_fee(monkeypatch, tmp_path: Path):
    from pvbess_opt.plotting.bess_revenue import plot_bess_revenue_waterfall

    econ = _econ(balancing_aggregator_fee_pct_revenue=25.0)
    labels = _capture_bar_labels(monkeypatch)
    plot_bess_revenue_waterfall(
        _stack_year1_kpis(), tmp_path / "wf.pdf", econ=econ,
    )
    assert "Balancing aggregator fee" in labels


def test_waterfall_no_fee_step_when_off(monkeypatch, tmp_path: Path):
    from pvbess_opt.plotting.bess_revenue import plot_bess_revenue_waterfall

    econ = _econ(balancing_aggregator_fee_pct_revenue=0.0)
    labels = _capture_bar_labels(monkeypatch)
    plot_bess_revenue_waterfall(
        _stack_year1_kpis(), tmp_path / "wf.pdf", econ=econ,
    )
    assert "Balancing aggregator fee" not in labels
