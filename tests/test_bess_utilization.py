"""BESS utilisation diagnostics + sanity check on the cycle counter.

This test confirms that, when PV surplus exists during daylight hours
and the BESS is sized to absorb it, the MILP actually cycles the
battery — i.e. the low-cycles observation in the case-study run is
genuinely sizing-driven (case A) rather than a bug in the unit
conversion (case B).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.kpis import compute_kpis
from pvbess_opt.optimization import run_scenario
from tests._pv_helpers import hourly_canonical_pv_window  # noqa: E402


def _surplus_ts(n_hours: int = 168) -> pd.DataFrame:
    """One-week timeseries where load is far below PV during daylight,
    leaving plenty of PV surplus to charge the BESS from."""
    timestamps = pd.date_range("2026-06-01 00:00", periods=n_hours, freq="h")
    pv = hourly_canonical_pv_window(n_hours, pv_nameplate_kwp=500.0)
    h = np.arange(n_hours).astype(float) % 24
    # Flat 20 kW load — well below the ~250 kW PV peak so daytime
    # generation overwhelmingly exceeds load.
    load = np.full(n_hours, 20.0)
    # Cheap night DAM, expensive evening DAM — gives the BESS a clean
    # discharge-to-grid signal once it's full.
    dam = 80.0 + 40.0 * np.sin(np.pi * (h - 18) / 12.0)
    return pd.DataFrame({
        "timestamp": timestamps,
        "pv_kwh": pv,
        "load_kwh": load,
        "dam_price_eur_per_mwh": dam,
    })


def _surplus_params() -> dict:
    return {
        "dt_minutes": 60,
        "efficiency_charge": 0.97,
        "efficiency_discharge": 0.97,
        "soc_min_frac": 0.10,
        "soc_max_frac": 0.95,
        "initial_soc_frac": 0.50,
        "terminal_soc_equal": True,
        "max_cycles_per_day": 1.0,
        "p_grid_export_max_kw": 500.0,
        "pv_nameplate_kwp": 500.0,
        "bess_power_kw": 100.0,
        "bess_capacity_kwh": 200.0,
        "retail_tariff_eur_per_mwh": 200.0,
        "settlement_minutes": 15,
        "mode": "vnb",
        "allow_bess_grid_charging": False,
        "show_titles": False,
    }


@pytest.fixture(scope="module")
def _solved_surplus():
    params = _surplus_params()
    ts = _surplus_ts()
    res, _ = run_scenario(
        params, ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=60,
    )
    return res, params


def test_diagnostics_block_present(_solved_surplus):
    res, params = _solved_surplus
    kpis = compute_kpis(res, params, verify_balance=False)
    diag = kpis.get("bess_utilization_diagnostics")
    assert diag is not None, "bess_utilization_diagnostics missing"
    for key in (
        "bess_charge_pv_surplus_mwh",
        "bess_charge_grid_mwh",
        "bess_discharge_load_mwh",
        "bess_discharge_grid_mwh",
        "bess_capacity_mwh",
        "bess_max_cycles_per_year_theoretical",
        "bess_actual_cycles_year1",
        "bess_utilization_pct",
    ):
        assert key in diag, f"missing diagnostic key {key!r}"


def test_bess_cycles_when_surplus_exists(_solved_surplus):
    """When PV surplus is abundant and the BESS is correctly sized, the
    solver should drive the battery toward its per-day cycle budget —
    confirming the cycle formula is dimensionally correct and that the
    low-cycles observation in the case study is purely a sizing /
    grid-charging-disabled artefact (case A).

    The diagnostics expose ``bess_actual_cycles_year1`` summed across
    the simulated window, so for a 7-day run we expect roughly
    ``max_cycles_per_day × 7`` cycles, not a full year's worth.
    """
    res, params = _solved_surplus
    kpis = compute_kpis(res, params, verify_balance=False)
    diag = kpis["bess_utilization_diagnostics"]
    actual_total_cycles = float(diag["bess_actual_cycles_year1"])
    n_days_simulated = pd.to_datetime(res["timestamp"]).dt.date.nunique()
    expected_window_max = (
        float(params["max_cycles_per_day"]) * float(n_days_simulated)
    )
    assert expected_window_max > 0.0
    # In a sized-correctly week the BESS should cycle near its daily
    # limit; require at least 50 % of the per-day budget over the
    # simulated window.
    assert actual_total_cycles >= 0.5 * expected_window_max, (
        f"BESS under-cycled: actual={actual_total_cycles:.2f}, "
        f"expected ≥ {0.5 * expected_window_max:.2f} over "
        f"{n_days_simulated} simulated days"
    )
