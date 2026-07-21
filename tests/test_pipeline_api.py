"""Programmatic API for the extracted pipeline + CLI.

``pvbess_opt.run(config)`` and ``pvbess_opt.cli.main([...])`` must run a
scenario end-to-end without the top-level ``main.py`` script — the
package is importable and testable on its own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import pvbess_opt
from pvbess_opt import Results, RunConfig, run

ROOT = Path(__file__).resolve().parent.parent


def _highs_available() -> bool:
    try:
        import highspy
    except ImportError:
        return False
    return bool(highspy)


def _short_workbook(tmp_path: Path) -> Path:
    """Write a 1-day (96-step) slice of the case-study workbook."""
    from pvbess_opt.io import read_workbook, write_workbook

    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)
    short = tmp_path / "short.xlsx"
    write_workbook(typed, short)
    return short


def test_run_config_and_results_are_exported():
    """The programmatic surface is re-exported from the package root."""
    assert pvbess_opt.RunConfig is RunConfig
    assert pvbess_opt.Results is Results
    assert pvbess_opt.run is run
    cfg = RunConfig(excel=Path("inputs/input.xlsx"))
    assert cfg.solver == "highs"
    assert cfg.outdir == Path("results")


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_run_returns_populated_results(tmp_path):
    short = _short_workbook(tmp_path)
    config = RunConfig(
        excel=short,
        solver="highs",
        outdir=tmp_path / "results",
        mip_gap=0.05,
        time_limit=180,
    )
    result = run(config)
    assert isinstance(result, Results)
    assert result.kpis, "Results.kpis should be populated"
    assert "profit_total_eur" in result.kpis
    assert result.out_dir.exists()
    assert (result.out_dir / "03_results.xlsx").exists()
    # The advertised output layout: SUMMARY.md digest + run log.
    assert (result.out_dir / "00_summary" / "SUMMARY.md").exists()
    assert (result.out_dir / "00_summary" / "run_log.txt").exists()
    # Public Results contract: the financial bundle is surfaced too.
    assert result.financial_kpis is not None
    assert result.yearly_cashflow is not None
    _ = (result.lifetime_yearly, result.sensitivity)


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_run_snapshot_exists_and_scrubs_entsoe_token(tmp_path):
    """End-to-end lock on the round-1 secret-scrub path: a workbook that
    carries an ENTSO-E token in its market_data cell must produce a
    01_inputs snapshot with the token blanked (no fetch happened, so the
    blank_entsoe_token branch runs) and an assumptions_summary that never
    prints the token."""
    import openpyxl

    short = _short_workbook(tmp_path)
    # Inject a token into the market_data.entsoe_token cell.
    token = "SECRET-TOKEN-1234567890-abcdef"
    wb = openpyxl.load_workbook(short)
    ws = wb["market_data"]
    for row in ws.iter_rows(min_row=2, max_col=2):
        if str(row[0].value).strip() == "entsoe_token":
            row[1].value = token
            break
    wb.save(short)

    config = RunConfig(
        excel=short, solver="highs", outdir=tmp_path / "results",
        mip_gap=0.05, time_limit=180,
    )
    result = run(config)

    snapshot = result.out_dir / "01_inputs" / "input_snapshot.xlsx"
    assert snapshot.exists(), "01_inputs/input_snapshot.xlsx must be written"
    snap_wb = openpyxl.load_workbook(snapshot)
    snap_ws = snap_wb["market_data"]
    token_cell = None
    for row in snap_ws.iter_rows(min_row=2, max_col=2):
        if str(row[0].value).strip() == "entsoe_token":
            token_cell = row[1].value
            break
    assert token_cell in (None, ""), (
        f"snapshot entsoe_token must be scrubbed, got {token_cell!r}"
    )
    summary = result.out_dir / "01_inputs" / "assumptions_summary.txt"
    assert summary.exists()
    assert token not in summary.read_text(encoding="utf-8")


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_cli_main_smoke(tmp_path):
    from pvbess_opt import cli

    rc = cli.main([
        str(_short_workbook(tmp_path)),
        "--solver", "highs",
        "--outdir", str(tmp_path / "cli_results"),
        "--mip-gap", "0.05",
        "--time-limit", "180",
    ])
    assert rc == 0
