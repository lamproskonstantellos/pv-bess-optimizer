"""v0.8.1 retail / DAM revenue-inflation split.

Lazard / Aurora / Gridcog do NOT apply CPI to wholesale exports.
retail_inflation_pct now indexes only load-coverage (PPA / VNB)
revenue; dam_inflation_pct indexes wholesale exports (default 0).
"""

from __future__ import annotations

import logging

import pytest

from pvbess_opt.economics import build_yearly_cashflow
from pvbess_opt.io import _parse_kv_sheet


def _base_econ() -> dict:
    return {
        "project_lifecycle_years": 25,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
        "capex_pv_eur_per_kw": 0.0,
        "capex_bess_eur_per_kw": 0.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 0.0,
        "opex_bess_eur_per_kw": 0.0,
        "pv_degradation_year1_pct": 0.0,
        "pv_degradation_annual_pct": 0.0,
        "bess_degradation_annual_pct": 0.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
    }


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 2000.0}


def _kpis_split() -> dict:
    return {
        "profit_total_eur": 100_000.0,
        "profit_load_from_pv_eur": 30_000.0,
        "profit_load_from_bess_eur": 10_000.0,
        "profit_export_from_pv_eur": 40_000.0,
        "profit_export_from_bess_eur": 25_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
    }


# ---------------------------------------------------------------------------
# Retail-only inflation applies only to the load-coverage stream
# ---------------------------------------------------------------------------


def test_retail_inflation_alone_applies_to_load_revenue():
    """Year 25 retail revenue must scale by (1.05)^24 with retail=5, dam=0."""
    econ = _base_econ()
    econ["retail_inflation_pct"] = 5.0
    econ["dam_inflation_pct"] = 0.0
    df = build_yearly_cashflow(_kpis_split(), econ, _caps())
    retail_y1 = float(
        df.loc[df["project_year"] == 1, "revenue_retail_eur"].iloc[0]
    )
    retail_y25 = float(
        df.loc[df["project_year"] == 25, "revenue_retail_eur"].iloc[0]
    )
    assert retail_y1 == pytest.approx(40_000.0, rel=1e-9)
    assert retail_y25 == pytest.approx(retail_y1 * (1.05) ** 24, rel=1e-9)


# ---------------------------------------------------------------------------
# DAM-only inflation applies only to the export stream
# ---------------------------------------------------------------------------


def test_dam_inflation_alone_applies_to_export_revenue():
    """Year 25 DAM revenue must scale by (1.03)^24 with retail=0, dam=3."""
    econ = _base_econ()
    econ["retail_inflation_pct"] = 0.0
    econ["dam_inflation_pct"] = 3.0
    df = build_yearly_cashflow(_kpis_split(), econ, _caps())
    # dam stream Y1 = 40 + 25 - 5 = 60 000
    dam_y1 = float(df.loc[df["project_year"] == 1, "revenue_dam_eur"].iloc[0])
    dam_y25 = float(df.loc[df["project_year"] == 25, "revenue_dam_eur"].iloc[0])
    assert dam_y1 == pytest.approx(60_000.0, rel=1e-9)
    assert dam_y25 == pytest.approx(dam_y1 * (1.03) ** 24, rel=1e-9)


# ---------------------------------------------------------------------------
# Default dam_inflation_pct = 0 → DAM stream stays flat (nominal)
# ---------------------------------------------------------------------------


def test_zero_dam_inflation_keeps_export_revenue_flat_nominal():
    econ = _base_econ()
    econ["retail_inflation_pct"] = 2.0  # retail moves
    econ["dam_inflation_pct"] = 0.0
    df = build_yearly_cashflow(_kpis_split(), econ, _caps())
    dam_y1 = float(df.loc[df["project_year"] == 1, "revenue_dam_eur"].iloc[0])
    dam_y10 = float(df.loc[df["project_year"] == 10, "revenue_dam_eur"].iloc[0])
    # No PV degradation, no DAM inflation → year-N DAM equals year-1.
    assert dam_y10 == pytest.approx(dam_y1, rel=1e-9)


# ---------------------------------------------------------------------------
# Legacy revenue_inflation_pct on the workbook maps to retail with WARN
# ---------------------------------------------------------------------------


def test_legacy_revenue_inflation_pct_emits_warning_and_maps_to_retail(caplog):
    flat = {
        "revenue_inflation_pct": 3.0,
        # nothing else — defaults fill in
    }
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io"):
        out = _parse_kv_sheet("economics", flat)
    assert out["retail_inflation_pct"] == pytest.approx(3.0)
    assert any(
        "revenue_inflation_pct" in rec.getMessage() and "retail_inflation_pct" in rec.getMessage()
        for rec in caplog.records
    ), "legacy rename warning missing"


# ---------------------------------------------------------------------------
# Sum invariant — retail + DAM == total revenue per year (post fee)
# ---------------------------------------------------------------------------


def test_revenue_split_sums_to_total():
    econ = _base_econ()
    econ["retail_inflation_pct"] = 2.0
    econ["dam_inflation_pct"] = 0.0
    econ["aggregator_fee_pct_revenue"] = 10.0
    df = build_yearly_cashflow(_kpis_split(), econ, _caps())
    op = df.loc[df["project_year"] >= 1]
    for _, row in op.iterrows():
        retail = float(row["revenue_retail_eur"])
        dam = float(row["revenue_dam_eur"])
        total = float(row["revenue_eur"])
        assert retail + dam == pytest.approx(total, rel=1e-9, abs=1e-6)
