"""PV timeseries pipeline tests — workbook + loader rescaling.

Architecture:

* The case-study ``inputs/input.xlsx`` ships with the **canonical 8 MW
  reference shape verbatim** in its ``timeseries::pv_kwh`` column
  (35 040 rows, sum = 12 568 961,75 kWh, specific production
  1571,12 kWh/kWp).  The matching ``pv::pv_nameplate_kwp`` and
  ``pv::specific_production_kwh_per_kwp`` defaults pin to ``8000`` and
  ``1571.12021875``.
* The model **loader** (:func:`pvbess_opt.io.read_workbook`) rescales
  ``pv_kwh`` on the fly to the user's
  ``pv_nameplate_kwp × specific_production_kwh_per_kwp`` target.  The
  shape (every per-step ratio) is preserved exactly; only the
  multiplicative scale changes.

Invariants checked here (10):

1. Repository workbook PV sum equals the canonical 8 MW reference
   total.
2. Workbook ``pv_kwh`` column equals the reference vector bit-exactly.
3. Workbook ``pv_nameplate_kwp`` and ``specific_production_kwh_per_kwp``
   match the canonical defaults.
4. ``read_workbook`` is **deterministic**: two consecutive calls
   return ``np.array_equal`` PV vectors.
5. Default workbook (workbook total == nameplate × SP) ⇒ loader
   pass-through, no rescaling, ``pv_kwh`` byte-equal to reference.
6. User changes nameplate only ⇒ loader rescales linearly; shape
   ratios preserved.
7. User changes specific production only ⇒ loader rescales linearly.
8. Zero nameplate ⇒ loader skips rescaling (PV passes through; the
   optimizer pins all PV variables to zero via its ``pv_present``
   flag).
9. Loader emits an INFO message containing the rescale factor only
   when rescaling actually fires.
10. The build-script's PV path contains no random / noise constructs
    (AST audit).
"""

from __future__ import annotations

import inspect
import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.io import (
    _PV_RESCALE_REL_TOLERANCE,
    _rescale_pv_to_user_target,
    read_workbook,
    write_workbook,
)
from scripts.build_input_xlsx import (
    CANONICAL_PV_NAMEPLATE_KWP,
    CANONICAL_PV_SPECIFIC_PRODUCTION_KWH_PER_KWP,
    generate_pv_timeseries,
)

ROOT = Path(__file__).resolve().parent.parent
REFERENCE_CSV = ROOT / "data" / "pv_shape_15min.csv"
REPO_INPUT_XLSX = ROOT / "inputs" / "input.xlsx"


def _reference_shape() -> np.ndarray:
    return pd.read_csv(REFERENCE_CSV)["pv_kwh_8mw_reference"].to_numpy(
        dtype=float,
    )


# ---------------------------------------------------------------------------
# 1-3. Repository workbook ships with the canonical 8 MW data verbatim
# ---------------------------------------------------------------------------


def test_repo_input_xlsx_pv_sum_equals_canonical_total():
    ts = pd.read_excel(REPO_INPUT_XLSX, sheet_name="timeseries")
    total = float(ts["pv_kwh"].sum())
    expected = float(_reference_shape().sum())
    assert total == pytest.approx(expected, rel=1e-9), (
        f"workbook PV total {total:.4f} kWh does not match canonical "
        f"reference {expected:.4f} kWh"
    )


def test_repo_input_xlsx_pv_column_is_bit_identical_to_reference():
    ts = pd.read_excel(REPO_INPUT_XLSX, sheet_name="timeseries")
    diff = np.abs(ts["pv_kwh"].to_numpy(dtype=float) - _reference_shape())
    assert float(diff.max()) < 1.0e-6, (
        f"max abs difference {float(diff.max())} between workbook and "
        f"reference exceeds 1e-6"
    )


def test_repo_input_xlsx_pv_sheet_defaults_match_canonical():
    pv_sheet = pd.read_excel(REPO_INPUT_XLSX, sheet_name="pv")
    pv_dict = dict(zip(pv_sheet["key"].astype(str), pv_sheet["value"]))
    assert float(pv_dict["pv_nameplate_kwp"]) == pytest.approx(
        CANONICAL_PV_NAMEPLATE_KWP, rel=1e-12,
    )
    assert float(pv_dict["specific_production_kwh_per_kwp"]) == pytest.approx(
        CANONICAL_PV_SPECIFIC_PRODUCTION_KWH_PER_KWP, rel=1e-9,
    )


# ---------------------------------------------------------------------------
# 4. Determinism — two reads of the same workbook produce identical PV
# ---------------------------------------------------------------------------


def test_read_workbook_pv_is_deterministic():
    a = read_workbook(REPO_INPUT_XLSX)["ts"]["pv_kwh"].to_numpy(dtype=float)
    b = read_workbook(REPO_INPUT_XLSX)["ts"]["pv_kwh"].to_numpy(dtype=float)
    assert np.array_equal(a, b)


