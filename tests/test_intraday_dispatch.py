"""Two-stage intraday re-dispatch — model, invariants, economics gates.

Covers the Stage-2 MILP block (Eqs. I1-I5): the zero-spread identity,
spread monotonicity, cap safety, the purchases and wear gates, the
INV-I invariant family contract, and the per-step settlement columns
(Eqs. E58/E59 seeds).  The input surface lives in
``tests/test_intraday_io.py``.
"""

from __future__ import annotations

import numpy as np
import pytest

from pvbess_opt.intraday import (
    DA_POSITION_COLUMNS,
    extract_da_position,
    redispatch_intraday,
    resolve_intraday_config,
)
from pvbess_opt.kpis import ENERGY_TOLERANCE, compute_kpis, verify_energy_balance
from pvbess_opt.optimization import (
    INTRADAY_INVARIANT_KEYS,
    run_scenario,
    verify_dispatch_invariants,
)
from tests.conftest import _make_short_ts, _short_params


def _highs_available() -> bool:
    try:
        import highspy  # noqa: F401
    except ImportError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _highs_available(), reason="HiGHS solver not installed",
)


def _intraday_params(**overrides) -> dict:
    params = _short_params("merchant")
    params["intraday"] = {
        "id_enabled": True,
        "id_max_deviation_frac_of_cap": 0.25,
        "id_allow_purchases": True,
        "id_fee_eur_per_mwh": 0.0,
        "id_inflation_pct": 0.0,
    }
    params["intraday"].update(overrides)
    return params


def _ts_with_ida(spread) -> object:
    """48 h merchant timeseries with an IDA column at DAM + spread."""
    ts = _make_short_ts(48, with_load=False)
    ts["ida_price_eur_per_mwh"] = (
        ts["dam_price_eur_per_mwh"].to_numpy(dtype=float)
        + np.asarray(spread, dtype=float)
    )
    return ts


def _structured_spread(n: int = 48) -> np.ndarray:
    """Afternoon premium / night discount — guarantees profitable trades."""
    h = np.arange(n) % 24
    return np.where(h >= 12, 30.0, -20.0)


def _two_stage(params, ts):
    res1, _, res1_full = run_scenario(params, ts, return_unrounded=True)
    res2, _, res2_full = redispatch_intraday(params, ts, res1_full)
    return res1, res1_full, res2, res2_full


# ---------------------------------------------------------------------------
# Identities and monotonicity
# ---------------------------------------------------------------------------


def test_zero_spread_zero_fee_is_stage1_identity():
    """ida == dam and no fee: no trades, Stage-2 profit == Stage-1."""
    params = _intraday_params()
    ts = _ts_with_ida(0.0)
    res1, _, res2, _ = _two_stage(params, ts)
    k1 = compute_kpis(res1.copy(), params, verify_balance=False)
    k2 = compute_kpis(res2.copy(), params, verify_balance=False)
    assert k2["id_traded_volume_mwh"] == 0.0
    assert k2["id_net_revenue_eur"] == 0.0
    assert k2["profit_total_eur"] == pytest.approx(
        k1["profit_total_eur"], abs=1.0,
    )


def test_spread_monotonicity_never_decreases_profit():
    """Widening a synthetic IDA-DAM spread never decreases profit."""
    profits = []
    for scale in (0.0, 0.5, 1.0):
        params = _intraday_params()
        ts = _ts_with_ida(scale * _structured_spread())
        _, _, res2, _ = _two_stage(params, ts)
        k2 = compute_kpis(res2.copy(), params, verify_balance=False)
        profits.append(float(k2["profit_total_eur"]))
    assert profits[1] >= profits[0] - 1.0
    assert profits[2] >= profits[1] - 1.0


