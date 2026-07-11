"""Reference-period support settlement: sliding FiP / two-way CfD
(Eqs. E55-E57).

The premium settles per month on the eligible PV-export volume
against a volume-weighted monthly reference price — one-way under the
Greek DAPEEP sliding Feed-in-Premium convention, two-way under a CfD.
Locked here: zero-default bit-identity, the E55 reference-price and
E56 premium arithmetic (including the hourly cross-check mode), the
E57 negative-hour suspension, the cashflow projection (flat strike,
escalating reference, PV-fade volume, term cutoff), monthly
reconciliation, the dedicated strike tornado driver, theme
registration and the loader validation.
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
from pvbess_opt.ppa import compute_support_settlement

STRIKE = 65.0


def _res(n_days: int = 4) -> pd.DataFrame:
    n = 24 * n_days
    hours = np.arange(n) % 24
    prices = np.where(hours < 8, -5.0, np.where(hours < 16, 40.0, 90.0))
    pv = np.where((hours >= 6) & (hours < 18), 500.0, 0.0)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "pv_to_grid_kwh": pv.astype(float),
        "dam_price_eur_per_mwh": prices.astype(float),
    })


# ---------------------------------------------------------------------------
# Engine (Eqs. E55-E57)
# ---------------------------------------------------------------------------


def test_monthly_reference_price_and_premium():
    res = _res()
    out = compute_support_settlement(
        res, scheme="sliding_fip", strike_eur_per_mwh=STRIKE,
    )
    e = res["pv_to_grid_kwh"] / 1000.0
    p = res["dam_price_eur_per_mwh"]
    p_ref = float((p * e).sum()) / float(e.sum())
    assert out["support_monthly_ref_price_eur_per_mwh"][0] == pytest.approx(
        p_ref,
    )
    expected = float(e.sum()) * max(STRIKE - p_ref, 0.0)
    assert out["support_settlement_eur"] == pytest.approx(expected)
    assert out["support_eligible_export_mwh"] == pytest.approx(
        float(e.sum()),
    )


def test_sliding_clamp_and_two_way_sign():
    res = _res()
    # A strike far below the reference clamps the sliding premium at 0
    # and turns the two-way settlement negative.
    low = compute_support_settlement(
        res, scheme="sliding_fip", strike_eur_per_mwh=1.0,
    )
    assert low["support_settlement_eur"] == 0.0
    two_way = compute_support_settlement(
        res, scheme="cfd_two_way", strike_eur_per_mwh=1.0,
    )
    assert two_way["support_settlement_eur"] < 0.0


def test_negative_hour_suspension_removes_both_sides():
    res = _res()
    out = compute_support_settlement(
        res, scheme="sliding_fip", strike_eur_per_mwh=STRIKE,
        suspend_negative=True,
    )
    mask = res["dam_price_eur_per_mwh"] >= 0.0
    e = (res["pv_to_grid_kwh"] / 1000.0).where(mask, 0.0)
    p = res["dam_price_eur_per_mwh"]
    p_ref = float((p * e).sum()) / float(e.sum())
    assert out["support_monthly_ref_price_eur_per_mwh"][0] == pytest.approx(
        p_ref,
    )
    assert out["support_eligible_export_mwh"] == pytest.approx(
        float(e.sum()),
    )
    assert out["support_settlement_eur"] == pytest.approx(
        float(e.sum()) * max(STRIKE - p_ref, 0.0),
    )


def test_hourly_mode_reproduces_per_step_cfd_algebra():
    res = _res()
    out = compute_support_settlement(
        res, scheme="cfd_two_way", strike_eur_per_mwh=STRIKE,
        ref_period="hourly",
    )
    e = res["pv_to_grid_kwh"] / 1000.0
    step = float((e * (STRIKE - res["dam_price_eur_per_mwh"])).sum())
    assert out["support_settlement_eur"] == pytest.approx(step)


# ---------------------------------------------------------------------------
# Cashflow projection + KPIs
# ---------------------------------------------------------------------------


def _econ(**overrides) -> dict:
    econ = {
        "project_lifecycle_years": 6,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 2.0,
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
    }
    econ.update(overrides)
    return econ


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}


def _kpis(**extra) -> dict:
    k = {
        "profit_load_from_pv_eur": 80_000.0,
        "profit_load_from_bess_eur": 20_000.0,
        "profit_export_from_pv_eur": 10_000.0,
        "profit_export_from_bess_eur": 30_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 135_000.0,
        "pv_export_mwh": 800.0,
        "bess_export_mwh": 300.0,
        "bess_total_discharge_mwh": 500.0,
    }
    k.update(extra)
    return k


def _support_kpis() -> dict:
    e_m = [100.0] * 12
    p_m = [50.0] * 12
    settlement = sum(e * max(STRIKE - p, 0.0) for e, p in zip(e_m, p_m, strict=True))
    return {
        "support_settlement_eur": settlement,
        "support_eligible_export_mwh": sum(e_m),
        "support_monthly_eligible_mwh": e_m,
        "support_monthly_ref_price_eur_per_mwh": p_m,
        "support_monthly_settlement_eur": [
            e * max(STRIKE - p, 0.0) for e, p in zip(e_m, p_m, strict=True)
        ],
    }


def test_zero_default_is_bit_identical():
    cf_absent = build_yearly_cashflow(_kpis(), _econ(), _caps())
    cf_none = build_yearly_cashflow(
        _kpis(), _econ(
            support_scheme="none",
            support_strike_eur_per_mwh=99.0,
            support_term_years=5,
        ), _caps(),
    )
    pd.testing.assert_frame_equal(cf_absent, cf_none)
    assert (cf_absent["support_settlement_eur"] == 0.0).all()


def test_cashflow_projection_flat_strike_escalating_reference():
    econ = _econ(
        support_scheme="sliding_fip",
        support_strike_eur_per_mwh=STRIKE,
        support_term_years=4,
    )
    kpis = _kpis(**_support_kpis())
    cf = build_yearly_cashflow(kpis, econ, _caps())
    op = cf[cf["project_year"] >= 1].set_index("project_year")
    # Year 1 equals the KPI base exactly.
    assert float(op.loc[1, "support_settlement_eur"]) == pytest.approx(
        kpis["support_settlement_eur"],
    )
    # Later years: volume on pv_factor, reference on dam inflation,
    # strike flat (Eq. E56).
    for y in (2, 3, 4):
        pv_f = float(op.loc[y, "pv_production_factor"])
        ref = 50.0 * 1.02 ** (y - 1)
        expected = sum(
            100.0 * pv_f * max(STRIKE - ref, 0.0) for _ in range(12)
        )
        assert float(op.loc[y, "support_settlement_eur"]) == pytest.approx(
            expected, abs=0.01,
        ), y
    # Term cutoff: zero after support_term_years.
    assert float(op.loc[5, "support_settlement_eur"]) == 0.0
    assert float(op.loc[6, "support_settlement_eur"]) == 0.0
    # Lifetime KPI mirrors the signed column sum.
    fin = compute_financial_kpis(cf, econ)
    assert fin["lifetime_support_settlement_eur"] == pytest.approx(
        float(op["support_settlement_eur"].sum()), abs=0.01,
    )


def test_two_way_negative_years_hit_the_net():
    econ = _econ(
        support_scheme="cfd_two_way",
        support_strike_eur_per_mwh=30.0,  # below the 50 reference
        support_term_years=6,
    )
    sup = _support_kpis()
    sup["support_settlement_eur"] = sum(
        e * (30.0 - p) for e, p in zip(
            sup["support_monthly_eligible_mwh"],
            sup["support_monthly_ref_price_eur_per_mwh"],
            strict=True,
        )
    )
    kpis = _kpis(**sup)
    cf = build_yearly_cashflow(kpis, econ, _caps())
    base = build_yearly_cashflow(_kpis(), _econ(), _caps())
    op = cf[cf["project_year"] >= 1].set_index("project_year")
    assert float(op.loc[1, "support_settlement_eur"]) < 0.0
    delta = (
        float(op.loc[1, "net_cashflow_eur"])
        - float(base.loc[base["project_year"] == 1,
                         "net_cashflow_eur"].iloc[0])
    )
    assert delta == pytest.approx(
        float(op.loc[1, "support_settlement_eur"]), abs=0.01,
    )


def test_monthly_reconciliation():
    econ = _econ(
        support_scheme="sliding_fip",
        support_strike_eur_per_mwh=STRIKE,
        support_term_years=6,
        project_lifecycle_years=4,
    )
    kpis = _kpis(**_support_kpis())
    cf = build_yearly_cashflow(kpis, econ, _caps())
    res = _res()
    res["savings_self_consumption_eur"] = 10.0
    res["profit_export_from_pv_eur"] = 5.0
    res["profit_export_from_bess_eur"] = 5.0
    res["expense_charge_bess_grid_eur"] = 1.0
    res["pv_kwh"] = 100.0
    monthly, quarterly = derive_monthly_cashflow(res, cf, econ)
    for y in range(1, 5):
        y_total = float(cf.loc[
            cf["project_year"] == y, "support_settlement_eur",
        ].iloc[0])
        m_total = float(monthly.loc[
            monthly["project_year"] == y, "support_settlement_eur",
        ].sum())
        assert m_total == pytest.approx(y_total, abs=1e-6), y
        y_net = float(cf.loc[
            cf["project_year"] == y, "net_cashflow_eur",
        ].iloc[0])
        m_net = float(monthly.loc[
            monthly["project_year"] == y, "net_cashflow_eur",
        ].sum())
        assert m_net == pytest.approx(y_net, abs=1e-6), y
    assert "support_settlement_eur" in quarterly.columns


def test_sensitivity_driver_and_component_identity():
    from pvbess_opt.economics import TAX_LAYER_COLUMNS
    from pvbess_opt.sensitivity import (
        _recompute_net,
        _scale_revenue,
        run_sensitivity_analysis,
        variables_for_npv_sensitivity,
    )

    econ = _econ(
        support_scheme="sliding_fip",
        support_strike_eur_per_mwh=STRIKE,
        support_term_years=6,
    )
    kpis = _kpis(**_support_kpis())
    cf = build_yearly_cashflow(kpis, econ, _caps())
    # NOT revenue-scaled (mixed administered strike / market reference).
    scaled = _scale_revenue(cf, 1.1, econ)
    assert float(scaled.loc[scaled["project_year"] == 1,
                            "support_settlement_eur"].iloc[0]) == (
        pytest.approx(float(cf.loc[cf["project_year"] == 1,
                                   "support_settlement_eur"].iloc[0]))
    )
    base = cf.drop(columns=list(TAX_LAYER_COLUMNS))
    recomputed = _recompute_net(base.copy())
    pd.testing.assert_series_equal(
        recomputed["net_cashflow_eur"], base["net_cashflow_eur"],
    )
    # The dedicated strike driver appears and is exact vs a manual
    # rebuild at the perturbed strike.
    names = [v["name"] for v in variables_for_npv_sensitivity(econ)]
    assert "SupportStrike" in names
    fin = compute_financial_kpis(cf, econ)
    sens = run_sensitivity_analysis(kpis, econ, _caps(), fin)
    rows = sens[sens["variable"] == "SupportStrike"]
    assert set(rows["scenario"]) == {"base", "low", "high"}
    high = rows[rows["scenario"] == "high"].iloc[0]
    manual = compute_financial_kpis(
        build_yearly_cashflow(
            kpis,
            {**econ, "support_strike_eur_per_mwh": STRIKE * 1.1},
            _caps(),
        ),
        econ,
    )
    assert float(high["npv_eur"]) == pytest.approx(
        manual["npv_eur"], abs=0.5,
    )


def test_theme_and_loader_validation(tmp_path):
    from pvbess_opt.theme import (
        FINANCIAL_LABELS,
        FINANCIAL_LEGEND_ORDER,
        financial_color,
    )
    assert "Support settlement (FiP/CfD)" in FINANCIAL_LABELS
    assert "Support settlement (FiP/CfD)" in FINANCIAL_LEGEND_ORDER
    assert financial_color("Support settlement (FiP/CfD)") == "#9E9D24"

    from pvbess_opt.io import (
        BALANCING_SHEET_DEFAULTS,
        BESS_SHEET_DEFAULTS,
        ECONOMICS_SHEET_DEFAULTS,
        PPA_SHEET_DEFAULTS,
        PROJECT_SHEET_DEFAULTS,
        PV_SHEET_DEFAULTS,
        SIMULATION_SHEET_DEFAULTS,
        read_workbook,
        write_workbook,
    )
    n = 24

    def _write(**ppa):
        path = tmp_path / f"wb{len(list(tmp_path.iterdir()))}.xlsx"
        typed = {
            "ts": pd.DataFrame({
                "timestamp": pd.date_range(
                    "2026-01-01", periods=n, freq="h",
                ),
                "pv_kwh": np.full(n, 100.0),
                "dam_price_eur_per_mwh": np.full(n, 60.0),
            }),
            "project": dict(PROJECT_SHEET_DEFAULTS, mode="merchant"),
            "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=1000.0),
            "bess": dict(BESS_SHEET_DEFAULTS),
            "economics": dict(ECONOMICS_SHEET_DEFAULTS),
            "simulation": dict(SIMULATION_SHEET_DEFAULTS),
            "balancing": dict(BALANCING_SHEET_DEFAULTS),
            "ppa": dict(PPA_SHEET_DEFAULTS, **ppa),
        }
        write_workbook(typed, path)
        return path

    back = read_workbook(_write(
        support_scheme="sliding_fip",
        support_strike_eur_per_mwh=63.0,
        support_term_years=15,
    ))
    assert back["ppa"]["support_scheme"] == "sliding_fip"
    assert back["ppa"]["support_strike_eur_per_mwh"] == 63.0
    with pytest.raises(ValueError, match="not both"):
        read_workbook(_write(
            support_scheme="sliding_fip",
            support_strike_eur_per_mwh=63.0,
            ppa_enabled=True,
            ppa_price_eur_per_mwh=65.0,
        ))
    with pytest.raises(ValueError, match="reference tariff"):
        read_workbook(_write(support_scheme="cfd_two_way"))
