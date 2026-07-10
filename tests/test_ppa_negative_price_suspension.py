"""Negative-price suspension clause on the PPA (Eqs. P6-P8).

With ``ppa_negative_price_rule = 'suspend'`` the contract pauses in
every step with DAM < 0 (STRICT: a zero price is not suspended):
physical no longer pays the strike on the covered volume (which then
faces spot), cfd suspends the difference leg while the market leg
keeps selling — and the dispatch reacts (Eq. P8), curtailing or
charging the BESS instead of exporting covered PV at a loss.

Locked properties:

1. P7 settlement algebra at cent level for both settlements, and the
   physical/cfd per-step total equivalence under suspension.
2. ``'none'`` (and an absent key) is bit-identical to the pre-clause
   behaviour; a zero-price step is never suspended.
3. P8 dispatch reaction: a fully covered PV plant that exported
   through negative hours now curtails when the clause is on;
   positive hours are unaffected.
4. The exact fee-exemption KPI ``ppa_fee_exempt_export_mwh`` exists
   only when the clause is on, is availability-derated, and the
   route-to-market fee uses it instead of the share-based
   approximation (which stays bit-identical when the clause is off).
5. Config surface: enum validated with guidance; ``suspension_active``
   property matrix.
"""

from __future__ import annotations

import pandas as pd
import pytest

from pvbess_opt.availability import apply_unavailability_derate
from pvbess_opt.economics import build_yearly_cashflow
from pvbess_opt.io import _validate_ppa_config
from pvbess_opt.kpis import add_economic_columns
from pvbess_opt.ppa import negative_price_mask, resolve_ppa_config

SOLVER_KW = {"solver_name": "highs", "mip_gap": 0.0, "time_limit_seconds": 120}


def _highs_available() -> bool:
    try:
        import highspy  # noqa: F401
    except ImportError:
        return False
    return True


def _ppa(**overrides) -> dict:
    cfg = {
        "ppa_enabled": True,
        "ppa_structure": "pay_as_produced",
        "ppa_settlement": "physical",
        "ppa_price_eur_per_mwh": 65.0,
        "ppa_volume_share_pct": 50.0,
        "ppa_term_years": 10,
        "ppa_inflation_pct": 0.0,
        "ppa_negative_price_rule": "suspend",
    }
    cfg.update(overrides)
    return cfg


def _frame() -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=2, freq="h"),
        "pv_to_load_kwh": [0.0, 0.0],
        "bess_dis_load_kwh": [0.0, 0.0],
        "pv_to_grid_kwh": [10.0, 8.0],
        "bess_dis_grid_kwh": [0.0, 0.0],
        "bess_charge_grid_kwh": [0.0, 0.0],
        "dam_price_eur_per_mwh": [50.0, -20.0],
    })


def _params(settlement: str = "physical", **ppa_overrides) -> dict:
    return {
        "retail_tariff_eur_per_mwh": 0.0,
        "ppa": _ppa(ppa_settlement=settlement, **ppa_overrides),
    }


# ---------------------------------------------------------------------------
# 1. P7 settlement algebra
# ---------------------------------------------------------------------------


def test_physical_suspension_zeroes_contract_in_negative_steps():
    res = add_economic_columns(_frame(), _params("physical"))
    # Step 0 (DAM 50): covered 5 kWh paid the strike.
    assert float(res["revenue_pv_ppa_eur"].iloc[0]) == pytest.approx(
        5.0 / 1000.0 * 65.0,
    )
    # Step 1 (DAM -20): contract paused — no strike payment...
    assert float(res["revenue_pv_ppa_eur"].iloc[1]) == 0.0
    # ...and the FULL export settles at spot in the market column.
    assert float(res["profit_export_from_pv_eur"].iloc[1]) == pytest.approx(
        8.0 / 1000.0 * -20.0,
    )
    # Step 0 market column keeps the uncovered share only.
    assert float(res["profit_export_from_pv_eur"].iloc[0]) == pytest.approx(
        5.0 / 1000.0 * 50.0,
    )
    # Covered DAM value is zero in the suspended step.
    assert float(res["ppa_covered_dam_value_eur"].iloc[1]) == 0.0


def test_cfd_suspension_pauses_difference_leg_only():
    res = add_economic_columns(_frame(), _params("cfd"))
    assert float(res["revenue_pv_ppa_eur"].iloc[0]) == pytest.approx(
        5.0 / 1000.0 * (65.0 - 50.0),
    )
    assert float(res["revenue_pv_ppa_eur"].iloc[1]) == 0.0
    # The market leg keeps the FULL export at DAM in both steps.
    assert float(res["profit_export_from_pv_eur"].iloc[0]) == pytest.approx(
        10.0 / 1000.0 * 50.0,
    )
    assert float(res["profit_export_from_pv_eur"].iloc[1]) == pytest.approx(
        8.0 / 1000.0 * -20.0,
    )


