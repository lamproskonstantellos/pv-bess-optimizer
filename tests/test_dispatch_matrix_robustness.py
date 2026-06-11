"""Dispatch-matrix robustness + invariant_7 negative-price correctness.

Two things are proven here:

1. The full ``mode x grid_cap_includes_load x allow_bess_grid_charging``
   matrix (8 cells) runs without error and satisfies every dispatch
   invariant, with binding per-source injection sub-caps AND a
   negative-DAM step present in the profile.  This is the no-error +
   correctness sweep for objectives C and D.

2. ``invariant_7`` (curtail-behaviour) must not flag the optimizer for
   curtailing surplus PV at a non-positive DAM price — curtailing rather
   than exporting at a loss is the profit-maximising optimum, not a
   violation.  A strictly positive export price must still flag genuine
   "lazy curtailment", so the gate is a discriminator, not a blanket
   silencer.
"""

from __future__ import annotations

import importlib
import itertools

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.kpis import ENERGY_TOLERANCE, verify_energy_balance
from pvbess_opt.optimization import run_scenario, verify_dispatch_invariants

SOLVER_KW = {"solver_name": "highs", "mip_gap": 0.0, "time_limit_seconds": 120}


def _highs_available() -> bool:
    try:
        importlib.import_module("highspy")
    except ImportError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _highs_available(), reason="HiGHS solver not installed",
)


def _matrix_params(**overrides) -> dict:
    params = {
        "dt_minutes": 60,
        "efficiency_charge": 0.95,
        "efficiency_discharge": 0.95,
        "soc_min_frac": 0.10,
        "soc_max_frac": 0.95,
        "initial_soc_frac": 0.50,
        "terminal_soc_equal": True,
        "max_cycles_per_day": 3.0,
        "p_grid_export_max_kw": 6.0,
        "pv_nameplate_kwp": 1000.0,
        "bess_power_kw": 5.0,
        "bess_capacity_kwh": 10.0,
        "retail_tariff_eur_per_mwh": 180.0,
        "mode": "self_consumption",
        "allow_bess_grid_charging": False,
        "show_titles": False,
    }
    params.update(overrides)
    return params


def _matrix_ts() -> pd.DataFrame:
    """24 h, dt = 1 h.  Midday PV bell, morning + evening load, and a
    NEGATIVE DAM price exactly at the PV peak so the matrix exercises
    optimal curtailment at a loss-making export price."""
    h = np.arange(24)
    pv = np.clip(10.0 * np.sin(np.pi * (h - 6) / 12.0), 0.0, None)
    pv[(h < 6) | (h > 18)] = 0.0
    load = (
        3.0
        + 5.0 * np.exp(-((h - 19.0) ** 2) / 6.0)
        + 2.5 * np.exp(-((h - 8.0) ** 2) / 6.0)
    )
    dam = 40.0 + 25.0 * np.sin(np.pi * (h - 15.0) / 12.0)
    dam[12] = -10.0  # loss-making export exactly at the PV peak
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=24, freq="h"),
        "pv_kwh": pv.astype(float),
        "load_kwh": load.astype(float),
        "dam_price_eur_per_mwh": dam.astype(float),
    })


def _binding_sub_caps() -> dict:
    """PV and BESS sub-caps that bind at specific hours (not flat 100 %)."""
    mi_pv = np.full(24, 100.0)
    mi_pv[13:15] = 60.0       # PV surplus export capped midday-afternoon
    mi_bess = np.full(24, 100.0)
    mi_bess[19] = 0.0         # battery cannot inject during the evening peak
    return {
        "max_injection_profile_pv": mi_pv,
        "max_injection_profile_bess": mi_bess,
    }


_MATRIX = list(itertools.product(
    ("self_consumption", "merchant"),  # mode
    (False, True),                      # grid_cap_includes_load
    (False, True),                      # allow_bess_grid_charging
))


@pytest.mark.parametrize("mode,cap_incl,grid_chg", _MATRIX)
@pytest.mark.parametrize("with_sub_caps", [False, True])
def test_matrix_no_error_and_invariants_clean(mode, cap_incl, grid_chg, with_sub_caps):
    """Every matrix cell solves, balances energy, and satisfies all
    invariants — with and without binding per-source sub-caps, and with a
    negative-DAM step present."""
    extra = _binding_sub_caps() if with_sub_caps else {}
    params = _matrix_params(
        mode=mode,
        grid_cap_includes_load=cap_incl,
        allow_bess_grid_charging=grid_chg,
        **extra,
    )
    ts = _matrix_ts()
    _res, _solver, full = run_scenario(
        params, ts, return_unrounded=True, **SOLVER_KW,
    )
    # No-error energy balance.
    verify_energy_balance(full, params, raise_on_failure=True)
    # All invariants (the nine general + six balancing) within tolerance.
    inv = verify_dispatch_invariants(full, params, mode=mode)
    for name, value in inv.items():
        assert value <= ENERGY_TOLERANCE, (
            f"{mode}/cap_incl={cap_incl}/grid_chg={grid_chg}/"
            f"sub_caps={with_sub_caps}: {name}={value:g}"
        )


