"""Batch scenario engine: inheritance, overrides, comparison, outputs."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from pvbess_opt.scenarios import (
    _COMPARISON_COLUMNS,
    _apply_scenario_overrides,
    evaluate_scenario,
    read_scenarios_file,
    resolve_inheritance,
    run_scenarios,
    write_scenario_comparison_workbook,
)
from pvbess_opt.theme import COL_WIDTH_MAX, COL_WIDTH_MIN, HEADER_FILL_HEX

ROOT = Path(__file__).resolve().parent.parent


def _highs_available() -> bool:
    try:
        import highspy
    except ImportError:
        return False
    return bool(highspy)


def _base_typed() -> dict:
    from pvbess_opt.io import (
        BALANCING_SHEET_DEFAULTS,
        BESS_SHEET_DEFAULTS,
        ECONOMICS_SHEET_DEFAULTS,
        PROJECT_SHEET_DEFAULTS,
        PV_SHEET_DEFAULTS,
        SIMULATION_SHEET_DEFAULTS,
    )

    return {
        "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=10000.0, capex_pv_eur_per_kw=500.0),
        "bess": dict(
            BESS_SHEET_DEFAULTS, bess_power_kw=5000.0, bess_capacity_kwh=10000.0,
            capex_bess_eur_per_kw=200.0,
        ),
        "project": dict(PROJECT_SHEET_DEFAULTS, site_capex_eur=1000.0),
        "economics": dict(ECONOMICS_SHEET_DEFAULTS),
        "simulation": dict(SIMULATION_SHEET_DEFAULTS),
        "balancing": dict(BALANCING_SHEET_DEFAULTS),
    }


def test_resolve_inheritance_clones_and_overrides():
    scns = [
        {"name": "A", "bess": {"power_kw": 1000}},
        {"name": "B", "inherits": "A", "balancing": True},
    ]
    resolved = {s["name"]: s for s in resolve_inheritance(scns)}
    assert resolved["B"]["bess"]["power_kw"] == 1000   # inherited
    assert resolved["B"]["balancing"] is True          # own override
    assert "inherits" not in resolved["B"]


def test_apply_overrides_shorthand_and_capex_multiplier():
    base = _base_typed()
    typed = _apply_scenario_overrides(base, {
        "name": "x",
        "pv": {"nameplate_kwp": 7000, "source": "pvgis"},
        "bess": {"power_kw": 3000, "capacity_kwh": 6000},
        "balancing": True,
        "capex_multiplier": 0.5,
    })
    assert typed["pv"]["pv_nameplate_kwp"] == 7000
    assert typed["pv"]["pv_source"] == "file"  # forced after resolution
    assert typed["bess"]["bess_power_kw"] == 3000
    assert typed["bess"]["bess_capacity_kwh"] == 6000
    assert typed["balancing"]["balancing_enabled"] is True
    assert typed["pv"]["capex_pv_eur_per_kw"] == pytest.approx(250.0)
    assert typed["bess"]["capex_bess_eur_per_kw"] == pytest.approx(100.0)
    assert typed["project"]["site_capex_eur"] == pytest.approx(500.0)
    assert base["pv"]["pv_nameplate_kwp"] == 10000.0  # base untouched


def test_balancing_off_shorthand():
    typed = _apply_scenario_overrides(
        _base_typed(), {"name": "x", "balancing": False},
    )
    assert typed["balancing"]["balancing_enabled"] is False


def test_read_scenarios_file(tmp_path):
    good = tmp_path / "s.yaml"
    good.write_text(
        "scenarios:\n  - name: A\n  - name: B\n    inherits: A\n",
        encoding="utf-8",
    )
    assert [s["name"] for s in read_scenarios_file(good)] == ["A", "B"]
    bad = tmp_path / "bad.yaml"
    bad.write_text("foo: 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_scenarios_file(bad)


def test_comparison_workbook_is_styled(tmp_path):
    comp = pd.DataFrame(
        [
            {c: ("A" if c == "name" else 0.0) for c in _COMPARISON_COLUMNS},
            {c: ("B" if c == "name" else 1.0) for c in _COMPARISON_COLUMNS},
        ],
        columns=list(_COMPARISON_COLUMNS),
    )
    out = write_scenario_comparison_workbook(tmp_path / "cmp.xlsx", comp)
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
def test_run_scenarios_three(tmp_path):
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
    scns = [
        {"name": "SC hybrid", "project": {"mode": "self_consumption"}},
        {"name": "Merchant hybrid", "project": {"mode": "merchant"}},
        {"name": "Cheap CAPEX", "inherits": "Merchant hybrid",
         "capex_multiplier": 0.8},
    ]
    result = run_scenarios(config, scns)
    assert list(result.comparison["name"]) == [
        "SC hybrid", "Merchant hybrid", "Cheap CAPEX",
    ]
    assert result.comparison["npv_eur"].notna().all()
    npv = dict(zip(
        result.comparison["name"], result.comparison["npv_eur"], strict=False,
    ))
    assert npv["Cheap CAPEX"] > npv["Merchant hybrid"]  # lower CAPEX -> higher NPV
    runs = list((tmp_path / "out").glob("*_scenarios_*"))
    assert runs
    assert (runs[0] / "scenario_comparison.xlsx").exists()
    assert (runs[0] / "scenario_comparison.pdf").exists()
    assert (runs[0] / "scenario_revenue_bridge.pdf").exists()


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_no_override_scenario_matches_standalone(tmp_path):
    from pvbess_opt.availability import apply_unavailability_derate
    from pvbess_opt.io import read_inputs, read_workbook, write_workbook
    from pvbess_opt.kpis import compute_kpis
    from pvbess_opt.optimization import run_scenario as solve
    from pvbess_opt.pipeline import _build_financials

    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)
    short = tmp_path / "short.xlsx"
    write_workbook(typed, short)
    opts = {
        "solver_name": "highs", "mip_gap": 0.05,
        "time_limit_seconds": 180, "tee": False,
    }

    params, ts = read_inputs(short)
    res, _s, _f = solve(params, ts, return_unrounded=True, **opts)
    kpis = apply_unavailability_derate(
        compute_kpis(res, params, verify_balance=False),
        float(params.get("unavailability_pct", 0.0) or 0.0),
    )
    fin = _build_financials(short, params, ts, kpis, res)["fin_kpis"]

    row = evaluate_scenario(read_workbook(short), {"name": "base"}, solver_opts=opts)
    assert row["npv_eur"] == pytest.approx(fin["npv_eur"], rel=1e-9, abs=1e-6)
    assert row["profit_total_eur"] == pytest.approx(
        kpis["profit_total_eur"], rel=1e-9, abs=1e-6,
    )
