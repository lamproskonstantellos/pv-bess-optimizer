"""Charging-side grid fee wedge (Eq. E26).

The regulated EUR/MWh wedge on grid-charged BESS energy enters the
MILP objective (buy price = DAM + wedge) AND the per-step economics,
so thin arbitrage spreads flip correctly and the paid wedge is
auditable.  Locked properties:

1. Thin-spread flip (HiGHS): a spread that beats the round-trip on the
   energy-only price but not on price+wedge stops the cycle.
2. Objective/KPI consistency: ``profit_total_eur`` equals the solved
   objective value with the wedge on (mip_gap 0, wear cost 0).
3. Zero-default bit-identity: fee absent = fee 0 = exempt TRUE, in the
   dispatch frame AND the KPI dict.
4. The fee KPI is availability-derated with the charging throughput.
5. Validation: negative wedge rejected; boolean exemption accepted;
   inert-fee warning when grid charging is disallowed.
"""

from __future__ import annotations

import pandas as pd
import pytest

from pvbess_opt.availability import apply_unavailability_derate
from pvbess_opt.io import validate_workbook_params
from pvbess_opt.kpis import compute_kpis

SOLVER_KW = {"solver_name": "highs", "mip_gap": 0.0, "time_limit_seconds": 120}


def _highs_available() -> bool:
    try:
        import highspy  # noqa: F401
    except ImportError:
        return False
    return True


def _arbitrage_setup(
    fee: float = 0.0, *, exempt: bool = False,
    allow_grid_charging: bool = True,
) -> tuple[dict, pd.DataFrame]:
    """BESS-only merchant, RTE = 1, buy at 10 sell at 30 (spread 20)."""
    params = {
        "dt_minutes": 60,
        "efficiency_charge": 1.0,
        "efficiency_discharge": 1.0,
        "soc_min_frac": 0.0,
        "soc_max_frac": 1.0,
        "initial_soc_frac": 0.0,
        "terminal_soc_equal": False,
        "max_cycles_per_day": 10.0,
        "p_grid_export_max_kw": 1000.0,
        "pv_nameplate_kwp": 0.0,
        "bess_power_kw": 100.0,
        "bess_capacity_kwh": 100.0,
        "retail_tariff_eur_per_mwh": 0.0,
        "mode": "merchant",
        "allow_bess_grid_charging": allow_grid_charging,
        "grid_charging_fee_eur_per_mwh": fee,
        "grid_charging_fee_exempt": exempt,
        "bess_wear_cost_eur_per_mwh": 0.0,
        "show_titles": False,
    }
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=2, freq="h"),
        "pv_kwh": [0.0, 0.0],
        "load_kwh": [0.0, 0.0],
        "dam_price_eur_per_mwh": [10.0, 30.0],
    })
    return params, ts


# ---------------------------------------------------------------------------
# 1+2. Thin-spread flip + objective/KPI consistency
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_thin_spread_flips_with_the_wedge():
    from pvbess_opt.optimization import run_scenario

    # Without the wedge: buy 100 kWh at 10, sell at 30 -> profit 2 EUR.
    params, ts = _arbitrage_setup(fee=0.0)
    res, _ = run_scenario(params, ts, **SOLVER_KW)
    assert float(res["bess_charge_grid_kwh"].sum()) == pytest.approx(
        100.0, abs=1e-6,
    )
    # Wedge 25: effective buy 35 > sell 30 -> the cycle must stop.
    params_fee, ts = _arbitrage_setup(fee=25.0)
    res_fee, _ = run_scenario(params_fee, ts, **SOLVER_KW)
    assert float(res_fee["bess_charge_grid_kwh"].sum()) == pytest.approx(
        0.0, abs=1e-6,
    )


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_profit_kpi_matches_wedge_adjusted_objective():
    from pvbess_opt.optimization import run_scenario

    # Wedge 5: effective buy 15, sell 30 -> still cycles; profit
    # = 100 kWh x (30 - 10 - 5) / 1000 = 1.5 EUR.
    params, ts = _arbitrage_setup(fee=5.0)
    res, _ = run_scenario(params, ts, **SOLVER_KW)
    kpis = compute_kpis(res, params, verify_balance=False)
    assert kpis["profit_total_eur"] == pytest.approx(1.5, abs=1e-6)
    assert kpis["expense_grid_charging_fee_eur"] == pytest.approx(
        0.5, abs=1e-6,
    )


# ---------------------------------------------------------------------------
# 3. Zero-default bit-identity + exemption switch
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_fee_zero_absent_and_exempt_are_identical():
    from pvbess_opt.optimization import run_scenario

    params_zero, ts = _arbitrage_setup(fee=0.0)
    params_absent, _ = _arbitrage_setup(fee=0.0)
    del params_absent["grid_charging_fee_eur_per_mwh"]
    del params_absent["grid_charging_fee_exempt"]
    params_exempt, _ = _arbitrage_setup(fee=25.0, exempt=True)

    res_zero, _ = run_scenario(params_zero, ts, **SOLVER_KW)
    res_absent, _ = run_scenario(params_absent, ts, **SOLVER_KW)
    res_exempt, _ = run_scenario(params_exempt, ts, **SOLVER_KW)

    pd.testing.assert_frame_equal(res_zero, res_absent)
    pd.testing.assert_frame_equal(res_zero, res_exempt)
    assert "expense_grid_charging_fee_eur" not in res_zero.columns

    k_zero = compute_kpis(res_zero, params_zero, verify_balance=False)
    k_exempt = compute_kpis(res_exempt, params_exempt, verify_balance=False)
    assert k_zero["expense_grid_charging_fee_eur"] == 0.0
    assert k_exempt["expense_grid_charging_fee_eur"] == 0.0
    assert k_zero["profit_total_eur"] == k_exempt["profit_total_eur"]


# ---------------------------------------------------------------------------
# 4. Availability derate
# ---------------------------------------------------------------------------


def test_fee_kpi_is_availability_derated():
    derated = apply_unavailability_derate(
        {"expense_grid_charging_fee_eur": 100.0, "profit_total_eur": 1.0},
        10.0,
    )
    assert derated["expense_grid_charging_fee_eur"] == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# 5. Validation + inert warning
# ---------------------------------------------------------------------------


def test_negative_wedge_rejected():
    from pvbess_opt.io import PROJECT_SHEET_DEFAULTS

    typed = {
        "project": dict(
            PROJECT_SHEET_DEFAULTS, grid_charging_fee_eur_per_mwh=-5.0,
        ),
        "pv": {}, "bess": {}, "economics": {}, "simulation": {},
        "balancing": {}, "ppa": {},
    }
    with pytest.raises(ValueError, match="grid_charging_fee_eur_per_mwh"):
        validate_workbook_params(typed)


def test_exempt_is_a_genuine_bool_key():
    from pvbess_opt.io import _BOOL_KEYS, _parse_value

    assert "grid_charging_fee_exempt" in _BOOL_KEYS
    assert _parse_value("grid_charging_fee_exempt", True, False) is True


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_inert_fee_warns_once(caplog):
    import pvbess_opt.optimization as opt_mod

    opt_mod._GRID_FEE_INERT_WARNED = False
    params, ts = _arbitrage_setup(fee=10.0, allow_grid_charging=False)
    with caplog.at_level("WARNING"):
        opt_mod.run_scenario(params, ts, **SOLVER_KW)
        opt_mod.run_scenario(params, ts, **SOLVER_KW)
    hits = [
        r for r in caplog.records
        if "cannot bind" in r.message
    ]
    assert len(hits) == 1  # latched: once per process
    opt_mod._GRID_FEE_INERT_WARNED = False
