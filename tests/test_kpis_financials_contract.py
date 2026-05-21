"""Regression tests for the compute_kpis -> financials ordering contract
and for flattening the BESS-utilization diagnostics in the results sheet.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import derive_monthly_cashflow
from pvbess_opt.io import write_results_workbook
from pvbess_opt.lifetime import (
    aggregate_lifetime_to_yearly,
    build_lifetime_dispatch,
)


def _econ() -> dict:
    return {
        "discount_rate_pct": 7.0,
        "project_start_year": 2026,
        "project_lifecycle_years": 5,
        "total_capex_eur": 1_000_000.0,
        "annual_opex_eur": 20_000.0,
    }


def _caps() -> dict:
    return {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}


def _res_without_economics(n: int = 96) -> pd.DataFrame:
    """A dispatch frame missing the per-step EUR columns (compute_kpis skipped)."""
    ts = pd.date_range("2026-01-01", periods=n, freq="15min")
    return pd.DataFrame({
        "timestamp": ts,
        "pv_kwh": np.ones(n) * 100.0,
        "pv_to_load_kwh": np.ones(n) * 40.0,
        "bess_dis_load_kwh": np.zeros(n),
        "bess_dis_grid_kwh": np.zeros(n),
        "bess_charge_grid_kwh": np.zeros(n),
        "pv_to_grid_kwh": np.ones(n) * 10.0,
        "grid_to_load_kwh": np.zeros(n),
        "grid_export_total_kwh": np.ones(n) * 10.0,
    })


def test_derive_monthly_cashflow_requires_kpis_first():
    # The ordering guard fires before yearly_cf is consumed.
    res = _res_without_economics()
    with pytest.raises(ValueError, match="compute_kpis"):
        derive_monthly_cashflow(res, pd.DataFrame(), _econ())


def test_build_lifetime_dispatch_requires_kpis_first():
    res = _res_without_economics()
    with pytest.raises(ValueError, match="compute_kpis"):
        build_lifetime_dispatch(res, _econ(), _caps())


def test_aggregate_lifetime_to_yearly_requires_economics():
    # A non-empty lifetime frame lacking the per-step EUR columns.
    lifetime_df = pd.DataFrame({
        "project_year": [1, 1],
        "calendar_year": [2026, 2026],
        "pv_kwh": [100.0, 100.0],
    })
    with pytest.raises(ValueError, match="compute_kpis"):
        aggregate_lifetime_to_yearly(lifetime_df)


def test_results_sheet_has_no_stringified_dict(tmp_path):
    """The kpis_year1 sheet must flatten nested diagnostics, never embed '{'."""
    res = _res_without_economics()
    kpis = {
        "npv_eur": 123.0,
        "bess_utilization_diagnostics": {
            "bess_charge_pv_surplus_mwh": 12.0,
            "bess_charge_grid_mwh": 3.0,
            "bess_utilization_pct": 41.5,
        },
    }
    out = write_results_workbook(
        tmp_path / "03_results.xlsx", res, kpis, None,
    )
    sheet = pd.read_excel(out, sheet_name="kpis_year1")
    # No cell may contain a stringified dict.
    for col in sheet.columns:
        joined = sheet[col].astype(str).str.cat()
        assert "{" not in joined, f"stringified dict leaked into column {col}"
    metrics = set(sheet["metric"])
    assert "bess_util_charge_pv_surplus_mwh" in metrics
    assert "bess_util_charge_grid_mwh" in metrics
    assert "bess_util_utilization_pct" in metrics
    assert "bess_utilization_diagnostics" not in metrics
