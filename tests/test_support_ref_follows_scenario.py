"""support_ref_follows_scenario: the support-reference escalation rule.

With the price-scenario engine ARMED (the ``_price_scenario_applied``
marker set by ``apply_price_scenarios``), every support REFERENCE leg —
the CfD difference legs (E45/E46) and the E56 settlement reference —
follows one rule: the scenario's PV-leg DAM path when the toggle is
TRUE (the default), the plain ``dam_inflation_pct`` scalar when FALSE.
Disarmed, each site keeps its historical series regardless of the
toggle (bit-identity): the CfD legs ride ``g_dam_pv``, the E56
reference the scalar.
"""

from __future__ import annotations

import pandas as pd
import pytest

from pvbess_opt.economics import build_yearly_cashflow

N_YEARS = 4
DECLINE = [1.0, 0.5, 0.5, 0.5]


def _econ(**overrides) -> dict:
    econ = {
        "project_lifecycle_years": N_YEARS,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 500.0,
        "capex_bess_eur_per_kwh": 100.0,
        "opex_pv_eur_per_kwp": 5.0,
        "opex_bess_eur_per_kw": 5.0,
        "pv_degradation_year1_pct": 0.0,
        "pv_degradation_annual_pct": 0.0,
        "bess_degradation_annual_pct": 0.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
        "trajectories": {
            "revenue_dam_pv": {"mode": "replace", "values": DECLINE},
        },
    }
    econ.update(overrides)
    return econ


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}


def _kpis(**extra) -> dict:
    kpis = {
        "profit_load_from_pv_eur": 50_000.0,
        "profit_load_from_bess_eur": 10_000.0,
        "profit_export_from_pv_eur": 60_000.0,
        "profit_export_from_bess_eur": 40_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 155_000.0,
        "pv_export_mwh": 800.0,
        "bess_export_mwh": 300.0,
    }
    kpis.update(extra)
    return kpis


def _settlement(cf: pd.DataFrame, column: str, year: int) -> float:
    row = cf[cf["project_year"] == year]
    return float(row[column].iloc[0])


# ---------------------------------------------------------------------------
# CfD reference leg (E45/E46)
# ---------------------------------------------------------------------------

_CFD_KW = dict(
    ppa_enabled=True, ppa_settlement="cfd", ppa_term_years=N_YEARS,
    ppa_inflation_pct=0.0, ppa_volume_share_pct=50.0,
)
_CFD_KPIS = dict(
    revenue_pv_ppa_eur=30_000.0, ppa_covered_dam_value_eur=25_000.0,
)


def test_cfd_ref_decouples_when_armed_and_opted_out():
    """Armed + FALSE: the CfD reference rides the scalar, so the
    scenario's DAM decline no longer inflates the CfD pay-out."""
    follows = build_yearly_cashflow(
        _kpis(**_CFD_KPIS),
        _econ(_price_scenario_applied="Central",
              support_ref_follows_scenario=True, **_CFD_KW),
        _caps(),
    )
    decoupled = build_yearly_cashflow(
        _kpis(**_CFD_KPIS),
        _econ(_price_scenario_applied="Central",
              support_ref_follows_scenario=False, **_CFD_KW),
        _caps(),
    )
    # Year 2: the followed reference halves (bigger difference leg);
    # the decoupled reference stays flat.  Exact gap: covered Year-1
    # DAM value x (1.0 - 0.5).
    gap = (
        _settlement(follows, "ppa_revenue_eur", 2)
        - _settlement(decoupled, "ppa_revenue_eur", 2)
    )
    assert gap == pytest.approx(25_000.0 * 0.5)
    # The merchant DAM stream itself declines identically in both.
    pd.testing.assert_series_equal(
        follows["revenue_dam_eur"], decoupled["revenue_dam_eur"],
    )


def test_cfd_ref_ignores_toggle_when_disarmed():
    """Disarmed, the toggle is inert: a user-declared trajectory keeps
    the historical CfD behaviour (the leg follows g_dam_pv) whatever
    the flag says — bit-identity for existing workbooks."""
    on = build_yearly_cashflow(
        _kpis(**_CFD_KPIS),
        _econ(support_ref_follows_scenario=True, **_CFD_KW),
        _caps(),
    )
    off = build_yearly_cashflow(
        _kpis(**_CFD_KPIS),
        _econ(support_ref_follows_scenario=False, **_CFD_KW),
        _caps(),
    )
    pd.testing.assert_frame_equal(on, off)
    # And the leg does follow the user trajectory (historical rule).
    flat = build_yearly_cashflow(
        _kpis(**_CFD_KPIS), _econ(trajectories=None, **_CFD_KW), _caps(),
    )
    assert _settlement(on, "ppa_revenue_eur", 2) > _settlement(
        flat, "ppa_revenue_eur", 2,
    )


# ---------------------------------------------------------------------------
# E56 support settlement reference
# ---------------------------------------------------------------------------

_E56_KW = dict(
    support_scheme="cfd_two_way",
    support_strike_eur_per_mwh=80.0,
    support_term_years=N_YEARS,
)
_E56_KPIS = dict(
    support_settlement_eur=12_000.0,
    support_monthly_eligible_mwh=[100.0] * 12,
    support_monthly_ref_price_eur_per_mwh=[70.0] * 12,
)


def test_e56_ref_follows_scenario_when_armed():
    """Armed + TRUE (the default): the E56 reference follows the
    scenario path — the capture-price decline REACHES the settlement.
    Armed + FALSE keeps the historical scalar reference."""
    follows = build_yearly_cashflow(
        _kpis(**_E56_KPIS),
        _econ(_price_scenario_applied="Central",
              support_ref_follows_scenario=True, **_E56_KW),
        _caps(),
    )
    decoupled = build_yearly_cashflow(
        _kpis(**_E56_KPIS),
        _econ(_price_scenario_applied="Central",
              support_ref_follows_scenario=False, **_E56_KW),
        _caps(),
    )
    # Year 2 with the reference halved: diff gains 12 x 100 x 70 x 0.5.
    gap = (
        _settlement(follows, "support_settlement_eur", 2)
        - _settlement(decoupled, "support_settlement_eur", 2)
    )
    assert gap == pytest.approx(12 * 100.0 * 70.0 * 0.5)
    # The decoupled run reproduces the historical scalar settlement:
    # dam_inflation_pct = 0 keeps Year-1's number flat.
    assert _settlement(
        decoupled, "support_settlement_eur", 2,
    ) == pytest.approx(12 * 100.0 * (80.0 - 70.0))


def test_e56_ref_stays_scalar_when_disarmed():
    """Disarmed bit-identity: even with a user-declared DAM
    trajectory, the E56 reference keeps its historical scalar series
    whatever the toggle says."""
    for flag in (True, False):
        cf = build_yearly_cashflow(
            _kpis(**_E56_KPIS),
            _econ(support_ref_follows_scenario=flag, **_E56_KW),
            _caps(),
        )
        assert _settlement(
            cf, "support_settlement_eur", 2,
        ) == pytest.approx(12 * 100.0 * (80.0 - 70.0))