@pytest.mark.parametrize("mode", ["self_consumption", "merchant"])
def test_invariant7_ignores_curtailment_at_negative_dam(mode):
    """Surplus PV curtailed at a negative DAM price (cap NOT binding) is the
    optimal dispatch, so invariant_7 must report zero — not a violation that
    would abort a correct run under --strict."""
    h = np.arange(24)
    pv = np.clip(10.0 * np.sin(np.pi * (h - 6) / 12.0), 0.0, None)
    pv[(h < 6) | (h > 18)] = 0.0
    load = np.full(24, 2.0)
    dam = np.full(24, 50.0)
    dam[10:15] = -20.0  # negative across the whole PV peak
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=24, freq="h"),
        "pv_kwh": pv, "load_kwh": load, "dam_price_eur_per_mwh": dam,
    })
    # No BESS so the surplus has nowhere to go but curtailment at the loss
    # hours — guarantees pv_curtail > 0 while the export cap stays slack.
    params = _matrix_params(
        mode=mode, bess_power_kw=0.0, bess_capacity_kwh=0.0,
        p_grid_export_max_kw=50.0,
    )
    _res, _solver, full = run_scenario(
        params, ts, return_unrounded=True, **SOLVER_KW,
    )
    # The negative-price hours must actually curtail with the cap slack,
    # otherwise the test would pass vacuously.
    neg = (full["dam_price_eur_per_mwh"].to_numpy() < 0)
    curtailed = full["pv_curtail_kwh"].to_numpy()[neg]
    cap = full["grid_export_cap_kwh"].to_numpy()[neg]
    inj = full["grid_injection_total_kwh"].to_numpy()[neg]
    assert curtailed.sum() > 1.0, "expected real curtailment at the loss hours"
    assert (cap - inj > 1e-3).any(), "expected slack cap at the loss hours"
    inv = verify_dispatch_invariants(full, params, mode=mode)
    assert inv["invariant_7_curtail_behavior_kwh"] == pytest.approx(0.0, abs=1e-9)


def test_invariant7_still_flags_lazy_curtailment_at_positive_dam():
    """The gate is a discriminator: with a strictly positive export price,
    curtailing PV while the cap has headroom IS a violation and must be
    counted.  Built as a direct dispatch frame so no solver can 'fix' it."""
    base = {
        "pv_to_load_kwh": [0.0, 0.0],
        "pv_to_bess_kwh": [0.0, 0.0],
        "bess_charge_grid_kwh": [0.0, 0.0],
        "bess_dis_load_kwh": [0.0, 0.0],
        "bess_dis_grid_kwh": [0.0, 0.0],
        "grid_to_load_kwh": [0.0, 0.0],
        "soc_kwh": [0.0, 0.0],
        "load_kwh": [0.0, 0.0],
        "pv_kwh": [5.0, 0.0],          # 5 = 2 exported + 3 curtailed
        "pv_to_grid_kwh": [2.0, 0.0],
        "pv_curtail_kwh": [3.0, 0.0],  # curtailed while cap has 8 kWh of room
        "grid_injection_total_kwh": [2.0, 0.0],
        "grid_export_cap_kwh": [10.0, 10.0],
        "timestamp": pd.date_range("2026-06-01", periods=2, freq="h"),
    }
    params = _matrix_params(mode="merchant")

    res_pos = pd.DataFrame({**base, "dam_price_eur_per_mwh": [50.0, 50.0]})
    inv_pos = verify_dispatch_invariants(res_pos, params, mode="merchant")
    assert inv_pos["invariant_7_curtail_behavior_kwh"] == pytest.approx(1.0)

    # Same frame, non-positive price -> the identical curtailment is optimal.
    res_neg = pd.DataFrame({**base, "dam_price_eur_per_mwh": [-50.0, 50.0]})
    inv_neg = verify_dispatch_invariants(res_neg, params, mode="merchant")
    assert inv_neg["invariant_7_curtail_behavior_kwh"] == pytest.approx(0.0)
