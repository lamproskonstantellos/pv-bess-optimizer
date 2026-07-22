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


def test_structured_config_anchors_stores_to_config_dir_not_temp(
    tmp_path, monkeypatch,
):
    """A structured (YAML/JSON) config is materialized to a throwaway temp
    workbook, so ``config.excel`` no longer sits beside the config's own
    price-scenario stores.  ``run`` must thread the ORIGINAL config directory
    as ``base_dir`` so a relative ``store_path`` resolves against it (mirroring
    ``scenarios.run_scenarios``), never the materialization temp dir — which
    would break the documented relative-store feature with a FileNotFound
    pointing at a random temp path the user never created."""
    import pvbess_opt.pipeline as pipeline

    cfg_dir = tmp_path / "project"
    cfg_dir.mkdir()
    cfg = cfg_dir / "config.yaml"
    cfg.write_text("# structured config\n", encoding="utf-8")

    captured: dict[str, object] = {}

    # Stub the heavy materialize/read path: this test pins only the base_dir
    # threading, not the solve.  materialize_to_xlsx returns a path INSIDE the
    # real mkdtemp temp dir, so config.excel's parent is the temp dir.
    monkeypatch.setattr(pipeline, "is_structured_config", lambda _p: True)
    monkeypatch.setattr(
        pipeline, "materialize_to_xlsx",
        lambda _src, dst: Path(dst) / "materialized.xlsx",
    )
    monkeypatch.setattr(pipeline, "read_inputs", lambda _p: ({}, None))
    monkeypatch.setattr(pipeline, "apply_ieee_style", lambda: None)
    monkeypatch.setattr(pipeline, "set_show_titles", lambda _v: None)

    sentinel = object()

    def _fake_run_one(
        params, ts, config, base_name, timestamp, base_dir=None,
    ):
        captured["base_dir"] = base_dir
        captured["excel_parent"] = Path(config.excel).parent
        return sentinel

    monkeypatch.setattr(pipeline, "_run_one", _fake_run_one)

    result = pipeline.run(RunConfig(excel=cfg, outdir=tmp_path / "out"))
    assert result is sentinel
    # base_dir is the config's OWN directory ...
    assert captured["base_dir"] == cfg_dir
    # ... and specifically NOT the materialization temp dir.
    assert captured["base_dir"] != captured["excel_parent"]


def test_structured_config_temp_workbook_is_cleaned_up(tmp_path, monkeypatch):
    """The throwaway workbook a structured config is materialized into has no
    consumer once the run has read it and copied its snapshot; ``run`` must
    remove the temp dir so batch/sweep invocations do not leak one per run."""
    import tempfile as _tempfile

    import pvbess_opt.pipeline as pipeline

    cfg = tmp_path / "config.yaml"
    cfg.write_text("# structured config\n", encoding="utf-8")

    made: list[Path] = []
    _real_mkdtemp = _tempfile.mkdtemp

    def _tracking_mkdtemp(*a, **k):
        d = _real_mkdtemp(*a, **k)
        made.append(Path(d))
        return d

    monkeypatch.setattr(pipeline.tempfile, "mkdtemp", _tracking_mkdtemp)
    monkeypatch.setattr(pipeline, "is_structured_config", lambda _p: True)
    monkeypatch.setattr(
        pipeline, "materialize_to_xlsx",
        lambda _src, dst: Path(dst) / "materialized.xlsx",
    )
    monkeypatch.setattr(pipeline, "read_inputs", lambda _p: ({}, None))
    monkeypatch.setattr(pipeline, "apply_ieee_style", lambda: None)
    monkeypatch.setattr(pipeline, "set_show_titles", lambda _v: None)
    monkeypatch.setattr(pipeline, "_run_one", lambda *a, **k: object())

    pipeline.run(RunConfig(excel=cfg, outdir=tmp_path / "out"))

    assert made, "run() should have created a materialization temp dir"
    assert not made[0].exists(), (
        f"materialization temp dir {made[0]} must be removed after the run"
    )


@pytest.mark.slow
@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_soh_plot_failure_does_not_abort_completed_run(tmp_path, monkeypatch):
    """A figure must never turn an otherwise-complete run into a reported
    failure: 03_results.xlsx and SUMMARY.md are written before any plot, so a
    raise inside ``plot_soh_trajectory`` (the one call that used to be
    unguarded, unlike its siblings) must be logged and swallowed, and the run
    must still return populated Results with the workbook on disk."""
    import pvbess_opt.pipeline as pipeline

    short = _short_workbook(tmp_path)
    called = {"n": 0}

    def _boom(*_a, **_k):
        called["n"] += 1
        raise RuntimeError("synthetic SOH-trajectory plot failure")

    monkeypatch.setattr(pipeline, "plot_soh_trajectory", _boom)

    result = run(RunConfig(
        excel=short, solver="highs", outdir=tmp_path / "results",
        mip_gap=0.05, time_limit=180,
    ))
    # The guarded plot was actually reached (the projection spans the
    # lifecycle regardless of the one-day dispatch slice) ...
    assert called["n"] >= 1, "plot_soh_trajectory must have been reached"
    # ... yet the run completed and the primary outputs are on disk.
    assert isinstance(result, Results)
    assert result.kpis
    assert (result.out_dir / "03_results.xlsx").exists()
    assert (result.out_dir / "00_summary" / "SUMMARY.md").exists()


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