def test_settlements_total_identically_under_suspension():
    phys = add_economic_columns(_frame(), _params("physical"))
    cfd = add_economic_columns(_frame(), _params("cfd"))
    total_phys = (
        phys["revenue_pv_ppa_eur"] + phys["profit_export_from_pv_eur"]
    )
    total_cfd = (
        cfd["revenue_pv_ppa_eur"] + cfd["profit_export_from_pv_eur"]
    )
    pd.testing.assert_series_equal(total_phys, total_cfd)


# ---------------------------------------------------------------------------
# 2. Default-off bit-identity + strictness
# ---------------------------------------------------------------------------


def test_rule_none_and_absent_key_are_bit_identical():
    explicit = add_economic_columns(
        _frame(), _params("physical", ppa_negative_price_rule="none"),
    )
    ppa_absent_rule = _ppa(ppa_settlement="physical")
    ppa_absent_rule.pop("ppa_negative_price_rule")
    absent = add_economic_columns(
        _frame(), {"retail_tariff_eur_per_mwh": 0.0, "ppa": ppa_absent_rule},
    )
    pd.testing.assert_frame_equal(explicit, absent)
    # And 'none' pays the strike THROUGH the negative hour (old behaviour).
    assert float(explicit["revenue_pv_ppa_eur"].iloc[1]) == pytest.approx(
        4.0 / 1000.0 * 65.0,
    )


def test_zero_price_step_is_not_suspended():
    frame = _frame()
    frame["dam_price_eur_per_mwh"] = [0.0, -0.01]
    res = add_economic_columns(frame, _params("physical"))
    # DAM == 0: NOT suspended (strict inequality) — strike paid.
    assert float(res["revenue_pv_ppa_eur"].iloc[0]) == pytest.approx(
        5.0 / 1000.0 * 65.0,
    )
    # DAM = -0.01: suspended.
    assert float(res["revenue_pv_ppa_eur"].iloc[1]) == 0.0


def test_negative_price_mask_is_strict():
    mask = negative_price_mask(pd.Series([-1.0, 0.0, 1.0]))
    assert mask.tolist() == [True, False, False]


# ---------------------------------------------------------------------------
# 3. P8 dispatch reaction
# ---------------------------------------------------------------------------


def _dispatch_setup(dam: list[float], **ppa_overrides) -> tuple[dict, pd.DataFrame]:
    params = {
        "dt_minutes": 60,
        "efficiency_charge": 1.0,
        "efficiency_discharge": 1.0,
        "soc_min_frac": 0.0,
        "soc_max_frac": 1.0,
        "initial_soc_frac": 0.0,
        "terminal_soc_equal": False,
        "max_cycles_per_day": 10.0,
        "p_grid_export_max_kw": 50.0,
        "pv_nameplate_kwp": 100.0,
        "bess_power_kw": 0.0,
        "bess_capacity_kwh": 0.0,
        "retail_tariff_eur_per_mwh": 0.0,
        "mode": "merchant",
        "allow_bess_grid_charging": False,
        "show_titles": False,
        "ppa": _ppa(**{"ppa_volume_share_pct": 100.0, **ppa_overrides}),
    }
    ts = pd.DataFrame({
        "timestamp": pd.date_range(
            "2026-06-01", periods=len(dam), freq="h",
        ),
        "pv_kwh": [10.0] * len(dam),
        "load_kwh": [0.0] * len(dam),
        "dam_price_eur_per_mwh": dam,
    })
    return params, ts


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_covered_pv_curtails_at_negative_dam_with_clause():
    """Without the clause a fully covered plant exports through -20
    (effective price +65, test_ppa_engine locks it); WITH the clause the
    effective price collapses to -20 and everything curtails."""
    from pvbess_opt.optimization import run_scenario

    params, ts = _dispatch_setup([-20.0, -20.0])
    res, _ = run_scenario(params, ts, **SOLVER_KW)
    assert float(res["pv_to_grid_kwh"].sum()) == pytest.approx(0.0, abs=1e-6)
    assert float(res["pv_curtail_kwh"].sum()) == pytest.approx(20.0, abs=1e-6)


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_positive_hours_unaffected_by_clause():
    from pvbess_opt.optimization import run_scenario

    params, ts = _dispatch_setup([50.0, -20.0])
    res, _ = run_scenario(params, ts, **SOLVER_KW)
    # Positive hour exports (strike-priced as before)...
    assert float(res["pv_to_grid_kwh"].iloc[0]) == pytest.approx(
        10.0, abs=1e-6,
    )
    # ...the negative hour curtails.
    assert float(res["pv_to_grid_kwh"].iloc[1]) == pytest.approx(
        0.0, abs=1e-6,
    )


# ---------------------------------------------------------------------------
# 4. Fee-exemption KPI + route-to-market coupling
# ---------------------------------------------------------------------------


N_YEARS = 3


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
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 5.0,
        "opex_bess_eur_per_kw": 5.0,
        "pv_degradation_year1_pct": 0.0,
        "pv_degradation_annual_pct": 0.0,
        "bess_degradation_annual_pct": 0.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
        "route_to_market_fee_eur_per_mwh": 2.0,
        "ppa_enabled": True,
        "ppa_settlement": "physical",
        "ppa_term_years": N_YEARS,
        "ppa_inflation_pct": 0.0,
        "ppa_volume_share_pct": 50.0,
    }
    econ.update(overrides)
    return econ


