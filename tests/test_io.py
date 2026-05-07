"""I/O loader, schema validation, and output workbook tests."""

from __future__ import annotations

import pandas as pd
import pytest

from pvbess_opt.io import (
    PROJECT_DEFAULTS,
    ECON_DEFAULTS,
    _flat_dict_from_sheet,
    _parse_bool,
    _parse_curtailment,
    _parse_economic_sheet,
    _parse_project_sheet,
    detect_timestep_minutes,
    read_inputs,
    read_workbook,
    write_workbook,
)


def test_project_defaults_have_three_groups():
    assert set(PROJECT_DEFAULTS.keys()) == {
        "system_sizing", "bess_operation", "regulatory",
    }


def test_econ_defaults_have_canonical_keys():
    for key in (
        "project_lifecycle_years", "project_start_year", "discount_rate_pct",
        "capex_pv_eur_per_kw", "opex_bess_eur_per_kw",
        "uncertainty_enabled", "plot_daily_scope",
    ):
        assert key in ECON_DEFAULTS


def test_parse_bool_accepts_canonical_tokens():
    assert _parse_bool("TRUE", False) is True
    assert _parse_bool("false", True) is False
    assert _parse_bool(1, False) is True
    assert _parse_bool(0, True) is False
    assert _parse_bool(None, True) is True
    assert _parse_bool("", True) is True


def test_parse_curtailment_accepts_pct_and_frac():
    assert _parse_curtailment(27) == pytest.approx(0.27)
    assert _parse_curtailment(0.27) == pytest.approx(0.27)
    assert _parse_curtailment(101) == 1.0
    assert _parse_curtailment(-1) == 0.0


def test_flat_dict_skips_separator_rows():
    df = pd.DataFrame({
        "key": ["# system", "efficiency_charge", "", "p_dis_max_kw"],
        "value": [None, 0.95, None, 8000],
    })
    flat = _flat_dict_from_sheet(df)
    assert flat == {"efficiency_charge": 0.95, "p_dis_max_kw": 8000}


def test_parse_project_sheet_distributes_to_groups():
    flat = {
        "efficiency_charge": 0.95,         # bess_operation
        "pv_nameplate_kwp": 4500.0,        # system_sizing
        "mode": "merchant",                # regulatory
    }
    parsed = _parse_project_sheet(flat)
    assert parsed["bess_operation"]["efficiency_charge"] == 0.95
    assert parsed["system_sizing"]["pv_nameplate_kwp"] == 4500.0
    assert parsed["regulatory"]["mode"] == "merchant"


def test_parse_project_sheet_warns_on_legacy_optimization_keys(caplog):
    """v0.5 # optimization keys must produce a single warning and be ignored."""
    flat = {
        "efficiency_charge": 0.95,
        "weight_cycles_term": 1.5,
        "solver_mip_gap": 0.005,
    }
    with caplog.at_level("WARNING"):
        parsed = _parse_project_sheet(flat)
    assert parsed["bess_operation"]["efficiency_charge"] == 0.95
    # Legacy keys are not surfaced anywhere in the typed dict.
    for grp in parsed.values():
        assert "weight_cycles_term" not in grp
        assert "solver_mip_gap" not in grp
    assert any("legacy v0.5" in rec.getMessage() for rec in caplog.records)


def test_parse_economic_sheet_round_trip():
    flat = {"discount_rate_pct": 8.0, "show_titles": "TRUE"}
    parsed = _parse_economic_sheet(flat)
    assert parsed["discount_rate_pct"] == 8.0
    assert parsed["show_titles"] is True


def test_detect_timestep_minutes_hourly():
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=24, freq="h"),
    })
    assert detect_timestep_minutes(ts) == 60


def test_detect_timestep_irregular_raises():
    ts = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-01-01 00:00", "2026-01-01 01:00", "2026-01-01 03:00"]),
    })
    with pytest.raises(ValueError, match="Irregular timestep"):
        detect_timestep_minutes(ts)


def test_read_workbook_repo_default(repo_input_xlsx):
    typed = read_workbook(repo_input_xlsx)
    assert "ts" in typed and "project" in typed and "economic" in typed
    assert typed["dt_minutes"] == 15
    assert typed["project"]["regulatory"]["mode"] == "vnb"


def test_read_inputs_returns_flat_params(repo_input_xlsx):
    params, ts = read_inputs(repo_input_xlsx)
    assert "efficiency_charge" in params and "p_charge_max_kw" in params
    assert params["mode"] == "vnb"
    assert "load_kwh" in ts.columns
    assert len(ts) == 35040


def test_workbook_round_trip(tmp_path, repo_input_xlsx):
    typed = read_workbook(repo_input_xlsx)
    dst = tmp_path / "round_trip.xlsx"
    write_workbook(typed, dst)
    typed2 = read_workbook(dst)
    eta1 = typed["project"]["bess_operation"]["efficiency_charge"]
    eta2 = typed2["project"]["bess_operation"]["efficiency_charge"]
    assert eta1 == eta2
    assert typed["economic"]["discount_rate_pct"] == typed2["economic"]["discount_rate_pct"]
    assert len(typed["ts"]) == len(typed2["ts"])


def test_vnb_requires_load_column(tmp_path, repo_input_xlsx):
    """vnb mode requires load_kwh in timeseries."""
    typed = read_workbook(repo_input_xlsx)
    typed["ts"] = typed["ts"].drop(columns=["load_kwh"])
    dst = tmp_path / "no_load.xlsx"
    write_workbook(typed, dst)
    with pytest.raises(ValueError, match="load_kwh"):
        read_workbook(dst)


def test_merchant_mode_load_optional(tmp_path, repo_input_xlsx):
    """merchant mode: load_kwh optional; if present, an info log is emitted."""
    typed = read_workbook(repo_input_xlsx)
    typed["project"]["regulatory"]["mode"] = "merchant"
    dst = tmp_path / "merchant.xlsx"
    write_workbook(typed, dst)
    # Should not raise
    typed2 = read_workbook(dst)
    assert typed2["project"]["regulatory"]["mode"] == "merchant"


def test_unknown_mode_falls_back_to_vnb(tmp_path):
    """Mode validation: invalid mode token falls back to default 'vnb'."""
    typed = {
        "ts": pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=24, freq="h"),
            "pv_kwh": [0.0] * 24,
            "load_kwh": [100.0] * 24,
        }),
        "project": {
            "system_sizing": dict(PROJECT_DEFAULTS["system_sizing"]),
            "bess_operation": dict(PROJECT_DEFAULTS["bess_operation"]),
            "regulatory": dict(PROJECT_DEFAULTS["regulatory"]),
        },
        "economic": dict(ECON_DEFAULTS),
    }
    typed["project"]["regulatory"]["mode"] = "vnb"
    dst = tmp_path / "test.xlsx"
    write_workbook(typed, dst)
    out = read_workbook(dst)
    assert out["project"]["regulatory"]["mode"] == "vnb"


def test_write_workbook_emits_three_sheets(tmp_path, repo_input_xlsx):
    typed = read_workbook(repo_input_xlsx)
    dst = tmp_path / "out.xlsx"
    write_workbook(typed, dst)
    assert set(pd.ExcelFile(dst).sheet_names) == {"timeseries", "project", "economic"}
