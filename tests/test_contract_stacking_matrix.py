"""Consolidated contract stacking-warning matrix.

Every stacking warning across the contracted-revenue structures lives
in the single data-driven ``io._CONTRACT_STACKING_RULES`` table.  Each
rule row is locked twice: it FIRES on its overlap configuration and
stays SILENT when the phase windows are disjoint (or the pairing is
absent).  The ``[contracted revenue]`` run-log audit line appears only
when a structure is active.
"""

from __future__ import annotations

import logging

import pytest

from pvbess_opt.economics import build_yearly_cashflow, compute_financial_kpis
from pvbess_opt.io import (
    _CONTRACT_STACKING_RULES,
    _SHEET_DEFAULTS,
    _phase_windows_overlap,
    validate_workbook_params,
)


def _typed(bess_power_kw: float = 500.0, **econ_overrides) -> dict:
    typed = {
        sheet: dict(defaults) for sheet, defaults in _SHEET_DEFAULTS.items()
    }
    typed["bess"]["bess_power_kw"] = bess_power_kw
    typed["economics"].update(econ_overrides)
    return typed


# Per rule id: a configuration that fires it, a phase-disjoint (or
# pairing-absent) configuration that must stay silent, and the message
# fragment asserted against the log.
_CASES: dict[str, dict] = {
    "toll_no_op": {
        "fires": dict(bess_toll_eur_per_mw_year=50_000.0),
        "fires_kw": 0.0,
        "silent": dict(bess_toll_eur_per_mw_year=50_000.0),
        "silent_kw": 500.0,
        "fragment": "the toll stream is a no-op",
    },
    "toll_retained": {
        "fires": dict(
            bess_toll_eur_per_mw_year=50_000.0,
            bess_toll_merchant_treatment="retained",
        ),
        "silent": dict(
            bess_toll_eur_per_mw_year=50_000.0,
            bess_toll_merchant_treatment="zeroed",
        ),
        "fragment": "double-monetising the same MW",
    },
    "toll_x_optimizer_share": {
        "fires": dict(
            bess_toll_eur_per_mw_year=50_000.0,
            bess_toll_year_from=1, bess_toll_year_to=5,
            optimizer_revenue_share_pct=15.0,
            optimizer_term_year_from=3, optimizer_term_year_to=0,
        ),
        "silent": dict(
            bess_toll_eur_per_mw_year=50_000.0,
            bess_toll_year_from=1, bess_toll_year_to=5,
            optimizer_revenue_share_pct=15.0,
            optimizer_term_year_from=6, optimizer_term_year_to=0,
        ),
        "fragment": "double-charge the",
    },
    "toll_x_optimizer_floor": {
        "fires": dict(
            bess_toll_eur_per_mw_year=50_000.0,
            bess_toll_year_from=1, bess_toll_year_to=5,
            optimizer_floor_enabled=True,
            optimizer_floor_eur_per_kw_year=100.0,
            optimizer_term_year_from=4, optimizer_term_year_to=0,
        ),
        "silent": dict(
            bess_toll_eur_per_mw_year=50_000.0,
            bess_toll_year_from=1, bess_toll_year_to=5,
            optimizer_floor_enabled=True,
            optimizer_floor_eur_per_kw_year=100.0,
            optimizer_term_year_from=6, optimizer_term_year_to=0,
        ),
        "fragment": "full floor top-up",
    },
    "toll_x_state_support": {
        "fires": dict(
            bess_toll_eur_per_mw_year=50_000.0,
            bess_toll_year_from=3, bess_toll_year_to=0,
            state_support_eur_per_mw_year=40_000.0,
            state_support_year_from=1, state_support_year_to=10,
        ),
        "silent": dict(
            bess_toll_eur_per_mw_year=50_000.0,
            bess_toll_year_from=11, bess_toll_year_to=0,
            state_support_eur_per_mw_year=40_000.0,
            state_support_year_from=1, state_support_year_to=10,
        ),
        "fragment": "cumulating two capacity payments",
    },
    "capacity_x_state_support": {
        "fires": dict(
            capacity_market_eur_per_mw_year=50_000.0,
            capacity_market_year_from=1, capacity_market_year_to=0,
            state_support_eur_per_mw_year=40_000.0,
            state_support_year_from=1, state_support_year_to=10,
        ),
        "silent": dict(
            capacity_market_eur_per_mw_year=50_000.0,
            capacity_market_year_from=11, capacity_market_year_to=0,
            state_support_eur_per_mw_year=40_000.0,
            state_support_year_from=1, state_support_year_to=10,
        ),
        "fragment": "support-cumulation",
    },
    "capacity_x_toll": {
        "fires": dict(
            capacity_market_eur_per_mw_year=50_000.0,
            capacity_market_year_from=1, capacity_market_year_to=0,
            bess_toll_eur_per_mw_year=50_000.0,
            bess_toll_year_from=1, bess_toll_year_to=5,
        ),
        "silent": dict(
            capacity_market_eur_per_mw_year=50_000.0,
            capacity_market_year_from=6, capacity_market_year_to=0,
            bess_toll_eur_per_mw_year=50_000.0,
            bess_toll_year_from=1, bess_toll_year_to=5,
        ),
        "fragment": "capacity obligation",
    },
}


