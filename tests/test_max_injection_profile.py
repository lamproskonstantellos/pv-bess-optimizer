"""Hourly / monthly max-injection-cap profile tests."""

from __future__ import annotations

import importlib

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.io import (
    BESS_SHEET_DEFAULTS,
    ECONOMICS_SHEET_DEFAULTS,
    PROJECT_SHEET_DEFAULTS,
    PV_SHEET_DEFAULTS,
    SIMULATION_SHEET_DEFAULTS,
    read_workbook,
    write_workbook,
)
from pvbess_opt.max_injection import build_per_step_max_injection_frac
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
        "max_injection_profile": profile,
    }


# ---------------------------------------------------------------------------
# Helper logic
# ---------------------------------------------------------------------------


def test_build_per_step_max_injection_frac_24x1():
    """73 % allowed-to-inject → 0.73 fraction at every step."""
    profile = np.full(24, 73.0, dtype=float)
    timestamps = pd.date_range("2026-06-01", periods=48, freq="h")
    out = build_per_step_max_injection_frac(timestamps, profile)
    assert out.shape == (48,)
    assert np.allclose(out, 0.73)


def test_build_per_step_max_injection_frac_24x12_picks_month():
    arr = np.zeros((24, 12), dtype=float)
    arr[:, 5] = 50.0   # June allows 50% of cap
    arr[:, 11] = 10.0  # December allows 10% of cap
    timestamps = pd.to_datetime([
        "2026-06-15 12:00", "2026-12-15 12:00",
    ])
    out = build_per_step_max_injection_frac(timestamps, profile=arr)
    assert out[0] == pytest.approx(0.50)
    assert out[1] == pytest.approx(0.10)


def test_build_per_step_max_injection_frac_none_falls_back_to_default():
    """``profile=None`` falls back to the no-curtailment default
    (``DEFAULT_MAX_INJECTION_PCT_HOURLY = 100``)."""
    timestamps = pd.date_range("2026-06-01", periods=4, freq="h")
    out = build_per_step_max_injection_frac(timestamps, profile=None)
    assert np.allclose(out, 1.0)


# ---------------------------------------------------------------------------
# I/O round-trip
# ---------------------------------------------------------------------------


def test_hourly_only_format_loads(tmp_path):
    arr = np.linspace(10.0, 50.0, 24).astype(float)
    typed = _minimal_typed(arr)
    dst = tmp_path / "wb.xlsx"
    write_workbook(typed, dst)
    out = read_workbook(dst)
    profile = np.asarray(out["max_injection_profile"], dtype=float)
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
    profile = np.asarray(out["max_injection_profile"], dtype=float)
    assert profile.shape == (24, 12)
    assert np.allclose(profile, arr)


def test_missing_sheet_falls_back_to_default(tmp_path, caplog):
    """When the max_injection_profile sheet is absent, the loader
    logs INFO and uses the no-curtailment default."""
    typed = _minimal_typed(np.full(24, 73.0, dtype=float))
    dst = tmp_path / "wb_dropped.xlsx"
    write_workbook(typed, dst)
    # Drop the max_injection_profile sheet with openpyxl — cell types stay
    # faithful.  (A pandas read->write round trip can coerce numeric 0/1
    # cells into genuine boolean cells, which the boolean-in-numeric-field
    # guard rightly rejects.)
    from openpyxl import load_workbook

    wb = load_workbook(dst)
    del wb["max_injection_profile"]
    wb.save(dst)
    with caplog.at_level("INFO", logger="pvbess_opt.io"):
        out = read_workbook(dst)
    assert np.allclose(np.asarray(out["max_injection_profile"]), 100.0)
    assert any(
        "max_injection_profile" in rec.getMessage().lower()
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Optimizer: a constant 73 % max-injection profile applies the export
# cap as 73 % of p_grid_export_max_kw in every hour.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_constant_73_pct_caps_export(short_params, short_ts):
    """A constant 73 % max-injection profile produces a flat 0.73
    per-step fraction and caps every hour's export at
    p_grid_export_max_kw * 0.73."""
    profile = np.full(24, 73.0, dtype=float)
    params = dict(short_params)
    params["max_injection_profile"] = profile
    res, _ = run_scenario(
        params, short_ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=30,
    )
    cap_series = res["grid_export_cap_kwh"].astype(float).to_numpy()
    expected = float(short_params["p_grid_export_max_kw"]) * 0.73
    assert np.allclose(cap_series, expected, atol=1e-6)


# ---------------------------------------------------------------------------
# Optimizer enforcement: zero allowed-injection during the solar window
# forces zero export
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_zero_during_solar_window(short_params):
    """max-injection = 0 % for hours 09..15 must drive pv_to_grid +
    bess_dis_grid == 0 in those steps."""
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

    profile = np.full(24, 73.0, dtype=float)
    profile[9:16] = 0.0  # 0 % allowed → no export 09..15

    params = dict(short_params)
    params["max_injection_profile"] = profile

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
# hour_of_day interval-string formatting + parser
# ---------------------------------------------------------------------------


def test_hour_of_day_renders_as_24h_interval_strings(tmp_path):
    """The xlsx writer renders ``hour_of_day`` as ``HH:00-HH:00`` strings."""
    typed = _minimal_typed(np.full(24, 73.0, dtype=float))
    dst = tmp_path / "wb_hours.xlsx"
    write_workbook(typed, dst)
    raw = pd.read_excel(dst, sheet_name="max_injection_profile")
    expected = [f"{h:02d}:00-{(h + 1):02d}:00" for h in range(24)]
    assert raw["hour_of_day"].astype(str).tolist() == expected


def test_repo_input_xlsx_hour_of_day_uses_interval_strings():
    from pathlib import Path
    repo_xlsx = Path(__file__).resolve().parent.parent / "inputs" / "input.xlsx"
    raw = pd.read_excel(repo_xlsx, sheet_name="max_injection_profile")
    expected = [f"{h:02d}:00-{(h + 1):02d}:00" for h in range(24)]
    assert raw["hour_of_day"].astype(str).tolist() == expected
    assert raw["hour_of_day"].iloc[0] == "00:00-01:00"
    assert raw["hour_of_day"].iloc[23] == "23:00-24:00"


def test_loader_parses_interval_string_hour_of_day(tmp_path):
    """The ``HH:00-HH:00`` interval string round-trips through the loader."""
    typed = _minimal_typed(np.linspace(15.0, 35.0, 24))
    dst = tmp_path / "wb_intervals.xlsx"
    write_workbook(typed, dst)
    out = read_workbook(dst)
    profile = np.asarray(out["max_injection_profile"], dtype=float)
    assert profile.shape == (24,)
    assert np.allclose(profile, np.linspace(15.0, 35.0, 24))


def test_loader_rejects_garbage_hour_of_day(tmp_path):
    """Cells that cannot be parsed as 0..23 raise ValueError."""
    from pvbess_opt.io import _parse_hour_of_day
    with pytest.raises(ValueError, match="cannot parse"):
        _parse_hour_of_day("not a time")
    with pytest.raises(ValueError, match=r"must be in 0\.\.23"):
        _parse_hour_of_day(24)
    with pytest.raises(ValueError, match=r"must be in 0\.\.23"):
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
