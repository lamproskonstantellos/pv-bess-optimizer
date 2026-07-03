"""PPA contract engine — cent-level locks (docs/ppa_design.md).

By-hand fixtures pin both settlements exactly:

    pv_to_grid = [10, 8] kWh, DAM = [50, -20] EUR/MWh, strike = 65,
    share = 50 %:

    physical:  revenue_pv_ppa = 0.5·(10+8)/1000·65            = 0.585
               profit_export_from_pv = 0.5·(10·50 + 8·(-20))/1000 = 0.17
    cfd:       revenue_pv_ppa = 0.5·(10·(65-50) + 8·(65+20))/1000 = 0.415
               profit_export_from_pv = (10·50 + 8·(-20))/1000     = 0.34
    identity:  0.585 + 0.17 == 0.415 + 0.34 == 0.755 (= covered·strike
               + uncovered·DAM).

Plus: the CfD leg goes negative when DAM > strike; the MILP keeps the
covered share exporting through negative-DAM hours while uncovered PV
curtails; the cashflow escalates the strike leg on ppa_inflation_pct,
ends the stream after the term, and reverts the covered volume to the
DAM stream under physical settlement; the availability derate scales
the PPA keys; and a disabled contract leaves every output bit-identical.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.availability import apply_unavailability_derate
from pvbess_opt.economics import build_yearly_cashflow
from pvbess_opt.kpis import add_economic_columns, compute_kpis
from pvbess_opt.optimization import run_scenario
from pvbess_opt.ppa import PpaConfig, resolve_ppa_config

SOLVER_KW = {"solver_name": "highs", "mip_gap": 0.0, "time_limit_seconds": 120}


def _ppa(**overrides) -> dict:
    cfg = {
        "ppa_enabled": True,
        "ppa_structure": "pay_as_produced",
        "ppa_settlement": "physical",
        "ppa_price_eur_per_mwh": 65.0,
        "ppa_volume_share_pct": 50.0,
        "ppa_term_years": 10,
        "ppa_inflation_pct": 0.0,
    }
    cfg.update(overrides)
    return cfg


def _frame() -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=2, freq="h"),
        "pv_to_load_kwh": [0.0, 0.0],
        "bess_dis_load_kwh": [0.0, 0.0],
        "pv_to_grid_kwh": [10.0, 8.0],
        "bess_dis_grid_kwh": [0.0, 0.0],
        "bess_charge_grid_kwh": [0.0, 0.0],
        "dam_price_eur_per_mwh": [50.0, -20.0],
    })


def _params(settlement: str = "physical", **ppa_overrides) -> dict:
    return {
        "retail_tariff_eur_per_mwh": 0.0,
        "ppa": _ppa(ppa_settlement=settlement, **ppa_overrides),
    }


# ---------------------------------------------------------------------------
# Per-step columns — cent level, including the negative-DAM hour
# ---------------------------------------------------------------------------


def test_physical_settlement_per_step_columns():
    res = add_economic_columns(_frame(), _params("physical"))
    assert float(res["revenue_pv_ppa_eur"].sum()) == pytest.approx(0.585)
    assert float(res["profit_export_from_pv_eur"].sum()) == pytest.approx(0.17)
    # covered DAM value carries the negative hour's sign
    assert float(res["ppa_covered_dam_value_eur"].sum()) == pytest.approx(0.17)
    assert float(res["ppa_covered_dam_value_eur"].iloc[1]) == pytest.approx(
        0.5 * 8.0 / 1000.0 * -20.0,
    )


def test_cfd_settlement_per_step_columns():
    res = add_economic_columns(_frame(), _params("cfd"))
    assert float(res["revenue_pv_ppa_eur"].sum()) == pytest.approx(0.415)
    # CfD leaves the full market leg in place.
    assert float(res["profit_export_from_pv_eur"].sum()) == pytest.approx(0.34)


def test_cfd_leg_negative_when_dam_above_strike():
    frame = _frame()
    frame["dam_price_eur_per_mwh"] = [100.0, 100.0]  # DAM > strike = 65
    res = add_economic_columns(frame, _params("cfd"))
    assert float(res["revenue_pv_ppa_eur"].sum()) == pytest.approx(
        0.5 * 18.0 / 1000.0 * (65.0 - 100.0),
    )
    assert float(res["revenue_pv_ppa_eur"].sum()) < 0.0


def test_settlements_total_identically():
    """physical contract+market == cfd contract+market == covered·strike
    + uncovered·DAM, per the two-way CfD equivalence."""
    phys = add_economic_columns(_frame(), _params("physical"))
    cfd = add_economic_columns(_frame(), _params("cfd"))
    total_phys = float(
        (phys["revenue_pv_ppa_eur"] + phys["profit_export_from_pv_eur"]).sum()
    )
    total_cfd = float(
        (cfd["revenue_pv_ppa_eur"] + cfd["profit_export_from_pv_eur"]).sum()
    )
    assert total_phys == pytest.approx(0.755)
    assert total_cfd == pytest.approx(total_phys)


def test_disabled_contract_leaves_frame_bit_identical():
    plain = add_economic_columns(_frame(), {"retail_tariff_eur_per_mwh": 0.0})
    off = add_economic_columns(
        _frame(), _params("physical", ppa_enabled=False),
    )
    pd.testing.assert_frame_equal(plain, off)
    assert "revenue_pv_ppa_eur" not in off.columns
    assert "ppa_covered_dam_value_eur" not in off.columns


def test_compute_kpis_folds_ppa_into_profit_total():
    frame = _frame()
    frame["timestamp"] = pd.date_range("2026-06-01", periods=2, freq="h")
    frame["pv_kwh"] = [10.0, 8.0]
    frame["load_kwh"] = [0.0, 0.0]
    frame["pv_to_bess_kwh"] = [0.0, 0.0]
    frame["pv_curtail_kwh"] = [0.0, 0.0]
    frame["grid_to_load_kwh"] = [0.0, 0.0]
    frame["soc_kwh"] = [0.0, 0.0]
    params = {
        "mode": "merchant",
        "efficiency_charge": 1.0,
        "efficiency_discharge": 1.0,
        "initial_soc_frac": 0.0,
        "bess_capacity_kwh": 0.0,
        "retail_tariff_eur_per_mwh": 0.0,
        "ppa": _ppa(ppa_settlement="physical"),
    }
    kpis = compute_kpis(frame, params, verify_balance=False)
    assert kpis["revenue_pv_ppa_eur"] == pytest.approx(0.585, abs=0.006)
    assert kpis["profit_total_eur"] == pytest.approx(0.755, abs=0.006)


def test_availability_derate_scales_ppa_keys():
    kpis = {
        "revenue_pv_ppa_eur": 100.0,
        "ppa_covered_dam_value_eur": 80.0,
        "profit_total_eur": 500.0,
    }
    derated = apply_unavailability_derate(kpis, 10.0)
    assert derated["revenue_pv_ppa_eur"] == pytest.approx(90.0)
    assert derated["ppa_covered_dam_value_eur"] == pytest.approx(72.0)


# ---------------------------------------------------------------------------
# Dispatch: covered PV exports through negative-DAM hours
# ---------------------------------------------------------------------------


def _negative_dam_params(share_pct: float) -> tuple[dict, pd.DataFrame]:
    params = {
        "dt_minutes": 60,
        "efficiency_charge": 1.0,
        "efficiency_discharge": 1.0,
        "soc_min_frac": 0.0,
        "soc_max_frac": 1.0,
        "initial_soc_frac": 0.0,
        "terminal_soc_equal": False,
        "max_cycles_per_day": 10.0,
        "p_grid_export_max_kw": 50.0,
        "pv_nameplate_kwp": 100.0,
        "bess_power_kw": 0.0,
        "bess_capacity_kwh": 0.0,
        "retail_tariff_eur_per_mwh": 0.0,
        "mode": "merchant",
        "allow_bess_grid_charging": False,
        "show_titles": False,
        "ppa": _ppa(ppa_volume_share_pct=share_pct),
    }
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=2, freq="h"),
        "pv_kwh": [10.0, 10.0],
        "load_kwh": [0.0, 0.0],
        "dam_price_eur_per_mwh": [-20.0, -20.0],
    })
    return params, ts


def test_fully_covered_pv_exports_at_negative_dam():
    """share = 100 %, strike 65: effective price +65 => export, no curtail
    (the documented as-produced dispatch distortion)."""
    params, ts = _negative_dam_params(100.0)
    res, _ = run_scenario(params, ts, **SOLVER_KW)
    assert float(res["pv_to_grid_kwh"].sum()) == pytest.approx(20.0, abs=1e-6)
    assert float(res["pv_curtail_kwh"].sum()) == pytest.approx(0.0, abs=1e-6)


def test_uncovered_pv_curtails_at_negative_dam():
    """share = 0 (contract disabled): exporting at -20 loses money =>
    everything curtails."""
    params, ts = _negative_dam_params(100.0)
    params["ppa"] = _ppa(ppa_enabled=False)
    res, _ = run_scenario(params, ts, **SOLVER_KW)
    assert float(res["pv_to_grid_kwh"].sum()) == pytest.approx(0.0, abs=1e-6)
    assert float(res["pv_curtail_kwh"].sum()) == pytest.approx(20.0, abs=1e-6)


def test_partial_cover_exports_exactly_the_covered_share():
    """share = 40 %, strike 65, DAM -20: effective export price
    0.6·(-20) + 0.4·65 = +14 > 0 => the share applies pro-rata, so ALL
    PV exports (the blended price is positive)."""
    params, ts = _negative_dam_params(40.0)
    res, _ = run_scenario(params, ts, **SOLVER_KW)
    assert float(res["pv_to_grid_kwh"].sum()) == pytest.approx(20.0, abs=1e-6)


def test_partial_cover_curtails_when_blend_negative():
    """share = 20 %, strike 65, DAM -20: blend 0.8·(-20)+0.2·65 = -3 < 0
    => curtail everything."""
    params, ts = _negative_dam_params(20.0)
    res, _ = run_scenario(params, ts, **SOLVER_KW)
    assert float(res["pv_to_grid_kwh"].sum()) == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Cashflow: escalation, term, post-term reversion
# ---------------------------------------------------------------------------


def _econ(settlement: str, **overrides) -> dict:
    out = {
        "project_lifecycle_years": 4,
        "project_start_year": 2026,
        "discount_rate_pct": 10.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 0.0,
        "capex_bess_eur_per_kwh": 0.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 0.0,
        "opex_bess_eur_per_kw": 0.0,
        "pv_degradation_year1_pct": 0.0,
        "pv_degradation_annual_pct": 0.0,
        "bess_degradation_annual_pct": 0.0,
        "bess_degradation_pct_per_cycle": 0.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
        "ppa_enabled": True,
        "ppa_settlement": settlement,
        "ppa_term_years": 2,
        "ppa_inflation_pct": 0.0,
    }
    out.update(overrides)
    return out


def _caps() -> dict:
    return {"pv_kwp": 100.0, "bess_kw": 0.0, "bess_kwh": 0.0}


def _year1_kpis(settlement: str) -> dict:
    """Year-1 KPI bases from the 2-step fixture, annual-scale x1000."""
    rev_ppa = 585.0 if settlement == "physical" else 415.0
    export_pv = 170.0 if settlement == "physical" else 340.0
    return {
        "profit_total_eur": export_pv + rev_ppa,
        "profit_load_from_pv_eur": 0.0,
        "profit_load_from_bess_eur": 0.0,
        "profit_export_from_pv_eur": export_pv,
        "profit_export_from_bess_eur": 0.0,
        "expense_charge_bess_grid_eur": 0.0,
        "revenue_pv_ppa_eur": rev_ppa,
        "ppa_covered_dam_value_eur": 170.0,
        "bess_total_discharge_mwh": 0.0,
    }


def test_physical_cashflow_term_and_reversion():
    """Term = 2 of 4 years: y1-y2 carry the contract leg with the DAM
    stream holding only the uncovered share; y3-y4 zero the contract leg
    and the covered volume's DAM value rejoins the DAM stream."""
    cf = build_yearly_cashflow(_year1_kpis("physical"), _econ("physical"), _caps())
    op = cf[cf["project_year"] >= 1].set_index("project_year")
    assert float(op.loc[1, "ppa_revenue_eur"]) == pytest.approx(585.0)
    assert float(op.loc[2, "ppa_revenue_eur"]) == pytest.approx(585.0)
    assert float(op.loc[1, "revenue_dam_eur"]) == pytest.approx(170.0)
    assert float(op.loc[3, "ppa_revenue_eur"]) == 0.0
    assert float(op.loc[4, "ppa_revenue_eur"]) == 0.0
    # post-term DAM stream = uncovered 170 + reverted covered 170 = 340.
    assert float(op.loc[3, "revenue_dam_eur"]) == pytest.approx(340.0)
    assert float(op.loc[4, "revenue_dam_eur"]) == pytest.approx(340.0)
    # In-term totals carry the strike premium (755); post-term the
    # covered volume reverts to its DAM value, so totals drop to 340 —
    # the cliff is real revenue economics, not an accounting gap.
    totals = op["revenue_eur"] + op["ppa_revenue_eur"]
    assert np.allclose(totals.loc[[1, 2]], 755.0)
    assert np.allclose(totals.loc[[3, 4]], 340.0)


