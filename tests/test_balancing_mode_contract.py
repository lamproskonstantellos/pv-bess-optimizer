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

import logging

import pytest

from pvbess_opt.kpis import compute_kpis
from pvbess_opt.optimization import build_model, run_scenario
from pvbess_opt.pipeline import _warn_self_consumption_balancing
from tests._balancing_helpers import _balancing_on

_GUARD_MARKER = "[balancing-in-self_consumption]"

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


# ---------------------------------------------------------------------------
# Decision 1 — self_consumption balancing guardrail (one warning, that mode
# only).  The both-mode contract above stays valid; this pins the caveat.
# ---------------------------------------------------------------------------


def test_guardrail_warns_in_self_consumption(caplog, short_params):
    """Balancing on + BESS present + self_consumption ⇒ exactly one warning."""
    params = _balancing_on(short_params)
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.pipeline"):
        _warn_self_consumption_balancing(params)
    hits = [r for r in caplog.records if _GUARD_MARKER in r.getMessage()]
    assert len(hits) == 1, f"expected one guardrail warning, got {len(hits)}"


def test_guardrail_silent_in_merchant(caplog, short_params_merchant):
    """The same feature in merchant mode emits NO guardrail warning."""
    params = _balancing_on(short_params_merchant)
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.pipeline"):
        _warn_self_consumption_balancing(params)
    assert not [r for r in caplog.records if _GUARD_MARKER in r.getMessage()]


def test_guardrail_silent_when_balancing_off(caplog, short_params):
    """No warning when balancing is not enabled (the default)."""
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.pipeline"):
        _warn_self_consumption_balancing(short_params)
    assert not [r for r in caplog.records if _GUARD_MARKER in r.getMessage()]


def test_guardrail_silent_without_bess(caplog, short_params):
    """A balancing-on but BESS-less (pv_only) self_consumption run is a
    no-op for balancing, so it must not warn."""
    params = _balancing_on(short_params)
    params["bess_power_kw"] = 0.0
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.pipeline"):
        _warn_self_consumption_balancing(params)
    assert not [r for r in caplog.records if _GUARD_MARKER in r.getMessage()]
