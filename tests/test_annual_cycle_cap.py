"""Annual throughput / cycle cap with warranty basis (Eqs. E46/E47).

`max_cycles_per_year` adds one year-long Year-1 MILP constraint
(CYC_ANNUAL); the projected years are checked analytically on the
chosen `cycle_cap_basis` and reported in the degradation sheet.
Locked: zero-default bit-identity, the binding-cap analytic case, the
daily/annual coexistence, the Year-1 basis invariance with the
E47-predicted report split, the replacement-reset warning path and
validation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.degradation import build_degradation_report
from pvbess_opt.lifetime import (
    bess_capacity_factors,
    warranty_cycle_utilisation,
)
from pvbess_opt.optimization import build_model, run_scenario


def _highs_available() -> bool:
    try:
        import highspy
    except ImportError:
        return False
    return bool(highspy)


pytestmark = pytest.mark.skipif(
    not _highs_available(), reason="requires HiGHS",
)


def _params(**o) -> dict:
    p = {
        "dt_minutes": 60,
        "mode": "merchant",
        "pv_nameplate_kwp": 0.0,
        "bess_power_kw": 1000.0,
        "bess_capacity_kwh": 2000.0,
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


def _ts(n_days: int = 3) -> pd.DataFrame:
    """Repeating daily valley/peak pattern: arbitrage wants one full
    cycle per day."""
    n = 24 * n_days
    hours = np.arange(n) % 24
    price = np.where(hours < 8, 10.0, np.where(hours < 16, 60.0, 200.0))
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "pv_kwh": np.zeros(n),
        "dam_price_eur_per_mwh": price.astype(float),
    })


def test_zero_default_is_bit_identical():
    ts = _ts()
    res_absent, _s, _f = run_scenario(
        _params(), ts, return_unrounded=True,
    )
    res_zero, _s2, _f2 = run_scenario(
        _params(max_cycles_per_year=0.0), ts, return_unrounded=True,
    )
    pd.testing.assert_frame_equal(res_absent, res_zero)
    m = build_model(_params(max_cycles_per_year=0.0), _ts())
    assert not hasattr(m, "CYC_ANNUAL")


def test_cap_binds():
    """Uncapped arbitrage discharges ~1 full cycle per day; a cap of
    1.5 cycles over the 3-day window must bind."""
    ts = _ts(n_days=3)
    _r, _s, free = run_scenario(_params(), ts, return_unrounded=True)
    discharge_free = float(
        (free["bess_dis_load_kwh"] + free["bess_dis_grid_kwh"]).sum()
    )
    assert discharge_free > 1.5 * 2000.0  # the cap will cut this
    capped_params = _params(max_cycles_per_year=1.5)
    _r2, _s2, capped = run_scenario(
        capped_params, ts, return_unrounded=True,
    )
    discharge_capped = float(
        (capped["bess_dis_load_kwh"] + capped["bess_dis_grid_kwh"]).sum()
    )
    assert discharge_capped <= 1.5 * 2000.0 + 1e-4
    assert discharge_capped < discharge_free


def test_daily_and_annual_coexist():
    ts = _ts(n_days=3)
    p = _params(max_cycles_per_day=0.5, max_cycles_per_year=2.0)
    _r, _s, full = run_scenario(p, ts, return_unrounded=True)
    discharge = (
        full["bess_dis_load_kwh"] + full["bess_dis_grid_kwh"]
    ).to_numpy(dtype=float)
    total = float(discharge.sum())
    assert total <= 2.0 * 2000.0 + 1e-4  # annual cap
    per_day = discharge.reshape(3, 24).sum(axis=1)
    assert float(per_day.max()) <= 0.5 * 2000.0 + 1e-4  # daily cap


def test_basis_switch_year1_invariant():
    """Year-1 dispatch identical for nameplate vs faded; the projected
    report splits exactly as Eq. E47 predicts."""
    ts = _ts()
    res_a, _s, _f = run_scenario(
        _params(max_cycles_per_year=1.5, cycle_cap_basis="nameplate"),
        ts, return_unrounded=True,
    )
    res_b, _s2, _f2 = run_scenario(
        _params(max_cycles_per_year=1.5, cycle_cap_basis="faded"),
        ts, return_unrounded=True,
    )
    pd.testing.assert_frame_equal(res_a, res_b)

    factors = bess_capacity_factors(
        10, d_bess_annual=0.02, year1_discharge_mwh=500.0,
        capacity_mwh=2.0,
    )
    cyc_nameplate, _ = warranty_cycle_utilisation(
        10, year1_discharge_mwh=500.0, capacity_mwh=2.0,
        factors=factors, basis="nameplate",
    )
    cyc_faded, _ = warranty_cycle_utilisation(
        10, year1_discharge_mwh=500.0, capacity_mwh=2.0,
        factors=factors, basis="faded",
    )
    # Faded: constant D1/E_N; nameplate: rides f_y (maximal Year 1).
    assert cyc_faded == pytest.approx([500.0 / 2.0] * 10)
    assert cyc_nameplate == pytest.approx(
        [500.0 * f / 2.0 for f in factors],
    )
    assert cyc_nameplate[0] == max(cyc_nameplate)


def test_degradation_report_columns_only_when_cap_set():
    soc = pd.Series(np.linspace(0.0, 1800.0, 48))
    base = build_degradation_report(
        soc, capacity_kwh=2000.0, soc_min_frac=0.0, soc_max_frac=1.0,
        degradation_pct_per_cycle=0.005, project_years=10,
        start_year=2026, degradation_annual_pct=2.0,
        year1_discharge_mwh=500.0,
    )
    assert "warranty_utilisation_pct" not in base.columns
    capped = build_degradation_report(
        soc, capacity_kwh=2000.0, soc_min_frac=0.0, soc_max_frac=1.0,
        degradation_pct_per_cycle=0.005, project_years=10,
        start_year=2026, degradation_annual_pct=2.0,
        year1_discharge_mwh=500.0,
        max_cycles_per_year=300.0, cycle_cap_basis="nameplate",
    )
    assert {"cycles_on_basis", "warranty_utilisation_pct"} <= set(
        capped.columns,
    )
    # Year 1: 500 MWh / 2 MWh = 250 cycles => 83.33 % of the 300 cap.
    assert capped["cycles_on_basis"].iloc[0] == pytest.approx(250.0)
    assert capped["warranty_utilisation_pct"].iloc[0] == pytest.approx(
        100.0 * 250.0 / 300.0, abs=1e-3,
    )
    # Nameplate utilisation is maximal in Year 1 (no replacement).
    assert capped["warranty_utilisation_pct"].iloc[0] == pytest.approx(
        float(capped["warranty_utilisation_pct"].max()),
    )


def test_replacement_reset_exceedance():
    """A replacement reset pushes the nameplate-basis utilisation back
    to the Year-1 level — with a cap sized between the faded Year-8
    ratio and the reset ratio the exceeds mask fires exactly there."""
    factors = bess_capacity_factors(
        10, d_bess_annual=5.0 / 100.0, year1_discharge_mwh=600.0,
        capacity_mwh=2.0, replacement_year=8,
    )
    cap = 299.0  # Year-1 ratio is 300 cycles; later years fade below
    cycles, exceeds = warranty_cycle_utilisation(
        10, year1_discharge_mwh=600.0, capacity_mwh=2.0,
        factors=factors, basis="nameplate", max_cycles_per_year=cap,
    )
    assert exceeds[0] is True or cycles[0] > cap
    # The reset year jumps back to the Year-1 ratio: flagged again
    # after years 2..7 faded below the cap.
    assert exceeds[7]
    assert not any(exceeds[1:7])


def test_validation_rejects_negative_and_bad_basis(tmp_path):
    from pvbess_opt.io import (
        BALANCING_SHEET_DEFAULTS,
        BESS_SHEET_DEFAULTS,
        PROJECT_SHEET_DEFAULTS,
        PV_SHEET_DEFAULTS,
        validate_workbook_params,
    )

    def _typed(**bess_overrides):
        return {
            "project": dict(PROJECT_SHEET_DEFAULTS, mode="merchant"),
            "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=0.0),
            "bess": dict(
                BESS_SHEET_DEFAULTS, bess_power_kw=100.0,
                bess_capacity_kwh=200.0, **bess_overrides,
            ),
            "economics": {},
            "simulation": {},
            "balancing": dict(BALANCING_SHEET_DEFAULTS),
        }

    with pytest.raises(ValueError, match="max_cycles_per_year"):
        validate_workbook_params(
            _typed(max_cycles_per_year=-5.0), dt_minutes=60,
        )
    from pvbess_opt.io import _parse_value

    with pytest.raises(ValueError, match="cycle_cap_basis"):
        _parse_value("cycle_cap_basis", "shiny", "nameplate")


def test_daily_tighter_than_annual_warns(caplog):
    import logging

    from pvbess_opt.io import (
        BALANCING_SHEET_DEFAULTS,
        BESS_SHEET_DEFAULTS,
        PROJECT_SHEET_DEFAULTS,
        PV_SHEET_DEFAULTS,
        validate_workbook_params,
    )

    typed = {
        "project": dict(PROJECT_SHEET_DEFAULTS, mode="merchant"),
        "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=0.0),
        "bess": dict(
            BESS_SHEET_DEFAULTS, bess_power_kw=100.0,
            bess_capacity_kwh=200.0,
            max_cycles_per_day=1.0, max_cycles_per_year=400.0,
        ),
        "economics": {},
        "simulation": {},
        "balancing": dict(BALANCING_SHEET_DEFAULTS),
    }
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io"):
        validate_workbook_params(typed, dt_minutes=60)
    assert any(
        "annual cap will never bind" in r.getMessage()
        for r in caplog.records
    )