def test_every_rule_row_has_a_case():
    """The parametrisation below covers the matrix 1:1 — a new rule
    row without fire/silent cases fails here before it ships."""
    assert {rule[0] for rule in _CONTRACT_STACKING_RULES} == set(_CASES)


@pytest.mark.parametrize("rule_id", sorted(_CASES))
def test_rule_fires_on_overlap_and_stays_silent_when_disjoint(
    rule_id, caplog,
):
    case = _CASES[rule_id]
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io"):
        validate_workbook_params(
            _typed(bess_power_kw=case.get("fires_kw", 500.0),
                   **case["fires"]),
            dt_minutes=15,
        )
    assert case["fragment"] in caplog.text, rule_id
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io"):
        validate_workbook_params(
            _typed(bess_power_kw=case.get("silent_kw", 500.0),
                   **case["silent"]),
            dt_minutes=15,
        )
    assert case["fragment"] not in caplog.text, rule_id


def test_defaults_fire_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io"):
        validate_workbook_params(_typed(), dt_minutes=15)
    for case in _CASES.values():
        assert case["fragment"] not in caplog.text


def test_phase_windows_overlap_helper():
    n = 20
    assert _phase_windows_overlap((1, 5), (5, 10), n)
    assert _phase_windows_overlap((1, 0), (20, 0), n)
    assert not _phase_windows_overlap((1, 5), (6, 0), n)
    assert not _phase_windows_overlap((11, 0), (1, 10), n)


# ---------------------------------------------------------------------------
# Run-log audit line
# ---------------------------------------------------------------------------


def _econ(**o) -> dict:
    econ = {
        "project_lifecycle_years": 3,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 500.0,
        "capex_bess_eur_per_kwh": 100.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 5.0,
        "opex_bess_eur_per_kw": 5.0,
        "pv_degradation_year1_pct": 2.0,
        "pv_degradation_annual_pct": 1.0,
        "bess_degradation_annual_pct": 0.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
    }
    econ.update(o)
    return econ


def _kpis() -> dict:
    return {
        "profit_load_from_pv_eur": 50_000.0,
        "profit_export_from_pv_eur": 30_000.0,
        "profit_export_from_bess_eur": 40_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 115_000.0,
    }


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}


def test_contracted_revenue_audit_line(caplog):
    # Silent in the all-merchant default...
    cf = build_yearly_cashflow(_kpis(), _econ(), _caps())
    with caplog.at_level(logging.INFO, logger="pvbess_opt.economics"):
        compute_financial_kpis(cf, _econ())
    assert "[contracted revenue]" not in caplog.text
    # ...one INFO line when any structure is active.
    caplog.clear()
    econ = _econ(bess_toll_eur_per_mw_year=80_000.0)
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    with caplog.at_level(logging.INFO, logger="pvbess_opt.economics"):
        compute_financial_kpis(cf, econ)
    assert "[contracted revenue]" in caplog.text
    assert caplog.text.count("[contracted revenue]") == 1
