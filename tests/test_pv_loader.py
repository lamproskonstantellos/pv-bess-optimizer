"""Loader contracts for the single ``pv_kwh`` column.

The loader sources PV as follows:

1. **File path** — the ``pv_kwh`` column (or an external ``timeseries_path``)
   is consumed **verbatim** as absolute kWh per step.  ``pv_nameplate_kwp``
   is metadata (per-kW CAPEX / OPEX and the sizing-sweep axis), never a
   rescale target.
2. **Deprecated fallback** — the legacy ``pv_kwh_override`` column is read
   only when ``pv_kwh`` is empty (so old files keep their data); it is used
   verbatim, a one-time deprecation warning is emitted, and partial NaN is
   rejected.  When ``pv_kwh`` is filled the override column is ignored.

These contracts pin the loader's behaviour for both paths.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.io import read_workbook, write_workbook

ROOT = Path(__file__).resolve().parent.parent
REPO_INPUT_XLSX = ROOT / "inputs" / "input.xlsx"


def _repo_pv_kwh() -> np.ndarray:
    """The pv_kwh column as stored in the repo workbook (absolute kWh)."""
    return pd.read_excel(
        REPO_INPUT_XLSX, sheet_name="timeseries",
    )["pv_kwh"].to_numpy(dtype=float)


# ---------------------------------------------------------------------------
# 1. File path — the pv_kwh column is used verbatim (no nameplate rescale).
# ---------------------------------------------------------------------------


def test_default_workbook_pv_used_verbatim():
    """The canonical workbook's pv_kwh column is loaded unchanged."""
    typed = read_workbook(REPO_INPUT_XLSX)
    loaded = typed["ts"]["pv_kwh"].to_numpy(dtype=float)
    assert np.allclose(loaded, _repo_pv_kwh(), rtol=0.0, atol=1e-6)


def test_nameplate_does_not_rescale_pv(tmp_path):
    """Changing pv_nameplate_kwp must not touch the pv_kwh magnitude."""
    typed = read_workbook(REPO_INPUT_XLSX)
    # An arbitrary nameplate unrelated to the column's annual energy.
    typed["pv"]["pv_nameplate_kwp"] = 999.0
    out = tmp_path / "renamed.xlsx"
    write_workbook(typed, out)
    loaded = read_workbook(out)["ts"]["pv_kwh"].to_numpy(dtype=float)
    assert np.allclose(loaded, _repo_pv_kwh(), rtol=0.0, atol=1e-6)


# ---------------------------------------------------------------------------
# 2. Deprecated pv_kwh_override fallback (verbatim, only when pv_kwh empty)
# ---------------------------------------------------------------------------


def _override_workbook(
    tmp_path: Path,
    override: np.ndarray,
    *,
    pv_kwp: float = 1000.0,
    clear_pv_kwh: bool = False,
) -> Path:
    typed = read_workbook(REPO_INPUT_XLSX)
    ts = typed["ts"].copy()
    if clear_pv_kwh:
        # Empty the single PV column so the deprecated override is the
        # only PV data — the backward-compatible fallback path.
        ts["pv_kwh"] = np.nan
    ts["pv_kwh_override"] = override.astype(float)
    typed["ts"] = ts
    typed["pv"]["pv_nameplate_kwp"] = float(pv_kwp)
    out = tmp_path / "override.xlsx"
    write_workbook(typed, out)
    return out


