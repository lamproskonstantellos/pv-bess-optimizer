"""Adversarial / degenerate-config robustness for the balancing-aggregator
(BSP) fee.

Every case must fail loudly with an actionable message OR degrade per a
documented contract — never an opaque crash or a silent wrong number.
"""

from __future__ import annotations

import logging

import numpy as np
import pytest

from pvbess_opt.economics import build_yearly_cashflow, compute_financial_kpis
from pvbess_opt.io import _SHEET_DEFAULTS, _parse_value, validate_workbook_params

_KEY = "balancing_aggregator_fee_pct_revenue"


def _econ(**kw):
    base = dict(
        project_lifecycle_years=5, project_start_year=2026,
        discount_rate_pct=7.0, opex_inflation_pct=0.0,
        retail_inflation_pct=0.0, dam_inflation_pct=0.0, bm_inflation_pct=0.0,
        pv_degradation_year1_pct=0.0, pv_degradation_annual_pct=0.0,
        bess_degradation_annual_pct=0.0, bess_degradation_pct_per_cycle=0.0,
        capex_pv_eur_per_kw=0.0, capex_bess_eur_per_kw=1.0,
        devex_pv_eur_per_kw=0.0, devex_bess_eur_per_kw=0.0,
        site_capex_eur=0.0, site_devex_eur=0.0,
        opex_pv_eur_per_kwp=0.0, opex_bess_eur_per_kw=0.0,
        aggregator_fee_pct_revenue=0.0,
        bess_replacement_year=0, bess_replacement_cost_pct=0.0,
    )
    base.update(kw)
    return base


def _caps():
    return {"pv_kwp": 0.0, "bess_kw": 1000.0, "bess_kwh": 4000.0}


# ---------------------------------------------------------------------------
# Degenerate configs — no phantom fee
# ---------------------------------------------------------------------------


def test_fee_is_noop_when_balancing_absent():
    """Fee set but NO balancing revenue (balancing off / no bids): the fee
    column must be identically zero — never a phantom deduction."""
    y1_no_balancing = {
        "profit_load_from_pv_eur": 300.0,
        "profit_export_from_pv_eur": 200.0,
        "bm_total_capacity_revenue_eur": 0.0,
        "bm_total_activation_revenue_eur": 0.0,
        "bess_total_discharge_mwh": 0.0,
    }
    df = build_yearly_cashflow(
        y1_no_balancing, _econ(balancing_aggregator_fee_pct_revenue=30.0), _caps(),
    )
    assert (df["balancing_aggregator_fee_eur"].abs() < 1e-12).all()
    # And the net equals the fee-off net (no spurious change).
    off = build_yearly_cashflow(
        y1_no_balancing, _econ(balancing_aggregator_fee_pct_revenue=0.0), _caps(),
    )
    np.testing.assert_allclose(
        df["net_cashflow_eur"].to_numpy(), off["net_cashflow_eur"].to_numpy(),
        rtol=1e-12, atol=1e-9,
    )


def test_fee_is_noop_for_pv_only_project():
    """A BESS-less (pv_only) project has no balancing revenue, so the fee is
    a no-op even when set."""
    y1 = {
        "profit_load_from_pv_eur": 100.0,
        "profit_export_from_pv_eur": 50.0,
        "bm_total_capacity_revenue_eur": 0.0,
        "bm_total_activation_revenue_eur": 0.0,
        "bess_total_discharge_mwh": 0.0,
    }
    caps_pv_only = {"pv_kwp": 1000.0, "bess_kw": 0.0, "bess_kwh": 0.0}
    df = build_yearly_cashflow(
        y1, _econ(capex_bess_eur_per_kw=0.0,
                  balancing_aggregator_fee_pct_revenue=25.0), caps_pv_only,
    )
    assert (df["balancing_aggregator_fee_eur"].abs() < 1e-12).all()


# ---------------------------------------------------------------------------
# Malformed values — coerce-with-warning, never crash
# ---------------------------------------------------------------------------


def test_malformed_fee_value_falls_back_to_default(caplog):
    """A non-numeric workbook cell for the fee coerces to the default (0.0)
    with a warning — the same contract as every other float key."""
    with caplog.at_level(logging.WARNING):
        out = _parse_value(_KEY, "not-a-number", _SHEET_DEFAULTS["economics"][_KEY])
    assert out == 0.0
    assert any("could not be parsed" in r.getMessage() for r in caplog.records)


def test_nan_fee_value_falls_back_to_default():
    """An empty cell (NaN) resolves to the default 0.0 (fee off)."""
    out = _parse_value(_KEY, float("nan"), _SHEET_DEFAULTS["economics"][_KEY])
    assert out == 0.0


@pytest.mark.parametrize("bad", [float("inf"), 1e9, 100.0001])
def test_out_of_range_or_infinite_fee_rejected(bad):
    """inf / >100 are rejected by validate_workbook_params (loud, actionable)."""
    typed = {s: dict(d) for s, d in _SHEET_DEFAULTS.items()}
    typed["economics"][_KEY] = bad
    with pytest.raises(ValueError, match=_KEY):
        validate_workbook_params(typed, dt_minutes=15)


# ---------------------------------------------------------------------------
# Determinism — the economics layer is pure
# ---------------------------------------------------------------------------


def test_fee_path_is_deterministic():
    """Repeated builds with the fee on yield byte-identical frames + KPIs
    (no dict-order / RNG / wall-clock dependence in the economics layer)."""
    y1 = {
        "profit_load_from_pv_eur": 300.0,
        "profit_export_from_pv_eur": 200.0,
        "bm_total_capacity_revenue_eur": 100.0,
        "bm_total_activation_revenue_eur": 50.0,
        "bess_total_discharge_mwh": 0.0,
    }
    econ = _econ(balancing_aggregator_fee_pct_revenue=20.0)
    a = build_yearly_cashflow(y1, econ, _caps())
    b = build_yearly_cashflow(y1, econ, _caps())
    assert a.equals(b)
    ka = compute_financial_kpis(a, econ)
    kb = compute_financial_kpis(b, econ)
    assert ka.keys() == kb.keys()
    for k in ka:
        va, vb = ka[k], kb[k]
        if isinstance(va, float) and np.isnan(va):
            assert isinstance(vb, float) and np.isnan(vb), k
        else:
            assert va == vb, k
