"""v0.6 workbook-schema tests.

Covers:
* The new ``# system_sizing`` / ``# bess_operation`` / ``# regulatory``
  three-group project sheet.
* The new ``# uncertainty`` group on the economic sheet.
* The renamed ``plot_daily_scope`` (replaces v0.5 ``plot_daily_year1``).
* The widened ``plot_yearly_scope`` vocabulary (now also accepts
  ``year1_only``).
* The two legacy-warning paths in ``read_workbook``: dropped
  ``# optimization`` keys and renamed ``plot_daily_year1``.
"""

from __future__ import annotations

import logging

import pandas as pd
import pytest

from pvbess_opt.io import (
    ECON_DEFAULTS,
    PROJECT_DEFAULTS,
    _LEGACY_OPTIMIZATION_KEYS,
    _parse_economic_sheet,
    _parse_project_sheet,
    read_workbook,
    write_workbook,
)


# ---------------------------------------------------------------------------
# Project sheet — three new groups
# ---------------------------------------------------------------------------


def test_project_groups_are_system_sizing_bess_operation_regulatory():
    assert tuple(PROJECT_DEFAULTS) == (
        "system_sizing", "bess_operation", "regulatory",
    )


def test_system_sizing_group_keys():
    expected = {
        "pv_nameplate_kwp", "bess_power_kw", "bess_capacity_kwh",
        "battery_hours", "p_charge_max_kw", "p_dis_max_kw",
        "p_grid_export_max_kw",
    }
    assert set(PROJECT_DEFAULTS["system_sizing"]) == expected


def test_bess_operation_group_keys():
    expected = {
        "efficiency_charge", "efficiency_discharge",
        "soc_min_frac", "soc_max_frac", "initial_soc_frac",
        "terminal_soc_equal", "max_cycles_per_day",
    }
    assert set(PROJECT_DEFAULTS["bess_operation"]) == expected


def test_regulatory_group_keys():
    expected = {
        "mode", "retail_tariff_eur_per_mwh", "curtailment_pct",
        "allow_bess_grid_charging", "settlement_minutes",
    }
    assert set(PROJECT_DEFAULTS["regulatory"]) == expected


# ---------------------------------------------------------------------------
# Economic sheet — uncertainty group + plot_daily_scope rename
# ---------------------------------------------------------------------------


def test_econ_defaults_have_eleven_uncertainty_keys():
    expected = {
        "uncertainty_enabled", "uncertainty_compare_sources",
        "uncertainty_n_seeds", "uncertainty_window_hours",
        "uncertainty_commit_hours",
        "uncertainty_dam_enabled", "uncertainty_pv_enabled",
        "uncertainty_load_enabled",
        "uncertainty_sigma_dam", "uncertainty_sigma_pv",
        "uncertainty_sigma_load",
    }
    assert expected.issubset(set(ECON_DEFAULTS))


def test_plot_daily_scope_is_str_enum():
    assert isinstance(ECON_DEFAULTS["plot_daily_scope"], str)
    assert ECON_DEFAULTS["plot_daily_scope"] in {"none", "year1_only", "all"}


def test_plot_yearly_scope_accepts_year1_only():
    flat = {"plot_yearly_scope": "year1_only"}
    parsed = _parse_economic_sheet(flat)
    assert parsed["plot_yearly_scope"] == "year1_only"


def test_plot_daily_scope_invalid_falls_back_to_default(caplog):
    flat = {"plot_daily_scope": "weekly"}
    with caplog.at_level("WARNING"):
        parsed = _parse_economic_sheet(flat)
    # invalid token → keeps default
    assert parsed["plot_daily_scope"] == ECON_DEFAULTS["plot_daily_scope"]


# ---------------------------------------------------------------------------
# Legacy v0.5 warning paths
# ---------------------------------------------------------------------------


