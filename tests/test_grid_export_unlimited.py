"""Tests for the optional / unlimited grid-export cap (Phase 2, v0.8.8).

The workbook value ``p_grid_export_max_kw`` may be left empty or set to
one of the disable tokens (``inf`` / ``unlimited`` / ``disabled`` …) to
remove the cap.  When disabled the loader substitutes a finite Big-M so
the MILP topology stays identical and the behaviour is solver-agnostic.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import openpyxl
import pytest

from pvbess_opt.io import read_inputs, read_workbook
from pvbess_opt.kpis import compute_kpis
from pvbess_opt.optimization import run_scenario, verify_dispatch_invariants

ROOT = Path(__file__).resolve().parent.parent
REPO_INPUT_XLSX = ROOT / "inputs" / "input.xlsx"


def _highs_available() -> bool:
    try:
        import highspy  # noqa: F401
    except ImportError:
        return False
    return True


def _workbook_with_grid_cap(tmp_path: Path, value: object, name: str) -> Path:
    """Copy the repo workbook, overwrite ``p_grid_export_max_kw`` cell."""
    dst = tmp_path / f"{name}.xlsx"
    shutil.copy(REPO_INPUT_XLSX, dst)
    wb = openpyxl.load_workbook(dst)
    ws = wb["project"]
    for row in ws.iter_rows():
        if row[0].value == "p_grid_export_max_kw":
            row[1].value = value
            break
    wb.save(dst)
    return dst


# ---------------------------------------------------------------------------
# 1. Empty cell → unlimited
# ---------------------------------------------------------------------------


def test_empty_cell_means_unlimited(tmp_path):
    wb = _workbook_with_grid_cap(tmp_path, None, "empty")
    params, _ = read_inputs(wb)
    assert params["grid_export_unlimited"] is True
    # The MILP bound is a finite Big-M, never infinity.
    assert params["p_grid_export_max_kw"] < float("inf")
    assert params["p_grid_export_max_kw"] >= 1.0e6


# ---------------------------------------------------------------------------
# 2. Disable-token string variants → unlimited
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token", ["inf", "Inf", "INFINITY", "unlimited", "disabled", "none"],
)
def test_inf_string_variants(tmp_path, token):
    wb = _workbook_with_grid_cap(tmp_path, token, f"tok_{token}")
    params, _ = read_inputs(wb)
    assert params["grid_export_unlimited"] is True
    typed = read_workbook(wb)
    assert typed["project"]["p_grid_export_max_kw"] == float("inf")


# ---------------------------------------------------------------------------
# 3. Negative / zero remain validation errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [-100, 0])
def test_negative_and_zero_still_invalid(tmp_path, bad):
    wb = _workbook_with_grid_cap(tmp_path, bad, f"bad_{bad}")
    with pytest.raises(ValueError, match="p_grid_export_max_kw"):
        read_workbook(wb)


# ---------------------------------------------------------------------------
# 4. Unlimited cap → zero curtailment, optimal solve
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
@pytest.mark.parametrize("mode", ["vnb", "merchant"])
def test_unlimited_zero_curtailment(short_ts, short_params, short_params_merchant, mode):
    params = dict(short_params if mode == "vnb" else short_params_merchant)
    # Disabled cap → finite Big-M substituted.
    params["p_grid_export_max_kw"] = 1.0e6
    params["grid_export_unlimited"] = True
    res, _ = run_scenario(params, short_ts, "highs")
    kpis = compute_kpis(res, params, verify_balance=False)
    assert kpis["pv_energy_curtailed_mwh"] == 0.0
    inv = verify_dispatch_invariants(res, params, mode=mode)
    # No cap-driven curtailment: invariant 7 (cap slack ⇒ curtail 0) holds.
    assert inv["invariant_7_curtail_behavior_kwh"] == 0.0
    assert float(res["pv_curtail_kwh"].sum()) == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 5. Finite cap path is a pure pass-through (regression guard)
# ---------------------------------------------------------------------------


def test_finite_cap_unchanged(tmp_path):
    wb = _workbook_with_grid_cap(tmp_path, 15000, "finite")
    params, _ = read_inputs(wb)
    # Finite cap: value preserved verbatim, no Big-M substitution.
    assert params["grid_export_unlimited"] is False
    assert params["p_grid_export_max_kw"] == 15000.0
    typed = read_workbook(wb)
    assert typed["project"]["p_grid_export_max_kw"] == 15000.0


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
@pytest.mark.parametrize("mode", ["vnb", "merchant"])
def test_finite_cap_kpis_identical_to_legacy_path(
    short_ts, short_params, short_params_merchant, mode,
):
    """A finite cap must produce the exact same dispatch whether or not
    the ``grid_export_unlimited`` flag is present in ``params``."""
    base = dict(short_params if mode == "vnb" else short_params_merchant)
    base["p_grid_export_max_kw"] = 5000.0

    legacy = dict(base)  # no grid_export_unlimited key — pre-v0.8.8 shape
    new = dict(base)
    new["grid_export_unlimited"] = False

    res_legacy, _ = run_scenario(legacy, short_ts, "highs")
    res_new, _ = run_scenario(new, short_ts, "highs")
    k_legacy = compute_kpis(res_legacy, legacy, verify_balance=False)
    k_new = compute_kpis(res_new, new, verify_balance=False)
    for key, value in k_legacy.items():
        if isinstance(value, (int, float)):
            assert k_new[key] == pytest.approx(value, rel=1e-9, abs=1e-9), key
