"""Cycle-based BESS degradation tests.

The cycle-fade term is additive (subtractive on the capacity factor) and
layered on top of the multiplicative calendar fade.  With the cycle
coefficient at 0 the pipeline output matches the stored calendar-only
baseline, guarding the no-op property.

The baseline fixture reflects per-stream BESS-revenue degradation:
BESS-origin revenue degrades on bess_factor rather than pv_factor, which
sets the multi-year revenue KPIs (NPV / IRR / lifetime revenue) on this
hybrid scenario.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import build_yearly_cashflow, compute_financial_kpis
from pvbess_opt.kpis import add_economic_columns
from pvbess_opt.lifetime import _bess_factor, build_lifetime_dispatch

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "kpi_baseline.json"


# ---------------------------------------------------------------------------
# Fixed baseline scenario — kept deterministic so the KPI fixture
# can be regenerated and re-compared.
# ---------------------------------------------------------------------------


def _econ(d_cycle: float | None = None) -> dict:
    econ = {
        "project_lifecycle_years": 20,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 1.0,
        "retail_inflation_pct": 2.0,
        "dam_inflation_pct": 0.0,
        "aggregator_fee_pct_revenue": 10.0,
        "capex_pv_eur_per_kw": 525.0,
        "capex_bess_eur_per_kw": 200.0,
        "devex_pv_eur_per_kw": 60.0,
        "devex_bess_eur_per_kw": 30.0,
        "opex_pv_eur_per_kwp": 7.0,
        "opex_bess_eur_per_kw": 14.0,
        "pv_degradation_year1_pct": 2.5,
        "pv_degradation_annual_pct": 0.55,
        "bess_degradation_annual_pct": 2.0,
        "bess_replacement_year": 10,
        "bess_replacement_cost_pct": 50.0,
    }
    if d_cycle is not None:
        econ["bess_degradation_pct_per_cycle"] = d_cycle
    return econ


def _year1_kpis() -> dict:
    return {
        "profit_load_from_pv_eur": 900_000.0,
        "profit_load_from_bess_eur": 300_000.0,
        "profit_export_from_pv_eur": 500_000.0,
        "profit_export_from_bess_eur": 200_000.0,
        "expense_charge_bess_grid_eur": 50_000.0,
        "profit_total_eur": 1_850_000.0,
        "bess_total_discharge_mwh": 8_000.0,
        "pv_generation_mwh": 22_500.0,
    }


def _capacities() -> dict:
    return {"pv_kwp": 15_000.0, "bess_kw": 15_000.0, "bess_kwh": 60_000.0}


def _lifetime_yearly() -> pd.DataFrame:
    """Deterministic per-year energy totals for the LCOE / LCOS path."""
    years = list(range(1, 21))
    pv_gen = [22_500.0 * (0.975 * 0.9945 ** (y - 2) if y >= 2 else 1.0)
              for y in years]
    bess_dis = [8_000.0 * (0.98 ** ((y - 1) % 10)) for y in years]
    return pd.DataFrame({
        "project_year": years,
        "calendar_year": [2025 + y for y in years],
        "pv_generation_mwh": pv_gen,
        "bess_discharge_mwh": bess_dis,
    })


_PRE_V088_KPI_KEYS = frozenset({
    "npv_eur", "irr_pct", "roi_pct", "bcr", "simple_payback_years",
    "discounted_payback_years", "total_capex_eur", "total_devex_eur",
    "total_capex_devex_eur", "total_opex_eur_lifecycle",
    "total_revenue_eur_lifecycle", "total_aggregator_fee_eur_lifecycle",
    "capex_year", "project_start_year", "project_end_year",
    "lcoe_eur_per_mwh", "lcos_eur_per_mwh", "pv_capacity_factor",
    "bess_lifetime_cycles",
    "revenue_breakdown_y1_load_pv_eur", "revenue_breakdown_y1_load_bess_eur",
    "revenue_breakdown_y1_export_pv_eur",
    "revenue_breakdown_y1_export_bess_eur",
    "revenue_breakdown_y1_grid_charge_cost_eur",
})


def _compute_baseline_kpis(d_cycle: float | None) -> dict:
    econ = _econ(d_cycle)
    yearly_cf = build_yearly_cashflow(_year1_kpis(), econ, _capacities())
    return compute_financial_kpis(
        yearly_cf, econ,
        capacities=_capacities(),
        lifetime_yearly=_lifetime_yearly(),
        year1_kpis=_year1_kpis(),
    )


# ---------------------------------------------------------------------------
# Byte-identical-when-disabled guard
# ---------------------------------------------------------------------------


def test_zero_cycle_pct_matches_calendar_only_baseline():
    """With bess_degradation_pct_per_cycle = 0 the financial KPIs must
    match the stored calendar-only baseline within 1e-9."""
    assert FIXTURE.exists(), (
        f"missing KPI baseline fixture {FIXTURE}"
    )
    baseline = json.loads(FIXTURE.read_text())
    kpis = _compute_baseline_kpis(d_cycle=0.0)
    for key, expected in baseline.items():
        actual = kpis[key]
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            if expected != expected:  # NaN
                assert actual != actual, f"{key}: expected NaN"
            else:
                assert actual == pytest.approx(expected, rel=1e-9, abs=1e-9), key
        else:
            assert actual == expected, key


def test_missing_key_matches_calendar_only_baseline():
    """A scenario with no bess_degradation_pct_per_cycle key behaves the
    same as one with the key explicitly 0."""
    baseline = json.loads(FIXTURE.read_text())
    kpis = _compute_baseline_kpis(d_cycle=None)
    for key, expected in baseline.items():
        actual = kpis[key]
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            if expected != expected:
                assert actual != actual
            else:
                assert actual == pytest.approx(expected, rel=1e-9, abs=1e-9), key


# ---------------------------------------------------------------------------
# New fade KPI fields
# ---------------------------------------------------------------------------


def test_new_fade_kpis_present_and_additive():
    kpis = _compute_baseline_kpis(d_cycle=0.008)
    for key in (
        "bess_calendar_fade_pct_y_final",
        "bess_cycle_fade_pct_y_final",
        "bess_total_fade_pct_y_final",
    ):
        assert key in kpis
    cal = kpis["bess_calendar_fade_pct_y_final"]
    cyc = kpis["bess_cycle_fade_pct_y_final"]
    tot = kpis["bess_total_fade_pct_y_final"]
    assert cyc > 0.0, "cycle fade must engage when d_cycle > 0"
    assert cal + cyc == pytest.approx(tot, rel=1e-9, abs=1e-9)


def test_disabled_cycle_fade_is_zero():
    kpis = _compute_baseline_kpis(d_cycle=0.0)
    assert kpis["bess_cycle_fade_pct_y_final"] == pytest.approx(0.0, abs=1e-12)
    assert kpis["bess_calendar_fade_pct_y_final"] == pytest.approx(
        kpis["bess_total_fade_pct_y_final"], rel=1e-9, abs=1e-9,
    )


# ---------------------------------------------------------------------------
# Lifetime dispatch — cycling amplifies degradation
# ---------------------------------------------------------------------------


def _make_year1_dispatch(discharge_per_step: float) -> pd.DataFrame:
    n = 8760
    timestamps = pd.date_range("2026-01-01", periods=n, freq="h")
    df = pd.DataFrame({
        "timestamp": timestamps,
        "pv_kwh": np.full(n, 1.0),
        "load_kwh": np.full(n, 1.0),
        "pv_to_load_kwh": np.full(n, 0.5),
        "pv_to_grid_kwh": np.full(n, 0.3),
        "pv_curtail_kwh": np.full(n, 0.0),
        "pv_to_bess_kwh": np.full(n, 0.2),
        "bess_dis_load_kwh": np.full(n, discharge_per_step),
        "bess_dis_grid_kwh": np.full(n, discharge_per_step),
        "bess_charge_grid_kwh": np.full(n, 0.0),
        "grid_to_load_kwh": np.full(n, 0.1),
        "grid_export_total_kwh": np.full(n, 0.5),
        "grid_export_cap_kwh": np.full(n, 5.0),
        "soc_kwh": np.full(n, 100.0),
        "soc_pct": np.full(n, 50.0),
        "dam_price_eur_per_mwh": np.full(n, 80.0),
    })
    # Mimic the post-compute_kpis state required by build_lifetime_dispatch.
    return add_economic_columns(df, {"retail_tariff_eur_per_mwh": 120.0})


def _final_year_bess_factor(res1: pd.DataFrame, econ: dict) -> float:
    caps = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20_000.0}
    lifetime = build_lifetime_dispatch(res1, econ, caps)
    n_years = int(econ["project_lifecycle_years"])
    y1 = float(lifetime.loc[lifetime["project_year"] == 1,
                            "bess_dis_load_kwh"].sum())
    yN = float(lifetime.loc[lifetime["project_year"] == n_years,
                            "bess_dis_load_kwh"].sum())
    return yN / y1


def test_high_cycling_amplifies_degradation():
    econ = _econ(d_cycle=0.01)
    econ["project_lifecycle_years"] = 20
    econ["bess_replacement_year"] = 0
    low = _final_year_bess_factor(_make_year1_dispatch(0.05), econ)
    high = _final_year_bess_factor(_make_year1_dispatch(0.10), econ)
    assert high < low, (
        "a more heavily cycled battery must end with a lower capacity factor"
    )


def test_zero_cycle_pct_lifetime_matches_calendar_only():
    """build_lifetime_dispatch with d_cycle = 0 reproduces the
    calendar-only scaling exactly."""
    econ_zero = _econ(d_cycle=0.0)
    econ_zero["project_lifecycle_years"] = 12
    econ_zero["bess_replacement_year"] = 0
    econ_calendar = dict(econ_zero)
    econ_calendar.pop("bess_degradation_pct_per_cycle", None)
    res1 = _make_year1_dispatch(0.2)
    f_zero = _final_year_bess_factor(res1, econ_zero)
    expected = 0.98 ** 11  # (1 - 2%)^(12 - 1)
    assert f_zero == pytest.approx(expected, rel=1e-9)


# ---------------------------------------------------------------------------
# Reconciliation invariant — calendar + cycle ≈ total at every year
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "d_annual,d_cycle,replacement_year",
    [(0.02, 0.0001, 0), (0.025, 0.00005, 10), (0.015, 0.00012, 15)],
)
def test_reconciliation_invariant(d_annual, d_cycle, replacement_year):
    cumulative = 0.0
    for y in range(1, 21):
        if replacement_year > 0 and y == replacement_year:
            cumulative = 0.0
        if replacement_year > 0 and y >= replacement_year:
            years_since = y - replacement_year
        else:
            years_since = y - 1
        calendar_factor = (1.0 - d_annual) ** years_since
        calendar_fade = 1.0 - calendar_factor
        cycle_fade = d_cycle * cumulative
        factor = _bess_factor(
            y, d_annual, replacement_year=replacement_year,
            d_bess_per_cycle=d_cycle, cumulative_cycles_through=cumulative,
        )
        total_fade = 1.0 - factor
        if calendar_factor - cycle_fade >= 0.0:
            assert calendar_fade + cycle_fade == pytest.approx(
                total_fade, rel=1e-9, abs=1e-9,
            )
        else:
            assert factor == 0.0
        cumulative += 120.0  # arbitrary positive cycle accrual per year


# ---------------------------------------------------------------------------
# Old workbook (no bess_degradation_pct_per_cycle) still loads
# ---------------------------------------------------------------------------


def test_old_workbook_loads(tmp_path, caplog):
    """A workbook lacking bess_degradation_pct_per_cycle loads, emits the
    INFO log, and defaults the coefficient to 0.0 (calendar-only mode)."""
    import openpyxl

    from pvbess_opt.io import read_workbook

    src = ROOT / "inputs" / "input.xlsx"
    dst = tmp_path / "old.xlsx"
    wb = openpyxl.load_workbook(src)
    ws = wb["bess"]
    # Delete the bess_degradation_pct_per_cycle row entirely.
    for idx, row in enumerate(ws.iter_rows(), start=1):
        if row[0].value == "bess_degradation_pct_per_cycle":
            ws.delete_rows(idx, 1)
            break
    wb.save(dst)

    with caplog.at_level(logging.INFO, logger="pvbess_opt.io"):
        typed = read_workbook(dst)
    assert typed["bess"]["bess_degradation_pct_per_cycle"] == 0.0
    assert any(
        "bess_degradation_pct_per_cycle not found" in r.getMessage()
        for r in caplog.records
    ), "expected INFO log about the missing key"
