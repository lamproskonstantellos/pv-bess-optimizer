"""Smoke tests for the case-study ``inputs/input.xlsx`` workbook.

These tests guard the acceptance criterion that a fresh clone can run
``python main.py inputs/input.xlsx --solver highs`` end-to-end, in both
``vnb`` and ``merchant`` modes.
"""

from __future__ import annotations

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


def test_repo_input_xlsx_has_seven_sheets():
    sheets = pd.ExcelFile(ROOT / "inputs" / "input.xlsx").sheet_names
    assert set(sheets) == {
        "timeseries", "project", "pv", "bess", "economics",
        "simulation", "curtailment_profile",
    }


def test_repo_input_xlsx_has_35040_timeseries_rows():
    ts = pd.read_excel(ROOT / "inputs" / "input.xlsx", sheet_name="timeseries")
    assert len(ts) == 35040


def test_repo_input_xlsx_has_negative_dam_hours():
    """Spec: 4 negative-price hours seeded so the no-sim-IO logic and
    the sign-aware noise actually exercise.  At 15-minute cadence each
    hour expands to 4 steps, so the 4 seeded hours give 16 negative
    steps; we allow a small tolerance for any noise that lands in the
    same bucket as a seeded hour."""
    ts = pd.read_excel(ROOT / "inputs" / "input.xlsx", sheet_name="timeseries")
    n_neg = int((ts["dam_price_eur_per_mwh"] < 0).sum())
    assert 12 <= n_neg <= 20


def test_read_workbook_round_trip_after_build_script():
    from pvbess_opt.io import read_workbook
    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    assert typed["dt_minutes"] == 15
    assert typed["project"]["mode"] == "vnb"
    assert typed["project"]["project_lifecycle_years"] == 25
    assert "load_kwh" in typed["ts"].columns


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_main_vnb_short_horizon(tmp_path, monkeypatch):
    """End-to-end smoke: main.py on a short window, vnb mode."""
    from pvbess_opt.io import read_workbook, write_workbook
    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)  # 1 day @ 15 min
    short_xlsx = tmp_path / "short.xlsx"
    write_workbook(typed, short_xlsx)

    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(ROOT))
    import main as main_module
    rc = main_module.main([
        str(short_xlsx),
        "--solver", "highs",
        "--mip-gap", "0.05",
        "--time-limit", "180",
    ])
    assert rc == 0


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_main_merchant_short_horizon(tmp_path, monkeypatch):
    """End-to-end smoke: main.py on a short window, merchant mode."""
    from pvbess_opt.io import read_workbook, write_workbook
    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)  # 1 day @ 15 min
    typed["project"]["mode"] = "merchant"
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
        "--time-limit", "180",
    ])
    assert rc == 0
