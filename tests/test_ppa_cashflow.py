"""PPA premium projected across the multi-year cash-flow.

The PPA premium is a parallel revenue stream in build_yearly_cashflow:
it degrades on the PV / BESS factors, carries its own escalation index,
folds into net_cashflow_eur, is excluded from the aggregator fee and
from LCOE / LCOS, and never trips the Year-1 revenue-split
reconciliation guard.  The Revenue sensitivity driver scales it.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import (
    build_yearly_cashflow,
    compute_financial_kpis,
    derive_monthly_cashflow,
)
from pvbess_opt.io import (
    BESS_SHEET_DEFAULTS,
    ECONOMICS_SHEET_DEFAULTS,
    PPA_SHEET_DEFAULTS,
    PROJECT_SHEET_DEFAULTS,
    PV_SHEET_DEFAULTS,
)
from pvbess_opt.sensitivity import (
    _recompute_net,
    _scale_revenue,
    run_sensitivity_analysis,
)


def _econ(**over) -> dict:
    econ: dict = {}
    for d in (
        PROJECT_SHEET_DEFAULTS, PV_SHEET_DEFAULTS, BESS_SHEET_DEFAULTS,
        ECONOMICS_SHEET_DEFAULTS, PPA_SHEET_DEFAULTS,
    ):
        econ.update(d)
    econ["project_lifecycle_years"] = 10
    econ["project_start_year"] = 2026
    econ["capex_pv_eur_per_kw"] = 525.0
    econ["capex_bess_eur_per_kw"] = 200.0
    econ.update(over)
    return econ


_CAPACITIES = {"pv_kwp": 6000.0, "bess_kw": 2000.0, "bess_kwh": 8000.0}


def _year1(ppa_pv: float = 0.0, ppa_bess: float = 0.0) -> dict:
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
    }


def _lifetime_yearly() -> pd.DataFrame:
    return pd.DataFrame({
        "project_year": list(range(1, 11)),
        "pv_generation_mwh": [9000.0] * 10,
        "bess_discharge_mwh": [1500.0] * 10,
    })


def _fin_kpis(econ, y1):
    cf = build_yearly_cashflow(y1, econ, _CAPACITIES)
    return cf, compute_financial_kpis(
        cf, econ, capacities=_CAPACITIES,
        lifetime_yearly=_lifetime_yearly(), year1_kpis=y1,
    )


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
# Yearly cash-flow
# ---------------------------------------------------------------------------


def test_ppa_revenue_column_year0_zero_year1_premium():
    econ = _econ()
    cf = build_yearly_cashflow(_year1(8000.0, 4000.0), econ, _CAPACITIES)
    assert "ppa_revenue_eur" in cf.columns
    assert float(cf.loc[cf.project_year == 0, "ppa_revenue_eur"].iloc[0]) == 0.0
    assert float(cf.loc[cf.project_year == 1, "ppa_revenue_eur"].iloc[0]) == (
        pytest.approx(12000.0, abs=1e-6)
    )


def test_ppa_folds_into_net_cashflow():
    econ = _econ()
    cf_off = build_yearly_cashflow(_year1(0.0, 0.0), econ, _CAPACITIES)
    cf_on = build_yearly_cashflow(_year1(8000.0, 4000.0), econ, _CAPACITIES)
    diff = (
        float(cf_on.loc[cf_on.project_year == 1, "net_cashflow_eur"].iloc[0])
        - float(cf_off.loc[cf_off.project_year == 1, "net_cashflow_eur"].iloc[0])
    )
    assert diff == pytest.approx(12000.0, abs=1e-6)


def test_ppa_degrades_on_pv_and_bess_factors():
    econ = _econ(ppa_escalation_pct=0.0)
    cf = build_yearly_cashflow(_year1(8000.0, 4000.0), econ, _CAPACITIES)
    for _, row in cf[cf.project_year >= 1].iterrows():
        expected = (
            8000.0 * float(row["pv_production_factor"])
            + 4000.0 * float(row["bess_capacity_factor"])
        )
        assert float(row["ppa_revenue_eur"]) == pytest.approx(expected, rel=1e-9)


def test_ppa_escalation_applied_in_cashflow():
    cf0 = build_yearly_cashflow(
        _year1(8000.0, 4000.0), _econ(ppa_escalation_pct=0.0), _CAPACITIES,
    )
    cf3 = build_yearly_cashflow(
        _year1(8000.0, 4000.0), _econ(ppa_escalation_pct=3.0), _CAPACITIES,
    )
    # Year 2: same degradation factors, but cf3 carries the 1.03 index.
    y2_0 = float(cf0.loc[cf0.project_year == 2, "ppa_revenue_eur"].iloc[0])
    y2_3 = float(cf3.loc[cf3.project_year == 2, "ppa_revenue_eur"].iloc[0])
    assert y2_3 == pytest.approx(y2_0 * 1.03, rel=1e-9)


def test_ppa_excluded_from_aggregator_fee():
    econ = _econ()
    cf_off = build_yearly_cashflow(_year1(0.0, 0.0), econ, _CAPACITIES)
    cf_on = build_yearly_cashflow(_year1(8000.0, 4000.0), econ, _CAPACITIES)
    # The fee is computed on retail + DAM gross only, so the PPA premium
    # must not change it.
    assert np.allclose(
        cf_off["aggregator_fee_eur"].to_numpy(),
        cf_on["aggregator_fee_eur"].to_numpy(),
    )


def test_reconciliation_guard_not_tripped(caplog):
    econ = _econ()
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.economics"):
        build_yearly_cashflow(_year1(8000.0, 4000.0), econ, _CAPACITIES)
    assert not any("revenue split drift" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# LCOE / LCOS exclusion + NPV sign + lifetime totals
# ---------------------------------------------------------------------------


def test_toggling_ppa_leaves_lcoe_lcos_identical():
    econ = _econ()
    _, fk_off = _fin_kpis(econ, _year1(0.0, 0.0))
    _, fk_pos = _fin_kpis(econ, _year1(8000.0, 4000.0))
    _, fk_neg = _fin_kpis(econ, _year1(-6000.0, -2000.0))
    assert fk_off["lcoe_eur_per_mwh"] == fk_pos["lcoe_eur_per_mwh"]
    assert fk_off["lcoe_eur_per_mwh"] == fk_neg["lcoe_eur_per_mwh"]
    assert fk_off["lcos_eur_per_mwh"] == fk_pos["lcos_eur_per_mwh"]
    assert fk_off["lcos_eur_per_mwh"] == fk_neg["lcos_eur_per_mwh"]


def test_ppa_positive_increases_npv_negative_decreases():
    econ = _econ()
    _, fk_off = _fin_kpis(econ, _year1(0.0, 0.0))
    _, fk_pos = _fin_kpis(econ, _year1(8000.0, 4000.0))
    _, fk_neg = _fin_kpis(econ, _year1(-6000.0, -2000.0))
    assert fk_pos["npv_eur"] > fk_off["npv_eur"]
    assert fk_neg["npv_eur"] < fk_off["npv_eur"]


def test_lifetime_ppa_totals():
    econ = _econ()
    cf, fk = _fin_kpis(econ, _year1(8000.0, 4000.0))
    assert "lifetime_ppa_revenue_total_eur" in fk
    assert fk["lifetime_ppa_revenue_total_eur"] == pytest.approx(
        float(cf.loc[cf.project_year >= 1, "ppa_revenue_eur"].sum()), abs=0.05,
    )
    assert len(fk["lifetime_ppa_revenue_eur_per_year"]) == 10


def test_lifetime_ppa_total_zero_when_off():
    _, fk = _fin_kpis(_econ(), _year1(0.0, 0.0))
    assert fk["lifetime_ppa_revenue_total_eur"] == 0.0


# ---------------------------------------------------------------------------
# Monthly / quarterly allocation
# ---------------------------------------------------------------------------


def test_monthly_ppa_reconciles_to_yearly():
    econ = _econ(ppa_escalation_pct=2.0)
    cf = build_yearly_cashflow(_year1(8000.0, 4000.0), econ, _CAPACITIES)
    mcf, qcf = derive_monthly_cashflow(_synthetic_res(), cf, econ)
    assert "ppa_revenue_eur" in mcf.columns
    assert "ppa_revenue_eur" in qcf.columns
    for y in (1, 2, 5):
        m_sum = float(mcf.loc[mcf.project_year == y, "ppa_revenue_eur"].sum())
        y_val = float(cf.loc[cf.project_year == y, "ppa_revenue_eur"].iloc[0])
        assert m_sum == pytest.approx(y_val, rel=1e-6)


def test_monthly_net_includes_ppa():
    econ = _econ()
    cf = build_yearly_cashflow(_year1(8000.0, 4000.0), econ, _CAPACITIES)
    mcf, _ = derive_monthly_cashflow(_synthetic_res(), cf, econ)
    sub = mcf[mcf.project_year == 1]
    expected = (
        sub["revenue_eur"] + sub["balancing_revenue_eur"]
        + sub["ppa_revenue_eur"] + sub["opex_eur"]
    )
    assert np.allclose(sub["net_cashflow_eur"].to_numpy(), expected.to_numpy())


# ---------------------------------------------------------------------------
# Sensitivity Revenue driver scales PPA
# ---------------------------------------------------------------------------


def test_scale_revenue_unity_is_noop_with_ppa():
    econ = _econ()
    cf = build_yearly_cashflow(_year1(8000.0, 4000.0), econ, _CAPACITIES)
    scaled = _scale_revenue(cf, 1.0)
    assert np.allclose(
        scaled["ppa_revenue_eur"].to_numpy(), cf["ppa_revenue_eur"].to_numpy(),
    )
    assert np.allclose(
        scaled["net_cashflow_eur"].to_numpy(),
        cf["net_cashflow_eur"].to_numpy(),
    )


def test_scale_revenue_scales_ppa():
    econ = _econ()
    cf = build_yearly_cashflow(_year1(8000.0, 4000.0), econ, _CAPACITIES)
    scaled = _scale_revenue(cf, 1.2)
    assert np.allclose(
        scaled["ppa_revenue_eur"].to_numpy(),
        cf["ppa_revenue_eur"].to_numpy() * 1.2,
    )


def test_recompute_net_includes_ppa():
    econ = _econ()
    cf = build_yearly_cashflow(_year1(8000.0, 4000.0), econ, _CAPACITIES)
    cf2 = cf.copy()
    cf2["ppa_revenue_eur"] = cf2["ppa_revenue_eur"] + 1000.0
    cf2 = _recompute_net(cf2)
    # Each operating year's net grew by exactly the +1000 PPA bump.
    op = cf["project_year"] >= 1
    delta = (
        cf2.loc[op, "net_cashflow_eur"].to_numpy()
        - cf.loc[op, "net_cashflow_eur"].to_numpy()
    )
    assert np.allclose(delta, 1000.0)


def test_revenue_sensitivity_moves_npv_with_ppa():
    econ = _econ()
    _, base_kpis = _fin_kpis(econ, _year1(8000.0, 4000.0))
    sens = run_sensitivity_analysis(
        _year1(8000.0, 4000.0), econ, _CAPACITIES, base_kpis,
    )
    rev = sens[sens.variable == "Revenue"]
    lo = float(rev[rev.scenario == "low"].npv_eur.iloc[0])
    hi = float(rev[rev.scenario == "high"].npv_eur.iloc[0])
    assert lo != hi
    assert hi > lo  # +revenue raises NPV