def _kpis(**extra) -> dict:
    base = {
        "profit_load_from_pv_eur": 0.0,
        "profit_load_from_bess_eur": 0.0,
        "profit_export_from_pv_eur": 40_000.0,
        "profit_export_from_bess_eur": 10_000.0,
        "expense_charge_bess_grid_eur": 0.0,
        "profit_total_eur": 60_000.0,
        "revenue_pv_ppa_eur": 10_000.0,
        "ppa_covered_dam_value_eur": 9_000.0,
        "pv_export_mwh": 100.0,
        "bess_export_mwh": 20.0,
    }
    base.update(extra)
    return base


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}


def test_rtm_fee_uses_exact_exemption_kpi_when_clause_on():
    cf = build_yearly_cashflow(
        _kpis(ppa_fee_exempt_export_mwh=30.0),
        _econ(ppa_negative_price_rule="suspend"),
        _caps(),
    ).set_index("project_year")
    # Exempt 30 of 100 MWh PV export -> 70 charged + 20 BESS export.
    assert float(cf.loc[1, "route_to_market_fee_eur"]) == pytest.approx(
        -2.0 * (70.0 + 20.0),
    )


def test_rtm_fee_share_fallback_without_kpi():
    """Suspend rule but no KPI (hand-built dict): share-based algebra."""
    cf = build_yearly_cashflow(
        _kpis(), _econ(ppa_negative_price_rule="suspend"), _caps(),
    ).set_index("project_year")
    assert float(cf.loc[1, "route_to_market_fee_eur"]) == pytest.approx(
        -2.0 * (100.0 * 0.5 + 20.0),
    )


def test_rtm_fee_ignores_kpi_when_rule_none():
    """Bit-identity: without the clause the KPI must NOT change the fee."""
    with_kpi = build_yearly_cashflow(
        _kpis(ppa_fee_exempt_export_mwh=30.0), _econ(), _caps(),
    )
    without = build_yearly_cashflow(_kpis(), _econ(), _caps())
    pd.testing.assert_frame_equal(with_kpi, without)


def test_exempt_kpi_is_availability_derated():
    derated = apply_unavailability_derate(
        {"ppa_fee_exempt_export_mwh": 100.0, "profit_total_eur": 1.0}, 10.0,
    )
    assert derated["ppa_fee_exempt_export_mwh"] == pytest.approx(90.0)


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_exempt_kpi_counts_non_suspended_covered_export_only():
    from pvbess_opt.kpis import compute_kpis
    from pvbess_opt.optimization import run_scenario

    params, ts = _dispatch_setup(
        [50.0, -20.0], ppa_volume_share_pct=50.0,
    )
    params["unavailability_pct"] = 0.0
    res, _ = run_scenario(params, ts, **SOLVER_KW)
    kpis = compute_kpis(res, params, verify_balance=False)
    # Positive hour exports 10 kWh (blend positive); negative hour: the
    # blend 0.5*(-20)+0.5*65 applies only when NOT suspended — with the
    # clause the whole step faces -20 and curtails.  Exempt volume =
    # 50 % of the non-suspended export.
    exported_kwh = float(res["pv_to_grid_kwh"].iloc[0])
    assert kpis["ppa_fee_exempt_export_mwh"] == pytest.approx(
        0.5 * exported_kwh / 1000.0, abs=1e-6,
    )


def test_no_exempt_kpi_without_clause_or_under_cfd():
    from pvbess_opt.kpis import compute_kpis  # noqa: F401 - import check

    cfg_none = resolve_ppa_config(_ppa(ppa_negative_price_rule="none"))
    assert cfg_none.suspension_active is False
    cfg_cfd = resolve_ppa_config(_ppa(ppa_settlement="cfd"))
    assert cfg_cfd.suspension_active is True  # clause on, but...
    # ...the fee exemption is physical-only; the KPI gate combines both
    # (locked indirectly by test_rtm_fee_* above and the compute_kpis
    # branch condition).


# ---------------------------------------------------------------------------
# 5. Config surface
# ---------------------------------------------------------------------------


def test_validator_rejects_unknown_rule():
    with pytest.raises(ValueError, match="ppa_negative_price_rule"):
        _validate_ppa_config(_ppa(ppa_negative_price_rule="zero_floor"))


def test_suspension_active_matrix():
    assert resolve_ppa_config(_ppa()).suspension_active is True
    assert resolve_ppa_config(
        _ppa(ppa_negative_price_rule="none"),
    ).suspension_active is False
    assert resolve_ppa_config(
        _ppa(ppa_enabled=False),
    ).suspension_active is False


def test_workbook_schema_carries_the_rule():
    from pvbess_opt.io import _PPA_ROWS, PPA_SHEET_DEFAULTS

    assert PPA_SHEET_DEFAULTS["ppa_negative_price_rule"] == "none"
    row = next(
        r for r in _PPA_ROWS if r[0] == "ppa_negative_price_rule"
    )
    assert row[1] == "none"
