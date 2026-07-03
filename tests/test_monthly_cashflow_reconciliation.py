"""Monthly cashflow must reconcile to the yearly cashflow row-for-row.

Previously ``derive_monthly_cashflow`` summed only DAM + retail per-step
EUR columns and pulled ``yearly_cf['revenue_eur']`` (which excludes
balancing). Sum-of-monthly Year-1 ``net_cashflow_eur`` therefore
diverged from yearly by the full ``balancing_revenue_eur`` total, and
the drift inflated with ``bm_inflation_pct`` over the project lifetime.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from pvbess_opt.economics import (
    build_yearly_cashflow,
    derive_monthly_cashflow,
)


def _econ() -> dict:
    return {
        "project_lifecycle_years": 5,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 1.0,
        "retail_inflation_pct": 2.0,
        "dam_inflation_pct": 0.0,
        "bm_inflation_pct": 2.0,
        "capex_pv_eur_per_kw": 525.0,
        "capex_bess_eur_per_kwh": 200.0,
        "devex_pv_eur_per_kw": 60.0,
        "devex_bess_eur_per_kw": 30.0,
        "opex_pv_eur_per_kwp": 7.0,
        "opex_bess_eur_per_kw": 14.0,
        "pv_degradation_year1_pct": 2.5,
        "pv_degradation_annual_pct": 0.55,
        "bess_degradation_annual_pct": 2.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 50.0,
        "aggregator_fee_pct_revenue": 5.0,
    }


def _capacities() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 1000.0, "bess_kwh": 4000.0}


def _kpis_with_balancing() -> dict:
    """Year-1 KPI dict that splits revenue across retail/DAM and adds a
    canonical balancing total.  Numbers chosen so each stream is
    non-zero and distinguishable in the reconciliation tests."""
    return {
        "profit_total_eur": 100_000.0,
        "profit_load_from_pv_eur": 30_000.0,
        "profit_load_from_bess_eur": 10_000.0,
        "profit_export_from_pv_eur": 40_000.0,
        "profit_export_from_bess_eur": 20_000.0,
        "expense_charge_bess_grid_eur": 0.0,
        "bm_total_capacity_revenue_eur": 15_000.0,
        "bm_total_activation_revenue_eur": 5_000.0,
        "bess_total_discharge_mwh": 1_000.0,
    }


def _make_res_frame(
    n: int = 35040,
    *,
    reservation_kw_per_step: np.ndarray | None = None,
) -> pd.DataFrame:
    """Synthetic Year-1 dispatch frame with the per-step economic
    columns and optional ``bm_reservation_<product>_kw`` columns."""
    timestamps = pd.date_range("2026-01-01", periods=n, freq="15min")
    base = pd.DataFrame({
        "timestamp": timestamps,
        "pv_kwh": np.ones(n) * 100.0,
        "profit_load_from_pv_eur": np.ones(n) * (30_000.0 / n),
        "profit_load_from_bess_eur": np.ones(n) * (10_000.0 / n),
        "profit_export_from_pv_eur": np.ones(n) * (40_000.0 / n),
        "profit_export_from_bess_eur": np.ones(n) * (20_000.0 / n),
        "expense_charge_bess_grid_eur": np.zeros(n),
    })
    products = ("fcr", "afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn")
    if reservation_kw_per_step is not None:
        for p in products:
            base[f"bm_reservation_{p}_kw"] = reservation_kw_per_step
    return base


def _build_year1_cf_and_res(**econ_overrides) -> tuple[pd.DataFrame, pd.DataFrame]:
    econ = {**_econ(), **econ_overrides}
    yearly_cf = build_yearly_cashflow(_kpis_with_balancing(), econ, _capacities())
    res = _make_res_frame()
    return yearly_cf, res, econ  # type: ignore[return-value]


def test_monthly_net_sums_to_yearly_balancing_enabled():
    """Sum of monthly net_cashflow_eur == yearly net for every operating year."""
    yearly_cf, res, econ = _build_year1_cf_and_res()  # type: ignore[misc]
    monthly_cf, _ = derive_monthly_cashflow(res, yearly_cf, econ)

    monthly_by_year = monthly_cf.groupby("project_year")["net_cashflow_eur"].sum()
    yearly_by_year = yearly_cf.set_index("project_year")["net_cashflow_eur"]
    for y, monthly_total in monthly_by_year.items():
        yearly_total = float(yearly_by_year.loc[y])
        assert abs(monthly_total - yearly_total) < 0.05, (
            f"Year {y}: monthly net {monthly_total} != yearly net {yearly_total}"
        )


def test_monthly_revenue_balancing_fee_sum_to_yearly():
    """For every operating year, sum of monthly column == yearly column."""
    yearly_cf, res, econ = _build_year1_cf_and_res()  # type: ignore[misc]
    monthly_cf, _ = derive_monthly_cashflow(res, yearly_cf, econ)

    yearly_indexed = yearly_cf.set_index("project_year")
    for col in ("revenue_eur", "balancing_revenue_eur", "aggregator_fee_eur"):
        assert col in monthly_cf.columns, f"missing column {col}"
        monthly_by_year = monthly_cf.groupby("project_year")[col].sum()
        for y, mtot in monthly_by_year.items():
            ytot = float(yearly_indexed.loc[y, col])
            assert abs(mtot - ytot) < 0.05, (
                f"Year {y}, column {col}: monthly {mtot} != yearly {ytot}"
            )


def test_monthly_balancing_share_matches_reservation_pattern():
    """When reservations concentrate in winter months, the per-month
    balancing share must follow that shape (not 1/12)."""
    yearly_cf, _, econ = _build_year1_cf_and_res()  # type: ignore[misc]

    n = 35040
    timestamps = pd.date_range("2026-01-01", periods=n, freq="15min")
    # Reservations only in January (month 1).
    res_kw = np.where(timestamps.month == 1, 500.0, 0.0)
    res = _make_res_frame(n=n, reservation_kw_per_step=res_kw)

    monthly_cf, _ = derive_monthly_cashflow(res, yearly_cf, econ)
    y1 = monthly_cf.loc[monthly_cf["project_year"] == 1].sort_values("period")
    jan_balancing = float(y1.loc[y1["period"] == 1, "balancing_revenue_eur"].iloc[0])
    feb_balancing = float(y1.loc[y1["period"] == 2, "balancing_revenue_eur"].iloc[0])

    yearly_y1_balancing = float(
        yearly_cf.loc[yearly_cf["project_year"] == 1, "balancing_revenue_eur"].iloc[0]
    )
    # All balancing should land in January because that's the only
    # month with non-zero reservation.
    assert abs(jan_balancing - yearly_y1_balancing) < 0.05, (
        f"January balancing {jan_balancing} != yearly Y1 {yearly_y1_balancing}"
    )
    assert abs(feb_balancing) < 0.05, (
        f"February balancing {feb_balancing} should be ~0 (no reservation)"
    )


def test_monthly_balancing_share_falls_back_to_flat_when_no_reservation():
    """All-zero reservation columns trigger the 1/12 fallback."""
    yearly_cf, _, econ = _build_year1_cf_and_res()  # type: ignore[misc]
    n = 35040
    res = _make_res_frame(n=n, reservation_kw_per_step=np.zeros(n))

    monthly_cf, _ = derive_monthly_cashflow(res, yearly_cf, econ)
    y1 = monthly_cf.loc[monthly_cf["project_year"] == 1].sort_values("period")
    yearly_y1_balancing = float(
        yearly_cf.loc[yearly_cf["project_year"] == 1, "balancing_revenue_eur"].iloc[0]
    )
    expected_per_month = yearly_y1_balancing / 12.0
    for m, val in zip(y1["period"], y1["balancing_revenue_eur"], strict=False):
        assert abs(float(val) - expected_per_month) < 0.05, (
            f"Month {m}: balancing {val} != flat {expected_per_month}"
        )


def test_monthly_balancing_share_falls_back_when_no_columns():
    """Reservation columns absent entirely → 1/12 fallback."""
    yearly_cf, _, econ = _build_year1_cf_and_res()  # type: ignore[misc]
    # _make_res_frame with default reservation_kw_per_step=None gives
    # no bm_reservation columns.
    res = _make_res_frame()
    monthly_cf, _ = derive_monthly_cashflow(res, yearly_cf, econ)
    y1 = monthly_cf.loc[monthly_cf["project_year"] == 1].sort_values("period")
    yearly_y1_balancing = float(
        yearly_cf.loc[yearly_cf["project_year"] == 1, "balancing_revenue_eur"].iloc[0]
    )
    expected_per_month = yearly_y1_balancing / 12.0
    for m, val in zip(y1["period"], y1["balancing_revenue_eur"], strict=False):
        assert abs(float(val) - expected_per_month) < 0.05, (
            f"Month {m}: balancing {val} != flat {expected_per_month}"
        )
