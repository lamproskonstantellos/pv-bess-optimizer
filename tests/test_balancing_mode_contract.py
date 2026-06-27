"""Balancing-market participation is valid in BOTH regulatory regimes.

Production-readiness contract (owner-confirmed): FCR / aFRR / mFRR
participation is an opt-in BESS feature available in ``self_consumption``
*and* ``merchant`` mode — it is TSO-settled (carries no aggregator fee) and
respects the SOC safety buffer in either regime.  The activation gate
(``optimization._resolve_balancing_inputs``) keys on
``balancing_enabled and bess_present`` only, never on ``mode``.

These tests pin the both-mode behaviour so the contract cannot silently
regress to merchant-only (the README frames balancing under the merchant
description for narrative reasons, but it is not merchant-exclusive).
"""

from __future__ import annotations

import pytest

from pvbess_opt.kpis import compute_kpis
from pvbess_opt.optimization import build_model, run_scenario
from tests._balancing_helpers import _balancing_on

# Symbols the balancing extension must attach to a built model (the same
# set pinned by tests/test_logic_spec_conformance.py).
_BM_SYMBOLS = (
    "r_balancing",
    "BM_POWER_UP",
    "BM_POWER_DN",
    "BM_SOC_UP",
    "BM_SOC_DN",
    "balancing_revenue_expr",
)


@pytest.mark.parametrize("mode", ["self_consumption", "merchant"])
def test_balancing_symbols_present_in_both_modes(
    mode, short_params, short_params_merchant, short_ts,
):
    """The reservation variable, power-budget / SOC-headroom constraints
    and the expected-revenue expression attach to a balancing-enabled
    model in either regulatory mode."""
    params = short_params if mode == "self_consumption" else short_params_merchant
    model = build_model(_balancing_on(params), short_ts)
    missing = [s for s in _BM_SYMBOLS if not hasattr(model, s)]
    assert not missing, f"{mode}: model missing balancing symbols {missing}"


@pytest.mark.parametrize("mode", ["self_consumption", "merchant"])
def test_balancing_revenue_settles_in_both_modes(
    mode, short_params, short_params_merchant, short_ts,
):
    """With the feature on and a BESS present, expected balancing revenue
    is booked in both regimes (positive, TSO-settled)."""
    params = _balancing_on(
        short_params if mode == "self_consumption" else short_params_merchant
    )
    res, _solver = run_scenario(
        params, short_ts, solver_name="highs", mip_gap=0.001,
        time_limit_seconds=60,
    )
    kpis = compute_kpis(res, params)
    assert kpis["bm_total_balancing_revenue_eur"] > 0.0, (
        f"{mode}: expected positive balancing revenue with the feature on, "
        f"got {kpis['bm_total_balancing_revenue_eur']!r}"
    )


@pytest.mark.parametrize("mode", ["self_consumption", "merchant"])
def test_balancing_off_books_zero_in_both_modes(
    mode, short_params, short_params_merchant, short_ts,
):
    """The default (feature off) books zero balancing revenue in both
    regimes — the opt-in switch is the only thing that turns it on."""
    params = short_params if mode == "self_consumption" else short_params_merchant
    res, _solver = run_scenario(
        params, short_ts, solver_name="highs", mip_gap=0.001,
        time_limit_seconds=60,
    )
    kpis = compute_kpis(res, params)
    assert kpis["bm_total_balancing_revenue_eur"] == 0.0
