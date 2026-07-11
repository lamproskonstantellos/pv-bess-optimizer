"""P90 production lender case and the lender_cases table (Eq. E44).

``production_p90_factor_pct`` is a deterministic INTER-ANNUAL resource
haircut on the PV-linked revenue streams — distinct from the
forecast-noise Monte Carlo, which perturbs intra-year dispatch.
Locked here: the f = 1 identity on mixed-sign (fee-clamped) frames,
the per-column scaling classification, the gross/net fee identity
through the clamp, table shape and NaN-safety, DSCR / capacity
monotonicity in the haircut, the sizing-on-P90 round-trip (the
headline acceptance of the lender workstream), validation ranges and
the SUMMARY / workbook surfaces.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import (
    _amortization_schedule,
    build_yearly_cashflow,
    resolve_debt_sizing,
)
from pvbess_opt.io import (
    validate_workbook_params,
    write_results_workbook,
    write_summary_md,
)
from pvbess_opt.lender import apply_production_case, build_lender_cases
from pvbess_opt.sensitivity import TAX_LAYER_COLUMNS

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
        "aggregator_fee_pct_revenue": 5.0,
        "debt_interest_rate_pct": 5.0,
        "debt_tenor_years": 6,
        "debt_repayment": "annuity",
    }
    econ.update(o)
    return econ


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}


def _kpis() -> dict:
    return {
        "profit_load_from_pv_eur": 60_000.0,
        "profit_load_from_bess_eur": 10_000.0,
        "profit_export_from_pv_eur": 40_000.0,
        "profit_export_from_bess_eur": 40_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 145_000.0,
        # Non-trivial balancing so the no-scale classification is
        # actually exercised.
        "bm_total_capacity_revenue_eur": 30_000.0,
        "bm_total_activation_revenue_eur": 8_000.0,
    }


def _cf(**eo) -> pd.DataFrame:
    return build_yearly_cashflow(_kpis(), _econ(**eo), _caps())


# ---------------------------------------------------------------------------
# apply_production_case: identity, classification, fee identity
# ---------------------------------------------------------------------------


def test_p90_factor_identity_at_100():
    """f = 1 is a no-op — including on a frame with a fee-clamped
    (negative-gross) year, the regression that breaks a naive
    net / (1 - frac) inversion."""
    cf = _cf()
    # Fabricate a clamped year: gross < 0, fee = 0 (the base build's
    # clamp regime), then refresh the net columns for consistency.
    from pvbess_opt.sensitivity import _recompute_net

    cf.loc[cf["project_year"] == 3, "revenue_eur"] = -5_000.0
    cf.loc[cf["project_year"] == 3, "aggregator_fee_eur"] = 0.0
    cf.loc[cf["project_year"] == 3, "revenue_retail_eur"] = -3_000.0
    cf.loc[cf["project_year"] == 3, "revenue_dam_eur"] = -2_000.0
    cf = _recompute_net(cf.copy())
    pd.testing.assert_frame_equal(
        apply_production_case(cf, 1.0),
        cf.drop(columns=[c for c in TAX_LAYER_COLUMNS if c in cf.columns]),
    )


def test_p90_scales_pv_streams_only():
    econ_kw = dict(
        ppa_enabled=True, ppa_price_eur_per_mwh=55.0,
        ppa_volume_share_pct=40.0, ppa_term_years=N_YEARS,
        route_to_market_fee_eur_per_mwh=2.0,
    )
    kpis = dict(_kpis(), pv_export_mwh=4_000.0, bess_export_mwh=1_000.0,
                pv_generation_mwh=7_000.0, revenue_pv_ppa_eur=20_000.0)
    cf = build_yearly_cashflow(kpis, _econ(**econ_kw), _caps())
    f = 0.92
    out = apply_production_case(cf, f)
    op = cf["project_year"] >= 1
    # PV-linked: whole retail/DAM family + PPA + route-to-market.
    gross_base = (
        cf["revenue_eur"].astype(float)
        + cf["aggregator_fee_eur"].astype(float).abs()
    )
    gross_out = (
        out["revenue_eur"].astype(float)
        + out["aggregator_fee_eur"].astype(float).abs()
    )
    pd.testing.assert_series_equal(gross_out, f * gross_base,
                                   check_names=False)
    for col in ("ppa_revenue_eur", "route_to_market_fee_eur"):
        assert (
            out.loc[op, col].to_numpy()
            == pytest.approx(f * cf.loc[op, col].to_numpy())
        ), col
        assert float(cf.loc[op, col].abs().sum()) > 0.0, col
    # Per-stream nets re-sum to the total.
    pd.testing.assert_series_equal(
        out["revenue_retail_eur"] + out["revenue_dam_eur"],
        out["revenue_eur"], check_names=False,
    )
    # NOT PV-linked: balancing, OPEX, CAPEX bit-unchanged.
    for col in ("balancing_revenue_eur", "balancing_capacity_revenue_eur",
                "balancing_activation_revenue_eur", "opex_eur",
                "capex_eur", "devex_eur"):
        if col in cf.columns:
            pd.testing.assert_series_equal(out[col], cf[col],
                                           check_names=False)
    assert float(cf["balancing_revenue_eur"].abs().sum()) > 0.0


def test_p90_fee_identity_through_clamp():
    """The rederived aggregator fee keeps the same fraction and the
    non-negative-gross clamp: fee == -frac * max(f*gross, 0)."""
    cf = _cf()
    frac = 0.05
    f = 0.9
    out = apply_production_case(cf, f)
    gross_base = (
        cf["revenue_eur"].astype(float)
        + cf["aggregator_fee_eur"].astype(float).abs()
    )
    expected_fee = -frac * (f * gross_base).clip(lower=0.0)
    assert out["aggregator_fee_eur"].to_numpy() == pytest.approx(
        expected_fee.to_numpy(), abs=1e-6,
    )


# ---------------------------------------------------------------------------
# Lender case table
# ---------------------------------------------------------------------------


def test_lender_cases_table_shape_and_ordering():
    cf = _cf(gearing_pct=60.0)
    table = build_lender_cases(
        cf, _econ(gearing_pct=60.0, production_p90_factor_pct=90.0),
    )
    assert list(table["case"]) == ["base", "p90"]
    assert list(table.columns) == [
        "case", "production_factor_pct", "min_dscr", "avg_dscr",
        "equity_irr_pct", "npv_eur", "debt_capacity_eur",
    ]
    assert table["production_factor_pct"].tolist() == [100.0, 90.0]
    geared = table.drop(columns=["case"]).to_numpy(dtype=float)
    assert np.isfinite(geared).all()


def test_lender_cases_nan_safe_all_equity():
    cf = _cf()
    table = build_lender_cases(
        cf, _econ(production_p90_factor_pct=90.0),
    )
    # Leverage KPIs are NaN without debt; NPV and capacity stay real.
    assert np.isnan(table["min_dscr"]).all()
    assert np.isnan(table["equity_irr_pct"]).all()
    assert np.isfinite(table["npv_eur"]).all()
    assert np.isfinite(table["debt_capacity_eur"]).all()


def test_p90_case_monotonicity():
    econ = _econ(gearing_pct=60.0, production_p90_factor_pct=85.0)
    table = build_lender_cases(_cf(gearing_pct=60.0), econ)
    base = table.loc[table["case"] == "base"].iloc[0]
    p90 = table.loc[table["case"] == "p90"].iloc[0]
    assert p90["min_dscr"] <= base["min_dscr"]
    assert p90["avg_dscr"] <= base["avg_dscr"]
    assert p90["npv_eur"] <= base["npv_eur"]
    assert p90["debt_capacity_eur"] <= base["debt_capacity_eur"]


def test_lender_cases_use_frozen_sized_debt():
    """With target-DSCR sizing resolved, both case rows run the SAME
    committed debt: base min_dscr == target, p90 min_dscr below it.
    The outlay is raised so the E43 cap does not bind (a binding cap
    would push coverage ABOVE the target)."""
    econ = _econ(debt_sizing_mode="target_dscr", target_dscr=1.4,
                 production_p90_factor_pct=90.0,
                 capex_pv_eur_per_kw=800.0)
    cf = _cf(capex_pv_eur_per_kw=800.0)
    resolve_debt_sizing(cf, econ)
    table = build_lender_cases(cf, econ)
    base = table.loc[table["case"] == "base"].iloc[0]
    p90 = table.loc[table["case"] == "p90"].iloc[0]
    assert base["min_dscr"] == pytest.approx(1.4, abs=1e-3)
    assert p90["min_dscr"] < base["min_dscr"]


# ---------------------------------------------------------------------------
# Sizing on the P90 case (the workstream's headline round-trip)
# ---------------------------------------------------------------------------


def test_debt_sizing_on_p90_roundtrip():
    econ = _econ(debt_sizing_mode="target_dscr", target_dscr=1.30,
                 debt_sizing_case="p90", production_p90_factor_pct=88.0,
                 capex_pv_eur_per_kw=800.0)
    cf = _cf(capex_pv_eur_per_kw=800.0)
    p90_frame = apply_production_case(cf, 0.88)
    sizing = resolve_debt_sizing(p90_frame, econ)
    assert sizing is not None and sizing.dscr_target_met
    # Replay the sized debt against the P90 CFADS: the target holds at
    # the binding year and everywhere else sits above it.
    p90_net = p90_frame["net_cashflow_eur"].to_numpy(dtype=float)
    sched = _amortization_schedule(
        sizing.sized_debt_eur, 0.05, 6, "annuity",
    )
    dscrs = [
        float(p90_net[int(r["year"])]) / r["debt_service_eur"]
        for r in sched
    ]
    assert min(dscrs) == pytest.approx(1.30, rel=1e-9)
    # The BASE cashflow then covers with margin (P90 is the downside).
    base_net = cf["net_cashflow_eur"].to_numpy(dtype=float)
    base_dscrs = [
        float(base_net[int(r["year"])]) / r["debt_service_eur"]
        for r in sched
    ]
    assert min(base_dscrs) > 1.30


# ---------------------------------------------------------------------------
# Validation + output surfaces
# ---------------------------------------------------------------------------


def _typed_with_econ(**econ) -> dict:
    return {
        "project": {"mode": "self_consumption"},
        "pv": {"pv_nameplate_kwp": 500.0},
        "bess": {"bess_power_kw": 100.0, "bess_capacity_kwh": 200.0},
        "economics": dict(econ),
        "simulation": {},
        "balancing": {"balancing_enabled": False},
    }


@pytest.mark.parametrize("bad", [0.0, -5.0, 150.0])
def test_validation_rejects_out_of_range_factor(bad):
    typed = _typed_with_econ(production_p90_factor_pct=bad)
    with pytest.raises(ValueError, match="production_p90_factor_pct"):
        validate_workbook_params(typed, dt_minutes=60)


def test_validation_accepts_p90_sizing_case_with_haircut():
    typed = _typed_with_econ(
        debt_sizing_mode="target_dscr", debt_sizing_case="p90",
        production_p90_factor_pct=92.0,
    )
    validate_workbook_params(typed, dt_minutes=60)


def test_validation_warns_degenerate_p90_case(caplog):
    typed = _typed_with_econ(
        debt_sizing_mode="target_dscr", debt_sizing_case="p90",
    )
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io"):
        validate_workbook_params(typed, dt_minutes=60)
    assert any(
        "degenerates to the base cashflow" in r.getMessage()
        for r in caplog.records
    )


def test_summary_and_workbook_surfaces(tmp_path):
    econ = _econ(gearing_pct=60.0, production_p90_factor_pct=90.0)
    cf = _cf(gearing_pct=60.0)
    table = build_lender_cases(cf, econ)
    out = tmp_path / "SUMMARY.md"
    write_summary_md(
        out, kpis_year1={"profit_total_eur": 1.0}, financial_kpis={},
        params={"mode": "merchant"}, lender_cases=table,
    )
    text = out.read_text(encoding="utf-8")
    assert "## Lender cases" in text
    assert "| base |" in text and "| p90 |" in text
    # Absent by default.
    out2 = tmp_path / "SUMMARY_default.md"
    write_summary_md(
        out2, kpis_year1={"profit_total_eur": 1.0}, financial_kpis={},
        params={"mode": "merchant"},
    )
    assert "Lender cases" not in out2.read_text(encoding="utf-8")

    res = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=4, freq="h"),
    })
    xlsx = write_results_workbook(
        tmp_path / "03_results.xlsx", res_year1=res,
        kpis_year1={"profit_total_eur": 1.0}, kpis_monthly_year1=None,
        lender_cases=table,
    )
    assert "lender_cases" in pd.ExcelFile(xlsx).sheet_names
    xlsx2 = write_results_workbook(
        tmp_path / "03_results_default.xlsx", res_year1=res,
        kpis_year1={"profit_total_eur": 1.0}, kpis_monthly_year1=None,
    )
    assert "lender_cases" not in pd.ExcelFile(xlsx2).sheet_names
