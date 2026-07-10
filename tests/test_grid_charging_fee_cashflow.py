"""Grid-charging fee cashflow line (Eq. E27).

The Year-1 wedge actually paid (KPI ``expense_grid_charging_fee_eur``,
already availability-derated and exemption-aware) projects over the
lifecycle as its own signed column: flat regulated rate, charged volume
fading on the BESS capacity curve.  Locked properties:

1. Cent-level: ``grid_charging_fee_eur`` = -fee_1 x bess_factor per
   operating year, zero in Year 0.
2. Zero-default bit-identity: a KPI dict without the fee key produces
   a bit-identical frame to fee = 0 (all-zero column, unchanged net).
3. No double count on the no-breakdown fallback: profit_total already
   nets the fee, so the fallback gross adds it back and the column
   carries the deduction alone.
4. Monthly reconciliation: the fee allocates on the Year-1 charging
   shape and monthly sums equal the yearly rows exactly.
5. Sensitivity: ``_recompute_net`` folds the column; the Revenue driver
   does NOT scale it (regulated rate x volume); lifetime total lands in
   the KPI dict and the SUMMARY optional-keys registry.
"""

from __future__ import annotations

import pandas as pd
import pytest

from pvbess_opt.economics import (
    build_yearly_cashflow,
    compute_financial_kpis,
    derive_monthly_cashflow,
)

N_YEARS = 4
FEE_1 = 12_000.0


def _econ(**overrides) -> dict:
    econ = {
        "project_lifecycle_years": N_YEARS,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 500.0,
        "capex_bess_eur_per_kwh": 100.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 5.0,
        "opex_bess_eur_per_kw": 5.0,
        "pv_degradation_year1_pct": 0.0,
        "pv_degradation_annual_pct": 0.0,
        "bess_degradation_annual_pct": 3.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
    }
    econ.update(overrides)
    return econ


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}


def _kpis(fee: float | None = FEE_1) -> dict:
    base = {
        "profit_load_from_pv_eur": 50_000.0,
        "profit_load_from_bess_eur": 10_000.0,
        "profit_export_from_pv_eur": 30_000.0,
        "profit_export_from_bess_eur": 40_000.0,
        "expense_charge_bess_grid_eur": 15_000.0,
        "profit_total_eur": 115_000.0 - (fee or 0.0),
        "bess_total_discharge_mwh": 400.0,
    }
    if fee is not None:
        base["expense_grid_charging_fee_eur"] = fee
    return base


# ---------------------------------------------------------------------------
# 1+2. Cent-level projection + zero-default bit-identity
# ---------------------------------------------------------------------------


def test_column_projects_on_the_bess_curve():
    cf = build_yearly_cashflow(_kpis(), _econ(), _caps())
    indexed = cf.set_index("project_year")
    assert float(indexed.loc[0, "grid_charging_fee_eur"]) == 0.0
    factors = indexed.loc[1:, "bess_capacity_factor"]
    for y in range(1, N_YEARS + 1):
        assert float(
            indexed.loc[y, "grid_charging_fee_eur"]
        ) == pytest.approx(-FEE_1 * float(factors.loc[y]), abs=0.01), y
    # The net carries the deduction.
    net_with = float(indexed.loc[1, "net_cashflow_eur"])
    base = build_yearly_cashflow(
        _kpis(fee=0.0), _econ(), _caps(),
    ).set_index("project_year")
    assert net_with == pytest.approx(
        float(base.loc[1, "net_cashflow_eur"]) - FEE_1, abs=0.01,
    )


def test_missing_kpi_equals_zero_fee_bitwise():
    without_key = build_yearly_cashflow(_kpis(fee=None), _econ(), _caps())
    zero_fee = build_yearly_cashflow(_kpis(fee=0.0), _econ(), _caps())
    pd.testing.assert_frame_equal(without_key, zero_fee)
    assert (without_key["grid_charging_fee_eur"] == 0.0).all()


# ---------------------------------------------------------------------------
# 3. Fallback carve-out (no double count)
# ---------------------------------------------------------------------------


