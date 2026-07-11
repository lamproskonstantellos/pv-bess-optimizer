"""Low-price-deck debt sizing case (activation of debt_sizing_case='low_price').

The sizing case re-dispatches the year with the named price deck
(Phase-1 `<column>__<deck>` variant machinery) and sizes the
target-DSCR debt on the deck's yearly cashflow — Eqs. E41-E44 apply
verbatim to the deck CFADS.  Locked: validation (deck columns
required, blank deck rejected, availability listed), the lender-table
low_price row (present only when the deck frame is supplied — the
table never triggers a solve), and the end-to-end wiring through
_build_financials on a small solvable workbook: deck CFADS below
base, the sized debt hitting the target on the deck case and covering
with margin on the base year.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import (
    _amortization_schedule,
    build_yearly_cashflow,
)
from pvbess_opt.io import validate_workbook_params
from pvbess_opt.lender import build_lender_cases


def _highs_available() -> bool:
    try:
        import highspy
    except ImportError:
        return False
    return bool(highspy)


N_STEPS = 48


def _typed_with_deck(*, econ_overrides: dict | None = None) -> dict:
    from pvbess_opt.io import (
        BALANCING_SHEET_DEFAULTS,
        BESS_SHEET_DEFAULTS,
        ECONOMICS_SHEET_DEFAULTS,
        PROJECT_SHEET_DEFAULTS,
        PV_SHEET_DEFAULTS,
        SIMULATION_SHEET_DEFAULTS,
    )

    rng = np.arange(N_STEPS)
    base_price = 80.0 + 40.0 * np.sin(rng * 2.0 * np.pi / 24.0)
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=N_STEPS, freq="h"),
        "pv_kwh": np.where((rng % 24 >= 7) & (rng % 24 <= 17), 900.0, 0.0),
        "load_kwh": np.full(N_STEPS, 300.0),
        "dam_price_eur_per_mwh": base_price,
        # The Low deck halves the price level: less revenue, lower
        # CFADS, smaller sustainable debt.
        "dam_price_eur_per_mwh__low": base_price * 0.5,
    })
    econ = dict(
        ECONOMICS_SHEET_DEFAULTS,
        debt_sizing_mode="target_dscr",
        target_dscr=1.30,
        debt_sizing_case="low_price",
        debt_interest_rate_pct=5.0,
        debt_tenor_years=8,
        debt_repayment="annuity",
        lender_cases_enabled=True,
        sensitivity_enabled=False,
    )
    econ.update(econ_overrides or {})
    # CAPEX / OPEX are scaled to the 48-hour dispatch window (the
    # Year-1 KPIs carry two days of revenue): the outlay must exceed
    # the sustainable debt so the E43 cap does not bind and the
    # round-trip hits the target exactly.
    return {
        "ts": ts,
        "project": dict(PROJECT_SHEET_DEFAULTS),
        "pv": dict(
            PV_SHEET_DEFAULTS, pv_nameplate_kwp=1000.0,
            capex_pv_eur_per_kw=20.0, devex_pv_eur_per_kw=0.0,
            opex_pv_eur_per_kwp=0.1,
        ),
        "bess": dict(
            BESS_SHEET_DEFAULTS, bess_power_kw=500.0,
            bess_capacity_kwh=1000.0,
            capex_bess_eur_per_kwh=1.0, devex_bess_eur_per_kw=0.0,
            opex_bess_eur_per_kw=0.1, bess_replacement_year=0,
        ),
        "economics": econ,
        "simulation": dict(SIMULATION_SHEET_DEFAULTS),
        "balancing": dict(BALANCING_SHEET_DEFAULTS),
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validation_accepts_low_price_with_deck_columns():
    validate_workbook_params(_typed_with_deck(), dt_minutes=60)


def test_validation_rejects_low_price_without_deck_columns():
    typed = _typed_with_deck()
    typed["ts"] = typed["ts"].drop(columns=["dam_price_eur_per_mwh__low"])
    with pytest.raises(ValueError, match="variant columns"):
        validate_workbook_params(typed, dt_minutes=60)


def test_validation_names_available_decks():
    typed = _typed_with_deck(
        econ_overrides={"debt_sizing_deck": "central"},
    )
    with pytest.raises(ValueError, match=r"decks available.*low"):
        validate_workbook_params(typed, dt_minutes=60)


def test_validation_rejects_blank_deck_name():
    typed = _typed_with_deck(econ_overrides={"debt_sizing_deck": "  "})
    with pytest.raises(ValueError, match="debt_sizing_deck"):
        validate_workbook_params(typed, dt_minutes=60)


def test_deck_keys_inert_in_manual_mode():
    typed = _typed_with_deck(
        econ_overrides={"debt_sizing_mode": "manual"},
    )
    typed["ts"] = typed["ts"].drop(columns=["dam_price_eur_per_mwh__low"])
    validate_workbook_params(typed, dt_minutes=60)


# ---------------------------------------------------------------------------
# Lender-table row
# ---------------------------------------------------------------------------


def _cf_pair() -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    econ = {
        "project_lifecycle_years": 8,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 800.0,
        "capex_bess_eur_per_kwh": 100.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 5.0,
        "opex_bess_eur_per_kw": 5.0,
        "pv_degradation_year1_pct": 2.0,
        "pv_degradation_annual_pct": 1.0,
        "bess_degradation_annual_pct": 1.5,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
        "gearing_pct": 60.0,
        "debt_interest_rate_pct": 5.0,
        "debt_tenor_years": 6,
        "debt_repayment": "annuity",
        "production_p90_factor_pct": 92.0,
    }
    caps = {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}
    kpis_base = {
        "profit_load_from_pv_eur": 60_000.0,
        "profit_load_from_bess_eur": 10_000.0,
        "profit_export_from_pv_eur": 40_000.0,
        "profit_export_from_bess_eur": 40_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 145_000.0,
    }
    kpis_low = dict(kpis_base)
    for key in ("profit_load_from_pv_eur", "profit_export_from_pv_eur",
                "profit_export_from_bess_eur"):
        kpis_low[key] = kpis_base[key] * 0.6
    base_cf = build_yearly_cashflow(kpis_base, econ, caps)
    low_cf = build_yearly_cashflow(kpis_low, econ, caps)
    return base_cf, low_cf, econ


def test_low_price_row_only_when_frame_supplied():
    base_cf, low_cf, econ = _cf_pair()
    without = build_lender_cases(base_cf, econ)
    assert list(without["case"]) == ["base", "p90"]
    table = build_lender_cases(base_cf, econ, low_price_cf=low_cf)
    assert list(table["case"]) == ["base", "p90", "low_price"]
    row = table.loc[table["case"] == "low_price"].iloc[0]
    base_row = table.loc[table["case"] == "base"].iloc[0]
    # A price case keeps the full production...
    assert row["production_factor_pct"] == 100.0
    # ...but carries less revenue: coverage and capacity sit below base.
    assert row["min_dscr"] < base_row["min_dscr"]
    assert row["debt_capacity_eur"] < base_row["debt_capacity_eur"]
    assert row["npv_eur"] < base_row["npv_eur"]


# ---------------------------------------------------------------------------
# End-to-end: deck re-dispatch + sizing round-trip
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _highs_available(), reason="requires HiGHS")
def test_low_price_sizing_roundtrip_end_to_end(tmp_path):
    from pvbess_opt.availability import apply_unavailability_derate
    from pvbess_opt.io import read_inputs, write_workbook
    from pvbess_opt.kpis import compute_kpis
    from pvbess_opt.optimization import run_scenario
    from pvbess_opt.pipeline import _build_financials

    xlsx = tmp_path / "deck_sizing.xlsx"
    write_workbook(_typed_with_deck(), xlsx)
    params, ts = read_inputs(xlsx)
    res, _solver, _full = run_scenario(params, ts, return_unrounded=True)
    kpis = compute_kpis(res, params, verify_balance=False)
    kpis = apply_unavailability_derate(
        kpis, float(params.get("unavailability_pct", 0.0) or 0.0),
    )
    bundle = _build_financials(xlsx, params, ts, kpis, res)
    econ = bundle["econ"]
    fin = bundle["fin_kpis"]
    base_cf = bundle["yearly_cf"]

    # Sizing resolved on the DECK cashflow (frozen underscore keys).
    assert econ.get("_sized_debt_eur") is not None
    assert fin["sized_debt_eur"] > 0.0
    assert fin["dscr_target_met"] == 1.0

    # The lender table carries the low_price row from the same solve.
    table = bundle["lender_cases"]
    assert table is not None
    assert list(table["case"]) == ["base", "p90", "low_price"]
    low_row = table.loc[table["case"] == "low_price"].iloc[0]
    base_row = table.loc[table["case"] == "base"].iloc[0]
    # Half prices => less revenue in the deck year.
    assert low_row["npv_eur"] < base_row["npv_eur"]

    # Round-trip: replaying the sized debt on the deck CFADS hits the
    # target at the binding year; the base year covers with margin.
    tenor = 8
    sched = _amortization_schedule(
        float(econ["_sized_debt_eur"]), 0.05, tenor, "annuity",
    )
    assert low_row["min_dscr"] == pytest.approx(1.30, abs=1e-3)
    base_net = base_cf["net_cashflow_eur"].to_numpy(dtype=float)
    base_dscrs = [
        float(base_net[int(r["year"])]) / r["debt_service_eur"]
        for r in sched
    ]
    assert min(base_dscrs) > 1.30
