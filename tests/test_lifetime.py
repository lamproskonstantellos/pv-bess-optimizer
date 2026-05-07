"""Multi-year lifetime dispatch projection tests."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.lifetime import (
    _bess_factor,
    aggregate_lifetime_to_yearly,
    build_lifetime_dispatch,
)


def _econ() -> dict:
    return {
        "project_lifecycle_years": 5,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 1.0,
        "revenue_inflation_pct": 2.0,
        "pv_degradation_year1_pct": 2.5,
        "pv_degradation_annual_pct": 0.55,
        "bess_degradation_annual_pct": 2.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 50.0,
    }


def _make_year1_dispatch() -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01", periods=8760, freq="h")
    return pd.DataFrame({
        "timestamp": timestamps,
        "pv_kwh": np.full(8760, 1.0),
        "load_kwh": np.full(8760, 1.0),
        "pv_to_load_kwh": np.full(8760, 0.5),
        "pv_to_grid_kwh": np.full(8760, 0.3),
        "pv_curtail_kwh": np.full(8760, 0.0),
        "pv_to_bess_kwh": np.full(8760, 0.2),
        "bess_dis_load_kwh": np.full(8760, 0.4),
        "bess_dis_grid_kwh": np.full(8760, 0.2),
        "bess_charge_grid_kwh": np.full(8760, 0.0),
        "grid_to_load_kwh": np.full(8760, 0.1),
        "grid_export_total_kwh": np.full(8760, 0.5),
        "grid_export_cap_kwh": np.full(8760, 5.0),
        "soc_kwh": np.full(8760, 100.0),
        "soc_pct": np.full(8760, 50.0),
    })


def test_lifetime_dispatch_pv_factor_invariant():
    res1 = _make_year1_dispatch()
    capacities = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    lifetime = build_lifetime_dispatch(res1, _econ(), capacities)
    y1_pv = float(lifetime.loc[lifetime["project_year"] == 1, "pv_kwh"].sum())
    for y in (1, 2, 5):
        sub = lifetime.loc[lifetime["project_year"] == y, "pv_kwh"].sum()
        if y == 1:
            assert abs(sub / y1_pv - 1.0) < 1e-6
        elif y == 2:
            assert abs(sub / y1_pv - (1.0 - 0.025)) < 1e-3
        elif y == 5:
            expected = (1.0 - 0.025) * (1.0 - 0.0055) ** 3
            assert abs(sub / y1_pv - expected) < 1e-3


def test_calendar_year_alignment():
    res1 = _make_year1_dispatch()
    capacities = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    lifetime = build_lifetime_dispatch(res1, _econ(), capacities)
    y1_cal = int(lifetime.loc[lifetime["project_year"] == 1, "calendar_year"].iloc[0])
    assert y1_cal == 2026


def test_aggregate_lifetime_yearly_columns():
    res1 = _make_year1_dispatch()
    capacities = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    lifetime = build_lifetime_dispatch(res1, _econ(), capacities)
    agg = aggregate_lifetime_to_yearly(lifetime)
    for col in (
        "project_year", "calendar_year", "pv_generation_mwh",
        "pv_to_load_mwh", "pv_to_grid_mwh",
        "bess_charge_mwh", "bess_discharge_mwh",
        "import_to_load_mwh", "export_total_mwh",
        "revenue_eur_total",
    ):
        assert col in agg.columns


def test_aggregate_lifetime_empty():
    out = aggregate_lifetime_to_yearly(pd.DataFrame())
    assert "project_year" in out.columns
    assert out.empty


def test_bess_replacement_resets_factor():
    """Section 2.4 of the upgrade spec: capacity factor resets at replacement year."""
    # No replacement: monotonic decay
    assert _bess_factor(1, 0.02, replacement_year=0) == 1.0
    assert _bess_factor(10, 0.02, replacement_year=0) == pytest.approx(0.98 ** 9)
    # Replacement at year 10
    assert _bess_factor(9, 0.02, replacement_year=10) == pytest.approx(0.98 ** 8)
    assert _bess_factor(10, 0.02, replacement_year=10) == 1.0
    assert _bess_factor(11, 0.02, replacement_year=10) == pytest.approx(0.98)
    assert _bess_factor(15, 0.02, replacement_year=10) == pytest.approx(0.98 ** 5)


def test_lifetime_dispatch_bess_replacement():
    """End-to-end: bess_dis_load_kwh ratio resets to 1 at replacement year."""
    res1 = _make_year1_dispatch()
    capacities = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    econ = _econ()
    econ["project_lifecycle_years"] = 12
    econ["bess_replacement_year"] = 10
    econ["bess_degradation_annual_pct"] = 2.0
    lifetime = build_lifetime_dispatch(res1, econ, capacities)
    y1 = float(lifetime.loc[lifetime["project_year"] == 1, "bess_dis_load_kwh"].sum())
    y10 = float(lifetime.loc[lifetime["project_year"] == 10, "bess_dis_load_kwh"].sum())
    y11 = float(lifetime.loc[lifetime["project_year"] == 11, "bess_dis_load_kwh"].sum())
    assert abs(y10 / y1 - 1.0) < 1e-3
    assert abs(y11 / y1 - 0.98) < 1e-3