def test_no_breakdown_fallback_does_not_double_count():
    """With only profit_total (which nets the fee) the fallback adds the
    fee back to the gross, so the column carries the deduction ONCE."""
    kpis_min = {
        "profit_total_eur": 100_000.0 - FEE_1,
        "expense_grid_charging_fee_eur": FEE_1,
    }
    cf = build_yearly_cashflow(kpis_min, _econ(), _caps())
    y1 = cf.set_index("project_year").loc[1]
    assert float(y1["revenue_eur"]) == pytest.approx(100_000.0, abs=0.01)
    assert float(y1["grid_charging_fee_eur"]) == pytest.approx(
        -FEE_1, abs=0.01,
    )
    # Net year 1 = gross - fee + opex.
    opex_1 = -(5.0 * 1000.0 + 5.0 * 500.0)
    assert float(y1["net_cashflow_eur"]) == pytest.approx(
        100_000.0 - FEE_1 + opex_1, abs=0.01,
    )


# ---------------------------------------------------------------------------
# 4. Monthly reconciliation on the charging shape
# ---------------------------------------------------------------------------


def test_monthly_reconciles_and_follows_charging_shape():
    import numpy as np

    from tests.test_monthly_cashflow_reconciliation import _make_res_frame

    res = _make_res_frame()
    # Winter-only charging fee shape (January).
    ts_month = pd.to_datetime(res["timestamp"]).dt.month
    res["expense_grid_charging_fee_eur"] = np.where(
        ts_month == 1, FEE_1 / (ts_month == 1).sum(), 0.0,
    )
    yearly = build_yearly_cashflow(_kpis(), _econ(), _caps())
    monthly, quarterly = derive_monthly_cashflow(res, yearly, _econ())

    yearly_indexed = yearly.set_index("project_year")
    for y in range(1, N_YEARS + 1):
        month_sum = float(
            monthly.loc[
                monthly["project_year"] == y, "grid_charging_fee_eur"
            ].sum()
        )
        assert month_sum == pytest.approx(
            float(yearly_indexed.loc[y, "grid_charging_fee_eur"]), abs=0.01,
        ), y
        net_sum = float(
            monthly.loc[
                monthly["project_year"] == y, "net_cashflow_eur"
            ].sum()
        )
        assert net_sum == pytest.approx(
            float(yearly_indexed.loc[y, "net_cashflow_eur"]), abs=0.05,
        ), y
    # The whole Year-1 fee lands in January (the charging shape).
    y1 = monthly.loc[monthly["project_year"] == 1]
    jan = float(
        y1.loc[y1["period"] == 1, "grid_charging_fee_eur"].iloc[0]
    )
    assert jan == pytest.approx(-FEE_1, abs=0.01)
    assert "grid_charging_fee_eur" in quarterly.columns


# ---------------------------------------------------------------------------
# 5. Sensitivity + lifetime total + SUMMARY registry
# ---------------------------------------------------------------------------


def test_recompute_net_folds_the_fee_and_revenue_driver_skips_it():
    from pvbess_opt.sensitivity import _recompute_net, _scale_revenue

    cf = build_yearly_cashflow(
        _kpis(), _econ(aggregator_fee_pct_revenue=5.0), _caps(),
    )
    recomputed = _recompute_net(cf.copy())
    pd.testing.assert_series_equal(
        recomputed["net_cashflow_eur"], cf["net_cashflow_eur"],
    )
    pd.testing.assert_frame_equal(_scale_revenue(cf, 1.0), cf)
    scaled = _scale_revenue(cf, 1.2)
    pd.testing.assert_series_equal(
        scaled["grid_charging_fee_eur"], cf["grid_charging_fee_eur"],
    )


def test_lifetime_total_and_summary_registry():
    from pvbess_opt.io import _SUMMARY_OPTIONAL_FINANCIAL_KEYS

    cf = build_yearly_cashflow(_kpis(), _econ(), _caps())
    fin = compute_financial_kpis(cf, _econ())
    expected = float(
        cf.loc[cf["project_year"] >= 1, "grid_charging_fee_eur"].sum()
    )
    assert fin["total_grid_charging_fee_eur_lifecycle"] == pytest.approx(
        expected, abs=0.01,
    )
    assert ("total_grid_charging_fee_eur_lifecycle",
            "Lifetime grid-charging fee [EUR]") in (
        _SUMMARY_OPTIONAL_FINANCIAL_KEYS
    )


def test_theme_registers_the_band_label():
    from pvbess_opt.theme import (
        FINANCIAL_COLORS,
        FINANCIAL_LABELS,
        FINANCIAL_LEGEND_ORDER,
    )

    assert "Grid-charging fee" in FINANCIAL_LABELS
    assert "Grid-charging fee" in FINANCIAL_LEGEND_ORDER
    assert FINANCIAL_COLORS["grid_charging_fee"] == "#F06292"
    # Unique hex within the financial palette.
    hexes = list(FINANCIAL_COLORS.values())
    assert hexes.count("#F06292") == 1
