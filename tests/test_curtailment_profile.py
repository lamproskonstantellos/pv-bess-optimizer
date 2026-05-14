"""Hourly / monthly curtailment-cap profile tests (Phase 3)."""

from __future__ import annotations

import importlib

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.curtailment import build_per_step_curtailment_frac
from pvbess_opt.io import (
    BESS_SHEET_DEFAULTS,
    ECONOMICS_SHEET_DEFAULTS,
    PROJECT_SHEET_DEFAULTS,
    PV_SHEET_DEFAULTS,
    SIMULATION_SHEET_DEFAULTS,
    read_workbook,
    write_workbook,
)
from pvbess_opt.optimization import run_scenario


def _highs_available() -> bool:
    try:
        importlib.import_module("highspy")
    except ImportError:
        return False
    return True


def _minimal_typed(profile, *, n: int = 48) -> dict:
    timestamps = pd.date_range("2026-06-01", periods=n, freq="h")
    h = np.arange(n).astype(float) % 24
    pv = 4000.0 * np.where((h >= 6) & (h <= 18),
                            np.sin(np.pi * (h - 6) / 12.0), 0.0)
    pv = np.maximum(pv, 0.0)
    load = 3000.0 + 1500.0 * np.exp(-((h - 9) ** 2) / 8.0)
    dam = 100.0 - 50.0 * np.sin(np.pi * (h - 6) / 12.0)
    ts = pd.DataFrame({
        "timestamp": timestamps,
        "pv_kwh": pv,
        "load_kwh": load,
        "dam_price_eur_per_mwh": dam,
    })
    return {
        "ts": ts,
        "project": dict(PROJECT_SHEET_DEFAULTS),
        "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=4500.0),
        "bess": dict(
            BESS_SHEET_DEFAULTS,
            bess_power_kw=5000.0, bess_capacity_kwh=20000.0,
        ),
        "economics": dict(ECONOMICS_SHEET_DEFAULTS),
        "simulation": dict(SIMULATION_SHEET_DEFAULTS),
        "curtailment_profile": profile,
    }


# ---------------------------------------------------------------------------
# Helper logic
# ---------------------------------------------------------------------------


def test_build_per_step_curtailment_frac_24x1():
    profile = np.full(24, 27.0, dtype=float)
    timestamps = pd.date_range("2026-06-01", periods=48, freq="h")
    out = build_per_step_curtailment_frac(timestamps, profile)
    assert out.shape == (48,)
    assert np.allclose(out, 0.27)


def test_build_per_step_curtailment_frac_24x12_picks_month():
    arr = np.zeros((24, 12), dtype=float)
    arr[:, 5] = 50.0   # June caps at 50%
    arr[:, 11] = 10.0  # December caps at 10%
    timestamps = pd.to_datetime([
        "2026-06-15 12:00", "2026-12-15 12:00",
    ])
    out = build_per_step_curtailment_frac(timestamps, profile=arr)
    assert out[0] == pytest.approx(0.50)
    assert out[1] == pytest.approx(0.10)


def test_build_per_step_curtailment_frac_none_falls_back_to_legacy():
    timestamps = pd.date_range("2026-06-01", periods=4, freq="h")
    out = build_per_step_curtailment_frac(timestamps, profile=None)
    assert np.allclose(out, 0.27)


# ---------------------------------------------------------------------------
# I/O round-trip
# ---------------------------------------------------------------------------


def test_hourly_only_format_loads(tmp_path):
    arr = np.linspace(10.0, 50.0, 24).astype(float)
    typed = _minimal_typed(arr)
    dst = tmp_path / "wb.xlsx"
    write_workbook(typed, dst)
    out = read_workbook(dst)
    profile = np.asarray(out["curtailment_profile"], dtype=float)
    assert profile.shape == (24,)
    assert np.allclose(profile, arr)


def test_monthly_format_loads(tmp_path):
    arr = np.zeros((24, 12), dtype=float)
    for m in range(12):
        arr[:, m] = float(m) * 5.0  # 0%, 5%, ..., 55%
    typed = _minimal_typed(arr)
    dst = tmp_path / "wb_monthly.xlsx"
    write_workbook(typed, dst)
    out = read_workbook(dst)
    profile = np.asarray(out["curtailment_profile"], dtype=float)
    assert profile.shape == (24, 12)
    assert np.allclose(profile, arr)


