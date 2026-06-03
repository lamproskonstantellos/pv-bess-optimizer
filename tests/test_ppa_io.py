"""Workbook-loader tests for the PPA (with merchant tail) schema and the
project-level zero-feed-in flag.

Mirrors :mod:`tests.test_balancing_io` — the PPA sheet is an optional
sheet with the same absent-falls-back-to-defaults contract.
"""

from __future__ import annotations

import logging
from pathlib import Path

import openpyxl
import pandas as pd
import pytest

from pvbess_opt.economics import read_economic_params
from pvbess_opt.io import (
    PPA_SHEET_DEFAULTS,
    read_inputs,
    read_workbook,
    validate_workbook_params,
    write_workbook,
)


def _load_typed(repo_input_xlsx: Path) -> dict:
    # Truncate the timeseries to a small slice: the PPA / zero-feed-in
    # round-trip and validation paths are independent of its length, and
    # write_workbook serialising all 35 040 rows of the shipped workbook
    # dominates the runtime of every round-trip test otherwise.
    typed = read_workbook(repo_input_xlsx)
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)
    return typed


def _strip_sheet(path: Path, sheet: str) -> None:
    wb = openpyxl.load_workbook(path)
    del wb[sheet]
    wb.save(path)


# ---------------------------------------------------------------------------
# Defaults + presence
# ---------------------------------------------------------------------------


def test_ppa_section_present_and_defaulted_in_repo_workbook(repo_input_xlsx):
    typed = _load_typed(repo_input_xlsx)
    assert "ppa" in typed
    # The shipped workbook carries the ppa sheet disabled, every key at
    # its canonical default.
    assert typed["ppa"]["ppa_enabled"] is False
    for key, default in PPA_SHEET_DEFAULTS.items():
        assert typed["ppa"][key] == default


def test_ppa_sheet_absent_falls_back_to_defaults(tmp_path, repo_input_xlsx):
    typed = _load_typed(repo_input_xlsx)
    dst = tmp_path / "no_ppa.xlsx"
    write_workbook(typed, dst)
    _strip_sheet(dst, "ppa")
    assert "ppa" not in set(pd.ExcelFile(dst).sheet_names)
    loaded = read_workbook(dst)
    assert loaded["ppa"] == dict(PPA_SHEET_DEFAULTS)


def test_ppa_sheet_round_trips_through_writer(tmp_path, repo_input_xlsx):
    typed = _load_typed(repo_input_xlsx)
    typed["ppa"]["ppa_enabled"] = True
    typed["ppa"]["ppa_structure"] = "baseload"
    typed["ppa"]["ppa_price_eur_per_mwh"] = 64.5
    typed["ppa"]["ppa_coverage_fraction"] = 0.8
    typed["ppa"]["ppa_baseload_mw"] = 2.5
    typed["ppa"]["ppa_escalation_pct"] = 1.5
    typed["ppa"]["ppa_dispatch_aware"] = True
    dst = tmp_path / "with_ppa.xlsx"
    write_workbook(typed, dst)
    assert "ppa" in set(pd.ExcelFile(dst).sheet_names)
    typed2 = read_workbook(dst)
    for key, value in typed["ppa"].items():
        assert typed2["ppa"][key] == value, key


def test_flat_params_carry_ppa_subdict_and_zero_feed_in(repo_input_xlsx):
    params, _ = read_inputs(repo_input_xlsx)
    assert isinstance(params["ppa"], dict)
    assert params["ppa"]["ppa_enabled"] is False
    assert params["zero_feed_in"] is False


def test_zero_feed_in_round_trips_through_writer(tmp_path, repo_input_xlsx):
    typed = _load_typed(repo_input_xlsx)
    typed["project"]["zero_feed_in"] = True
    dst = tmp_path / "zfi.xlsx"
    write_workbook(typed, dst)
    typed2 = read_workbook(dst)
    assert typed2["project"]["zero_feed_in"] is True
    params, _ = read_inputs(dst)
    assert params["zero_feed_in"] is True


# ---------------------------------------------------------------------------
# read_economic_params carries the PPA keys (multi-year escalation needs them)
# ---------------------------------------------------------------------------


def test_read_economic_params_carries_ppa_keys(repo_input_xlsx):
    econ = read_economic_params(repo_input_xlsx)
    for key in PPA_SHEET_DEFAULTS:
        assert key in econ, key
    assert econ["ppa_escalation_pct"] == PPA_SHEET_DEFAULTS["ppa_escalation_pct"]