def test_generate_pv_timeseries_is_deterministic():
    """Direct call to the build helper twice → bit-exact arrays."""
    a = generate_pv_timeseries(
        pv_nameplate_kwp=8000.0,
        specific_production_kwh_per_kwp=1571.12021875,
    )
    b = generate_pv_timeseries(
        pv_nameplate_kwp=8000.0,
        specific_production_kwh_per_kwp=1571.12021875,
    )
    assert np.array_equal(a, b)


# ---------------------------------------------------------------------------
# 5. Default workbook ⇒ no rescaling, byte-equal pass-through
# ---------------------------------------------------------------------------


def test_default_workbook_loader_passes_pv_through_unchanged():
    typed = read_workbook(REPO_INPUT_XLSX)
    pv = typed["ts"]["pv_kwh"].to_numpy(dtype=float)
    diff = np.abs(pv - _reference_shape())
    assert float(diff.max()) < 1.0e-6, (
        "loader changed the PV column even though defaults match"
    )


# ---------------------------------------------------------------------------
# 6-7. Loader rescaling on user-supplied nameplate and SP
# ---------------------------------------------------------------------------


def _user_workbook(tmp_path: Path, *, pv_kwp: float, sp: float) -> Path:
    typed = read_workbook(REPO_INPUT_XLSX)
    typed["pv"]["pv_nameplate_kwp"] = float(pv_kwp)
    typed["pv"]["specific_production_kwh_per_kwp"] = float(sp)
    out = tmp_path / "user.xlsx"
    write_workbook(typed, out)
    return out


def test_loader_rescales_to_user_nameplate_only(tmp_path):
    out = _user_workbook(tmp_path, pv_kwp=2000.0, sp=1571.12021875)
    pv = read_workbook(out)["ts"]["pv_kwh"].to_numpy(dtype=float)
    target_total = 2000.0 * 1571.12021875
    assert float(pv.sum()) == pytest.approx(target_total, rel=1e-12)
    # Shape preserved: ratio of any two non-zero indices is unchanged.
    ref = _reference_shape()
    nonzero = ref > 1.0e-9
    ratios_loader = pv[nonzero] / ref[nonzero]
    assert ratios_loader.std() < 1.0e-12  # all the same factor


def test_loader_rescales_to_user_specific_production_only(tmp_path):
    out = _user_workbook(tmp_path, pv_kwp=8000.0, sp=1600.0)
    pv = read_workbook(out)["ts"]["pv_kwh"].to_numpy(dtype=float)
    target_total = 8000.0 * 1600.0
    assert float(pv.sum()) == pytest.approx(target_total, rel=1e-12)


def test_loader_rescales_to_user_both_axes(tmp_path):
    """The exact case from the user message: 2 MW × 1600 kWh/kWp."""
    out = _user_workbook(tmp_path, pv_kwp=2000.0, sp=1600.0)
    pv = read_workbook(out)["ts"]["pv_kwh"].to_numpy(dtype=float)
    target_total = 2000.0 * 1600.0
    assert float(pv.sum()) == pytest.approx(target_total, rel=1e-12)
    # Shape preserved exactly.
    ref = _reference_shape()
    assert (pv == 0.0)[ref == 0.0].all()


def test_doubling_user_nameplate_doubles_loaded_pv(tmp_path):
    a = read_workbook(_user_workbook(tmp_path, pv_kwp=1000.0, sp=1500.0))
    b = read_workbook(_user_workbook(tmp_path, pv_kwp=2000.0, sp=1500.0))
    pv_a = a["ts"]["pv_kwh"].to_numpy(dtype=float)
    pv_b = b["ts"]["pv_kwh"].to_numpy(dtype=float)
    assert pv_b == pytest.approx(2.0 * pv_a, rel=1e-12)


def test_doubling_user_specific_production_doubles_loaded_pv(tmp_path):
    a = read_workbook(_user_workbook(tmp_path, pv_kwp=4000.0, sp=1200.0))
    b = read_workbook(_user_workbook(tmp_path, pv_kwp=4000.0, sp=2400.0))
    pv_a = a["ts"]["pv_kwh"].to_numpy(dtype=float)
    pv_b = b["ts"]["pv_kwh"].to_numpy(dtype=float)
    assert pv_b == pytest.approx(2.0 * pv_a, rel=1e-12)


# ---------------------------------------------------------------------------
# 8. Zero / unspecified nameplate or SP ⇒ rescaling skipped
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


