"""KPI computation + lowercase naming + energy-balance verification tests."""

from __future__ import annotations

import pytest

from pvbess_opt.kpis import (
    add_economic_columns,
    attribute_green_discharge,
    compute_kpis,
    compute_monthly_kpis,
    verify_energy_balance,
)
from pvbess_opt.optimization import run_scenario


@pytest.fixture(scope="module")
def _solved_self_consumption_short(short_params, short_ts):
    res, _ = run_scenario(
        short_params, short_ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=30,
    )
    return res


def test_compute_kpis_keys_are_lowercase(short_params, _solved_self_consumption_short):
    res = _solved_self_consumption_short
    kpis = compute_kpis(res, short_params, verify_balance=False)
    for key in kpis:
        assert key == key.lower(), f"KPI key {key!r} not lowercase"


def test_compute_kpis_contains_canonical_lowercase_keys(
    short_params, _solved_self_consumption_short,
):
    res = _solved_self_consumption_short
    kpis = compute_kpis(res, short_params, verify_balance=False)
    for key in (
        "profit_total_eur", "system_total_export_mwh", "bess_total_charge_mwh",
        "soc_min_pct", "pv_energy_curtailed_mwh", "e_cap_mwh",
        "load_coverage_from_pv_frac",
    ):
        assert key in kpis, f"missing canonical KPI key {key!r}"


def test_no_uppercase_kpi_keys(short_params, _solved_self_consumption_short):
    """Every KPI key must be lowercase snake_case."""
    res = _solved_self_consumption_short
    kpis = compute_kpis(res, short_params, verify_balance=False)
    for key in kpis:
        assert any(ch.isupper() for ch in key) is False, (
            f"KPI key {key!r} contains uppercase characters"
        )


def test_verify_energy_balance_residuals_under_tolerance(
    short_params, _solved_self_consumption_short,
):
    res = _solved_self_consumption_short
    residuals = verify_energy_balance(res, short_params, raise_on_failure=False)
    for name, val in residuals.items():
        assert val < 1.0e-3, f"{name}: {val}"


def test_attribute_green_discharge_adds_columns(short_params, _solved_self_consumption_short):
    res = _solved_self_consumption_short
    out = attribute_green_discharge(res.copy(), short_params)
    for col in ("bess_dis_load_green_kwh", "bess_dis_grid_green_kwh", "soc_green_kwh"):
        assert col in out.columns


def test_add_economic_columns_lowercase(short_params, _solved_self_consumption_short):
    res = _solved_self_consumption_short
    out = add_economic_columns(res.copy(), short_params)
    for col in (
        "profit_load_from_pv_eur", "profit_load_from_bess_eur",
        "profit_export_from_pv_eur", "profit_export_from_bess_eur",
        "expense_charge_bess_grid_eur",
    ):
        assert col in out.columns


def test_merchant_zeroes_load_kpis(short_params_merchant, short_ts):
    res, _ = run_scenario(
        short_params_merchant, short_ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=30,
    )
    kpis = compute_kpis(res, short_params_merchant, verify_balance=False)
    assert kpis["load_energy_mwh"] == 0.0
    assert kpis["pv_direct_to_load_mwh"] == 0.0
    assert kpis["bess_to_load_mwh"] == 0.0
    assert kpis["load_coverage_from_pv_frac"] == 0.0


def test_compute_monthly_kpis_lowercase(short_params, _solved_self_consumption_short):
    res = _solved_self_consumption_short
    monthly = compute_monthly_kpis(res)
    for col in monthly.columns:
        assert col == col.lower()