def test_override_used_as_fallback_when_pv_kwh_empty(tmp_path, caplog):
    """With pv_kwh empty the deprecated pv_kwh_override is used verbatim and
    a one-time deprecation warning is emitted."""
    n = 35040
    hour_of_day = (np.arange(n) // 4) % 24
    override = np.where(
        (hour_of_day >= 6) & (hour_of_day < 18),
        100.0 + 10.0 * (hour_of_day - 6),  # arbitrary deterministic shape
        0.0,
    )
    out = _override_workbook(tmp_path, override, pv_kwp=1000.0, clear_pv_kwh=True)
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io_read"):
        loaded = read_workbook(out)["ts"]["pv_kwh"].to_numpy(dtype=float)
    assert np.allclose(loaded, override)
    assert any(
        "pv_kwh_override is deprecated" in r.getMessage() for r in caplog.records
    )


def test_filled_pv_kwh_ignores_override(tmp_path):
    """When pv_kwh carries data the override column is ignored (pv_kwh wins
    verbatim) and dropped from the loaded frame."""
    n = 35040
    override = np.full(n, 999.0)  # would be obvious if it leaked through
    # clear_pv_kwh defaults to False, so the repo pv_kwh shape stays filled.
    out = _override_workbook(tmp_path, override, pv_kwp=1000.0)
    loaded = read_workbook(out)["ts"]["pv_kwh"].to_numpy(dtype=float)
    assert not np.allclose(loaded, override)
    assert np.allclose(loaded, _repo_pv_kwh(), rtol=0.0, atol=1e-6)


# ---------------------------------------------------------------------------
# 3. Override is verbatim — nameplate never changes its magnitude
# ---------------------------------------------------------------------------


def test_override_verbatim_independent_of_nameplate(tmp_path):
    n = 35040
    override = np.full(n, 50.0)  # constant 50 kWh per 15-min step
    annual_sum = float(override.sum())
    # Used verbatim when pv_kwh is empty, so doubling the nameplate must not
    # change the annual sum.
    out = _override_workbook(tmp_path, override, pv_kwp=2000.0, clear_pv_kwh=True)
    loaded = read_workbook(out)["ts"]["pv_kwh"].to_numpy(dtype=float)
    assert float(loaded.sum()) == pytest.approx(annual_sum, rel=1e-12)


# ---------------------------------------------------------------------------
# 4. Override column is dropped from the loaded frame
# ---------------------------------------------------------------------------


def test_loader_drops_override_column_after_use(tmp_path):
    n = 35040
    override = np.full(n, 50.0)
    out = _override_workbook(tmp_path, override)
    typed = read_workbook(out)
    assert "pv_kwh_override" not in typed["ts"].columns


# ---------------------------------------------------------------------------
# 5. Partial NaN override raises
# ---------------------------------------------------------------------------


def test_partial_nan_override_raises(tmp_path):
    n = 35040
    override = np.full(n, 50.0)
    override[:n // 2] = np.nan  # half empty, half filled
    # With pv_kwh empty the override is the fallback source; a partial-NaN
    # fallback is rejected rather than silently producing a garbage profile.
    out = _override_workbook(tmp_path, override, clear_pv_kwh=True)
    with pytest.raises(ValueError, match="pv_kwh_override has"):
        read_workbook(out)


# ---------------------------------------------------------------------------
# 6. All-NaN override falls back to the pv_kwh column (verbatim)
# ---------------------------------------------------------------------------


def test_all_nan_override_falls_back_to_pv_column(tmp_path):
    n = 35040
    override = np.full(n, np.nan)
    out = _override_workbook(tmp_path, override, pv_kwp=1000.0)
    typed = read_workbook(out)
    # pv_kwh equals the workbook's stored column (used verbatim) and the
    # override column has been stripped from the frame.
    loaded = typed["ts"]["pv_kwh"].to_numpy(dtype=float)
    assert np.allclose(loaded, _repo_pv_kwh(), rtol=0.0, atol=1e-6)
    assert "pv_kwh_override" not in typed["ts"].columns


# ---------------------------------------------------------------------------
# 7. Implausible implied specific production emits WARNING (override path)
# ---------------------------------------------------------------------------


def test_implausible_specific_production_warns(tmp_path, caplog):
    n = 35040
    # 1 MW nameplate with override summing to 4 000 000 kWh ⇒
    # implied SP = 4000 kWh/kWp, well above the 500-2500 band.
    override = np.full(n, 4_000_000.0 / n)
    out = _override_workbook(tmp_path, override, pv_kwp=1000.0, clear_pv_kwh=True)
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io_read"):
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
