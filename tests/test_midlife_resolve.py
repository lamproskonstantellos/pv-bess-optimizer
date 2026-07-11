"""Mid-life re-solve validation (Eq. E53).

`midlife_resolve_year` re-solves the dispatch at project year k with
faded parameters (BESS energy x f_k, PV column x pv_factor(k), power
and prices at Year-1 levels) and reports a scaled-vs-resolved delta
table — diagnostic only.  Locked here: zero-default bit-identity (no
extra solve, no workbook sheet, no SUMMARY section), the exact faded
parameters handed to the solver, the zero-degradation ~zero-delta
sanity case, the delta-table schema with its MIP-gap row, the
no-mutation guarantee on the inputs, and the loader validation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import pvbess_opt.pipeline as pipeline
from pvbess_opt.io import write_results_workbook, write_summary_md
from pvbess_opt.kpis import compute_kpis
from pvbess_opt.lifetime import (
    aggregate_lifetime_to_yearly,
    build_lifetime_dispatch,
    factors_for_year,
)
from pvbess_opt.optimization import run_scenario


def _highs_available() -> bool:
    try:
        import highspy
    except ImportError:
        return False
    return bool(highspy)


def _params(**o) -> dict:
    p = {
        "dt_minutes": 60,
        "mode": "merchant",
        "pv_nameplate_kwp": 500.0,
        "bess_power_kw": 500.0,
        "bess_capacity_kwh": 1000.0,
        "efficiency_charge": 1.0,
        "efficiency_discharge": 1.0,
        "soc_min_frac": 0.0,
        "soc_max_frac": 1.0,
        "initial_soc_frac": 0.0,
        "terminal_soc_equal": False,
        "max_cycles_per_day": 10.0,
        "p_grid_export_max_kw": 5000.0,
        "retail_tariff_eur_per_mwh": 120.0,
        "allow_bess_grid_charging": True,
    }
    p.update(o)
    return p


def _ts(n_days: int = 2) -> pd.DataFrame:
    n = 24 * n_days
    hours = np.arange(n) % 24
    price = np.where(hours < 8, 10.0, np.where(hours < 16, 60.0, 200.0))
    pv = np.where((hours >= 8) & (hours < 16), 300.0, 0.0)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "pv_kwh": pv.astype(float),
        "dam_price_eur_per_mwh": price.astype(float),
    })


def _econ(**o) -> dict:
    econ = {
        "project_lifecycle_years": 6,
        "project_start_year": 2026,
        "pv_degradation_year1_pct": 0.0,
        "pv_degradation_annual_pct": 0.0,
        "bess_degradation_annual_pct": 0.0,
        "bess_degradation_pct_per_cycle": 0.0,
        "bess_replacement_year": 0,
        "unavailability_pct": 0.0,
        "midlife_resolve_year": 0,
    }
    econ.update(o)
    return econ


def test_off_returns_none_without_solving(monkeypatch):
    calls = {"n": 0}

    def _no_solve(*a, **kw):
        calls["n"] += 1
        raise AssertionError("run_scenario must not be called when off")

    monkeypatch.setattr(pipeline, "run_scenario", _no_solve)
    ly = pd.DataFrame({"project_year": [1, 2], "pv_generation_mwh": [1., 1.]})
    for econ in (_econ(), _econ(midlife_resolve_year=0)):
        assert pipeline._run_midlife_resolve(
            _params(), _ts(), econ, {}, ly,
        ) is None
    assert calls["n"] == 0


def test_workbook_sheet_and_summary_only_when_present(tmp_path):
    res = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=4, freq="h"),
        "pv_kwh": [1.0, 2.0, 3.0, 4.0],
    })
    delta = pd.DataFrame({
        "kpi": ["pv_generation_mwh", "requested_mip_gap"],
        "scaled": [100.0, 0.001], "resolved": [100.2, 0.001],
        "delta": [0.2, 0.0], "delta_pct": [0.2, 0.0],
    })
    off = tmp_path / "off.xlsx"
    on = tmp_path / "on.xlsx"
    write_results_workbook(off, res_year1=res, kpis_year1={"x": 1.0},
                           kpis_monthly_year1=None)
    write_results_workbook(on, res_year1=res, kpis_year1={"x": 1.0},
                           kpis_monthly_year1=None, midlife_resolve=delta)
    assert "midlife_resolve" not in pd.ExcelFile(off).sheet_names
    assert "midlife_resolve" in pd.ExcelFile(on).sheet_names
    back = pd.read_excel(on, sheet_name="midlife_resolve")
    assert list(back.columns) == [
        "kpi", "scaled", "resolved", "delta", "delta_pct",
    ]

    md_off = tmp_path / "off.md"
    md_on = tmp_path / "on.md"
    write_summary_md(md_off, kpis_year1={"profit_total_eur": 1.0},
                     financial_kpis=None, params={})
    write_summary_md(md_on, kpis_year1={"profit_total_eur": 1.0},
                     financial_kpis=None, params={}, midlife_resolve=delta)
    assert "Mid-life re-solve" not in md_off.read_text()
    text = md_on.read_text()
    assert "## Mid-life re-solve validation" in text
    assert "MIP gap (0.001)" in text
    assert "pv_generation_mwh" in text
    # The gap row is prose, not a table row.
    assert "| requested_mip_gap" not in text


def test_faded_params_passed_exactly(monkeypatch):
    class _Captured(Exception):
        pass

    seen: dict = {}

    def _capture(p, t, **kw):
        seen["params"] = p
        seen["ts"] = t
        raise _Captured()

    monkeypatch.setattr(pipeline, "run_scenario", _capture)
    econ = _econ(
        midlife_resolve_year=4,
        pv_degradation_year1_pct=2.0,
        pv_degradation_annual_pct=1.0,
        bess_degradation_annual_pct=3.0,
    )
    params = _params()
    ts = _ts()
    ly = pd.DataFrame({
        "project_year": [4], "pv_generation_mwh": [100.0],
    })
    kpis = {"bess_total_discharge_mwh": 400.0}
    with pytest.raises(_Captured):
        pipeline._run_midlife_resolve(params, ts, econ, kpis, ly)
    pv_f, bess_f = factors_for_year(
        econ, year=4, year1_discharge_mwh=400.0, capacity_mwh=1.0,
    )
    assert seen["params"]["bess_capacity_kwh"] == pytest.approx(
        1000.0 * bess_f,
    )
    np.testing.assert_allclose(
        seen["ts"]["pv_kwh"].to_numpy(),
        ts["pv_kwh"].to_numpy(dtype=float) * pv_f,
    )
    # No-mutation guarantee: the caller's params and ts are untouched.
    assert params["bess_capacity_kwh"] == 1000.0
    assert float(ts["pv_kwh"].max()) == 300.0


@pytest.mark.skipif(not _highs_available(), reason="requires HiGHS")
def test_no_degradation_yields_zero_delta_and_schema():
    params = _params()
    ts = _ts()
    res, _s, _f = run_scenario(
        params, ts, return_unrounded=True, mip_gap=1e-6,
    )
    kpis = compute_kpis(res, params, verify_balance=False)
    econ = _econ(midlife_resolve_year=5)
    capacities = {"pv_kwp": 500.0, "bess_kw": 500.0, "bess_kwh": 1000.0}
    lifetime_df = build_lifetime_dispatch(res, econ, capacities)
    ly = aggregate_lifetime_to_yearly(lifetime_df)

    delta = pipeline._run_midlife_resolve(
        params, ts, econ, kpis, ly, solver_opts={"mip_gap": 1e-6},
    )
    assert delta is not None
    assert list(delta.columns) == [
        "kpi", "scaled", "resolved", "delta", "delta_pct",
    ]
    gap_row = delta.loc[delta["kpi"] == "requested_mip_gap"]
    assert len(gap_row) == 1
    assert float(gap_row["scaled"].iloc[0]) == pytest.approx(1e-6)
    body = delta.loc[delta["kpi"] != "requested_mip_gap"]
    assert not body.empty
    # All degradation knobs are 0, so the re-solved problem is the
    # SAME problem: every delta is solver noise, bounded by the gap.
    for _, row in body.iterrows():
        assert abs(float(row["delta_pct"])) <= 0.05, row["kpi"]
    # Feature never mutates its inputs (diagnostic-only guarantee).
    assert params["bess_capacity_kwh"] == 1000.0
    assert float(ts["pv_kwh"].max()) == 300.0


def test_year_not_in_projection_skips(monkeypatch, caplog):
    def _no_solve(*_a, **_k):
        raise AssertionError("no solve")

    monkeypatch.setattr(pipeline, "run_scenario", _no_solve)
    ly = pd.DataFrame({"project_year": [1, 2], "pv_generation_mwh": [1., 1.]})
    with caplog.at_level("WARNING"):
        out = pipeline._run_midlife_resolve(
            _params(), _ts(), _econ(midlife_resolve_year=5), {}, ly,
        )
    assert out is None
    assert "skipping" in caplog.text


def test_loader_validation(tmp_path):
    from pvbess_opt.io import (
        BALANCING_SHEET_DEFAULTS,
        BESS_SHEET_DEFAULTS,
        ECONOMICS_SHEET_DEFAULTS,
        PROJECT_SHEET_DEFAULTS,
        PV_SHEET_DEFAULTS,
        SIMULATION_SHEET_DEFAULTS,
        read_workbook,
        write_workbook,
    )

    _counter = iter(range(100))

    def _write(**sim_overrides):
        path = tmp_path / f"wb{next(_counter)}.xlsx"
        n = 24
        typed = {
            "ts": pd.DataFrame({
                "timestamp": pd.date_range(
                    "2026-01-01", periods=n, freq="h",
                ),
                "pv_kwh": np.full(n, 100.0),
                "dam_price_eur_per_mwh": np.full(n, 60.0),
            }),
            "project": dict(
                PROJECT_SHEET_DEFAULTS, mode="merchant",
                project_lifecycle_years=10,
            ),
            "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=1000.0),
            "bess": dict(
                BESS_SHEET_DEFAULTS, bess_power_kw=500.0,
                bess_capacity_kwh=1000.0,
            ),
            "economics": dict(ECONOMICS_SHEET_DEFAULTS),
            "simulation": dict(SIMULATION_SHEET_DEFAULTS, **sim_overrides),
            "balancing": dict(BALANCING_SHEET_DEFAULTS),
        }
        write_workbook(typed, path)
        return path

    # Valid settings parse.
    typed_back = read_workbook(_write(midlife_resolve_year=5))
    assert typed_back["simulation"]["midlife_resolve_year"] == 5
    read_workbook(_write(midlife_resolve_year=0))
    read_workbook(_write(midlife_resolve_year=10))

    # Year 1 is the solved dispatch; out-of-range years are rejected.
    with pytest.raises(ValueError, match=r"2\.\.project_lifecycle_years"):
        read_workbook(_write(midlife_resolve_year=1))
    with pytest.raises(ValueError, match=r"2\.\.project_lifecycle_years"):
        read_workbook(_write(midlife_resolve_year=11))
