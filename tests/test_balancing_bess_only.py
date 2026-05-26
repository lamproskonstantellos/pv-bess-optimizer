"""Regression test for the BESS-capacity-only balancing contract.

The balancing block of the MILP and every ``bm_*`` KPI must be
structurally bound to BESS power capacity. When the project has no
BESS (``bess_power_kw == 0``) the optimizer must not emit any
``bm_reservation_*_kw`` columns and every balancing revenue KPI must
be exactly zero — even when ``balancing_enabled`` is True.

See the module docstring of :mod:`pvbess_opt.balancing` for the
formal contract this test enforces.
"""

from __future__ import annotations

from pvbess_opt.balancing import PRODUCTS_ALL, PRODUCTS_WITH_ACTIVATION
from pvbess_opt.io import BALANCING_SHEET_DEFAULTS
from pvbess_opt.kpis import _compute_balancing_kpis
from pvbess_opt.optimization import run_scenario


def _balancing_on_no_bess(params: dict) -> dict:
    out = dict(params)
    out["bess_power_kw"] = 0.0
    out["bess_capacity_kwh"] = 0.0
    out["allow_bess_grid_charging"] = False
    bm = dict(BALANCING_SHEET_DEFAULTS, balancing_enabled=True)
    bm["bm_settlement_minutes"] = int(out.get("dt_minutes", 60))
    out["balancing"] = bm
    return out


def test_pv_only_with_balancing_enabled_solves(short_params, short_ts):
    """A PV-only project with balancing_enabled=True must solve."""
    p = _balancing_on_no_bess(short_params)
    res, _ = run_scenario(p, short_ts)
    assert not res.empty


def test_pv_only_dispatch_has_no_reservation_columns(short_params, short_ts):
    """No ``bm_reservation_*_kw`` columns when BESS is absent."""
    p = _balancing_on_no_bess(short_params)
    res, _ = run_scenario(p, short_ts)
    assert not any(c.startswith("bm_reservation_") for c in res.columns)


def test_pv_only_balancing_kpis_are_all_zero(short_params, short_ts):
    """Every ``bm_*_revenue_eur`` KPI is exactly 0 when BESS is absent.

    Mirrors the BESS-capacity-only contract: with no BESS to reserve,
    the optimizer never emits reservation columns and the KPI helper
    must short-circuit to zeros for every product, every aggregate, and
    every derived energy / share figure.
    """
    p = _balancing_on_no_bess(short_params)
    res, _ = run_scenario(p, short_ts)
    out = _compute_balancing_kpis(res, p)

    for product in PRODUCTS_ALL:
        assert out[f"bm_{product}_capacity_revenue_eur"] == 0.0
        assert out[f"bm_reservation_avg_kw_{product}"] == 0.0
    for product in PRODUCTS_WITH_ACTIVATION:
        assert out[f"bm_{product}_activation_revenue_eur"] == 0.0
    assert out["bm_total_capacity_revenue_eur"] == 0.0
    assert out["bm_total_activation_revenue_eur"] == 0.0
    assert out["bm_total_balancing_revenue_eur"] == 0.0
    assert out["bm_expected_activation_energy_up_kwh"] == 0.0
    assert out["bm_expected_activation_energy_dn_kwh"] == 0.0
    assert out["bm_revenue_share_pct"] == 0.0
