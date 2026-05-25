"""DEVEX / availability / aggregator-fee economics tests.

Covers DEVEX, unavailability_pct, aggregator_fee_pct_revenue, plus the
year-on-year revenue monotonicity invariant under the default
inflation / degradation constants.
"""

from __future__ import annotations

import pytest

from pvbess_opt.availability import (
    apply_unavailability_derate,
    availability_factor,
)
from pvbess_opt.economics import (
    build_yearly_cashflow,
    compute_financial_kpis,
)


def _econ() -> dict:
    return {
        "project_lifecycle_years": 5,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 1.0,
        "retail_inflation_pct": 2.0,
        "dam_inflation_pct": 2.0,
        "aggregator_fee_pct_revenue": 10.0,
        "capex_pv_eur_per_kw": 525.0,
        "capex_bess_eur_per_kw": 200.0,
        "devex_pv_eur_per_kw": 60.0,
        "devex_bess_eur_per_kw": 30.0,
        "opex_pv_eur_per_kwp": 7.0,
        "opex_bess_eur_per_kw": 14.0,
        "pv_degradation_year1_pct": 2.5,
        "pv_degradation_annual_pct": 0.55,
        "bess_degradation_annual_pct": 2.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 50.0,
        "sensitivity_capex_delta_pct": 10.0,
        "sensitivity_opex_delta_pct": 10.0,
        "unavailability_pct": 1.0,
    }


def _caps() -> dict:
    return {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}


# ---------------------------------------------------------------------------
# DEVEX cashflow
# ---------------------------------------------------------------------------


def test_devex_year0_only():
    """devex_eur is negative in Year 0 only; zero everywhere else."""
    kpis = {"profit_total_eur": 200_000.0}
    df = build_yearly_cashflow(kpis, _econ(), _caps())
    assert "devex_eur" in df.columns
    y0_dev = float(df.loc[df["project_year"] == 0, "devex_eur"].iloc[0])
    assert y0_dev < 0
    expected = -(60.0 * 4500.0 + 30.0 * 5000.0)  # 270 000 + 150 000 = 420 000
    assert y0_dev == pytest.approx(expected)
    op = df.loc[df["project_year"] >= 1, "devex_eur"].astype(float)
    assert (op == 0.0).all()


def test_total_capex_devex_kpi_present():
    kpis = {"profit_total_eur": 200_000.0}
    df = build_yearly_cashflow(kpis, _econ(), _caps())
    fin = compute_financial_kpis(df, _econ())
    assert "total_devex_eur" in fin
    assert "total_capex_devex_eur" in fin
    assert fin["total_capex_devex_eur"] == pytest.approx(
        fin["total_capex_eur"] + fin["total_devex_eur"], rel=1e-6,
    )


# ---------------------------------------------------------------------------
# Unavailability derate
# ---------------------------------------------------------------------------


def test_availability_factor_rounds():
    assert availability_factor(0.0) == 1.0
    assert availability_factor(1.0) == pytest.approx(0.99)
    assert availability_factor(100.0) == 0.0
    assert availability_factor(150.0) == 0.0


def test_unavailability_derate_applied():
    """Year-1 PV generation drops by 1 % when unavailability_pct = 1.0."""
    raw = {
        "pv_generation_mwh": 1000.0,
        "bess_total_discharge_mwh": 200.0,
        "profit_total_eur": 5000.0,
    }
    derated = apply_unavailability_derate(raw, 1.0)
    assert derated["pv_generation_mwh"] == pytest.approx(990.0)
    assert derated["bess_total_discharge_mwh"] == pytest.approx(198.0)
    assert derated["profit_total_eur"] == pytest.approx(4950.0)
    assert derated["availability_factor"] == pytest.approx(0.99)


# ---------------------------------------------------------------------------
# Aggregator fee
# ---------------------------------------------------------------------------


def test_aggregator_fee_reduces_revenue():
    """revenue_eur in Year 1 is 0.9 * gross when aggregator_fee_pct = 10."""
    kpis = {"profit_total_eur": 100_000.0}
    df = build_yearly_cashflow(kpis, _econ(), _caps())
    y1 = df.loc[df["project_year"] == 1].iloc[0]
    # Year-1 pv_factor is 1.0, rev_infl exponent is 0 ⇒ gross = 100 000
    assert y1["revenue_eur"] == pytest.approx(90_000.0, rel=1e-6)
    assert y1["aggregator_fee_eur"] == pytest.approx(-10_000.0, rel=1e-6)


def test_aggregator_fee_zero_default_path():
    econ = _econ()
    econ["aggregator_fee_pct_revenue"] = 0.0
    kpis = {"profit_total_eur": 100_000.0}
    df = build_yearly_cashflow(kpis, econ, _caps())
    y1 = df.loc[df["project_year"] == 1].iloc[0]
    assert y1["aggregator_fee_eur"] == pytest.approx(0.0)
    assert y1["revenue_eur"] == pytest.approx(100_000.0, rel=1e-6)


# ---------------------------------------------------------------------------
# Year-2..N revenue monotonically increasing under defaults
# ---------------------------------------------------------------------------


def test_revenue_grows_yoy_under_default_inflation_and_degradation():
    """+2 % inflation × -0.55 %/yr degradation = ~+1.4 %/yr — strictly up."""
    kpis = {"profit_total_eur": 200_000.0}
    df = build_yearly_cashflow(kpis, _econ(), _caps())
    rev = df.loc[df["project_year"] >= 2, "revenue_eur"].astype(float)
    assert rev.is_monotonic_increasing


# ---------------------------------------------------------------------------
# Strict regression guard: with DEVEX = 0, unavail = 0, fee = 0,
# the pre-DEVEX baseline numbers reappear.
# ---------------------------------------------------------------------------


def test_baseline_reproducible_when_extras_off():
    econ = _econ()
    econ["devex_pv_eur_per_kw"] = 0.0
    econ["devex_bess_eur_per_kw"] = 0.0
    econ["aggregator_fee_pct_revenue"] = 0.0
    econ["unavailability_pct"] = 0.0
    kpis = {"profit_total_eur": 100_000.0}
    df = build_yearly_cashflow(kpis, econ, _caps())
    # Year-0 capex matches exactly (capex_pv*pv_kwp + capex_bess*bess_kw).
    expected_capex_y0 = -(525.0 * 4500.0 + 200.0 * 5000.0)
    y0_capex = float(df.loc[df["project_year"] == 0, "capex_eur"].iloc[0])
    y0_devex = float(df.loc[df["project_year"] == 0, "devex_eur"].iloc[0])
    assert y0_capex == pytest.approx(expected_capex_y0)
    assert y0_devex == 0.0
    # Year 1 revenue equals raw profit (no fee, no unavailability).
    y1_rev = float(df.loc[df["project_year"] == 1, "revenue_eur"].iloc[0])
    assert y1_rev == pytest.approx(100_000.0, rel=1e-6)
    # Year-1 aggregator_fee zeroed.
    y1_fee = float(df.loc[df["project_year"] == 1, "aggregator_fee_eur"].iloc[0])
    assert y1_fee == 0.0
