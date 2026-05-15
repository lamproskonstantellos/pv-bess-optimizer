"""Loader contracts for the PV column.

The repo workbook ships with a 1 MW × 1500 kWh/kWp default (1 500 000
kWh annual).  The loader supports two paths:

1. **Default path** — ``pv_nameplate_kwp`` × ``specific_production_kwh_per_kwp``
   rescales the workbook ``pv_kwh`` column proportionally.  Same shape,
   different magnitude.
2. **Override path** — if the optional ``pv_kwh_override`` column is
   present **and complete**, it is used verbatim and the rescaling is
   skipped.  Partial NaN is rejected.

These 11 contracts pin the loader's behaviour for both paths.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.io import (
    _rescale_pv_to_user_target,
    _resolve_pv_column,
    read_workbook,
    write_workbook,
)

ROOT = Path(__file__).resolve().parent.parent
REPO_INPUT_XLSX = ROOT / "inputs" / "input.xlsx"


# ---------------------------------------------------------------------------
# 1. Default pass-through — workbook ships at its declared target so the
#    loader must NOT touch pv_kwh.
# ---------------------------------------------------------------------------


def test_default_workbook_pass_through():
    raw_ts = pd.read_excel(REPO_INPUT_XLSX, sheet_name="timeseries")
    typed = read_workbook(REPO_INPUT_XLSX)
    raw_pv = raw_ts["pv_kwh"].to_numpy(dtype=float)
    loaded_pv = typed["ts"]["pv_kwh"].to_numpy(dtype=float)
    assert float(np.abs(loaded_pv - raw_pv).max()) < 1.0e-9


# ---------------------------------------------------------------------------
# 2-3. Rescaling on user-supplied nameplate / specific production
# ---------------------------------------------------------------------------


def _user_workbook(
    tmp_path: Path, *, pv_kwp: float, sp: float,
) -> Path:
    typed = read_workbook(REPO_INPUT_XLSX)
    typed["pv"]["pv_nameplate_kwp"] = float(pv_kwp)
    typed["pv"]["specific_production_kwh_per_kwp"] = float(sp)
    out = tmp_path / "user.xlsx"
    write_workbook(typed, out)
    return out


def test_loader_rescales_to_user_nameplate_only(tmp_path):
    raw_pv = pd.read_excel(
        REPO_INPUT_XLSX, sheet_name="timeseries",
    )["pv_kwh"].to_numpy(dtype=float)
    out = _user_workbook(tmp_path, pv_kwp=2000.0, sp=1500.0)
    pv = read_workbook(out)["ts"]["pv_kwh"].to_numpy(dtype=float)
    assert float(pv.sum()) == pytest.approx(2000.0 * 1500.0, rel=1e-9)
    # Shape preserved: ratio of any two non-zero indices is unchanged.
    nonzero = raw_pv > 1.0e-9
    ratios = pv[nonzero] / raw_pv[nonzero]
    assert ratios.std() < 1.0e-9


def test_loader_rescales_to_user_specific_production_only(tmp_path):
    out = _user_workbook(tmp_path, pv_kwp=1000.0, sp=1600.0)
    pv = read_workbook(out)["ts"]["pv_kwh"].to_numpy(dtype=float)
    assert float(pv.sum()) == pytest.approx(1000.0 * 1600.0, rel=1e-9)


# ---------------------------------------------------------------------------
# 4. Loader logs the rescale factor only when rescaling actually fires
# ---------------------------------------------------------------------------


def test_loader_logs_rescale_factor_only_on_rescale(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="pvbess_opt.io"):
        read_workbook(REPO_INPUT_XLSX)
    rescaled = [r for r in caplog.records if "rescaled" in r.getMessage()]
    assert not rescaled, "default workbook unexpectedly triggered rescaling"

    caplog.clear()
    out = _user_workbook(tmp_path, pv_kwp=2000.0, sp=1600.0)
    with caplog.at_level(logging.INFO, logger="pvbess_opt.io"):
        read_workbook(out)
    rescaled = [r for r in caplog.records if "rescaled" in r.getMessage()]
    assert len(rescaled) == 1
    msg = rescaled[0].getMessage()
    assert "2000.0 kWp" in msg
    assert "1600.0000 kWh/kWp" in msg


# ---------------------------------------------------------------------------
# 5. Zero nameplate skips rescaling
# ---------------------------------------------------------------------------


def test_zero_nameplate_skips_rescaling():
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=4, freq="h"),
        "pv_kwh": [10.0, 20.0, 30.0, 40.0],
        "load_kwh": [1.0] * 4,
    })
    out = _rescale_pv_to_user_target(
        ts, pv_nameplate_kwp=0.0, specific_production_kwh_per_kwp=1500.0,
    )
    assert (out["pv_kwh"].to_numpy() == ts["pv_kwh"].to_numpy()).all()


# ---------------------------------------------------------------------------
# 6. Override column used verbatim when fully populated
# ---------------------------------------------------------------------------


def _override_workbook(
    tmp_path: Path, override: np.ndarray, *, pv_kwp: float = 1000.0,
) -> Path:
    typed = read_workbook(REPO_INPUT_XLSX)
    ts = typed["ts"].copy()
    ts["pv_kwh_override"] = override.astype(float)
    typed["ts"] = ts
    typed["pv"]["pv_nameplate_kwp"] = float(pv_kwp)
    out = tmp_path / "override.xlsx"
    write_workbook(typed, out)
    return out


def test_loader_uses_pv_kwh_override_when_present(tmp_path):
    # Build a synthetic override: zero at night, ramped during daylight.
    n = 35040
    hour_of_day = (np.arange(n) // 4) % 24
    override = np.where(
        (hour_of_day >= 6) & (hour_of_day < 18),
        100.0 + 10.0 * (hour_of_day - 6),  # arbitrary deterministic shape
        0.0,
    )
    out = _override_workbook(tmp_path, override, pv_kwp=1000.0)
    loaded = read_workbook(out)["ts"]["pv_kwh"].to_numpy(dtype=float)
    assert np.allclose(loaded, override)


# ---------------------------------------------------------------------------
# 7. Override bypasses the rescaling pipeline
# ---------------------------------------------------------------------------


def test_loader_skips_rescaling_when_override_used(tmp_path):
    n = 35040
    override = np.full(n, 50.0)  # constant 50 kWh per 15-min step
    annual_sum = float(override.sum())
    # Doubling nameplate would normally double the annual sum via
    # rescaling, but override must bypass that path.
    out = _override_workbook(tmp_path, override, pv_kwp=2000.0)
    loaded = read_workbook(out)["ts"]["pv_kwh"].to_numpy(dtype=float)
    assert float(loaded.sum()) == pytest.approx(annual_sum, rel=1e-12)


# ---------------------------------------------------------------------------
# 8. Override column is dropped from the loaded frame
# ---------------------------------------------------------------------------


def test_loader_drops_override_column_after_use(tmp_path):
    n = 35040
    override = np.full(n, 50.0)
    out = _override_workbook(tmp_path, override)
    typed = read_workbook(out)
    assert "pv_kwh_override" not in typed["ts"].columns


# ---------------------------------------------------------------------------
# 9. Partial NaN override raises
# ---------------------------------------------------------------------------


def test_partial_nan_override_raises(tmp_path):
    n = 35040
    override = np.full(n, 50.0)
    override[:n // 2] = np.nan  # half empty, half filled
    out = _override_workbook(tmp_path, override)
    with pytest.raises(ValueError, match="pv_kwh_override has"):
        read_workbook(out)


# ---------------------------------------------------------------------------
# 10. All-NaN override falls back to the rescaling path
# ---------------------------------------------------------------------------


def test_all_nan_override_falls_back_to_rescaling(tmp_path):
    n = 35040
    override = np.full(n, np.nan)
    out = _override_workbook(tmp_path, override, pv_kwp=1000.0)
    typed = read_workbook(out)
    # pv_kwh equals the workbook's default (rescale factor 1.0) and the
    # override column has been stripped from the frame.
    raw_pv = pd.read_excel(
        REPO_INPUT_XLSX, sheet_name="timeseries",
    )["pv_kwh"].to_numpy(dtype=float)
    loaded = typed["ts"]["pv_kwh"].to_numpy(dtype=float)
    assert float(np.abs(loaded - raw_pv).max()) < 1.0e-9
    assert "pv_kwh_override" not in typed["ts"].columns


# ---------------------------------------------------------------------------
# 11. Implausible implied specific production emits WARNING
# ---------------------------------------------------------------------------


def test_implausible_specific_production_warns(tmp_path, caplog):
    n = 35040
    # 1 MW nameplate with override summing to 4 000 000 kWh ⇒
    # implied SP = 4000 kWh/kWp, well above the 500-2500 band.
    override = np.full(n, 4_000_000.0 / n)
    out = _override_workbook(tmp_path, override, pv_kwp=1000.0)
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io"):
        read_workbook(out)
    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and "implied specific production" in r.getMessage()
        and "outside the plausible" in r.getMessage()
    ]
    assert warnings, (
        "expected at least one WARNING about implausible implied SP, "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )
