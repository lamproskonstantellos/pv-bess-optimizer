"""Contracted-BESS foundations: phase windows (E25) and the market-
revenue base column (E25a).

The phase-window helper and the informational
``bess_market_revenue_eur`` column are the shared primitives every
contracted BESS structure (tolling, optimizer floor + share, state
support with clawback, capacity market) reads.  Locked properties:

1. E25 boundaries: active exactly on ``[year_from, year_to]``
   inclusive; ``year_to = 0`` spans through end-of-life; Year 0 is
   never in any phase.
2. E25a cent-level: the column equals the UNclamped BESS DAM margin
   plus balancing revenue net of the BSP fee — including a
   negative-margin year (no clamp on the base) — and is zero in
   Year 0.
3. The column is informational: net / discounted / cumulative columns
   are bit-identical with and without it, ``_recompute_net`` does not
   fold it in, and ``_scale_revenue`` scales it with the Revenue
   driver (price-proportional) while keeping the unit factor a no-op.
"""

from __future__ import annotations

import pandas as pd
import pytest

from pvbess_opt.economics import _contract_phase, build_yearly_cashflow

N_YEARS = 3
DAM_INFL = 0.02


def _econ(**overrides) -> dict:
    econ = {
        "project_lifecycle_years": N_YEARS,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": DAM_INFL * 100.0,
        "bm_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 500.0,
        "capex_bess_eur_per_kwh": 100.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 5.0,
        "opex_bess_eur_per_kw": 5.0,
        "pv_degradation_year1_pct": 0.0,
        "pv_degradation_annual_pct": 0.0,
        "bess_degradation_annual_pct": 0.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
        "balancing_aggregator_fee_pct_revenue": 10.0,
    }
    econ.update(overrides)
    return econ


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}


def _kpis(*, dam_bess_margin: float) -> dict:
    """Year-1 KPI dict whose BESS DAM margin is exactly the argument."""
    return {
        "profit_load_from_pv_eur": 10_000.0,
        "profit_load_from_bess_eur": 5_000.0,
        "profit_export_from_pv_eur": 20_000.0,
        "profit_export_from_bess_eur": dam_bess_margin + 8_000.0,
        "expense_charge_bess_grid_eur": 8_000.0,
        "profit_total_eur": 35_000.0 + dam_bess_margin + 8_000.0 - 8_000.0,
        "bm_total_capacity_revenue_eur": 12_000.0,
        "bm_total_activation_revenue_eur": 4_000.0,
    }


# ---------------------------------------------------------------------------
# 1. E25 boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("y", "y_from", "y_to", "expect"), [
    (1, 1, 3, True),
    (3, 1, 3, True),
    (4, 1, 3, False),
    (2, 3, 5, False),
    (3, 3, 5, True),
    (5, 3, 0, True),      # year_to = 0 -> end-of-life (n_years = 5)
    (6, 3, 0, False),     # past end-of-life
    (0, 1, 0, False),     # Year 0 never in phase
    (-1, 1, 0, False),
])
def test_phase_window_boundaries(y, y_from, y_to, expect):
    n_years = 5
    assert _contract_phase(y, y_from, y_to, n_years) is expect


# ---------------------------------------------------------------------------
# 2. E25a cent-level lock
# ---------------------------------------------------------------------------


def _expected_base(margin: float, y: int) -> float:
    dam_leg = margin * (1.0 + DAM_INFL) ** (y - 1)
    balancing_gross = 12_000.0 + 4_000.0
    bsp_fee = -0.10 * balancing_gross
    return dam_leg + balancing_gross + bsp_fee


@pytest.mark.parametrize("margin", [30_000.0, -7_500.0])
def test_market_revenue_base_cent_level(margin):
    cf = build_yearly_cashflow(
        _kpis(dam_bess_margin=margin), _econ(), _caps(),
    ).set_index("project_year")
    assert float(cf.loc[0, "bess_market_revenue_eur"]) == 0.0
    for y in range(1, N_YEARS + 1):
        assert float(
            cf.loc[y, "bess_market_revenue_eur"]
        ) == pytest.approx(_expected_base(margin, y), abs=0.01), y


def test_negative_margin_base_is_unclamped():
    """Unlike the E13d optimizer fee, the E25a base keeps the sign."""
    cf = build_yearly_cashflow(
        _kpis(dam_bess_margin=-50_000.0),
        _econ(balancing_aggregator_fee_pct_revenue=0.0),
        _caps(),
    ).set_index("project_year")
    assert float(cf.loc[1, "bess_market_revenue_eur"]) == pytest.approx(
        -50_000.0 + 16_000.0, abs=0.01,
    )


def test_base_rides_the_dam_trajectory():
    """E25a uses the same g_dam series as the DAM revenue (E24)."""
    decline = {"revenue_dam": {"mode": "replace",
                               "values": [1.0, 0.5, 0.25]}}
    cf = build_yearly_cashflow(
        _kpis(dam_bess_margin=10_000.0),
        _econ(trajectories=decline,
              balancing_aggregator_fee_pct_revenue=0.0),
        _caps(),
    ).set_index("project_year")
    assert float(cf.loc[2, "bess_market_revenue_eur"]) == pytest.approx(
        10_000.0 * 0.5 + 16_000.0, abs=0.01,
    )


# ---------------------------------------------------------------------------
# 3. Informational-column contract
# ---------------------------------------------------------------------------


def test_column_has_zero_financial_impact():
    cf = build_yearly_cashflow(
        _kpis(dam_bess_margin=30_000.0), _econ(), _caps(),
    )
    without = cf.drop(columns=["bess_market_revenue_eur"])
    # Rebuild net from the frame's own components — must match exactly.
    from pvbess_opt.sensitivity import _recompute_net

    recomputed = _recompute_net(without.copy())
    pd.testing.assert_series_equal(
        recomputed["net_cashflow_eur"], cf["net_cashflow_eur"],
    )
    pd.testing.assert_series_equal(
        recomputed["discounted_cf_eur"], cf["discounted_cf_eur"],
    )


def test_recompute_net_ignores_the_base_column():
    from pvbess_opt.sensitivity import _recompute_net

    cf = build_yearly_cashflow(
        _kpis(dam_bess_margin=30_000.0), _econ(), _caps(),
    )
    poisoned = cf.copy()
    poisoned["bess_market_revenue_eur"] = 1.0e9
    recomputed = _recompute_net(poisoned)
    pd.testing.assert_series_equal(
        recomputed["net_cashflow_eur"], cf["net_cashflow_eur"],
    )


def test_scale_revenue_scales_the_base_and_unit_is_noop():
    from pvbess_opt.sensitivity import _scale_revenue

    cf = build_yearly_cashflow(
        _kpis(dam_bess_margin=30_000.0),
        _econ(aggregator_fee_pct_revenue=5.0),
        _caps(),
    )
    pd.testing.assert_frame_equal(_scale_revenue(cf, 1.0), cf)
    scaled = _scale_revenue(cf, 1.1)
    pd.testing.assert_series_equal(
        scaled["bess_market_revenue_eur"],
        cf["bess_market_revenue_eur"] * 1.1,
    )
