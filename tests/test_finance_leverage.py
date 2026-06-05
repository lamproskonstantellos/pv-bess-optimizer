"""Debt/equity leverage: amortization, equity IRR, DSCR, no unlevered drift."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from pvbess_opt.economics import (
    _amortization_schedule,
    _leverage_kpis,
    build_debt_schedule,
    calculate_irr,
)
from pvbess_opt.io_read import load_structured_config
from pvbess_opt.theme import COL_WIDTH_MAX, COL_WIDTH_MIN, HEADER_FILL_HEX

ROOT = Path(__file__).resolve().parent.parent


def _highs_available() -> bool:
    try:
        import highspy
    except ImportError:
        return False
    return bool(highspy)


@pytest.mark.parametrize("repayment", ["annuity", "linear"])
def test_amortization_balances_to_zero(repayment):
    sched = _amortization_schedule(10_000.0, 0.06, 12, repayment)
    assert len(sched) == 12
    assert sched[-1]["debt_balance_eur"] == pytest.approx(0.0, abs=1e-6)
    assert sum(r["principal_eur"] for r in sched) == pytest.approx(10_000.0, rel=1e-9)


def test_amortization_empty_without_debt_or_tenor():
    assert _amortization_schedule(0.0, 0.05, 10, "annuity") == []
    assert _amortization_schedule(1000.0, 0.05, 0, "annuity") == []


def test_equity_irr_exceeds_project_irr_on_positive_spread():
    net_cf = np.array([-1000.0] + [200.0] * 10)
    project_irr = calculate_irr(net_cf) * 100.0
    equity_irr, min_dscr = _leverage_kpis(net_cf, {
        "gearing_pct": 70.0, "debt_interest_rate_pct": 5.0,
        "debt_tenor_years": 7, "debt_repayment": "annuity",
    })
    assert equity_irr > project_irr            # leverage amplifies the spread
    assert np.isfinite(min_dscr) and min_dscr > 0.0


def test_all_equity_returns_nan():
    net_cf = np.array([-1000.0] + [200.0] * 10)
    eq, dscr = _leverage_kpis(net_cf, {"gearing_pct": 0.0})
    assert np.isnan(eq) and np.isnan(dscr)


def test_build_debt_schedule_columns_and_none_when_all_equity():
    yearly = pd.DataFrame({"net_cashflow_eur": [-1000.0] + [200.0] * 10})
    sched = build_debt_schedule(yearly, {
        "gearing_pct": 60.0, "debt_interest_rate_pct": 5.0,
        "debt_tenor_years": 6, "debt_repayment": "linear",
    })
    assert sched is not None and len(sched) == 6
    assert {
        "year", "debt_service_eur", "equity_cf_eur", "dscr", "debt_balance_eur",
    }.issubset(sched.columns)
    assert sched["debt_balance_eur"].iloc[-1] == pytest.approx(0.0, abs=1e-6)
    assert build_debt_schedule(yearly, {"gearing_pct": 0.0}) is None


def test_financing_block_alias(tmp_path):
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2019-01-01", periods=4, freq="15min"),
        "dam_price_eur_per_mwh": [50.0] * 4,
        "pv_kwh": [0.0] * 4,
    })
    ts.to_csv(tmp_path / "ts.csv", index=False)
    cfg = tmp_path / "run.yaml"
    cfg.write_text(
        "financing:\n"
        "  gearing: 0.7\n"
        "  interest_rate: 0.05\n"
        "  tenor_years: 12\n"
        "  repayment: linear\n"
        "timeseries_path: ts.csv\n",
        encoding="utf-8",
    )
    econ = load_structured_config(cfg)["economics"]
    assert econ["gearing_pct"] == pytest.approx(70.0)
    assert econ["debt_interest_rate_pct"] == pytest.approx(5.0)
    assert econ["debt_tenor_years"] == 12
    assert econ["debt_repayment"] == "linear"


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_leverage_full_run_sheet_and_unlevered_unchanged(tmp_path):
    from pvbess_opt import RunConfig, run
    from pvbess_opt.io import read_workbook, write_workbook

    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)
    unlevered = tmp_path / "unlev.xlsx"
    write_workbook(typed, unlevered)

    typed_lev = read_workbook(ROOT / "inputs" / "input.xlsx")
    typed_lev["ts"] = typed_lev["ts"].iloc[:96].reset_index(drop=True)
    typed_lev["economics"]["gearing_pct"] = 70.0
    typed_lev["economics"]["debt_tenor_years"] = 10
    levered = tmp_path / "lev.xlsx"
    write_workbook(typed_lev, levered)

    common = dict(solver="highs", mip_gap=0.05, time_limit=180)
    r0 = run(RunConfig(excel=unlevered, outdir=tmp_path / "a", **common))
    r1 = run(RunConfig(excel=levered, outdir=tmp_path / "b", **common))

    assert "equity_irr_pct" in r1.financial_kpis
    assert "min_dscr" in r1.financial_kpis
    # Leverage does not touch the unlevered metrics.
    assert r1.financial_kpis["npv_eur"] == pytest.approx(
        r0.financial_kpis["npv_eur"], rel=1e-9, abs=1e-6,
    )
    # Debt-schedule sheet only when financing is configured, and styled.
    wb1 = load_workbook(r1.out_dir / "03_results.xlsx")
    wb0 = load_workbook(r0.out_dir / "03_results.xlsx")
    assert "debt_schedule" in wb1.sheetnames
    assert "debt_schedule" not in wb0.sheetnames
    ws = wb1["debt_schedule"]
    assert ws.freeze_panes == "A2"
    for cell in ws[1]:
        if cell.value is None:
            continue
        rgb = (getattr(cell.fill.fgColor, "rgb", None) or "")
        assert rgb.upper().lstrip("0").rjust(6, "0")[-6:] == HEADER_FILL_HEX
    for c in range(1, ws.max_column + 1):
        dim = ws.column_dimensions.get(get_column_letter(c))
        assert dim is not None and COL_WIDTH_MIN <= float(dim.width) <= COL_WIDTH_MAX
