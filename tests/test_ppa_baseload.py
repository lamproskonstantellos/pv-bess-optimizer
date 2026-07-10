"""Baseload PPA structure (Eqs. P9-P11, E45).

A contracted flat band ``ppa_baseload_mw`` settles a fixed per-step
volume financially against the plant's total export: shortfall is
implicitly bought at spot, excess sold at spot, which under symmetric
settlement is IDENTICAL to the net leg ``Q x (strike - DAM)`` on top
of full merchant revenue.  Locked here: the cent-level P9/P10
settlement and the buy/sell identity, dt-honouring band sizing, the
P11 dispatch-neutrality lock at the solver level, the E45 no-fade /
no-reversion yearly stream, the availability and lifetime
classifications of the fixed-volume leg, monthly reconciliation with
a mixed-sign leg, the PpaPrice tornado reconstruction, the suspension
interaction and zero-default bit-identity.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.availability import apply_unavailability_derate
from pvbess_opt.economics import build_yearly_cashflow
from pvbess_opt.kpis import add_economic_columns
from pvbess_opt.ppa import PpaConfig, resolve_ppa_config


def _highs_available() -> bool:
    try:
        import highspy
    except ImportError:
        return False
    return bool(highspy)


def _ppa(**overrides) -> dict:
    cfg = {
        "ppa_enabled": True,
        "ppa_structure": "baseload",
        "ppa_settlement": "cfd",
        "ppa_price_eur_per_mwh": 65.0,
        "ppa_volume_share_pct": 100.0,
        "ppa_term_years": 10,
        "ppa_inflation_pct": 0.0,
        "ppa_baseload_mw": 5.0,
    }
    cfg.update(overrides)
    return cfg


def _frame() -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=3, freq="h"),
        "pv_to_load_kwh": [0.0, 0.0, 0.0],
        "bess_dis_load_kwh": [0.0, 0.0, 0.0],
        "pv_to_grid_kwh": [8_000.0, 2_000.0, 0.0],
        "bess_dis_grid_kwh": [0.0, 1_000.0, 0.0],
        "bess_charge_grid_kwh": [0.0, 0.0, 0.0],
        "dam_price_eur_per_mwh": [50.0, 90.0, -20.0],
    })


def _params(**ppa_overrides) -> dict:
    return {
        "retail_tariff_eur_per_mwh": 0.0,
        "dt_minutes": 60,
        "ppa": _ppa(**ppa_overrides),
    }


# ---------------------------------------------------------------------------
# Config surface
# ---------------------------------------------------------------------------


def test_config_properties():
    cfg = resolve_ppa_config(_ppa())
    assert cfg.active
    assert cfg.share_frac == 0.0            # the band is absolute
    assert not cfg.reshapes_dispatch_price  # P11: dispatch-neutral
    assert resolve_ppa_config(_ppa(ppa_baseload_mw=0.0)).active is False
    pap = PpaConfig(ppa_enabled=True, ppa_volume_share_pct=50.0)
    assert pap.reshapes_dispatch_price      # pay_as_produced still does


# ---------------------------------------------------------------------------
# P9/P10 settlement, cent level
# ---------------------------------------------------------------------------


def test_p9_settlement_per_step_columns():
    res = add_economic_columns(_frame(), _params())
    # Q = 5 MW x 1 h = 5 MWh per step; leg = Q x (strike - DAM).
    assert res["revenue_pv_ppa_eur"].tolist() == pytest.approx(
        [5.0 * 15.0, 5.0 * -25.0, 5.0 * 85.0],
    )
    assert res["ppa_covered_dam_value_eur"].tolist() == pytest.approx(
        [5.0 * 50.0, 5.0 * 90.0, 5.0 * -20.0],
    )
    # Market columns untouched: all export still sells at DAM.
    assert res["profit_export_from_pv_eur"].tolist() == pytest.approx(
        [8.0 * 50.0, 2.0 * 90.0, 0.0],
    )
    assert res["profit_export_from_bess_eur"].tolist() == pytest.approx(
        [0.0, 1.0 * 90.0, 0.0],
    )


def test_p9_buy_shortfall_sell_excess_identity():
    """Net-leg form == explicit buy-shortfall/sell-excess settlement:
    Q*strike + (delivered - Q)*DAM == delivered*DAM + Q*(strike-DAM)."""
    res = add_economic_columns(_frame(), _params())
    delivered_mwh = (
        res["pv_to_grid_kwh"] + res["bess_dis_grid_kwh"]
    ) / 1000.0
    dam = res["dam_price_eur_per_mwh"].astype(float)
    strike = 65.0
    q = 5.0
    explicit = q * strike + (delivered_mwh - q) * dam
    net_form = (
        delivered_mwh * dam
        + res["revenue_pv_ppa_eur"].astype(float)
    )
    assert net_form.to_numpy() == pytest.approx(explicit.to_numpy())


def test_p10_shortfall_excess_split():
    res = add_economic_columns(_frame(), _params())
    # Q = 5000 kWh vs delivered [8000, 3000, 0].
    assert res["ppa_baseload_excess_kwh"].tolist() == pytest.approx(
        [3_000.0, 0.0, 0.0],
    )
    assert res["ppa_baseload_shortfall_kwh"].tolist() == pytest.approx(
        [0.0, 2_000.0, 5_000.0],
    )


def test_band_honours_step_length():
    """15-min steps: Q_t = MW x 0.25 h — a hardcoded 1 h would
    mis-size the band fourfold."""
    frame = _frame()
    frame["timestamp"] = pd.date_range(
        "2026-06-01", periods=3, freq="15min",
    )
    params = _params()
    params["dt_minutes"] = 15
    res = add_economic_columns(frame, params)
    assert res["revenue_pv_ppa_eur"].iloc[0] == pytest.approx(
        5.0 * 0.25 * (65.0 - 50.0),
    )
    assert res["ppa_baseload_shortfall_kwh"].iloc[2] == pytest.approx(
        5.0 * 0.25 * 1000.0,
    )


def test_suspension_masks_negative_hours_only():
    res = add_economic_columns(
        _frame(), _params(ppa_negative_price_rule="suspend"),
    )
    # Step 3 (DAM = -20) is suspended: the leg and its DAM shadow are 0.
    assert res["revenue_pv_ppa_eur"].tolist() == pytest.approx(
        [5.0 * 15.0, 5.0 * -25.0, 0.0],
    )
    assert res["ppa_covered_dam_value_eur"].iloc[2] == 0.0
    # Diagnostics stay physical (coverage is about energy, not the
    # settlement mask).
    assert res["ppa_baseload_shortfall_kwh"].iloc[2] == pytest.approx(
        5_000.0,
    )


def test_zero_band_bit_identical():
    plain = add_economic_columns(
        _frame(), {"retail_tariff_eur_per_mwh": 0.0, "dt_minutes": 60},
    )
    off = add_economic_columns(_frame(), _params(ppa_baseload_mw=0.0))
    pd.testing.assert_frame_equal(plain, off)
    assert "ppa_baseload_shortfall_kwh" not in off.columns


# ---------------------------------------------------------------------------
# E45 yearly stream: no fade, term cutoff, no reversion
# ---------------------------------------------------------------------------

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
        "ppa_enabled": True,
        "ppa_structure": "baseload",
        "ppa_settlement": "cfd",
        "ppa_term_years": 5,
        "ppa_inflation_pct": 2.0,
        "ppa_baseload_mw": 5.0,
    }
    econ.update(o)
    return econ


def _kpis(**o) -> dict:
    kpis = {
        "profit_load_from_pv_eur": 60_000.0,
        "profit_load_from_bess_eur": 10_000.0,
        "profit_export_from_pv_eur": 40_000.0,
        "profit_export_from_bess_eur": 40_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 165_000.0,  # incl. the 20k cfd leg below
        # Year-1 baseload leg: strike leg 90k, DAM shadow 70k
        # => net cfd leg 20k.
        "revenue_pv_ppa_eur": 20_000.0,
        "ppa_covered_dam_value_eur": 70_000.0,
        "ppa_baseload_shortfall_mwh": 120.0,
        "ppa_baseload_excess_mwh": 300.0,
    }
    kpis.update(o)
    return kpis


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}


def test_e45_no_fade_term_cutoff_no_reversion():
    econ = _econ(dam_inflation_pct=1.0)
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    op = cf.set_index("project_year")
    strike_1 = 20_000.0 + 70_000.0
    for y in range(1, 6):
        expected = (
            strike_1 * 1.02 ** (y - 1)
            - 70_000.0 * 1.01 ** (y - 1)
        )
        assert float(op.loc[y, "ppa_revenue_eur"]) == pytest.approx(
            expected, abs=0.01,
        ), y
    # Term cutoff: nothing after year 5, and NO reversion into the DAM
    # stream (cfd: nothing was sleeved).
    for y in range(6, N_YEARS + 1):
        assert float(op.loc[y, "ppa_revenue_eur"]) == 0.0
    # The no-fade lock: the year-5 leg exceeds a PV-faded counterpart.
    faded_leg = (
        strike_1 * (0.98 * 0.99 ** 3) * 1.02 ** 4
        - 70_000.0 * (0.98 * 0.99 ** 3) * 1.01 ** 4
    )
    assert float(op.loc[5, "ppa_revenue_eur"]) > faded_leg


def test_monthly_reconciles_mixed_sign_leg():
    from pvbess_opt.economics import derive_monthly_cashflow

    econ = _econ(dam_inflation_pct=8.0)  # leg flips sign mid-term
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    res = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=8760, freq="h"),
        "profit_load_from_pv_eur": np.full(8760, 60_000.0 / 8760),
        "profit_load_from_bess_eur": np.full(8760, 10_000.0 / 8760),
        "profit_export_from_pv_eur": np.full(8760, 40_000.0 / 8760),
        "profit_export_from_bess_eur": np.full(8760, 40_000.0 / 8760),
        "expense_charge_bess_grid_eur": np.full(8760, 5_000.0 / 8760),
        "revenue_pv_ppa_eur": np.full(8760, 20_000.0 / 8760),
        "ppa_covered_dam_value_eur": np.full(8760, 70_000.0 / 8760),
    })
    monthly, _q = derive_monthly_cashflow(res, cf, econ)
    yearly_ppa = cf.set_index("project_year")["ppa_revenue_eur"]
    monthly_ppa = monthly.groupby("project_year")["ppa_revenue_eur"].sum()
    for y in range(1, N_YEARS + 1):
        assert monthly_ppa.loc[y] == pytest.approx(
            float(yearly_ppa.loc[y]), abs=0.01,
        ), y


# ---------------------------------------------------------------------------
# Availability + lifetime classification
# ---------------------------------------------------------------------------


def test_baseload_leg_not_derated():
    kpis = _kpis(pv_generation_mwh=7_000.0)
    derated = apply_unavailability_derate(kpis, 10.0)
    # The fixed-volume leg is production-decoupled: untouched.
    assert derated["revenue_pv_ppa_eur"] == 20_000.0
    assert derated["ppa_covered_dam_value_eur"] == 70_000.0
    # Diagnostics stay raw.
    assert derated["ppa_baseload_shortfall_mwh"] == 120.0
    assert derated["ppa_baseload_excess_mwh"] == 300.0
    # Production keys still derate.
    assert derated["pv_generation_mwh"] == pytest.approx(6_300.0)
    # The pay-as-produced leg (no marker key) keeps deratng.
    pap = dict(_kpis())
    pap.pop("ppa_baseload_shortfall_mwh")
    pap.pop("ppa_baseload_excess_mwh")
    derated_pap = apply_unavailability_derate(pap, 10.0)
    assert derated_pap["revenue_pv_ppa_eur"] == pytest.approx(18_000.0)


def test_lifetime_frame_skips_pv_fade_on_baseload_leg():
    from pvbess_opt.lifetime import build_lifetime_dispatch

    res = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=48, freq="h"),
        "pv_kwh": np.full(48, 100.0),
        "pv_to_grid_kwh": np.full(48, 80.0),
        "pv_to_load_kwh": np.full(48, 0.0),
        "pv_to_bess_kwh": np.full(48, 0.0),
        "pv_curtail_kwh": np.full(48, 0.0),
        "bess_dis_grid_kwh": np.full(48, 0.0),
        "bess_dis_load_kwh": np.full(48, 0.0),
        "bess_charge_grid_kwh": np.full(48, 0.0),
        "revenue_pv_ppa_eur": np.full(48, 10.0),
        "ppa_covered_dam_value_eur": np.full(48, 6.0),
        "profit_export_from_pv_eur": np.full(48, 4.0),
    })
    base_kwargs = dict(capacities={"pv_kwp": 100.0, "bess_kw": 0.0,
                                   "bess_kwh": 0.0})
    life_bl = build_lifetime_dispatch(
        res, _econ(project_lifecycle_years=3), **base_kwargs,
    )
    life_pap = build_lifetime_dispatch(
        res, _econ(project_lifecycle_years=3,
                   ppa_structure="pay_as_produced",
                   ppa_settlement="physical"),
        **base_kwargs,
    )
    y3_bl = life_bl.loc[life_bl["project_year"] == 3,
                        "revenue_pv_ppa_eur"].sum()
    y3_pap = life_pap.loc[life_pap["project_year"] == 3,
                          "revenue_pv_ppa_eur"].sum()
    y1_bl = life_bl.loc[life_bl["project_year"] == 1,
                        "revenue_pv_ppa_eur"].sum()
    # Baseload: no PV fade — year 3 equals year 1; pay_as_produced fades.
    assert y3_bl == pytest.approx(y1_bl)
    assert y3_pap < y3_bl
    # Market revenue still fades in both.
    m3 = life_bl.loc[life_bl["project_year"] == 3,
                     "profit_export_from_pv_eur"].sum()
    m1 = life_bl.loc[life_bl["project_year"] == 1,
                     "profit_export_from_pv_eur"].sum()
    assert m3 < m1


# ---------------------------------------------------------------------------
# Tornado interaction
# ---------------------------------------------------------------------------


def test_ppa_price_driver_reconstructs_baseload_strike_leg():
    """The cfd strike-leg reconstruction (rev1 + covered_dam ==
    strike x Q-volume) is exact for baseload, so the PpaPrice driver
    is present and strictly monotonic."""
    from pvbess_opt.economics import compute_financial_kpis
    from pvbess_opt.sensitivity import run_sensitivity_analysis

    econ = _econ(sensitivity_enabled=True, ppa_price_eur_per_mwh=65.0,
                 sensitivity_ppa_price_delta_pct=20.0)
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    fin = compute_financial_kpis(cf, econ)
    sens = run_sensitivity_analysis(_kpis(), econ, _caps(), fin)
    rows = sens[sens["variable"] == "PpaPrice"].set_index("scenario")
    assert {"base", "low", "high"} <= set(rows.index)
    assert float(rows.loc["high", "npv_eur"]) > float(fin["npv_eur"])
    assert float(rows.loc["low", "npv_eur"]) < float(fin["npv_eur"])


# ---------------------------------------------------------------------------
# P11 dispatch neutrality at the solver level
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _highs_available(), reason="requires HiGHS")
def test_dispatch_neutrality_lock():
    from pvbess_opt.optimization import run_scenario

    n = 24
    rng = np.arange(n)
    params = {
        "mode": "merchant",
        "dt_minutes": 60,
        "pv_nameplate_kwp": 1000.0,
        "bess_power_kw": 500.0,
        "bess_capacity_kwh": 1000.0,
        "efficiency_charge": 0.95,
        "efficiency_discharge": 0.95,
        "soc_min_frac": 0.1,
        "soc_max_frac": 0.95,
        "initial_soc_frac": 0.5,
        "max_cycles_per_day": 2.0,
        "allow_bess_grid_charging": True,
        "p_grid_export_max_kw": 2000.0,
        "p_grid_import_max_kw": 2000.0,
    }
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=n, freq="h"),
        "pv_kwh": np.where((rng >= 7) & (rng <= 17), 800.0, 0.0),
        "load_kwh": np.zeros(n),
        "dam_price_eur_per_mwh": 60.0 + 50.0 * np.sin(
            rng * 2.0 * np.pi / 24.0,
        ),
    })
    res_off, _s1, _f1 = run_scenario(
        dict(params), ts.copy(), return_unrounded=True,
    )
    res_on, _s2, _f2 = run_scenario(
        dict(params, ppa=_ppa()), ts.copy(), return_unrounded=True,
    )
    dispatch_cols = [
        c for c in res_off.columns
        if c.endswith("_kwh") or c == "soc_kwh"
    ]
    pd.testing.assert_frame_equal(
        res_on[dispatch_cols], res_off[dispatch_cols],
    )
    # Suspension x baseload hardening: the masked leg is STILL
    # variable-free, so the neutrality lock holds through negative-DAM
    # hours with the clause on.
    ts_neg = ts.copy()
    ts_neg.loc[2:5, "dam_price_eur_per_mwh"] = -40.0
    res_off_neg, _s3, _f3 = run_scenario(
        dict(params), ts_neg.copy(), return_unrounded=True,
    )
    res_susp, _s4, _f4 = run_scenario(
        dict(params, ppa=_ppa(ppa_negative_price_rule="suspend")),
        ts_neg.copy(), return_unrounded=True,
    )
    pd.testing.assert_frame_equal(
        res_susp[dispatch_cols], res_off_neg[dispatch_cols],
    )
