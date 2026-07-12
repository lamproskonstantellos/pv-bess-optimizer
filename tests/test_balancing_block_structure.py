"""Balancing reservation block structure (Eq. B9).

`bm_block_hours > 0` pins every per-product reservation to its
block-anchor value (multi-hour capacity-auction blocks, anchored on
hour-of-year multiples), a pure restriction of the per-step feasible
set — the B1-B8 machinery and the objective are untouched.  Locked:
zero-default bit-identity, within-block constancy at 1 h and 15 min
steps, the restriction property (blocked objective never beats
per-step), the B-invariants on a blocked solve, validation of the
divisibility rules and the scenario dotted-target round-trip.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.balancing import resolve_balancing_config
from pvbess_opt.optimization import (
    build_model,
    run_scenario,
    verify_dispatch_invariants,
)
from tests._balancing_helpers import _balancing_on


def _highs_available() -> bool:
    try:
        import highspy
    except ImportError:
        return False
    return bool(highspy)


pytestmark = pytest.mark.skipif(
    not _highs_available(), reason="requires HiGHS",
)


def _params(dt_minutes: int = 60, **o) -> dict:
    p = {
        "dt_minutes": dt_minutes,
        "mode": "merchant",
        "pv_nameplate_kwp": 0.0,
        "bess_power_kw": 1000.0,
        "bess_capacity_kwh": 4000.0,
        "efficiency_charge": 0.95,
        "efficiency_discharge": 0.95,
        "soc_min_frac": 0.1,
        "soc_max_frac": 0.9,
        "initial_soc_frac": 0.5,
        "terminal_soc_equal": False,
        "max_cycles_per_day": 2.0,
        "p_grid_export_max_kw": 5000.0,
        "retail_tariff_eur_per_mwh": 120.0,
        "allow_bess_grid_charging": True,
    }
    p.update(o)
    return p


def _ts(n: int = 24, dt_minutes: int = 60) -> pd.DataFrame:
    hours = np.arange(n) * dt_minutes / 60.0
    return pd.DataFrame({
        "timestamp": pd.date_range(
            "2026-03-01", periods=n, freq=f"{dt_minutes}min",
        ),
        "pv_kwh": np.zeros(n),
        "dam_price_eur_per_mwh": 60.0 + 45.0 * np.sin(
            hours * 2.0 * np.pi / 24.0,
        ),
    })


def _reservation_columns(res: pd.DataFrame) -> list[str]:
    return [
        c for c in res.columns if c.startswith("bm_reservation_")
    ]


def test_zero_default_is_bit_identical():
    ts = _ts()
    base = _balancing_on(_params())
    res_absent, _s, _f = run_scenario(base, ts, return_unrounded=True)
    explicit = _balancing_on(_params(), bm_block_hours=0)
    res_zero, _s2, _f2 = run_scenario(explicit, ts, return_unrounded=True)
    pd.testing.assert_frame_equal(res_absent, res_zero)
    m = build_model(explicit, ts)
    assert not hasattr(m, "BM_BLOCK_LINK")


@pytest.mark.parametrize(("dt_minutes", "n"), [(60, 24), (15, 96)])
def test_block_constancy(dt_minutes, n):
    ts = _ts(n=n, dt_minutes=dt_minutes)
    p = _balancing_on(_params(dt_minutes), bm_block_hours=4)
    _res, _s, full = run_scenario(p, ts, return_unrounded=True)
    steps_per_block = int(4 * 60 / dt_minutes)
    cols = _reservation_columns(full)
    assert cols, "balancing reservations missing from the frame"
    for col in cols:
        values = full[col].to_numpy(dtype=float)
        blocks = values.reshape(-1, steps_per_block)
        assert np.allclose(blocks, blocks[:, :1], atol=1e-6), (
            f"{col} varies within a {4}h block"
        )


def test_block_objective_dominated():
    """The blocked solution is a restriction: its balancing-revenue
    plus arbitrage objective can never beat the per-step solve."""
    import pyomo.environ as pyo

    ts = _ts()
    objectives = {}
    for label, block_hours in (("free", 0), ("blocked", 6)):
        m = build_model(
            _balancing_on(_params(), bm_block_hours=block_hours), ts,
        )
        solver = pyo.SolverFactory("appsi_highs")
        solver.solve(m)
        objectives[label] = float(pyo.value(m.OBJ))
    assert objectives["blocked"] <= objectives["free"] + 1e-4
    # And the restriction genuinely binds on this instance (the sine
    # price profile makes per-step reservations strictly better).
    assert objectives["blocked"] < objectives["free"] - 1e-4


def test_block_invariants_green():
    ts = _ts()
    p = _balancing_on(_params(), bm_block_hours=4)
    _res, _s, full = run_scenario(p, ts, return_unrounded=True)
    inv = verify_dispatch_invariants(full, p)
    bad = {
        k: v for k, v in inv.items()
        if k.startswith("invariant_b") and v > 1e-3
    }
    assert not bad, f"balancing invariants violated on a blocked solve: {bad}"


def test_config_resolves_int():
    cfg = resolve_balancing_config({
        "balancing_enabled": True, "bm_block_hours": 4.0,
    })
    assert cfg.bm_block_hours == 4
    assert isinstance(cfg.bm_block_hours, int)
    assert resolve_balancing_config({}).bm_block_hours == 0


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _typed(block_hours) -> dict:
    from pvbess_opt.io import (
        BALANCING_SHEET_DEFAULTS,
        BESS_SHEET_DEFAULTS,
        PROJECT_SHEET_DEFAULTS,
        PV_SHEET_DEFAULTS,
    )

    return {
        "project": dict(PROJECT_SHEET_DEFAULTS, mode="merchant"),
        "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=0.0),
        "bess": dict(
            BESS_SHEET_DEFAULTS, bess_power_kw=1000.0,
            bess_capacity_kwh=4000.0,
        ),
        "economics": {},
        "simulation": {},
        "balancing": dict(
            BALANCING_SHEET_DEFAULTS, balancing_enabled=True,
            bm_settlement_minutes=60, bm_block_hours=block_hours,
        ),
    }


def test_validation_accepts_divisible_blocks():
    from pvbess_opt.io import validate_workbook_params

    for hours in (0, 1, 4, 6, 24):
        validate_workbook_params(_typed(hours), dt_minutes=60)


def test_validation_rejects_non_divisor_of_day():
    from pvbess_opt.io import validate_workbook_params

    with pytest.raises(ValueError, match="divide 24"):
        validate_workbook_params(_typed(5), dt_minutes=60)


def test_validation_rejects_negative():
    from pvbess_opt.io import validate_workbook_params

    with pytest.raises(ValueError, match="bm_block_hours"):
        validate_workbook_params(_typed(-4), dt_minutes=60)


def test_scenario_dotted_target_round_trips():
    from pvbess_opt.scenarios import validate_scenario_overrides

    validate_scenario_overrides({
        "name": "blocked", "balancing": {"bm_block_hours": 4},
    })
    with pytest.raises(ValueError, match="bm_block_hour"):
        validate_scenario_overrides({
            "name": "typo", "balancing": {"bm_block_hour": 4},
        })
