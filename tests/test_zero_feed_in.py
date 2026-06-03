"""Zero feed-in (self-consumption export prohibition) dispatch tests.

``zero_feed_in`` is implemented as a single chokepoint: when set,
:func:`pvbess_opt.optimization._resolve_max_injection_per_step` returns a
flat 0 % max-injection profile (an all-zero per-step fraction array),
which forces ``grid_export_cap_kwh = 0`` for every step and therefore
``pv_to_grid + bess_dis_grid = 0`` via the existing EXPORT_CAP
constraint.  Surplus PV beyond load + BESS charging is curtailed.  No
new constraint and no objective edit are introduced, so every dispatch
invariant continues to hold.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.kpis import (
    ENERGY_TOLERANCE,
    add_economic_columns,
    compute_kpis,
)
from pvbess_opt.optimization import (
    _resolve_max_injection_per_step,
    run_scenario,
    verify_dispatch_invariants,
)

_TOL = ENERGY_TOLERANCE


def _mk_ts(n: int = 48) -> pd.DataFrame:
    """Synthetic self-consumption week with a large PV surplus.

    PV peaks at 8 MWh/h against a flat 1 MWh/h load so the un-curtailed
    surplus is exported when ``zero_feed_in`` is off and must be
    curtailed when it is on.
    """
    timestamps = pd.date_range("2026-06-01", periods=n, freq="h")
    h = np.arange(n).astype(float) % 24
    pv = 8000.0 * np.where((h >= 6) & (h <= 18),
                           np.sin(np.pi * (h - 6) / 12.0), 0.0)
    pv = np.maximum(pv, 0.0)
    load = np.full(n, 1000.0)
    dam = 60.0 - 20.0 * np.sin(np.pi * (h - 6) / 12.0)  # positive: export attractive
    return pd.DataFrame({
        "timestamp": timestamps,
        "pv_kwh": pv,
        "load_kwh": load,
        "dam_price_eur_per_mwh": dam,
    })


def _params(**over) -> dict:
    base = dict(
        dt_minutes=60,
        efficiency_charge=0.97,
        efficiency_discharge=0.97,
        soc_min_frac=0.20,
        soc_max_frac=0.95,
        initial_soc_frac=0.50,
        terminal_soc_equal=True,
        max_cycles_per_day=1.0,
        p_grid_export_max_kw=5000.0,
        retail_tariff_eur_per_mwh=120.0,
        settlement_minutes=15,
        mode="self_consumption",
        allow_bess_grid_charging=False,
        show_titles=False,
    )
    base.update(over)
    return base


_ASSET_CONFIGS = {
    "hybrid": dict(pv_nameplate_kwp=6000.0, bess_power_kw=2000.0,
                   bess_capacity_kwh=8000.0),
    "pv_only": dict(pv_nameplate_kwp=6000.0, bess_power_kw=0.0,
                    bess_capacity_kwh=0.0),
    "bess_only": dict(pv_nameplate_kwp=0.0, bess_power_kw=2000.0,
                      bess_capacity_kwh=8000.0, allow_bess_grid_charging=True),
}


def _assert_invariants_ok(inv: dict[str, float]) -> None:
    """Assert every dispatch invariant is within tolerance.

    Mirrors ``main._check_strict_invariants``: invariant 5 is a product
    (kWh^2) so it is compared to ``tol**2``; the balancing-invariant keys
    are zero here (no balancing) and use the plain tolerance.
    """
    for name, value in inv.items():
        if name == "invariant_5_no_sim_grid_io_max_product_kwh2":
            assert value <= _TOL ** 2, (name, value)
        elif name == "invariant_b1_capacity_share_sum_pct_excess":
            assert value <= 0.5, (name, value)
        else:
            assert value <= _TOL, (name, value)


# ---------------------------------------------------------------------------
# Chokepoint unit test
# ---------------------------------------------------------------------------


def test_resolve_max_injection_zeros_when_zero_feed_in():
    ts = _mk_ts(24)
    # A 100 % profile would normally expand to a flat 1.0 fraction;
    # zero_feed_in must override it to all zeros.
    params = {
        "zero_feed_in": True,
        "max_injection_profile": np.full(24, 100.0, dtype=float),
    }
    frac = _resolve_max_injection_per_step(params, ts)
    assert frac.shape == (len(ts),)
    assert np.all(frac == 0.0)


def test_resolve_max_injection_unchanged_when_zero_feed_in_off():
    ts = _mk_ts(24)
    params = {
        "zero_feed_in": False,
        "max_injection_profile": np.full(24, 100.0, dtype=float),
    }
    frac = _resolve_max_injection_per_step(params, ts)
    assert np.allclose(frac, 1.0)


# ---------------------------------------------------------------------------
# Dispatch: zero export under zero_feed_in for every asset config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("asset", list(_ASSET_CONFIGS))
def test_zero_feed_in_forces_zero_export(asset):
    ts = _mk_ts()
    params = _params(zero_feed_in=True, **_ASSET_CONFIGS[asset])
    res, _ = run_scenario(params, ts, solver_name="highs", mip_gap=0.0)

    export = (res["pv_to_grid_kwh"] + res["bess_dis_grid_kwh"]).to_numpy()
    assert np.all(np.abs(export) <= _TOL), export.max()
    assert np.all(res["grid_export_total_kwh"].to_numpy() <= _TOL)
    # The per-step export cap reported on the frame is exactly zero.
    assert np.all(res["grid_export_cap_kwh"].to_numpy() == 0.0)

    inv = verify_dispatch_invariants(res, params, mode="self_consumption")
    _assert_invariants_ok(inv)


def test_zero_feed_in_curtails_surplus_without_invariant_violation():
    ts = _mk_ts()
    params = _params(zero_feed_in=True, **_ASSET_CONFIGS["pv_only"])
    res, _ = run_scenario(params, ts, solver_name="highs", mip_gap=0.0)
    # Surplus PV (well above the 1 MWh/h load) must be curtailed since no
    # export is allowed and there is no BESS to absorb it.
    assert float(res["pv_curtail_kwh"].sum()) > 1.0
    inv = verify_dispatch_invariants(res, params, mode="self_consumption")
    # Invariant 7 (cap-not-binding => no curtail) must NOT fire: the cap
    # is zero, so cap_residual is zero and the expected curtailment is
    # not flagged.
    assert inv["invariant_7_curtail_behavior_kwh"] <= _TOL
    _assert_invariants_ok(inv)


def test_zero_feed_in_export_kpis_zero():
    ts = _mk_ts()
    params = _params(zero_feed_in=True, **_ASSET_CONFIGS["hybrid"])
    res, _ = run_scenario(params, ts, solver_name="highs", mip_gap=0.0)
    add_economic_columns(res, params)
    kpis = compute_kpis(res, params, verify_balance=False)
    assert kpis["system_total_export_mwh"] == pytest.approx(0.0, abs=1e-9)
    assert kpis["profit_export_from_pv_eur"] == pytest.approx(0.0, abs=1e-6)
    assert kpis["profit_export_from_bess_eur"] == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# zero_feed_in OFF (and absent) is bit-identical to the pre-feature run
# ---------------------------------------------------------------------------


def test_zero_feed_in_off_matches_key_absent_bit_identical():
    ts = _mk_ts()
    params_absent = _params(**_ASSET_CONFIGS["hybrid"])  # no zero_feed_in key
    params_off = _params(zero_feed_in=False, **_ASSET_CONFIGS["hybrid"])
    res_absent, _ = run_scenario(params_absent, ts, solver_name="highs", mip_gap=0.0)
    res_off, _ = run_scenario(params_off, ts, solver_name="highs", mip_gap=0.0)
    for col in res_absent.columns:
        if pd.api.types.is_numeric_dtype(res_absent[col]):
            assert np.array_equal(
                res_absent[col].to_numpy(dtype=float),
                res_off[col].to_numpy(dtype=float),
            ), col
    # And the off run DOES export (so the feature is what removes it).
    assert float((res_off["pv_to_grid_kwh"] + res_off["bess_dis_grid_kwh"]).sum()) > 1.0


def test_zero_feed_in_overrides_max_injection_profile():
    ts = _mk_ts()
    # A permissive 100 % profile would allow full export; zero_feed_in
    # must still force zero.
    params = _params(
        zero_feed_in=True,
        max_injection_profile=np.full(24, 100.0, dtype=float),
        **_ASSET_CONFIGS["hybrid"],
    )
    res, _ = run_scenario(params, ts, solver_name="highs", mip_gap=0.0)
    export = (res["pv_to_grid_kwh"] + res["bess_dis_grid_kwh"]).to_numpy()
    assert np.all(np.abs(export) <= _TOL)
