"""Runtime verification of INV-B1..INV-B6 by ``verify_dispatch_invariants``.

The balancing invariants were previously only enforced in
``tests/test_balancing_invariants.py``.  ``run --strict`` outside the
pytest harness could pass with a balancing-side violation; these tests
lock the runtime check.

Each test constructs a deliberately invariant-violating result frame
(or, for INV-B6, runs on a balancing-OFF scenario) and asserts the
verifier reports the corresponding key above tolerance.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.balancing import PRODUCTS_ALL
from pvbess_opt.io import BALANCING_SHEET_DEFAULTS
from pvbess_opt.optimization import (
    BALANCING_INVARIANT_KEYS,
    run_scenario,
    verify_dispatch_invariants,
)


def _balancing_on(params: dict, **overrides) -> dict:
    out = dict(params)
    bm = dict(BALANCING_SHEET_DEFAULTS, balancing_enabled=True)
    bm["bm_settlement_minutes"] = int(out.get("dt_minutes", 60))
    bm.update(overrides)
    out["balancing"] = bm
    return out


def test_verifier_emits_every_balancing_invariant_key():
    """All six INV-B keys appear in the verifier's return dict."""
    n = 24
    res = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=n, freq="h"),
            "pv_kwh": np.zeros(n),
            "load_kwh": np.zeros(n),
            "pv_to_load_kwh": np.zeros(n),
            "pv_to_bess_kwh": np.zeros(n),
            "pv_to_grid_kwh": np.zeros(n),
            "pv_curtail_kwh": np.zeros(n),
            "bess_charge_grid_kwh": np.zeros(n),
            "bess_dis_load_kwh": np.zeros(n),
            "bess_dis_grid_kwh": np.zeros(n),
            "grid_to_load_kwh": np.zeros(n),
            "grid_export_total_kwh": np.zeros(n),
            "soc_kwh": np.full(n, 10_000.0),
            "soc_pct": np.full(n, 50.0),
        }
    )
    params = {
        "dt_minutes": 60,
        "efficiency_charge": 0.95,
        "efficiency_discharge": 0.95,
        "soc_min_frac": 0.20,
        "soc_max_frac": 0.95,
        "initial_soc_frac": 0.50,
        "bess_capacity_kwh": 20_000.0,
        "bess_power_kw": 5_000.0,
        "mode": "merchant",
        "terminal_soc_equal": True,
    }
    inv = verify_dispatch_invariants(res, params, mode="merchant")
    for key in BALANCING_INVARIANT_KEYS:
        assert key in inv, f"verifier missed {key}"


def test_invb6_off_run_surfaces_general_invariant_violation(
    short_params, short_ts,
):
    """When balancing is OFF and a general invariant is violated, B6
    bubbles the worst general residual up."""
    res, _ = run_scenario(short_params, short_ts)
    # Drop a chunk of pv_to_load to inject an INV-1 / INV-2 violation.
    res = res.copy()
    res.loc[res.index[:5], "pv_to_load_kwh"] = (
        res.loc[res.index[:5], "pv_to_load_kwh"] - 5.0
    )
    inv = verify_dispatch_invariants(res, short_params)
    # The general invariants must register the violation.
    assert inv["invariant_1_pv_balance_kwh"] > 1.0
    # And INV-B6 must surface a residual at least as large as the worst
    # general residual (since balancing is off in short_params).
    general_max = max(
        v for k, v in inv.items()
        if not k.startswith("invariant_b") and k != "invariant_5_no_sim_grid_io_max_product_kwh2"
    )
    assert inv["invariant_b6_off_invariants_max_residual"] >= general_max - 1e-9


def test_invb2_share_cap_violation_detected(short_params, short_ts):
    """Reservation above the per-product share cap is reported by B2."""
    p_on = _balancing_on(short_params)
    res, _ = run_scenario(p_on, short_ts)
    res = res.copy()
    # Overshoot the FCR share by 1 % of bess_power_kw (50 kW).
    overshoot_kw = 0.50 * float(p_on["bess_power_kw"])
    res.loc[res.index[:5], "bm_reservation_fcr_kw"] = (
        res.loc[res.index[:5], "bm_reservation_fcr_kw"] + overshoot_kw
    )
    inv = verify_dispatch_invariants(res, p_on)
    assert (
        inv["invariant_b2_reservation_share_cap_excess_kw"] > 1.0
    ), inv["invariant_b2_reservation_share_cap_excess_kw"]


