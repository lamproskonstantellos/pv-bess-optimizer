"""Structural market-access fees: route-to-market (E13c) + optimizer (E13d).

Locks the two fee columns' algebra against ``docs/economics_design.md``:

* E13c: ``route_to_market_fee_eur = -rate x (pv_export_1 x f_pv x
  (1 - s_ppa_in_term) + bess_export_1 x f_bess)`` — flat rate, exported
  MWh fading per origin, PPA-covered PV share exempt while a physical
  (sleeved) contract is in term, full base post-term and under CfD.
* E13d: ``optimizer_fee_eur = -share x max(rev1_dam_bess x f_bess x
  (1+i_dam)^(y-1), 0)`` — never a share of a trading loss.
* Zero-default bit-identity, net-cashflow identity, monthly
  reconciliation, sensitivity list guard, and the stacking warning.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import (
    build_yearly_cashflow,
    compute_financial_kpis,
    derive_monthly_cashflow,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RTM_RATE = 2.0
OPT_SHARE_PCT = 15.0


def _econ(**overrides) -> dict:
    econ = {
        "project_lifecycle_years": 6,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 2.0,
        "capex_pv_eur_per_kw": 500.0,
        "capex_bess_eur_per_kwh": 100.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 5.0,
        "opex_bess_eur_per_kw": 5.0,
        "pv_degradation_year1_pct": 2.0,
        "pv_degradation_annual_pct": 1.0,
        "bess_degradation_annual_pct": 3.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
        "route_to_market_fee_eur_per_mwh": RTM_RATE,
        "optimizer_revenue_share_pct": OPT_SHARE_PCT,
    }
    econ.update(overrides)
    return econ


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}


def _kpis_negative_margin() -> dict:
    """Self-consumption-style year: grid charging exceeds BESS export."""
    return {
        "profit_load_from_pv_eur": 100_000.0,
        "profit_load_from_bess_eur": 30_000.0,
        "profit_export_from_pv_eur": 8_000.0,
        "profit_export_from_bess_eur": 28_000.0,
        "expense_charge_bess_grid_eur": 30_000.0,
        "profit_total_eur": 136_000.0,
        "pv_export_mwh": 100.0,
        "bess_export_mwh": 400.0,
    }


def _kpis_positive_margin() -> dict:
    """Merchant-style year: the battery earns a positive trading margin."""
    k = _kpis_negative_margin()
    k["profit_export_from_bess_eur"] = 50_000.0
    k["expense_charge_bess_grid_eur"] = 0.0
    k["profit_total_eur"] = 188_000.0
    return k


def _op(cf: pd.DataFrame) -> pd.DataFrame:
    return cf[cf["project_year"] >= 1].set_index("project_year")


# ---------------------------------------------------------------------------
# E13c — route-to-market fee
# ---------------------------------------------------------------------------


def test_rtm_fee_follows_per_origin_degradation():
    op = _op(build_yearly_cashflow(_kpis_negative_margin(), _econ(), _caps()))
    for y in op.index:
        f_pv = float(op.loc[y, "pv_production_factor"])
        f_b = float(op.loc[y, "bess_capacity_factor"])
        expected = -RTM_RATE * (100.0 * f_pv + 400.0 * f_b)
        assert op.loc[y, "route_to_market_fee_eur"] == pytest.approx(
            expected, abs=1e-9,
        )


def test_rtm_fee_exempts_covered_pv_share_in_term_physical_only():
    """Sleeved in-term: covered PV share exempt; CfD and post-term: full."""
    k = dict(
        _kpis_negative_margin(),
        revenue_pv_ppa_eur=5_000.0,
        ppa_covered_dam_value_eur=4_000.0,
        profit_total_eur=141_000.0,
    )
    ppa = {"ppa_enabled": True, "ppa_term_years": 2,
           "ppa_inflation_pct": 0.0, "ppa_volume_share_pct": 80.0}

    op_phys = _op(build_yearly_cashflow(
        k, _econ(ppa_settlement="physical", **ppa), _caps(),
    ))
    op_cfd = _op(build_yearly_cashflow(
        dict(k, profit_export_from_pv_eur=12_000.0,
             revenue_pv_ppa_eur=1_000.0, profit_total_eur=141_000.0),
        _econ(ppa_settlement="cfd", **ppa), _caps(),
    ))
    for y in op_phys.index:
        f_pv = float(op_phys.loc[y, "pv_production_factor"])
        f_b = float(op_phys.loc[y, "bess_capacity_factor"])
        s = 0.8 if y <= 2 else 0.0                       # in-term exemption
        assert op_phys.loc[y, "route_to_market_fee_eur"] == pytest.approx(
            -RTM_RATE * (100.0 * f_pv * (1.0 - s) + 400.0 * f_b), abs=1e-9,
        )
        # CfD sells the full volume at DAM through the aggregator: no
        # exemption in any year.
        assert op_cfd.loc[y, "route_to_market_fee_eur"] == pytest.approx(
            -RTM_RATE * (100.0 * f_pv + 400.0 * f_b), abs=1e-9,
        )


def test_rtm_fee_zero_without_export_split_keys():
    """Older KPI dicts without pv/bess_export_mwh charge no RTM fee."""
    k = {key: v for key, v in _kpis_negative_margin().items()
         if not key.endswith("_export_mwh") and key != "pv_export_mwh"}
    k.pop("bess_export_mwh", None)
    op = _op(build_yearly_cashflow(k, _econ(), _caps()))
    assert float(op["route_to_market_fee_eur"].abs().max()) == 0.0


# ---------------------------------------------------------------------------
# E13d — optimizer revenue share
# ---------------------------------------------------------------------------


def test_optimizer_fee_clamped_at_zero_for_negative_margin():
    """A grid-charging battery with a trading loss pays NO optimizer share
    — exactly the S3 regime (export 28k < charging 30k)."""
    op = _op(build_yearly_cashflow(_kpis_negative_margin(), _econ(), _caps()))
    assert float(op["optimizer_fee_eur"].abs().max()) == 0.0
    # and the stored zeros are clean (no -0.0)
    assert not any(np.signbit(v) for v in op["optimizer_fee_eur"])


def test_optimizer_fee_scales_on_bess_fade_and_dam_inflation():
    op = _op(build_yearly_cashflow(_kpis_positive_margin(), _econ(), _caps()))
    share = OPT_SHARE_PCT / 100.0
    for y in op.index:
        f_b = float(op.loc[y, "bess_capacity_factor"])
        expected = -share * 50_000.0 * f_b * 1.02 ** (y - 1)
        assert op.loc[y, "optimizer_fee_eur"] == pytest.approx(
            expected, abs=1e-9,
        )


# ---------------------------------------------------------------------------
# Cross-cutting identities
# ---------------------------------------------------------------------------


def test_net_cashflow_identity_with_fees_on():
    op = _op(build_yearly_cashflow(_kpis_positive_margin(), _econ(), _caps()))
    rebuilt = (
        op["revenue_eur"] + op["balancing_revenue_eur"]
        + op["balancing_aggregator_fee_eur"]
        + op["route_to_market_fee_eur"] + op["optimizer_fee_eur"]
        + op["ppa_revenue_eur"] + op["opex_eur"]
        + op["capex_eur"] + op["devex_eur"]
    )
    assert float((rebuilt - op["net_cashflow_eur"]).abs().max()) < 1e-9


def test_zero_default_is_bit_identical():
    """Keys absent == keys 0 == all-zero columns; NPV unchanged."""
    k = _kpis_positive_margin()
    econ_absent = {key: v for key, v in _econ().items()
                   if key not in ("route_to_market_fee_eur_per_mwh",
                                  "optimizer_revenue_share_pct")}
    cf_absent = build_yearly_cashflow(k, econ_absent, _caps())
    cf_zero = build_yearly_cashflow(
        k, _econ(route_to_market_fee_eur_per_mwh=0.0,
                 optimizer_revenue_share_pct=0.0), _caps(),
    )
    pd.testing.assert_frame_equal(cf_absent, cf_zero)
    assert float(cf_absent["route_to_market_fee_eur"].abs().max()) == 0.0
    assert float(cf_absent["optimizer_fee_eur"].abs().max()) == 0.0


def test_lifetime_totals_and_npv_reduction():
    k = _kpis_positive_margin()
    fin_on = compute_financial_kpis(
        build_yearly_cashflow(k, _econ(), _caps()), _econ(),
    )
    fin_off = compute_financial_kpis(
        build_yearly_cashflow(k, _econ(
            route_to_market_fee_eur_per_mwh=0.0,
            optimizer_revenue_share_pct=0.0,
        ), _caps()), _econ(),
    )
    assert fin_on["total_route_to_market_fee_eur_lifecycle"] < 0.0
    assert fin_on["total_optimizer_fee_eur_lifecycle"] < 0.0
    assert fin_on["npv_eur"] < fin_off["npv_eur"]
    assert fin_off["total_route_to_market_fee_eur_lifecycle"] == 0.0
    assert fin_off["total_optimizer_fee_eur_lifecycle"] == 0.0
    # LCOE/LCOS are fee-agnostic by convention: same inputs -> same values
    # (both NaN here without production series, equally absent both sides).


def test_monthly_reconciles_to_yearly_with_fees_on():
    k = _kpis_positive_margin()
    econ = _econ()
    yearly = build_yearly_cashflow(k, econ, _caps())
    res = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=8760, freq="h"),
        "profit_load_from_pv_eur": 100_000.0 / 8760.0,
        "profit_load_from_bess_eur": 30_000.0 / 8760.0,
        "profit_export_from_pv_eur": 8_000.0 / 8760.0,
        "profit_export_from_bess_eur": 50_000.0 / 8760.0,
        "expense_charge_bess_grid_eur": 0.0,
        "pv_kwh": 1.0,
    })
    monthly, _quarterly = derive_monthly_cashflow(res, yearly, econ)
    for y in range(1, 7):
        sub = monthly[monthly["project_year"] == y]
        row = yearly[yearly["project_year"] == y].iloc[0]
        for col in ("route_to_market_fee_eur", "optimizer_fee_eur",
                    "net_cashflow_eur"):
            assert float(sub[col].sum()) == pytest.approx(
                float(row[col]), abs=0.02,
            ), (y, col)


# ---------------------------------------------------------------------------
# Sensitivity guard — the hardcoded lists must track the emitted columns
# ---------------------------------------------------------------------------


def test_sensitivity_recompute_net_matches_cashflow_identity():
    """_recompute_net rebuilds the exact net the cashflow builder emitted,
    fee columns included — the guard that a new column cannot silently
    drop out of the tornado."""
    from pvbess_opt.sensitivity import _recompute_net

    cf = build_yearly_cashflow(_kpis_positive_margin(), _econ(), _caps())
    rebuilt = _recompute_net(cf.copy())
    assert float(
        (rebuilt["net_cashflow_eur"] - cf["net_cashflow_eur"]).abs().max()
    ) < 1e-9


def test_sensitivity_revenue_delta_scales_optimizer_not_rtm():
    from pvbess_opt.sensitivity import _recompute_net, _scale_revenue

    cf = build_yearly_cashflow(_kpis_positive_margin(), _econ(), _caps())
    scaled = _recompute_net(_scale_revenue(cf.copy(), 1.10))
    op0, op1 = _op(cf), _op(scaled)
    # optimizer share is price-proportional -> scales with the driver
    assert float(op1.loc[1, "optimizer_fee_eur"]) == pytest.approx(
        1.10 * float(op0.loc[1, "optimizer_fee_eur"]), rel=1e-9,
    )
    # route-to-market fee is volume-based -> unchanged
    assert float(op1.loc[1, "route_to_market_fee_eur"]) == pytest.approx(
        float(op0.loc[1, "route_to_market_fee_eur"]), rel=1e-12,
    )


# ---------------------------------------------------------------------------
# Loader validation + stacking warning
# ---------------------------------------------------------------------------


def test_workbook_validation_rejects_out_of_range_values():
    from pvbess_opt.io import read_workbook, validate_workbook_params
    typed = read_workbook("inputs/input.xlsx")

    bad_share = {**typed, "economics": dict(
        typed["economics"], optimizer_revenue_share_pct=150.0,
    )}
    with pytest.raises(ValueError, match="optimizer_revenue_share_pct"):
        validate_workbook_params(bad_share, dt_minutes=15)

    bad_rtm = {**typed, "economics": dict(
        typed["economics"], route_to_market_fee_eur_per_mwh=-1.0,
    )}
    with pytest.raises(ValueError, match="route_to_market_fee_eur_per_mwh"):
        validate_workbook_params(bad_rtm, dt_minutes=15)


def test_stacking_aggregator_and_optimizer_warns(caplog):
    from pvbess_opt.io import read_workbook, validate_workbook_params
    typed = read_workbook("inputs/input.xlsx")
    stacked = {**typed, "economics": dict(
        typed["economics"],
        aggregator_fee_pct_revenue=5.0,
        optimizer_revenue_share_pct=15.0,
    )}
    with caplog.at_level("WARNING", logger="pvbess_opt.io"):
        validate_workbook_params(stacked, dt_minutes=15)
    assert any(
        "optimizer_revenue_share_pct" in rec.getMessage()
        and "BOTH" in rec.getMessage()
        for rec in caplog.records
    )