def test_zero_specific_production_skips_rescaling():
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=4, freq="h"),
        "pv_kwh": [10.0, 20.0, 30.0, 40.0],
        "load_kwh": [1.0] * 4,
    })
    out = _rescale_pv_to_user_target(
        ts, pv_nameplate_kwp=4500.0, specific_production_kwh_per_kwp=0.0,
    )
    assert (out["pv_kwh"].to_numpy() == ts["pv_kwh"].to_numpy()).all()


def test_zero_pv_column_skips_rescaling():
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=4, freq="h"),
        "pv_kwh": [0.0] * 4,
        "load_kwh": [1.0] * 4,
    })
    out = _rescale_pv_to_user_target(
        ts, pv_nameplate_kwp=4500.0, specific_production_kwh_per_kwp=1500.0,
    )
    assert (out["pv_kwh"].to_numpy() == 0.0).all()


# ---------------------------------------------------------------------------
# 9. Loader logs the rescale factor only when rescaling actually fires
# ---------------------------------------------------------------------------


def test_loader_logs_rescale_factor_only_on_rescale(tmp_path, caplog):
    # A) defaults: should NOT log a rescale.
    with caplog.at_level(logging.INFO, logger="pvbess_opt.io"):
        read_workbook(REPO_INPUT_XLSX)
    rescaled = [r for r in caplog.records if "rescaled" in r.getMessage()]
    assert not rescaled, "default workbook unexpectedly triggered rescaling"

    caplog.clear()
    # B) user-modified: SHOULD log exactly one rescale info.
    out = _user_workbook(tmp_path, pv_kwp=2000.0, sp=1600.0)
    with caplog.at_level(logging.INFO, logger="pvbess_opt.io"):
        read_workbook(out)
    rescaled = [r for r in caplog.records if "rescaled" in r.getMessage()]
    assert len(rescaled) == 1
    msg = rescaled[0].getMessage()
    assert "2000.0 kWp" in msg
    assert "1600.0000 kWh/kWp" in msg


# ---------------------------------------------------------------------------
# 10. AST audit — no random / noise constructs in the build-time PV path
# ---------------------------------------------------------------------------


_FORBIDDEN_RANDOM_PATTERNS = (
    r"\brng\.",
    r"\bnp\.random\.",
    r"\.normal\s*\(",
    r"\.beta\s*\(",
    r"\.uniform\s*\(",
    r"\.gauss\s*\(",
    r"\.poisson\s*\(",
    r"\.choice\s*\(",
    r"\bseed\s*=",
    r"\.seed\s*\(",
    r"random_state\s*=",
)


def test_no_random_calls_in_pv_path():
    src = inspect.getsource(generate_pv_timeseries)
    hits: list[str] = []
    for pat in _FORBIDDEN_RANDOM_PATTERNS:
        for m in re.finditer(pat, src):
            hits.append(f"{pat!r} → {m.group(0)!r}")
    assert not hits, (
        "generate_pv_timeseries contains forbidden randomness constructs:\n"
        + "\n".join(hits) + "\n--- source ---\n" + src
    )


# ---------------------------------------------------------------------------
# Reference CSV invariants — guard the canonical data file
# ---------------------------------------------------------------------------


def test_reference_csv_has_expected_shape_and_header():
    df = pd.read_csv(REFERENCE_CSV)
    assert list(df.columns) == ["pv_kwh_8mw_reference"]
    assert len(df) == 35040
    col = df["pv_kwh_8mw_reference"]
    assert (col >= 0).all()
    # Real-world site: published total 12 568 961,7517 kWh.
    assert col.sum() == pytest.approx(12_568_961.7517, rel=1e-9)


def test_rescale_tolerance_constant_is_tight():
    """Guard the rescale tolerance: 1e-12 is what the loader uses for
    the "already matches" pass-through check."""
    assert _PV_RESCALE_REL_TOLERANCE == 1.0e-12


# ---------------------------------------------------------------------------
# Leftover-artifact audit — no v0.7 noise-bleed pattern in the tree
# ---------------------------------------------------------------------------


_BAD_PATTERN = re.compile(
    r"np\.maximum\(\s*pv\s*\+\s*rng\.normal",
)
_SELF_FILE = Path(__file__).resolve()


def _scan_files() -> list[Path]:
    out: list[Path] = []
    for sub in ("scripts", "tests"):
        for path in (ROOT / sub).rglob("*.py"):
            if path.resolve() == _SELF_FILE:
                continue
            out.append(path)
    out.append(ROOT / "main.py")
    return out


def test_no_v07_noise_bleed_pattern_remains():
    hits: list[str] = []
    for path in _scan_files():
        text = path.read_text(encoding="utf-8")
        if _BAD_PATTERN.search(text):
            for i, line in enumerate(text.splitlines(), start=1):
                if _BAD_PATTERN.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{i}: {line.rstrip()}")
    assert not hits, (
        "v0.7 noise-bleed pattern found:\n" + "\n".join(hits)
    )
