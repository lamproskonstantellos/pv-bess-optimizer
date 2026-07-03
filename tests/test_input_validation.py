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
    """Minimal valid typed dict for the validator to chew on.

    Cost keys are placed on the SAME sheets ``_SHEET_DEFAULTS`` uses (the
    PV/BESS cost block on ``pv`` / ``bess``, the site lump sums on
    ``project``).  An earlier revision of this fixture parked them all on
    ``economics`` — a placement that never occurs in a real workbook — so
    the "negative cost rejected" tests passed against a section the
    validator was (incorrectly) reading, masking the fact that the real
    pv/bess-sheet values were never checked.
    """
    return {
        "project": {
            "mode": "self_consumption",
            "site_capex_eur": 0.0,
            "site_devex_eur": 0.0,
        },
        "pv": {
            "pv_nameplate_kwp": 500.0,
            "capex_pv_eur_per_kw": 525.0,
            "devex_pv_eur_per_kw": 60.0,
            "opex_pv_eur_per_kwp": 7.0,
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
            "capex_bess_eur_per_kwh": 200.0,
            "devex_bess_eur_per_kw": 30.0,
            "opex_bess_eur_per_kw": 14.0,
            "bess_replacement_cost_pct": 50.0,
        },
        "economics": {
            "gearing_pct": 0.0,
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
    t["pv"]["capex_pv_eur_per_kw"] = -50.0
    _assert_raises_containing(t, fragment="capex_pv_eur_per_kw")


def test_negative_capex_bess_rejected():
    t = _base_typed()
    t["bess"]["capex_bess_eur_per_kwh"] = -50.0
    _assert_raises_containing(t, fragment="capex_bess_eur_per_kwh")


def test_negative_opex_pv_rejected():
    t = _base_typed()
    t["pv"]["opex_pv_eur_per_kwp"] = -1.0
    _assert_raises_containing(t, fragment="opex_pv_eur_per_kwp")


def test_negative_opex_bess_rejected():
    t = _base_typed()
    t["bess"]["opex_bess_eur_per_kw"] = -1.0
    _assert_raises_containing(t, fragment="opex_bess_eur_per_kw")


def test_negative_devex_pv_rejected():
    t = _base_typed()
    t["pv"]["devex_pv_eur_per_kw"] = -1.0
    _assert_raises_containing(t, fragment="devex_pv_eur_per_kw")


def test_negative_devex_bess_rejected():
    t = _base_typed()
    t["bess"]["devex_bess_eur_per_kw"] = -1.0
    _assert_raises_containing(t, fragment="devex_bess_eur_per_kw")


def test_negative_site_capex_rejected():
    t = _base_typed()
    t["project"]["site_capex_eur"] = -1000.0
    _assert_raises_containing(t, fragment="site_capex_eur")


def test_negative_site_devex_rejected():
    t = _base_typed()
    t["project"]["site_devex_eur"] = -500.0
    _assert_raises_containing(t, fragment="site_devex_eur")


def test_gearing_above_100_rejected():
    t = _base_typed()
    t["economics"]["gearing_pct"] = 150.0
    _assert_raises_containing(t, fragment="gearing_pct")


def test_gearing_negative_rejected():
    t = _base_typed()
    t["economics"]["gearing_pct"] = -10.0
    _assert_raises_containing(t, fragment="gearing_pct")


def test_cost_keys_validated_on_real_workbook_sections():
    """Regression for the section-placement bug: a negative CAPEX on the
    REAL pv/bess sheets (as produced by ``read_workbook``) must be
    rejected.  The previous validator read these keys from ``economics``
    where they never live, so the check silently no-opped."""
    from pvbess_opt.io import _SHEET_DEFAULTS

    typed = {sheet: dict(defaults) for sheet, defaults in _SHEET_DEFAULTS.items()}
    typed["pv"]["capex_pv_eur_per_kw"] = -525.0
    _assert_raises_containing(typed, dt_minutes=15, fragment="capex_pv_eur_per_kw")


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


def test_negative_pv_degradation_year1_rejected():
    t = _base_typed()
    t["pv"]["pv_degradation_year1_pct"] = -2.0
    _assert_raises_containing(t, fragment="pv_degradation_year1_pct")


def test_negative_pv_degradation_annual_rejected():
    t = _base_typed()
    t["pv"]["pv_degradation_annual_pct"] = -0.5
    _assert_raises_containing(t, fragment="pv_degradation_annual_pct")


def test_negative_bess_degradation_annual_rejected():
    t = _base_typed()
    t["bess"]["bess_degradation_annual_pct"] = -2.0
    _assert_raises_containing(t, fragment="bess_degradation_annual_pct")


def test_negative_bess_cycle_fade_rejected():
    t = _base_typed()
    t["bess"]["bess_degradation_pct_per_cycle"] = -0.01
    _assert_raises_containing(t, fragment="bess_degradation_pct_per_cycle")


def test_negative_replacement_year_rejected():
    t = _base_typed()
    t["bess"]["bess_replacement_year"] = -1
    _assert_raises_containing(t, fragment="bess_replacement_year")


def test_co2_decline_above_100_rejected():
    """A grid-CO2 annual decline above 100 % would flip the projection
    factor (1-decline)^y negative and invert the avoided-emissions sign."""
    t = _base_typed()
    t["economics"]["grid_co2_annual_decline_pct"] = 150.0
    _assert_raises_containing(t, fragment="grid_co2_annual_decline_pct")


def test_negative_co2_intensity_rejected():
    t = _base_typed()
    t["economics"]["grid_co2_intensity_kg_per_mwh"] = -100.0
    _assert_raises_containing(t, fragment="grid_co2_intensity_kg_per_mwh")


def test_invalid_ppa_settlement_rejected():
    """A PPA contract with an unknown settlement must be rejected: the KPI
    engine branches on ``== 'physical'`` while the cashflow branches on
    ``== 'cfd'``, so an unrecognised value would decompose the revenue
    inconsistently between them."""
    t = _base_typed()
    t["ppa"] = {
        "ppa_enabled": True,
        "ppa_structure": "pay_as_produced",
        "ppa_settlement": "sleeved",
        "ppa_price_eur_per_mwh": 65.0,
        "ppa_volume_share_pct": 50.0,
        "ppa_term_years": 10,
    }
    _assert_raises_containing(t, dt_minutes=15, fragment="ppa_settlement")


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
