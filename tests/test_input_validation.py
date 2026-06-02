"""Workbook input validation must reject out-of-range values upfront.

The previous loader silently clamped negatives in
``derive_asset_capacities`` and skipped most range checks, so a
workbook with ``bess_power_kw=-100``, ``efficiency_charge=1.5`` and
similar nonsense loaded without complaint and produced garbage outputs.
``validate_workbook_params`` now rejects every such input at the first
failure with a per-key error message.
"""

from __future__ import annotations

import pytest

from pvbess_opt.io import validate_workbook_params


def _base_typed() -> dict:
    """Minimal valid typed dict for the validator to chew on."""
    return {
        "project": {
            "mode": "self_consumption",
        },
        "pv": {
            "pv_nameplate_kwp": 500.0,
        },
        "bess": {
            "bess_power_kw": 1000.0,
            "bess_capacity_kwh": 4000.0,
            "soc_min_frac": 0.1,
            "soc_max_frac": 0.95,
            "initial_soc_frac": 0.5,
            "efficiency_charge": 0.97,
            "efficiency_discharge": 0.97,
            "max_cycles_per_day": 1.0,
        },
        "economics": {
            "capex_pv_eur_per_kw": 525.0,
            "capex_bess_eur_per_kw": 200.0,
            "opex_pv_eur_per_kwp": 7.0,
            "opex_bess_eur_per_kw": 14.0,
        },
        "simulation": {},
        "balancing": {"balancing_enabled": False},
    }


def _assert_raises_containing(typed: dict, *, dt_minutes: int | None = 60,
                              fragment: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        validate_workbook_params(typed, dt_minutes=dt_minutes)
    assert fragment in str(exc_info.value), (
        f"Expected error to mention {fragment!r}; got {exc_info.value!s}"
    )


def test_valid_typed_dict_passes():
    validate_workbook_params(_base_typed(), dt_minutes=15)


def test_negative_bess_power_rejected():
    t = _base_typed()
    t["bess"]["bess_power_kw"] = -100.0
    _assert_raises_containing(t, fragment="bess_power_kw")


def test_negative_bess_capacity_rejected():
    t = _base_typed()
    t["bess"]["bess_capacity_kwh"] = -50.0
    _assert_raises_containing(t, fragment="bess_capacity_kwh")


def test_negative_pv_nameplate_rejected():
    t = _base_typed()
    t["pv"]["pv_nameplate_kwp"] = -10.0
    _assert_raises_containing(t, fragment="pv_nameplate_kwp")


def test_negative_capex_pv_rejected():
    t = _base_typed()
    t["economics"]["capex_pv_eur_per_kw"] = -50.0
    _assert_raises_containing(t, fragment="capex_pv_eur_per_kw")


def test_negative_capex_bess_rejected():
    t = _base_typed()
    t["economics"]["capex_bess_eur_per_kw"] = -50.0
    _assert_raises_containing(t, fragment="capex_bess_eur_per_kw")


def test_negative_opex_pv_rejected():
    t = _base_typed()
    t["economics"]["opex_pv_eur_per_kwp"] = -1.0
    _assert_raises_containing(t, fragment="opex_pv_eur_per_kwp")


def test_negative_opex_bess_rejected():
    t = _base_typed()
    t["economics"]["opex_bess_eur_per_kw"] = -1.0
    _assert_raises_containing(t, fragment="opex_bess_eur_per_kw")


def test_soc_min_out_of_range_low():
    t = _base_typed()
    t["bess"]["soc_min_frac"] = -0.1
    _assert_raises_containing(t, fragment="soc_min_frac")


def test_soc_min_out_of_range_high():
    t = _base_typed()
    t["bess"]["soc_min_frac"] = 1.2
    _assert_raises_containing(t, fragment="soc_min_frac")


def test_soc_max_out_of_range_high():
    t = _base_typed()
    t["bess"]["soc_max_frac"] = 1.2
    _assert_raises_containing(t, fragment="soc_max_frac")


def test_soc_min_greater_than_max_rejected():
    t = _base_typed()
    t["bess"]["soc_min_frac"] = 0.9
    t["bess"]["soc_max_frac"] = 0.5
    t["bess"]["initial_soc_frac"] = 0.7
    _assert_raises_containing(t, fragment="soc_min_frac")


def test_initial_soc_outside_bounds_rejected():
    t = _base_typed()
    t["bess"]["initial_soc_frac"] = -0.1
    _assert_raises_containing(t, fragment="initial_soc_frac")


def test_initial_soc_above_max_rejected():
    t = _base_typed()
    t["bess"]["soc_max_frac"] = 0.8
    t["bess"]["initial_soc_frac"] = 0.95
    _assert_raises_containing(t, fragment="initial_soc_frac")


def test_efficiency_charge_zero_rejected():
    t = _base_typed()
    t["bess"]["efficiency_charge"] = 0.0
    _assert_raises_containing(t, fragment="efficiency_charge")


def test_efficiency_charge_above_one_rejected():
    t = _base_typed()
    t["bess"]["efficiency_charge"] = 1.5
    _assert_raises_containing(t, fragment="efficiency_charge")


def test_efficiency_discharge_negative_rejected():
    t = _base_typed()
    t["bess"]["efficiency_discharge"] = -0.5
    _assert_raises_containing(t, fragment="efficiency_discharge")


def test_max_cycles_per_day_negative_rejected():
    t = _base_typed()
    t["bess"]["max_cycles_per_day"] = -1.0
    _assert_raises_containing(t, fragment="max_cycles_per_day")


def test_dt_minutes_must_divide_60():
    t = _base_typed()
    _assert_raises_containing(t, dt_minutes=7, fragment="dt_minutes")


def test_dt_minutes_zero_rejected():
    t = _base_typed()
    _assert_raises_containing(t, dt_minutes=0, fragment="dt_minutes")


def test_clamp_in_derive_asset_capacities():
    """``derive_asset_capacities`` clamps negative capacities to zero as
    defense-in-depth.  The validator rejects negative inputs upstream,
    but a hand-built params dict that bypasses validation must not
    propagate a negative capacity into the EUR/kW math."""
    import pandas as pd

    from pvbess_opt.economics import derive_asset_capacities

    caps = derive_asset_capacities(
        {}, {"pv_nameplate_kwp": -100.0, "bess_power_kw": -50.0,
              "bess_capacity_kwh": -200.0},
        pd.DataFrame(),
    )
    # Negative values are clamped to zero.
    assert caps["pv_kwp"] == 0.0
    assert caps["bess_kw"] == 0.0
    assert caps["bess_kwh"] == 0.0


def test_balancing_probability_validation_runs():
    """When balancing is enabled, the existing balancing validator
    still runs and rejects out-of-range probabilities."""
    t = _base_typed()
    t["balancing"] = {
        "balancing_enabled": True,
        "fcr_bid_acceptance_pct": -10.0,
        "fcr_required_duration_hours": 1.0,
        "bm_settlement_minutes": 15,
    }
    with pytest.raises(ValueError):
        validate_workbook_params(t, dt_minutes=15)