def test_legacy_plot_daily_year1_warns_and_is_ignored(caplog):
    flat = {"plot_daily_year1": True}
    with caplog.at_level("WARNING"):
        parsed = _parse_economic_sheet(flat)
    # The renamed key is NOT silently translated.
    assert parsed["plot_daily_scope"] == ECON_DEFAULTS["plot_daily_scope"]
    assert any(
        "plot_daily_year1" in rec.getMessage()
        and "plot_daily_scope" in rec.getMessage()
        for rec in caplog.records
    )


def test_legacy_optimization_keys_warn_once(caplog):
    """All four legacy # optimization keys produce a single combined WARNING."""
    flat = {
        "weight_curtail_tiebreak": 1e-5,
        "weight_cycles_term": 0.0,
        "solver_mip_gap": 0.001,
        "solver_time_limit_seconds": 1800,
        "efficiency_charge": 0.95,  # valid v0.6 key, must still parse
    }
    with caplog.at_level("WARNING"):
        parsed = _parse_project_sheet(flat)
    assert parsed["bess_operation"]["efficiency_charge"] == 0.95
    legacy_warnings = [
        rec.getMessage() for rec in caplog.records
        if "legacy v0.5" in rec.getMessage().lower()
    ]
    assert len(legacy_warnings) == 1
    msg = legacy_warnings[0]
    for key in _LEGACY_OPTIMIZATION_KEYS:
        assert key in msg


# ---------------------------------------------------------------------------
# End-to-end round-trip for the new schema
# ---------------------------------------------------------------------------


def _build_minimal_typed(year: int = 2026) -> dict:
    n = 24
    ts = pd.DataFrame({
        "timestamp": pd.date_range(f"{year}-01-01", periods=n, freq="h"),
        "pv_kwh": [100.0] * n,
        "load_kwh": [50.0] * n,
        "dam_price_eur_per_mwh": [80.0] * n,
    })
    return {
        "ts": ts,
        "project": {
            "system_sizing": dict(PROJECT_DEFAULTS["system_sizing"],
                                  pv_nameplate_kwp=1000.0,
                                  bess_power_kw=500.0,
                                  bess_capacity_kwh=2000.0),
            "bess_operation": dict(PROJECT_DEFAULTS["bess_operation"]),
            "regulatory": dict(PROJECT_DEFAULTS["regulatory"]),
        },
        "economic": dict(ECON_DEFAULTS),
    }


def test_v06_workbook_round_trip_emits_no_warnings(tmp_path, caplog):
    typed = _build_minimal_typed()
    dst = tmp_path / "v06.xlsx"
    write_workbook(typed, dst)
    with caplog.at_level("WARNING", logger="pvbess_opt.io"):
        out = read_workbook(dst)
    # No WARNING records from the io module — schema is clean.
    assert not any(
        rec.levelno >= logging.WARNING and rec.name.startswith("pvbess_opt.io")
        for rec in caplog.records
    )
    # Round-trip preserves all three project groups.
    assert (
        out["project"]["bess_operation"]["efficiency_charge"]
        == typed["project"]["bess_operation"]["efficiency_charge"]
    )
    assert (
        out["project"]["system_sizing"]["pv_nameplate_kwp"]
        == typed["project"]["system_sizing"]["pv_nameplate_kwp"]
    )


def test_v06_workbook_round_trip_uncertainty_group(tmp_path):
    typed = _build_minimal_typed()
    typed["economic"]["uncertainty_enabled"] = True
    typed["economic"]["uncertainty_compare_sources"] = True
    typed["economic"]["uncertainty_sigma_dam"] = 0.25
    dst = tmp_path / "v06_unc.xlsx"
    write_workbook(typed, dst)
    out = read_workbook(dst)
    assert out["economic"]["uncertainty_enabled"] is True
    assert out["economic"]["uncertainty_compare_sources"] is True
    assert out["economic"]["uncertainty_sigma_dam"] == pytest.approx(0.25)