def test_two_stage_uplift_and_settlement_columns():
    """A structured spread produces trades, a positive net margin and
    internally consistent settlement columns (Eq. I3 spread form)."""
    params = _intraday_params(id_fee_eur_per_mwh=0.1)
    ts = _ts_with_ida(_structured_spread())
    res1, _, res2, _ = _two_stage(params, ts)
    k1 = compute_kpis(res1.copy(), params, verify_balance=False)
    k2 = compute_kpis(res2.copy(), params, verify_balance=False)
    assert k2["id_traded_volume_mwh"] > 0.0
    assert k2["profit_total_eur"] >= k1["profit_total_eur"] - 1e-6
    # Per-step margin column equals the spread times the net trade.
    from pvbess_opt.kpis import add_economic_columns

    frame = add_economic_columns(res2.copy(), params)
    spread = (
        frame["ida_price_eur_per_mwh"].to_numpy(dtype=float)
        - frame["dam_price_eur_per_mwh"].to_numpy(dtype=float)
    )
    net_trade = (
        frame["id_sell_pv_kwh"].to_numpy(dtype=float)
        + frame["id_sell_bess_kwh"].to_numpy(dtype=float)
        - frame["id_buy_kwh"].to_numpy(dtype=float)
    )
    expected = spread / 1000.0 * net_trade
    assert np.abs(
        frame["id_revenue_eur"].to_numpy(dtype=float) - expected
    ).max() < 1e-3
    # Venue fee charges the traded volume in both directions.
    assert k2["id_venue_fee_eur"] == pytest.approx(
        0.1 * k2["id_traded_volume_mwh"], abs=0.02,
    )
    # The Stage-1 result never carries intraday columns.
    assert "id_sell_pv_kwh" not in res1.columns


def test_deviation_cap_zero_rejected_by_redispatch():
    """Zero deviation budget disables trading — callers skip Stage 2."""
    params = _intraday_params(id_max_deviation_frac_of_cap=0.0)
    ts = _ts_with_ida(_structured_spread())
    _res1, _, res1_full = run_scenario(params, ts, return_unrounded=True)
    with pytest.raises(ValueError, match="disables intraday trading"):
        redispatch_intraday(params, ts, res1_full)


# ---------------------------------------------------------------------------
# Physical safety
# ---------------------------------------------------------------------------


def test_cap_safety_and_deviation_budget():
    """Combined DA+ID injection honours the export cap; per-step traded
    volume honours the deviation budget (Eq. I2)."""
    params = _intraday_params()
    ts = _ts_with_ida(np.full(48, 80.0))  # strong sell incentive
    _, _, _res2, res2_full = _two_stage(params, ts)
    cap = res2_full["grid_export_cap_kwh"].to_numpy(dtype=float)
    injection = res2_full["grid_injection_total_kwh"].to_numpy(dtype=float)
    assert float(np.maximum(0.0, injection - cap).max()) <= ENERGY_TOLERANCE
    dev_cap = 0.25 * float(params["p_grid_export_max_kw"]) * 1.0
    traded = (
        res2_full["id_sell_pv_kwh"] + res2_full["id_sell_bess_kwh"]
        + res2_full["id_buy_kwh"]
    ).to_numpy(dtype=float)
    assert float(np.maximum(0.0, traded - dev_cap).max()) <= ENERGY_TOLERANCE


def test_purchases_gating():
    """id_allow_purchases = FALSE pins every IDA buy to zero."""
    params = _intraday_params(id_allow_purchases=False)
    ts = _ts_with_ida(_structured_spread())
    _, _, res2, _ = _two_stage(params, ts)
    assert float(res2["id_buy_kwh"].abs().sum()) == 0.0


def test_sell_only_uses_curtailed_pv():
    """Negative-DAM midday hours curtail PV day-ahead; a positive IDA
    price re-sells that energy intraday without any purchases."""
    params = _intraday_params(id_allow_purchases=False)
    ts = _make_short_ts(48, with_load=False)
    h = np.arange(48) % 24
    dam = np.where((h >= 11) & (h <= 14), -10.0, 60.0)
    ts["dam_price_eur_per_mwh"] = dam
    ts["ida_price_eur_per_mwh"] = np.where(
        (h >= 11) & (h <= 14), 40.0, 60.0,
    )
    res1, _res1_full, res2, _ = _two_stage(params, ts)
    assert float(res1["pv_curtail_kwh"].sum()) > 0.0
    assert float(res2["id_sell_pv_kwh"].sum()) > 0.0
    assert float(res2["id_buy_kwh"].abs().sum()) == 0.0
    # Stage 2 curtails less than Stage 1 — the venue absorbs the spill.
    assert (
        float(res2["pv_curtail_kwh"].sum())
        < float(res1["pv_curtail_kwh"].sum())
    )


