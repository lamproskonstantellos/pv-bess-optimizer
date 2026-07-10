"""Imbalance settlement engine (Eqs. U6-U9) and its input surface.

Locked properties:

1. ``settle_imbalance`` algebra: dual-price cost is non-negative under
   incentive-compatible prices and prices short/long volumes on their
   own sides (U7); single-price cost is sign-indefinite (U8).
2. ``resolve_imbalance_prices``: explicit columns win; the DAM proxy is
   sign-aware (U8a) so ``long <= DAM <= short`` holds at negative DAM.
3. Rolling-horizon integration: the nomination capture consumes NO rng
   draws (same seed with the feature on/off produces an identical
   dispatch), the settlement KPIs appear only when enabled, and the
   first commit block settles at zero deviation.
4. Validation: imbalance requires the rolling-horizon MC and a
   lookahead of at least one commit block; the single regime requires
   its price column; negative multipliers are rejected and
   non-incentive-compatible proxies warn.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.io import validate_workbook_params
from pvbess_opt.rolling_horizon import (
    _net_grid_position_kwh,
    resolve_imbalance_prices,
    rolling_horizon_dispatch,
    settle_imbalance,
)

SOLVER_KW = {"solver_name": "highs", "mip_gap": 0.0, "time_limit_seconds": 120}


def _highs_available() -> bool:
    try:
        import highspy  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# 1. Settlement algebra (U7/U8)
# ---------------------------------------------------------------------------


def test_dual_price_settlement_costs_both_sides():
    dam = np.array([100.0, 100.0, 100.0])
    short = np.array([125.0, 125.0, 125.0])
    long_ = np.array([75.0, 75.0, 75.0])
    dev = np.array([-2.0, 3.0, 0.0])  # short 2 MWh, long 3 MWh, exact
    cost = settle_imbalance(dev, dam, short, long_, pricing="dual")
    assert cost[0] == pytest.approx(2.0 * 25.0)   # short pays the spread
    assert cost[1] == pytest.approx(3.0 * 25.0)   # long is paid less
    assert cost[2] == 0.0
    assert (cost >= 0.0).all()


def test_single_price_settlement_is_sign_indefinite():
    dam = np.array([100.0, 100.0])
    imb = np.array([120.0, 80.0])
    dev = np.array([-1.0, -1.0])  # short in both steps
    cost = settle_imbalance(dev, dam, imb, imb, pricing="single")
    assert cost[0] == pytest.approx(20.0)    # short at a premium: cost
    assert cost[1] == pytest.approx(-20.0)   # short at a discount: profit


# ---------------------------------------------------------------------------
# 2. Price resolution (U8a)
# ---------------------------------------------------------------------------


def test_dam_proxy_is_sign_aware_at_negative_prices():
    ts = pd.DataFrame({"pv_kwh": [0.0, 0.0]})
    dam = np.array([100.0, -100.0])
    short, long_ = resolve_imbalance_prices(
        ts, dam, pricing="dual", mult_short=1.25, mult_long=0.75,
    )
    assert short[0] == pytest.approx(125.0)
    assert long_[0] == pytest.approx(75.0)
    # Negative hour: a naive DAM*m would flip the spread; sign-aware
    # keeps long <= DAM <= short.
    assert short[1] == pytest.approx(-75.0)
    assert long_[1] == pytest.approx(-125.0)
    assert (long_ <= dam).all() and (dam <= short).all()


def test_explicit_columns_win_over_proxy():
    ts = pd.DataFrame({
        "imbalance_price_short_eur_per_mwh": [140.0],
        "imbalance_price_long_eur_per_mwh": [60.0],
    })
    short, long_ = resolve_imbalance_prices(
        ts, np.array([100.0]), pricing="dual",
        mult_short=1.25, mult_long=0.75,
    )
    assert short[0] == 140.0 and long_[0] == 60.0


def test_net_grid_position_handles_missing_columns():
    frame = pd.DataFrame({
        "pv_to_grid_kwh": [10.0], "bess_dis_grid_kwh": [5.0],
        "bess_charge_grid_kwh": [3.0],
    })  # merchant: no grid_to_load_kwh
    assert _net_grid_position_kwh(frame)[0] == pytest.approx(12.0)


# ---------------------------------------------------------------------------
# 3. Rolling-horizon integration
# ---------------------------------------------------------------------------


def _rh_setup() -> tuple[dict, pd.DataFrame]:
    n = 12  # hourly steps; window 4h / commit 2h -> nominations exist
    params = {
        "dt_minutes": 60,
        "efficiency_charge": 1.0,
        "efficiency_discharge": 1.0,
        "soc_min_frac": 0.0,
        "soc_max_frac": 1.0,
        "initial_soc_frac": 0.5,
        "terminal_soc_equal": False,
        "max_cycles_per_day": 10.0,
        "p_grid_export_max_kw": 1000.0,
        "pv_nameplate_kwp": 100.0,
        "bess_power_kw": 50.0,
        "bess_capacity_kwh": 100.0,
        "retail_tariff_eur_per_mwh": 0.0,
        "mode": "merchant",
        "allow_bess_grid_charging": True,
        "unavailability_pct": 0.0,
        "show_titles": False,
    }
    rng = np.random.default_rng(7)
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=n, freq="h"),
        "pv_kwh": np.clip(rng.normal(50.0, 20.0, n), 0.0, None),
        "load_kwh": np.zeros(n),
        "dam_price_eur_per_mwh": rng.normal(80.0, 30.0, n),
    })
    return params, ts


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_rh_settlement_keys_and_rng_neutrality():
    params, ts = _rh_setup()
    kw = dict(
        window_hours=4, commit_hours=2, forecast_seed=11,
        sigma_dam=0.2, sigma_pv=0.15, sigma_load=0.0, **SOLVER_KW,
    )
    full_off, kpis_off = rolling_horizon_dispatch(params, ts, **kw)
    full_on, kpis_on = rolling_horizon_dispatch(
        params, ts, imbalance_enabled=True, **kw,
    )
    # Nomination capture consumes no rng draws: identical dispatch.
    pd.testing.assert_frame_equal(full_off, full_on)
    assert kpis_off["profit_total_eur"] == kpis_on["profit_total_eur"]
    # Keys appear only when enabled.
    for key in (
        "imbalance_cost_eur", "imbalance_short_mwh", "imbalance_long_mwh",
        "imbalance_cost_pv_only_eur", "bess_imbalance_hedge_value_eur",
    ):
        assert key not in kpis_off
        assert key in kpis_on
    # With PV noise on, some deviation must exist beyond block 0.
    assert (
        kpis_on["imbalance_short_mwh"] + kpis_on["imbalance_long_mwh"]
    ) > 0.0
    # Dual regime with incentive-compatible proxy: non-negative cost.
    assert kpis_on["imbalance_cost_eur"] >= 0.0


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_rh_no_noise_unique_optimum_settles_at_zero():
    """Deterministic RH on a PV-only plant (unique optimum: export
    everything at positive prices): nomination == realised, cost 0.
    A BESS would admit alternate optima across window boundaries, so
    PV-only is the clean plumbing check."""
    params, ts = _rh_setup()
    params["bess_power_kw"] = 0.0
    params["bess_capacity_kwh"] = 0.0
    ts["dam_price_eur_per_mwh"] = np.abs(
        ts["dam_price_eur_per_mwh"].to_numpy()
    ) + 5.0
    _full, kpis = rolling_horizon_dispatch(
        params, ts, window_hours=4, commit_hours=2, forecast_seed=None,
        imbalance_enabled=True, **SOLVER_KW,
    )
    assert kpis["imbalance_cost_eur"] == pytest.approx(0.0, abs=1e-6)
    assert kpis["imbalance_short_mwh"] == pytest.approx(0.0, abs=1e-6)
    assert kpis["imbalance_long_mwh"] == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 4. Input-surface validation
# ---------------------------------------------------------------------------


def _typed(**sim) -> dict:
    from pvbess_opt.io import (
        PROJECT_SHEET_DEFAULTS,
        SIMULATION_SHEET_DEFAULTS,
    )

    return {
        "project": dict(PROJECT_SHEET_DEFAULTS),
        "pv": {}, "bess": {}, "economics": {}, "balancing": {}, "ppa": {},
        "simulation": dict(SIMULATION_SHEET_DEFAULTS, **sim),
    }


def test_imbalance_requires_uncertainty():
    with pytest.raises(ValueError, match="uncertainty_enabled"):
        validate_workbook_params(
            _typed(imbalance_enabled=True, uncertainty_enabled=False),
        )


def test_imbalance_requires_lookahead_window():
    with pytest.raises(ValueError, match="2 x uncertainty_commit_hours"):
        validate_workbook_params(_typed(
            imbalance_enabled=True, uncertainty_enabled=True,
            uncertainty_window_hours=24, uncertainty_commit_hours=24,
        ))


def test_negative_multipliers_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        validate_workbook_params(_typed(
            imbalance_enabled=True, uncertainty_enabled=True,
            uncertainty_window_hours=48, uncertainty_commit_hours=24,
            imbalance_price_mult_short=-1.0,
        ))


def test_non_incentive_compatible_proxy_warns(caplog):
    with caplog.at_level("WARNING"):
        validate_workbook_params(_typed(
            imbalance_enabled=True, uncertainty_enabled=True,
            uncertainty_window_hours=48, uncertainty_commit_hours=24,
            imbalance_price_mult_short=0.9,
        ))
    assert any(
        "incentive-compatible" in r.message for r in caplog.records
    )


def test_simulation_schema_carries_the_keys():
    from pvbess_opt.io import _SIMULATION_ROWS, SIMULATION_SHEET_DEFAULTS

    assert SIMULATION_SHEET_DEFAULTS["imbalance_enabled"] is False
    assert SIMULATION_SHEET_DEFAULTS["imbalance_pricing"] == "dual"
    keys = {r[0] for r in _SIMULATION_ROWS}
    assert {
        "imbalance_enabled", "imbalance_pricing",
        "imbalance_price_mult_short", "imbalance_price_mult_long",
    } <= keys