def test_cfd_cashflow_term_zeroes_the_leg_only():
    cf = build_yearly_cashflow(_year1_kpis("cfd"), _econ("cfd"), _caps())
    op = cf[cf["project_year"] >= 1].set_index("project_year")
    assert float(op.loc[1, "ppa_revenue_eur"]) == pytest.approx(415.0)
    assert float(op.loc[2, "ppa_revenue_eur"]) == pytest.approx(415.0)
    # The market leg holds the FULL export every year.
    assert np.allclose(op["revenue_dam_eur"], 340.0)
    assert float(op.loc[3, "ppa_revenue_eur"]) == 0.0
    # In-term totals match physical; post-term reverts to pure DAM.
    assert float(op.loc[1, "revenue_eur"] + op.loc[1, "ppa_revenue_eur"]) == (
        pytest.approx(755.0)
    )
    assert float(op.loc[3, "revenue_eur"]) == pytest.approx(340.0)


def test_ppa_inflation_escalates_the_strike_leg_only():
    """ppa_inflation 10 %: physical year-2 leg = 585 · 1.1; the CfD leg
    escalates its strike part only (DAM part stays at dam_inflation)."""
    cf_phys = build_yearly_cashflow(
        _year1_kpis("physical"),
        _econ("physical", ppa_inflation_pct=10.0),
        _caps(),
    )
    op = cf_phys[cf_phys["project_year"] >= 1].set_index("project_year")
    assert float(op.loc[2, "ppa_revenue_eur"]) == pytest.approx(585.0 * 1.1)

    cf_cfd = build_yearly_cashflow(
        _year1_kpis("cfd"),
        _econ("cfd", ppa_inflation_pct=10.0),
        _caps(),
    )
    opc = cf_cfd[cf_cfd["project_year"] >= 1].set_index("project_year")
    # strike leg y2 = (415 + 170) · 1.1; DAM leg unescalated = 170.
    assert float(opc.loc[2, "ppa_revenue_eur"]) == pytest.approx(
        585.0 * 1.1 - 170.0,
    )


