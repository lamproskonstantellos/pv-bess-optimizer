"""Capacity sizing: grid parsing, sweep, marginal value, break-even, output."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from pvbess_opt.sizing import (
    SizingResult,
    compute_marginal_value_of_storage,
    find_oversizing_breakeven,
    parse_sizing_grid,
    rank_frontier,
    read_sizing_block,
    run_sizing,
    run_sizing_sweep,
    write_sizing_workbook,
)
from pvbess_opt.theme import COL_WIDTH_MAX, COL_WIDTH_MIN, HEADER_FILL_HEX

ROOT = Path(__file__).resolve().parent.parent


def _highs_available() -> bool:
    try:
        import highspy
    except ImportError:
        return False
    return bool(highspy)


def _synthetic_frontier() -> pd.DataFrame:
    mwh = np.arange(1, 10, dtype=float)
    npv = -(mwh - 5.0) ** 2 + 100.0  # peaks at 5 MWh
    return pd.DataFrame({
        "pv_nameplate_kwp": np.full(mwh.size, 1000.0),
        "bess_power_kw": np.full(mwh.size, 500.0),
        "bess_capacity_kwh": mwh * 1000.0,
        "bess_capacity_mwh": mwh,
        "npv_eur": npv,
    })


def test_parse_sizing_grid_list_range_and_duration():
    grid = parse_sizing_grid({
        "pv_nameplate_kwp": [1000, 2000],
        "bess_power_kw": {"min": 500, "max": 1000, "step": 500},
        "bess_capacity_kwh": [1000],
    })
    assert len(grid) == 2 * 2 * 1
    assert (1000.0, 500.0, 1000.0) in grid
    dur = parse_sizing_grid({
        "pv_nameplate_kwp": [1000],
        "bess_power_kw": [1000],
        "bess_duration_hours": [2, 4],
    })
    assert (1000.0, 1000.0, 2000.0) in dur
    assert (1000.0, 1000.0, 4000.0) in dur


def test_marginal_value_and_breakeven_on_synthetic_peak():
    frontier = _synthetic_frontier()
    mv = compute_marginal_value_of_storage(frontier)
    below = mv[mv["bess_capacity_mwh"] < 5.0]["marginal_npv_eur_per_mwh"]
    above = mv[mv["bess_capacity_mwh"] > 5.0]["marginal_npv_eur_per_mwh"]
    assert (below > 0).all()
    assert (above < 0).all()
    be = find_oversizing_breakeven(
        frontier["bess_capacity_mwh"], frontier["npv_eur"],
    )
    assert be == pytest.approx(5.0, abs=1e-6)


def test_breakeven_nan_when_npv_monotone_increasing():
    mwh = np.arange(1, 6, dtype=float)
    npv = mwh * 10.0
    assert np.isnan(find_oversizing_breakeven(mwh, npv))


def test_rank_frontier_sorts_by_npv_desc():
    ranked = rank_frontier(pd.DataFrame({"npv_eur": [1.0, 3.0, 2.0]}))
    assert list(ranked["npv_eur"]) == [3.0, 2.0, 1.0]


def test_read_sizing_block(tmp_path):
    cfg = tmp_path / "run.yaml"
    cfg.write_text("sizing:\n  pv_nameplate_kwp: [1000, 2000]\n", encoding="utf-8")
    assert read_sizing_block(cfg) == {"pv_nameplate_kwp": [1000, 2000]}
    assert read_sizing_block(tmp_path / "x.xlsx") is None


def test_write_sizing_workbook_is_styled(tmp_path):
    frontier = rank_frontier(_synthetic_frontier().assign(
        irr_pct=5.0, simple_payback_years=8.0,
        lcoe_eur_per_mwh=50.0, lcos_eur_per_mwh=150.0,
    ))
    result = SizingResult(
        frontier=frontier,
        marginal_value=compute_marginal_value_of_storage(frontier),
        oversizing_breakeven_mwh=5.0,
    )
    out = write_sizing_workbook(tmp_path / "sizing.xlsx", result)
    wb = load_workbook(out)
    assert wb.sheetnames
    for sn in wb.sheetnames:
        ws = wb[sn]
        assert ws.freeze_panes == "A2"
        for cell in ws[1]:
            if cell.value is None:
                continue
            rgb = (getattr(cell.fill.fgColor, "rgb", None) or "")
            assert rgb.upper().lstrip("0").rjust(6, "0")[-6:] == HEADER_FILL_HEX
        for c in range(1, ws.max_column + 1):
            dim = ws.column_dimensions.get(get_column_letter(c))
            assert dim is not None and dim.width is not None
            assert COL_WIDTH_MIN <= float(dim.width) <= COL_WIDTH_MAX


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_sizing_sweep_runs_2x2x2(tmp_path):
    from pvbess_opt.io import _typed_to_flat, read_workbook, write_workbook

    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)
    short = tmp_path / "short.xlsx"
    write_workbook(typed, short)
    base = read_workbook(short)
    base_params, base_ts = _typed_to_flat(base)
    grid = parse_sizing_grid({
        "pv_nameplate_kwp": [4000, 8000],
        "bess_power_kw": [1000, 2000],
        "bess_capacity_kwh": [2000, 4000],
    })
    frontier = run_sizing_sweep(
        base_params, base_ts, base["pv"], short, grid,
        solver_opts={
            "solver_name": "highs", "mip_gap": 0.05,
            "time_limit_seconds": 180, "tee": False,
        },
    )
    assert len(frontier) == 8
    assert frontier["npv_eur"].notna().all()
    assert frontier["npv_eur"].iloc[0] >= frontier["npv_eur"].iloc[-1]


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_run_sizing_end_to_end(tmp_path):
    from pvbess_opt import RunConfig
    from pvbess_opt.io import read_workbook, write_workbook

    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)
    short = tmp_path / "short.xlsx"
    write_workbook(typed, short)

    config = RunConfig(
        excel=short, solver="highs", outdir=tmp_path / "out",
        mip_gap=0.05, time_limit=180,
    )
    block = {
        "pv_nameplate_kwp": [4000, 8000],
        "bess_power_kw": [1000],
        "bess_capacity_kwh": [2000, 4000],
    }
    result = run_sizing(config, block)
    assert len(result.frontier) == 4
    runs = list((tmp_path / "out").glob("*_sizing_*"))
    assert runs
    assert (runs[0] / "sizing.xlsx").exists()
    assert (runs[0] / "efficient_frontier.pdf").exists()
    assert (runs[0] / "npv_vs_capacity.pdf").exists()