def test_wear_cost_blocks_thin_spreads():
    """A large wear cost keeps the BESS from re-cycling on small spreads
    (the incremental-throughput coupling of Eq. I3)."""
    params = _intraday_params()
    params["pv_nameplate_kwp"] = 0.0
    params["allow_bess_grid_charging"] = True
    params["bess_wear_cost_eur_per_mwh"] = 500.0
    ts = _ts_with_ida(5.0)
    _, _, res2, _ = _two_stage(params, ts)
    k2 = compute_kpis(res2.copy(), params, verify_balance=False)
    assert k2["id_traded_volume_mwh"] == 0.0


# ---------------------------------------------------------------------------
# Invariants and verification
# ---------------------------------------------------------------------------


def test_intraday_invariants_pass_on_stage2_frame():
    params = _intraday_params(id_fee_eur_per_mwh=0.1)
    ts = _ts_with_ida(_structured_spread())
    _, _, _, res2_full = _two_stage(params, ts)
    residuals = verify_energy_balance(
        res2_full, params, raise_on_failure=False,
    )
    assert max(residuals.values()) <= ENERGY_TOLERANCE
    inv = verify_dispatch_invariants(res2_full, params, mode="merchant")
    for key in INTRADAY_INVARIANT_KEYS:
        assert key in inv
        tol = (
            ENERGY_TOLERANCE ** 2
            if key.endswith("kwh2") else ENERGY_TOLERANCE
        )
        assert inv[key] <= tol, f"{key}={inv[key]}"


def test_intraday_invariants_zero_when_disabled(short_params_merchant):
    """The INV-I family is always emitted and vacuously 0.0 on frames
    without the Stage-2 columns (stable-contract convention)."""
    params = dict(short_params_merchant)
    ts = _make_short_ts(24, with_load=False)
    _res, _, res_full = run_scenario(params, ts, return_unrounded=True)
    inv = verify_dispatch_invariants(res_full, params, mode="merchant")
    for key in INTRADAY_INVARIANT_KEYS:
        assert inv[key] == 0.0


def test_stage1_of_two_stage_run_is_bit_identical_to_id_off():
    """id_enabled with the IDA column present but no pinned position
    builds the unchanged day-ahead model (Stage-1 bit-identity)."""
    import pandas as pd

    params_on = _intraday_params()
    params_off = _short_params("merchant")
    ts = _ts_with_ida(_structured_spread())
    ts_off = ts.drop(columns=["ida_price_eur_per_mwh"])
    res_on, _, _ = run_scenario(params_on, ts, return_unrounded=True)
    res_off, _, _ = run_scenario(params_off, ts_off, return_unrounded=True)
    # The only difference is the echoed IDA price column.
    pd.testing.assert_frame_equal(
        res_on.drop(columns=["ida_price_eur_per_mwh"]), res_off,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_extract_da_position_identity():
    params = _short_params("merchant")
    ts = _make_short_ts(24, with_load=False)
    res, _ = run_scenario(params, ts)
    pos = extract_da_position(res)
    assert set(pos.columns) == set(DA_POSITION_COLUMNS)
    expected = (
        res["pv_to_grid_kwh"].astype(float)
        + res["bess_dis_grid_kwh"].astype(float)
        - res["bess_charge_grid_kwh"].astype(float).fillna(0.0)
    )
    assert np.allclose(pos["id_da_position_kwh"], expected)


def test_resolve_intraday_config_coercion():
    cfg = resolve_intraday_config({
        "id_enabled": 1,
        "id_max_deviation_frac_of_cap": "0.4",
        "id_allow_purchases": 0,
        "id_fee_eur_per_mwh": 2,
        "unknown_key": "ignored",
    })
    assert cfg.id_enabled is True
    assert cfg.id_max_deviation_frac_of_cap == 0.4
    assert cfg.id_allow_purchases is False
    assert cfg.id_fee_eur_per_mwh == 2.0
    assert cfg.id_inflation_pct == 0.0