def test_missing_sheet_falls_back_to_scalar(tmp_path, caplog):
    typed = _minimal_typed(np.full(24, 27.0, dtype=float))
    dst = tmp_path / "wb_dropped.xlsx"
    write_workbook(typed, dst)
    # Drop the curtailment_profile sheet.
    with pd.ExcelFile(dst) as xls:
        keep = {
            name: pd.read_excel(dst, sheet_name=name)
            for name in xls.sheet_names if name != "curtailment_profile"
        }
    with pd.ExcelWriter(dst, engine="openpyxl") as writer:
        for name, df in keep.items():
            df.to_excel(writer, sheet_name=name, index=False)
    with caplog.at_level("INFO", logger="pvbess_opt.io"):
        out = read_workbook(dst)
    assert np.allclose(np.asarray(out["curtailment_profile"]), 27.0)
    assert any(
        "curtailment_profile" in rec.getMessage().lower()
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Optimizer regression: constant 27% reproduces the scalar export caps
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_constant_27_matches_v07_export_caps(short_params, short_ts):
    """A constant 27 % profile must produce a flat 0.27 per-step cap
    identical to the scalar path."""
    profile = np.full(24, 27.0, dtype=float)
    params = dict(short_params)
    params["curtailment_profile"] = profile
    res, _ = run_scenario(
        params, short_ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=30,
    )
    cap_series = res["grid_export_cap_kwh"].astype(float).to_numpy()
    expected = float(short_params["p_grid_export_max_kw"]) * (1.0 - 0.27)
    assert np.allclose(cap_series, expected, atol=1e-6)


# ---------------------------------------------------------------------------
# Optimizer enforcement: zero cap during the solar window forces zero export
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_zero_during_solar_window(short_params):
    """Cap = 100 % (zero cap remaining) for hours 09..15 must drive
    pv_to_grid + bess_dis_grid == 0 in those steps."""
    n = 48
    timestamps = pd.date_range("2026-06-01 00:00", periods=n, freq="h")
    h = np.arange(n).astype(float) % 24
    pv = np.maximum(
        4000.0 * np.where(
            (h >= 6) & (h <= 18), np.sin(np.pi * (h - 6) / 12.0), 0.0,
        ),
        0.0,
    )
    load = np.full(n, 100.0, dtype=float)  # tiny load — most PV must be exported or curtailed
    dam = np.full(n, 100.0, dtype=float)
    ts = pd.DataFrame({
        "timestamp": timestamps,
        "pv_kwh": pv,
        "load_kwh": load,
        "dam_price_eur_per_mwh": dam,
    })

    profile = np.full(24, 27.0, dtype=float)
    profile[9:16] = 100.0  # 0 kWh allowed export in 09..15

    params = dict(short_params)
    params["curtailment_profile"] = profile

    res, _ = run_scenario(
        params, ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=30,
    )
    export = (
        res["pv_to_grid_kwh"].to_numpy(dtype=float)
        + res["bess_dis_grid_kwh"].to_numpy(dtype=float)
    )
    hours = pd.to_datetime(res["timestamp"]).dt.hour.to_numpy()
    blocked = (hours >= 9) & (hours <= 15)
    assert (export[blocked] <= 1e-3).all(), export[blocked]


# ---------------------------------------------------------------------------
# v0.8 hour_of_day interval-string formatting + backward-compat parsing
# ---------------------------------------------------------------------------


def test_hour_of_day_renders_as_24h_interval_strings(tmp_path):
    """The xlsx writer renders ``hour_of_day`` as ``HH:00-HH:00`` strings."""
    typed = _minimal_typed(np.full(24, 27.0, dtype=float))
    dst = tmp_path / "wb_hours.xlsx"
    write_workbook(typed, dst)
    raw = pd.read_excel(dst, sheet_name="curtailment_profile")
    expected = [f"{h:02d}:00-{(h + 1):02d}:00" for h in range(24)]
    assert raw["hour_of_day"].astype(str).tolist() == expected


def test_repo_input_xlsx_hour_of_day_uses_interval_strings():
    from pathlib import Path
    repo_xlsx = Path(__file__).resolve().parent.parent / "inputs" / "input.xlsx"
    raw = pd.read_excel(repo_xlsx, sheet_name="curtailment_profile")
    expected = [f"{h:02d}:00-{(h + 1):02d}:00" for h in range(24)]
    assert raw["hour_of_day"].astype(str).tolist() == expected
    assert raw["hour_of_day"].iloc[0] == "00:00-01:00"
    assert raw["hour_of_day"].iloc[23] == "23:00-24:00"


def test_loader_parses_legacy_integer_hour_of_day(tmp_path):
    """Legacy workbooks with integer 0..23 hour_of_day still load."""
    timestamps = pd.date_range("2026-06-01", periods=24, freq="h")
    ts = pd.DataFrame({
        "timestamp": timestamps,
        "pv_kwh": np.zeros(24),
        "load_kwh": np.full(24, 100.0),
        "dam_price_eur_per_mwh": np.full(24, 80.0),
    })
    project_kv = pd.DataFrame({
        "key": ["mode", "p_grid_export_max_kw"],
        "value": ["vnb", 5000.0],
        "unit": ["", ""],
        "notes": ["", ""],
    })
    pv_kv = pd.DataFrame({
        "key": ["pv_nameplate_kwp"],
        "value": [1000.0],
        "unit": [""],
        "notes": [""],
    })
    bess_kv = pd.DataFrame({
        "key": ["bess_power_kw", "bess_capacity_kwh"],
        "value": [500.0, 2000.0],
        "unit": ["", ""],
        "notes": ["", ""],
    })
    economics_kv = pd.DataFrame({"key": [], "value": [], "unit": [], "notes": []})
    simulation_kv = pd.DataFrame({"key": [], "value": [], "unit": [], "notes": []})
    legacy_curt = pd.DataFrame({
        "hour_of_day": np.arange(24, dtype=int),
        "curtailment_pct": np.full(24, 27.0, dtype=float),
    })
    dst = tmp_path / "legacy_hours.xlsx"
    with pd.ExcelWriter(dst, engine="openpyxl") as writer:
        ts.to_excel(writer, sheet_name="timeseries", index=False)
        project_kv.to_excel(writer, sheet_name="project", index=False)
        pv_kv.to_excel(writer, sheet_name="pv", index=False)
        bess_kv.to_excel(writer, sheet_name="bess", index=False)
        economics_kv.to_excel(writer, sheet_name="economics", index=False)
        simulation_kv.to_excel(writer, sheet_name="simulation", index=False)
        legacy_curt.to_excel(
            writer, sheet_name="curtailment_profile", index=False,
        )
    out = read_workbook(dst)
    profile = np.asarray(out["curtailment_profile"], dtype=float)
    assert profile.shape == (24,)
    assert np.allclose(profile, 27.0)


def test_loader_parses_v08_interval_string_hour_of_day(tmp_path):
    """The v0.8 string format ``HH:00-HH:00`` round-trips through the loader."""
    typed = _minimal_typed(np.linspace(15.0, 35.0, 24))
    dst = tmp_path / "wb_intervals.xlsx"
    write_workbook(typed, dst)
    out = read_workbook(dst)
    profile = np.asarray(out["curtailment_profile"], dtype=float)
    assert profile.shape == (24,)
    assert np.allclose(profile, np.linspace(15.0, 35.0, 24))


def test_loader_rejects_garbage_hour_of_day(tmp_path):
    """Cells that cannot be parsed as 0..23 raise ValueError."""
    from pvbess_opt.io import _parse_hour_of_day
    with pytest.raises(ValueError, match="cannot parse"):
        _parse_hour_of_day("not a time")
    with pytest.raises(ValueError, match="must be in 0..23"):
        _parse_hour_of_day(24)
    with pytest.raises(ValueError, match="must be in 0..23"):
        _parse_hour_of_day("99:00-100:00")


def test_parse_hour_of_day_accepts_both_formats():
    from pvbess_opt.io import _parse_hour_of_day
    assert _parse_hour_of_day(0) == 0
    assert _parse_hour_of_day(23) == 23
    assert _parse_hour_of_day("00:00-01:00") == 0
    assert _parse_hour_of_day("23:00-24:00") == 23
    assert _parse_hour_of_day("12:00-13:00") == 12
    # Forgiving: leading-digit run is what matters.
    assert _parse_hour_of_day("7") == 7
    assert _parse_hour_of_day("07") == 7
    assert _parse_hour_of_day("07:00") == 7