def test_invb3_soc_headroom_up_violation_detected(short_params, short_ts):
    """Zeroed SOC margin trips the headroom-UP invariant."""
    p_on = _balancing_on(short_params)
    res, _ = run_scenario(p_on, short_ts)
    res = res.copy()
    # Force SOC to the floor so the up-headroom requirement can't fit.
    soc_min = float(p_on["soc_min_frac"]) * float(p_on["bess_capacity_kwh"])
    res["soc_kwh"] = soc_min
    inv = verify_dispatch_invariants(res, p_on)
    # When all reservations are non-zero in at least one step the
    # required headroom is positive, so the residual must exceed tol.
    assert inv["invariant_b3_soc_headroom_up_excess_kwh"] > 0.0


def test_invb4_soc_headroom_dn_violation_detected(short_params, short_ts):
    """Saturated SOC trips the headroom-DN invariant."""
    p_on = _balancing_on(short_params)
    res, _ = run_scenario(p_on, short_ts)
    res = res.copy()
    soc_max = float(p_on["soc_max_frac"]) * float(p_on["bess_capacity_kwh"])
    res["soc_kwh"] = soc_max
    inv = verify_dispatch_invariants(res, p_on)
    assert inv["invariant_b4_soc_headroom_dn_excess_kwh"] > 0.0


def test_invb5_power_budget_violation_detected(short_params, short_ts):
    """Force per-direction power budget overshoot and confirm B5 fires."""
    p_on = _balancing_on(short_params)
    res, _ = run_scenario(p_on, short_ts)
    res = res.copy()
    dt_h = float(p_on["dt_minutes"]) / 60.0
    p_bess = float(p_on["bess_power_kw"])
    # Inject excess discharge that overshoots p_bess * dt_h.
    res.loc[res.index[:3], "bess_dis_grid_kwh"] = (
        res.loc[res.index[:3], "bess_dis_grid_kwh"] + p_bess * dt_h
    )
    inv = verify_dispatch_invariants(res, p_on)
    assert inv["invariant_b5_power_budget_excess_kwh"] > 1.0


def test_clean_balancing_run_satisfies_all_invariants(short_params, short_ts):
    """A clean balancing-on dispatch from ``run_scenario`` passes B1..B5."""
    p_on = _balancing_on(short_params)
    res, _ = run_scenario(p_on, short_ts)
    inv = verify_dispatch_invariants(res, p_on)
    tol = 1e-3
    assert inv["invariant_b1_capacity_share_sum_pct_excess"] <= 0.5
    assert inv["invariant_b2_reservation_share_cap_excess_kw"] <= tol
    assert inv["invariant_b3_soc_headroom_up_excess_kwh"] <= tol
    assert inv["invariant_b4_soc_headroom_dn_excess_kwh"] <= tol
    assert inv["invariant_b5_power_budget_excess_kwh"] <= tol


def test_balancing_invariants_present_for_off_scenario(short_params, short_ts):
    """Even on a balancing-OFF run, all B1..B6 keys are emitted."""
    res, _ = run_scenario(short_params, short_ts)
    inv = verify_dispatch_invariants(res, short_params)
    for key in BALANCING_INVARIANT_KEYS:
        assert key in inv
        assert isinstance(inv[key], float)


def test_existing_general_invariant_keys_still_present(short_params, short_ts):
    """Adding INV-B keys must not drop any of the original nine keys."""
    res, _ = run_scenario(short_params, short_ts)
    inv = verify_dispatch_invariants(res, short_params)
    for i in range(1, 10):
        suffix = {
            1: "_pv_balance_kwh",
            2: "_load_balance_kwh",
            3: "_soc_dynamics_kwh",
            4: "_rte_bound_excess_kwh",
            5: "_no_sim_grid_io_max_product_kwh2",
            6: "_load_priority_violations",
            7: "_curtail_behavior_kwh",
            8: "_soc_closed_cycle_kwh",
            9: "_pv_load_priority_kwh",
        }[i]
        assert f"invariant_{i}{suffix}" in inv


# A couple of additional fixtures: short_params and short_ts must exist
# in conftest; pull them in via the standard fixture mechanism above.
# The skip below is a safety net for environments without HiGHS.
def _solver_available() -> bool:
    try:
        from pyomo.opt import SolverFactory
        return bool(SolverFactory("highs").available(exception_flag=False))
    except Exception:  # pragma: no cover - environment guard
        return False


pytestmark = pytest.mark.skipif(
    not _solver_available(),
    reason="HiGHS solver not available",
)
