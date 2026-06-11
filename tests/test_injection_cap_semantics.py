"""Analytic end-to-end semantics of the max-injection cap trio.

Hand-solvable mini-scenarios (0 MIP gap pins the unique optimum) that
evaluate the OUTPUT frames and KPIs — not just constraint construction —
for the combined ``max_injection_profile`` and the per-source
``max_injection_profile_pv`` / ``_bess`` sub-caps:

* a binding 50 % hour clips total export to exactly half the cap;
* hour-of-day and calendar-month indexing line up exactly with the
  timestamps (no off-by-one; the monthly column switches at midnight);
* the kW-share -> kWh-per-step conversion is exact at 15-min cadence;
* a PV sub-cap below the available surplus curtails PV while the BESS
  still exports within the combined cap;
* per-source sub-caps that sum above the combined cap never relax it.

Timestamps are tz-naive throughout the model (the uniform 35,040-step
grid carries no DST transitions by design — see
``pvbess_opt.timeutils.apply_fixed_utc_offset``), so hour-of-day
indexing is wall-clock arithmetic with no DST edge.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.kpis import compute_kpis
from pvbess_opt.optimization import run_scenario

SOLVER_KW = {"solver_name": "highs", "mip_gap": 0.0, "time_limit_seconds": 120}


def _params_merchant(**overrides) -> dict:
    """Lossless merchant baseline so every expectation is exact algebra."""
    params = {
        "dt_minutes": 60,
        "efficiency_charge": 1.0,
        "efficiency_discharge": 1.0,
        "soc_min_frac": 0.0,
        "soc_max_frac": 1.0,
        "initial_soc_frac": 0.5,
        "terminal_soc_equal": False,
        "max_cycles_per_day": 10.0,
        "p_grid_export_max_kw": 10.0,
        "pv_nameplate_kwp": 100.0,
        "bess_power_kw": 0.0,
        "bess_capacity_kwh": 0.0,
        "retail_tariff_eur_per_mwh": 0.0,
        "settlement_minutes": 60,
        "mode": "merchant",
        "allow_bess_grid_charging": False,
        "show_titles": False,
    }
    params.update(overrides)
    return params


def _ts_hours(pv, dam=100.0, start="2026-06-01") -> pd.DataFrame:
    n = len(pv)
    dam_col = [float(dam)] * n if np.isscalar(dam) else [float(x) for x in dam]
    return pd.DataFrame({
        "timestamp": pd.date_range(start, periods=n, freq="h"),
        "pv_kwh": [float(x) for x in pv],
        "load_kwh": [0.0] * n,
        "dam_price_eur_per_mwh": dam_col,
    })


def test_binding_half_share_hour_clips_export_to_half_cap():
    """profile[12] = 50 % => export at hour 12 is exactly cap/2; the rest
    of the surplus is curtailed, and the KPI matches the frame."""
    pv = [0.0] * 24
    pv[12] = 20.0  # far above the cap
    profile = np.full(24, 100.0)
    profile[12] = 50.0
    params = _params_merchant()
    params["max_injection_profile"] = profile
    res, _ = run_scenario(params, _ts_hours(pv), **SOLVER_KW)
    assert res.loc[12, "grid_export_total_kwh"] == pytest.approx(5.0, abs=1e-6)
    assert res.loc[12, "pv_curtail_kwh"] == pytest.approx(15.0, abs=1e-6)
    kpis = compute_kpis(res, params, verify_balance=False)
    assert kpis["pv_energy_curtailed_mwh"] * 1000.0 == pytest.approx(
        float(res["pv_curtail_kwh"].sum()), abs=1e-3,
    )


def test_hourly_indexing_is_exact_against_timestamps():
    """A unique share per hour, saturating PV: the cap column and the
    binding export both equal profile[hour-of-day] — any off-by-one
    between the profile row and the timestamp hour fails here."""
    pv = [50.0] * 24
    profile = np.linspace(10.0, 56.0, 24)  # unique value per hour
    params = _params_merchant()
    params["max_injection_profile"] = profile
    res, _ = run_scenario(params, _ts_hours(pv), **SOLVER_KW)
    hours = pd.to_datetime(res["timestamp"]).dt.hour.to_numpy()
    expected = 10.0 * profile[hours] / 100.0
    np.testing.assert_allclose(
        res["grid_export_cap_kwh"].to_numpy(), expected, atol=1e-9,
    )
    np.testing.assert_allclose(
        res["grid_export_total_kwh"].to_numpy(), expected, atol=1e-6,
    )


def test_kw_share_to_kwh_per_step_at_15min_cadence():
    """cap_kwh per step = p_grid_export_max_kw x 0.25 h x share."""
    n = 96
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=n, freq="15min"),
        "pv_kwh": [50.0] * n,
        "load_kwh": [0.0] * n,
        "dam_price_eur_per_mwh": [100.0] * n,
    })
    params = _params_merchant(dt_minutes=15)
    params["max_injection_profile"] = np.full(24, 40.0)
    res, _ = run_scenario(params, ts, **SOLVER_KW)
    # 10 kW x 0.25 h x 0.40 = 1.0 kWh per step.
    np.testing.assert_allclose(res["grid_export_cap_kwh"].to_numpy(), 1.0)
    np.testing.assert_allclose(
        res["grid_export_total_kwh"].to_numpy(), 1.0, atol=1e-6,
    )


def test_monthly_columns_switch_exactly_at_month_boundary():
    """(24, 12) profile spanning Jun 30 -> Jul 1: the cap follows the
    calendar month of each timestamp, switching at midnight."""
    n = 48
    ts = _ts_hours([50.0] * n, start="2026-06-30")
    profile = np.full((24, 12), 100.0)
    profile[:, 5] = 50.0   # June
    profile[:, 6] = 25.0   # July
    params = _params_merchant()
    params["max_injection_profile"] = profile
    res, _ = run_scenario(params, ts, **SOLVER_KW)
    months = pd.to_datetime(res["timestamp"]).dt.month.to_numpy()
    cap = res["grid_export_cap_kwh"].to_numpy()
    np.testing.assert_allclose(cap[months == 6], 5.0)
    np.testing.assert_allclose(cap[months == 7], 2.5)
    np.testing.assert_allclose(
        res["grid_export_total_kwh"].to_numpy()[months == 7], 2.5, atol=1e-6,
    )


def test_pv_sub_cap_binds_while_bess_fills_the_remaining_total():
    """PV sub-cap 40 % with a price peak at hour 12: PV export pins to
    its sub-cap, the (full, 6 kWh) BESS fills the combined cap, and the
    PV above the sub-cap is curtailed (the BESS is discharging, so it
    cannot simultaneously absorb the surplus — MODE_LINK)."""
    pv = [0.0] * 24
    pv[12] = 20.0
    dam = [1.0] * 24
    dam[12] = 100.0
    params = _params_merchant(
        bess_power_kw=10.0, bess_capacity_kwh=6.0, initial_soc_frac=1.0,
    )
    mi_pv = np.full(24, 100.0)
    mi_pv[12] = 40.0  # cap_pv = 4 kWh at hour 12
    params["max_injection_profile_pv"] = mi_pv
    res, _ = run_scenario(params, _ts_hours(pv, dam=dam), **SOLVER_KW)
    assert res.loc[12, "pv_to_grid_kwh"] == pytest.approx(4.0, abs=1e-6)
    assert res.loc[12, "bess_dis_grid_kwh"] == pytest.approx(6.0, abs=1e-6)
    assert res.loc[12, "grid_export_total_kwh"] == pytest.approx(10.0, abs=1e-6)
    assert res.loc[12, "pv_curtail_kwh"] == pytest.approx(16.0, abs=1e-6)


def test_sub_caps_summing_above_total_never_relax_the_combined_cap():
    """PV and BESS sub-caps of 80 % each (sum 160 %) on a 10 kWh total:
    the combined cap still binds at 10, with each source within its own
    sub-cap."""
    pv = [0.0] * 24
    pv[12] = 20.0
    dam = [1.0] * 24
    dam[12] = 100.0
    params = _params_merchant(
        bess_power_kw=10.0, bess_capacity_kwh=8.0, initial_soc_frac=1.0,
    )
    params["max_injection_profile_pv"] = np.full(24, 80.0)
    params["max_injection_profile_bess"] = np.full(24, 80.0)
    res, _ = run_scenario(params, _ts_hours(pv, dam=dam), **SOLVER_KW)
    assert res.loc[12, "grid_export_total_kwh"] == pytest.approx(10.0, abs=1e-6)
    assert res.loc[12, "pv_to_grid_kwh"] <= 8.0 + 1e-6
    assert res.loc[12, "bess_dis_grid_kwh"] <= 8.0 + 1e-6