# ---------------------------------------------------------------------------
# Validation — errors and warnings
# ---------------------------------------------------------------------------


def test_ppa_coverage_out_of_range_raises(tmp_path, repo_input_xlsx):
    typed = _load_typed(repo_input_xlsx)
    typed["ppa"]["ppa_enabled"] = True
    typed["ppa"]["ppa_coverage_fraction"] = 1.5
    dst = tmp_path / "bad_cov.xlsx"
    write_workbook(typed, dst)
    with pytest.raises(ValueError, match="ppa_coverage_fraction"):
        read_workbook(dst)


def test_ppa_negative_price_raises(tmp_path, repo_input_xlsx):
    typed = _load_typed(repo_input_xlsx)
    typed["ppa"]["ppa_enabled"] = True
    typed["ppa"]["ppa_price_eur_per_mwh"] = -10.0
    dst = tmp_path / "bad_price.xlsx"
    write_workbook(typed, dst)
    with pytest.raises(ValueError, match="ppa_price_eur_per_mwh"):
        read_workbook(dst)


def test_ppa_negative_baseload_raises(tmp_path, repo_input_xlsx):
    typed = _load_typed(repo_input_xlsx)
    typed["ppa"]["ppa_enabled"] = True
    typed["ppa"]["ppa_baseload_mw"] = -1.0
    dst = tmp_path / "bad_base.xlsx"
    write_workbook(typed, dst)
    with pytest.raises(ValueError, match="ppa_baseload_mw"):
        read_workbook(dst)


def test_ppa_bad_structure_raises_via_direct_validation():
    # An invalid ppa_structure in the workbook is coerced to the default
    # with a warning by the enum parser, so the explicit raise only fires
    # for programmatically built typed dicts that bypass the parser.
    typed = {
        "project": {"mode": "self_consumption"},
        "ppa": {"ppa_enabled": True, "ppa_structure": "tolling"},
    }
    with pytest.raises(ValueError, match="ppa_structure"):
        validate_workbook_params(typed)


def test_ppa_validation_skipped_when_disabled(tmp_path, repo_input_xlsx):
    typed = _load_typed(repo_input_xlsx)
    typed["ppa"]["ppa_enabled"] = False
    typed["ppa"]["ppa_coverage_fraction"] = 9.0  # bad, but gated off
    dst = tmp_path / "disabled_bad.xlsx"
    write_workbook(typed, dst)
    loaded = read_workbook(dst)  # must not raise
    assert loaded["ppa"]["ppa_enabled"] is False


def test_invalid_structure_in_workbook_coerced_to_default(
    tmp_path, repo_input_xlsx, caplog,
):
    typed = _load_typed(repo_input_xlsx)
    typed["ppa"]["ppa_enabled"] = True
    typed["ppa"]["ppa_structure"] = "tolling"
    dst = tmp_path / "coerced.xlsx"
    write_workbook(typed, dst)
    with caplog.at_level(logging.WARNING):
        loaded = read_workbook(dst)
    # Enum parser coerces to default (pay_as_produced) and warns.
    assert loaded["ppa"]["ppa_structure"] == "pay_as_produced"


def test_zero_feed_in_merchant_raises(tmp_path, repo_input_xlsx):
    typed = _load_typed(repo_input_xlsx)
    typed["project"]["mode"] = "merchant"
    typed["project"]["zero_feed_in"] = True
    # merchant mode forbids a load column; drop it so the loader does not
    # complain about something unrelated first.
    if "load_kwh" in typed["ts"].columns:
        typed["ts"] = typed["ts"].drop(columns=["load_kwh"])
    dst = tmp_path / "zfi_merchant.xlsx"
    write_workbook(typed, dst)
    with pytest.raises(ValueError, match="self_consumption"):
        read_workbook(dst)


def test_zero_feed_in_with_ppa_enabled_warns(repo_input_xlsx, caplog):
    typed = _load_typed(repo_input_xlsx)
    typed["project"]["zero_feed_in"] = True
    typed["ppa"]["ppa_enabled"] = True
    with caplog.at_level(logging.WARNING):
        validate_workbook_params(typed, dt_minutes=15)
    assert any("zero effect" in r.message for r in caplog.records)
