"""Smoke tests for the case-study ``inputs/input.xlsx`` workbook.

These tests guard the acceptance criterion that a fresh clone can run
``python main.py inputs/input.xlsx --solver highs`` end-to-end, in both
``vnb`` and ``merchant`` modes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _highs_available() -> bool:
    try:
        import highspy  # noqa: F401
    except ImportError:
        return False
    return bool(highspy)


def test_repo_input_xlsx_exists():
    assert (ROOT / "inputs" / "input.xlsx").exists()


def test_repo_input_xlsx_has_three_sheets():
    sheets = pd.ExcelFile(ROOT / "inputs" / "input.xlsx").sheet_names
    assert set(sheets) == {"timeseries", "project", "economic"}


def test_repo_input_xlsx_has_8760_timeseries_rows():
    ts = pd.read_excel(ROOT / "inputs" / "input.xlsx", sheet_name="timeseries")
    assert len(ts) == 8760


def test_repo_input_xlsx_has_negative_dam_hours():
    """Spec: 3-5 negative-price hours seeded so the no-sim-IO logic and
    the sign-aware noise (Phase B) actually exercise."""
    ts = pd.read_excel(ROOT / "inputs" / "input.xlsx", sheet_name="timeseries")
    n_neg = int((ts["dam_price_eur_per_mwh"] < 0).sum())
    assert 3 <= n_neg <= 5


def test_build_input_xlsx_script_runs(tmp_path, monkeypatch):
    """``scripts/build_input_xlsx.py`` regenerates a valid workbook."""
    monkeypatch.chdir(ROOT)
    proc = subprocess.run(
        [sys.executable, "scripts/build_input_xlsx.py"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr


def test_read_workbook_round_trip_after_build_script():
    from pvbess_opt.io import read_workbook
    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    assert typed["dt_minutes"] == 60
    assert typed["project"]["regulatory"]["mode"] == "vnb"
    assert typed["economic"]["project_lifecycle_years"] == 25
    assert "load_kwh" in typed["ts"].columns


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_main_vnb_short_horizon(tmp_path, monkeypatch):
    """End-to-end smoke: main.py on a short window, vnb mode."""
    from pvbess_opt.io import read_workbook, write_workbook
    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    typed["ts"] = typed["ts"].iloc[:72].reset_index(drop=True)
    short_xlsx = tmp_path / "short.xlsx"
    write_workbook(typed, short_xlsx)

    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(ROOT))
    import main as main_module
    rc = main_module.main([
        str(short_xlsx),
        "--solver", "highs",
        "--mip-gap", "0.05",
        "--time-limit", "60",
    ])
    assert rc == 0


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_main_merchant_short_horizon(tmp_path, monkeypatch):
    """End-to-end smoke: main.py on a short window, merchant mode."""
    from pvbess_opt.io import read_workbook, write_workbook
    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    typed["ts"] = typed["ts"].iloc[:72].reset_index(drop=True)
    typed["project"]["regulatory"]["mode"] = "merchant"
    short_xlsx = tmp_path / "short_merchant.xlsx"
    write_workbook(typed, short_xlsx)

    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(ROOT))
    import main as main_module
    rc = main_module.main([
        str(short_xlsx),
        "--mode", "merchant",
        "--solver", "highs",
        "--mip-gap", "0.05",
        "--time-limit", "60",
    ])
    assert rc == 0