def test_legacy_v05_workbook_loads_with_warnings(tmp_path, caplog):
    """A v0.5 workbook (with optimization group + plot_daily_year1) loads."""
    n = 24
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "pv_kwh": [100.0] * n,
        "load_kwh": [50.0] * n,
        "dam_price_eur_per_mwh": [80.0] * n,
    })
    # Build a v0.5-style flat sheet by hand: # optimization rows present.
    project_rows = [
        {"key": "# system_sizing", "value": "", "unit": "", "notes": ""},
        {"key": "pv_nameplate_kwp", "value": 1000.0, "unit": "", "notes": ""},
        {"key": "bess_power_kw", "value": 500.0, "unit": "", "notes": ""},
        {"key": "# bess_operation", "value": "", "unit": "", "notes": ""},
        {"key": "efficiency_charge", "value": 0.95, "unit": "", "notes": ""},
        {"key": "# regulatory", "value": "", "unit": "", "notes": ""},
        {"key": "mode", "value": "vnb", "unit": "", "notes": ""},
        {"key": "# optimization (legacy v0.5)", "value": "", "unit": "", "notes": ""},
        {"key": "weight_curtail_tiebreak", "value": 1e-5, "unit": "", "notes": ""},
        {"key": "weight_cycles_term", "value": 0.0, "unit": "", "notes": ""},
        {"key": "solver_mip_gap", "value": 0.001, "unit": "", "notes": ""},
        {"key": "solver_time_limit_seconds", "value": 1800, "unit": "", "notes": ""},
    ]
    project_df = pd.DataFrame(project_rows)
    econ_rows = [
        {"key": "project_lifecycle_years", "value": 5, "unit": "", "notes": ""},
        {"key": "project_start_year", "value": 2026, "unit": "", "notes": ""},
        {"key": "discount_rate_pct", "value": 7.0, "unit": "", "notes": ""},
        {"key": "opex_inflation_pct", "value": 1.0, "unit": "", "notes": ""},
        {"key": "revenue_inflation_pct", "value": 2.0, "unit": "", "notes": ""},
        {"key": "capex_pv_eur_per_kw", "value": 525.0, "unit": "", "notes": ""},
        {"key": "capex_bess_eur_per_kw", "value": 200.0, "unit": "", "notes": ""},
        {"key": "capex_licenses_eur_per_kw", "value": 90.0, "unit": "", "notes": ""},
        {"key": "opex_pv_eur_per_kwp", "value": 7.0, "unit": "", "notes": ""},
        {"key": "opex_bess_eur_per_kw", "value": 14.0, "unit": "", "notes": ""},
        {"key": "pv_degradation_year1_pct", "value": 2.5, "unit": "", "notes": ""},
        {"key": "pv_degradation_annual_pct", "value": 0.55, "unit": "", "notes": ""},
        {"key": "bess_degradation_annual_pct", "value": 2.0, "unit": "", "notes": ""},
        {"key": "bess_replacement_year", "value": 0, "unit": "", "notes": ""},
        {"key": "bess_replacement_cost_pct", "value": 50.0, "unit": "", "notes": ""},
        {"key": "plot_daily_year1", "value": True, "unit": "", "notes": ""},
    ]
    econ_df = pd.DataFrame(econ_rows)
    dst = tmp_path / "legacy_v05.xlsx"
    with pd.ExcelWriter(dst, engine="openpyxl") as writer:
        ts.to_excel(writer, sheet_name="timeseries", index=False)
        project_df.to_excel(writer, sheet_name="project", index=False)
        econ_df.to_excel(writer, sheet_name="economic", index=False)
    with caplog.at_level("WARNING"):
        out = read_workbook(dst)
    # Loader still produces a working typed dict.
    assert out["project"]["regulatory"]["mode"] == "vnb"
    assert out["project"]["bess_operation"]["efficiency_charge"] == 0.95
    # plot_daily_year1 was renamed — falls back to default scope.
    assert out["economic"]["plot_daily_scope"] == ECON_DEFAULTS["plot_daily_scope"]
    # Both legacy paths warned.
    msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "plot_daily_year1" in msgs
    assert "legacy v0.5" in msgs.lower()