def test_aggregator_fee_never_touches_the_ppa_stream():
    econ = _econ("physical", aggregator_fee_pct_revenue=10.0)
    cf = build_yearly_cashflow(_year1_kpis("physical"), econ, _caps())
    op = cf[cf["project_year"] >= 1].set_index("project_year")
    # In-term: fee on the uncovered DAM gross only (170 -> 17).
    assert float(op.loc[1, "aggregator_fee_eur"]) == pytest.approx(-17.0)
    assert float(op.loc[1, "ppa_revenue_eur"]) == pytest.approx(585.0)
    # Post-term: the reverted covered volume is market revenue -> fee on
    # the full 340 gross.
    assert float(op.loc[3, "aggregator_fee_eur"]) == pytest.approx(-34.0)


def test_ppa_disabled_cashflow_has_zero_stream():
    econ = _econ("physical", ppa_enabled=False)
    kpis = _year1_kpis("physical")
    kpis["revenue_pv_ppa_eur"] = 0.0
    kpis["ppa_covered_dam_value_eur"] = 0.0
    kpis["profit_total_eur"] = kpis["profit_export_from_pv_eur"]
    cf = build_yearly_cashflow(kpis, econ, _caps())
    assert float(cf["ppa_revenue_eur"].abs().sum()) == 0.0


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def test_resolve_ppa_config_defaults_and_share():
    assert resolve_ppa_config(None) == PpaConfig()
    assert not resolve_ppa_config(None).active
    cfg = resolve_ppa_config(_ppa(ppa_volume_share_pct=37.5))
    assert cfg.active
    assert cfg.share_frac == pytest.approx(0.375)
    off = resolve_ppa_config(_ppa(ppa_enabled=False))
    assert off.share_frac == 0.0
