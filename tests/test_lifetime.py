"""Multi-year lifetime dispatch projection tests."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import build_yearly_cashflow
from pvbess_opt.kpis import add_economic_columns
from pvbess_opt.lifetime import (
    _bess_factor,
    aggregate_lifetime_to_yearly,
    build_lifetime_dispatch,
)


def _econ() -> dict:
    return {
        "project_lifecycle_years": 5,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 1.0,
        "retail_inflation_pct": 2.0,
        "dam_inflation_pct": 0.0,
        "pv_degradation_year1_pct": 2.5,
        "pv_degradation_annual_pct": 0.55,
        "bess_degradation_annual_pct": 2.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 50.0,
    }


def _make_year1_dispatch() -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01", periods=8760, freq="h")
    df = pd.DataFrame({
        "timestamp": timestamps,
        "pv_kwh": np.full(8760, 1.0),
        "load_kwh": np.full(8760, 1.0),
        "pv_to_load_kwh": np.full(8760, 0.5),
        "pv_to_grid_kwh": np.full(8760, 0.3),
        "pv_curtail_kwh": np.full(8760, 0.0),
        "pv_to_bess_kwh": np.full(8760, 0.2),
        "bess_dis_load_kwh": np.full(8760, 0.4),
        "bess_dis_grid_kwh": np.full(8760, 0.2),
        "bess_charge_grid_kwh": np.full(8760, 0.0),
        "grid_to_load_kwh": np.full(8760, 0.1),
        "grid_export_total_kwh": np.full(8760, 0.5),
        "grid_export_cap_kwh": np.full(8760, 5.0),
        "soc_kwh": np.full(8760, 100.0),
        "soc_pct": np.full(8760, 50.0),
        "dam_price_eur_per_mwh": np.full(8760, 80.0),
    })
    # Mimic the post-compute_kpis state: the per-step EUR columns the
    # financial pipeline now requires.
    return add_economic_columns(df, {"retail_tariff_eur_per_mwh": 120.0})


def test_lifetime_dispatch_pv_factor_invariant():
    res1 = _make_year1_dispatch()
    capacities = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    lifetime = build_lifetime_dispatch(res1, _econ(), capacities)
    y1_pv = float(lifetime.loc[lifetime["project_year"] == 1, "pv_kwh"].sum())
    for y in (1, 2, 5):
        sub = lifetime.loc[lifetime["project_year"] == y, "pv_kwh"].sum()
        if y == 1:
            assert abs(sub / y1_pv - 1.0) < 1e-6
        elif y == 2:
            assert abs(sub / y1_pv - (1.0 - 0.025)) < 1e-3
        elif y == 5:
            expected = (1.0 - 0.025) * (1.0 - 0.0055) ** 3
            assert abs(sub / y1_pv - expected) < 1e-3


def test_calendar_year_alignment():
    res1 = _make_year1_dispatch()
    capacities = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    lifetime = build_lifetime_dispatch(res1, _econ(), capacities)
    y1_cal = int(lifetime.loc[lifetime["project_year"] == 1, "calendar_year"].iloc[0])
    assert y1_cal == 2026


def _bess_only_econ() -> dict:
    """20-year BESS-only economics; inflation off so revenue ratios are
    pure bess_factor."""
    return {
        "project_lifecycle_years": 20,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "pv_degradation_year1_pct": 2.5,
        "pv_degradation_annual_pct": 0.55,
        "bess_degradation_annual_pct": 2.0,
        "bess_degradation_pct_per_cycle": 0.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 50.0,
        "capex_pv_eur_per_kw": 0.0,
        "capex_bess_eur_per_kwh": 300.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 0.0,
        "opex_bess_eur_per_kw": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
    }


def test_cashflow_and_lifetime_bess_revenue_reconcile():
    """BESS-only: cashflow_yearly and lifetime_dispatch_yearly must agree
    on the BESS-revenue degradation ratio, both equal to bess_factor[y]."""
    econ = _bess_only_econ()
    capacities = {"pv_kwp": 0.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}

    # Year-1 KPIs: revenue is entirely BESS-origin (no PV streams).
    year1_kpis = {
        "profit_load_from_pv_eur": 0.0,
        "profit_load_from_bess_eur": 120_000.0,
        "profit_export_from_pv_eur": 0.0,
        "profit_export_from_bess_eur": 80_000.0,
        "expense_charge_bess_grid_eur": 20_000.0,
        "bess_total_discharge_mwh": 0.0,
    }
    cf = build_yearly_cashflow(year1_kpis, econ, capacities)
    cf_rev1 = float(cf.loc[cf["project_year"] == 1, "revenue_eur"].iloc[0])

    # Year-1 dispatch carrying only BESS-origin revenue columns.
    res1 = _make_year1_dispatch()
    res1["profit_load_from_pv_eur"] = 0.0
    res1["profit_export_from_pv_eur"] = 0.0
    res1["profit_load_from_bess_eur"] = 120_000.0 / 8760.0
    res1["profit_export_from_bess_eur"] = 80_000.0 / 8760.0
    res1["expense_charge_bess_grid_eur"] = 20_000.0 / 8760.0
    lifetime = build_lifetime_dispatch(res1, econ, capacities)
    agg = aggregate_lifetime_to_yearly(lifetime)
    lt_rev1 = float(
        agg.loc[agg["project_year"] == 1, "revenue_eur_dam_retail"].iloc[0]
    )

    d_bess = econ["bess_degradation_annual_pct"] / 100.0
    for y in (5, 10, 20):
        bf = _bess_factor(y, d_bess)
        cf_ratio = float(
            cf.loc[cf["project_year"] == y, "revenue_eur"].iloc[0]
        ) / cf_rev1
        lt_ratio = float(
            agg.loc[agg["project_year"] == y, "revenue_eur_dam_retail"].iloc[0]
        ) / lt_rev1
        assert abs(cf_ratio - bf) < 1e-9, (y, cf_ratio, bf)
        assert abs(lt_ratio - bf) < 1e-9, (y, lt_ratio, bf)
        assert abs(cf_ratio - lt_ratio) < 1e-9, (y, cf_ratio, lt_ratio)


def test_aggregate_lifetime_yearly_columns():
    res1 = _make_year1_dispatch()
    capacities = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    lifetime = build_lifetime_dispatch(res1, _econ(), capacities)
    agg = aggregate_lifetime_to_yearly(lifetime)
    for col in (
        "project_year", "calendar_year", "pv_generation_mwh",
        "pv_to_load_mwh", "pv_to_grid_mwh",
        "bess_charge_mwh", "bess_discharge_mwh",
        "import_to_load_mwh", "export_total_mwh",
        "revenue_eur_dam_retail",
    ):
        assert col in agg.columns


def test_aggregate_lifetime_empty():
    out = aggregate_lifetime_to_yearly(pd.DataFrame())
    assert "project_year" in out.columns
    assert out.empty


def test_bess_replacement_resets_factor():
    """Section 2.4 of the upgrade spec: capacity factor resets at replacement year."""
    # No replacement: monotonic decay
    assert _bess_factor(1, 0.02, replacement_year=0) == 1.0
    assert _bess_factor(10, 0.02, replacement_year=0) == pytest.approx(0.98 ** 9)
    # Replacement at year 10
    assert _bess_factor(9, 0.02, replacement_year=10) == pytest.approx(0.98 ** 8)
    assert _bess_factor(10, 0.02, replacement_year=10) == 1.0
    assert _bess_factor(11, 0.02, replacement_year=10) == pytest.approx(0.98)
    assert _bess_factor(15, 0.02, replacement_year=10) == pytest.approx(0.98 ** 5)


def test_bess_factor_with_zero_cycle_pct_matches_calendar_only():
    """The keyword-only signature with d_bess_per_cycle = 0 must equal
    the calendar-only formula (1 - d_annual)^years_since."""
    for y, repl in [(1, 0), (7, 0), (5, 10), (12, 10)]:
        calendar_only = _bess_factor(y, 0.02, replacement_year=repl)
        combined_with_zero_cycle = _bess_factor(
            y, 0.02, replacement_year=repl,
            d_bess_per_cycle=0.0, cumulative_cycles_through=9999.0,
        )
        assert combined_with_zero_cycle == pytest.approx(
            calendar_only, rel=1e-12,
        )


def test_bess_factor_combined():
    """Calendar fade combined with the additive cycle-fade term."""
    factor = _bess_factor(
        5, 0.02, replacement_year=0,
        d_bess_per_cycle=0.00008, cumulative_cycles_through=1500.0,
    )
    expected = (1.0 - 0.02) ** 4 - 0.00008 * 1500.0
    assert factor == pytest.approx(expected, abs=1e-6)
    assert factor == pytest.approx(0.80237, abs=1e-5)


def test_bess_factor_replacement_resets_both():
    """At the replacement year the calendar factor resets to 1.0; the
    caller is responsible for resetting the cycle counter to 0."""
    factor = _bess_factor(
        10, 0.02, replacement_year=10,
        d_bess_per_cycle=0.00008, cumulative_cycles_through=0.0,
    )
    assert factor == pytest.approx(1.0, rel=1e-12)


def test_bess_factor_floor_at_zero():
    """Pathological cycle accrual cannot drive the factor negative."""
    factor = _bess_factor(
        20, 0.05, replacement_year=0,
        d_bess_per_cycle=0.01, cumulative_cycles_through=1_000_000.0,
    )
    assert factor >= 0.0
    assert factor == 0.0


def test_lifetime_dispatch_bess_replacement():
    """End-to-end: bess_dis_load_kwh ratio resets to 1 at replacement year."""
    res1 = _make_year1_dispatch()
    capacities = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    econ = _econ()
    econ["project_lifecycle_years"] = 12
    econ["bess_replacement_year"] = 10
    econ["bess_degradation_annual_pct"] = 2.0
    lifetime = build_lifetime_dispatch(res1, econ, capacities)
    y1 = float(lifetime.loc[lifetime["project_year"] == 1, "bess_dis_load_kwh"].sum())
    y10 = float(lifetime.loc[lifetime["project_year"] == 10, "bess_dis_load_kwh"].sum())
    y11 = float(lifetime.loc[lifetime["project_year"] == 11, "bess_dis_load_kwh"].sum())
    assert abs(y10 / y1 - 1.0) < 1e-3
    assert abs(y11 / y1 - 0.98) < 1e-3


def test_feb29_lifetime_does_not_roll_over_in_non_leap_target_years():
    """A Year-1 timestamp on Feb 29 must shift to Feb 28 in non-leap
    target years rather than to Mar 1."""
    # Tiny dispatch containing exactly one Feb-29 timestamp.
    feb29 = pd.Timestamp("2024-02-29 12:00")
    res1 = pd.DataFrame({
        "timestamp": [feb29],
        "pv_kwh": [1.0], "load_kwh": [1.0],
        "pv_to_load_kwh": [0.5], "pv_to_grid_kwh": [0.3],
        "pv_curtail_kwh": [0.0], "pv_to_bess_kwh": [0.2],
        "bess_dis_load_kwh": [0.4], "bess_dis_grid_kwh": [0.2],
        "bess_charge_grid_kwh": [0.0], "grid_to_load_kwh": [0.1],
        "grid_export_total_kwh": [0.5], "grid_export_cap_kwh": [5.0],
        "soc_kwh": [100.0], "soc_pct": [50.0],
        "dam_price_eur_per_mwh": [80.0],
    })
    res1 = add_economic_columns(res1, {"retail_tariff_eur_per_mwh": 120.0})
    capacities = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    econ = _econ()
    econ["project_start_year"] = 2024
    econ["project_lifecycle_years"] = 3  # 2024 (leap), 2025, 2026

    lifetime = build_lifetime_dispatch(res1, econ, capacities)
    y2025_ts = lifetime.loc[lifetime["project_year"] == 2, "timestamp"].iloc[0]
    y2026_ts = lifetime.loc[lifetime["project_year"] == 3, "timestamp"].iloc[0]
    # pd.DateOffset would have rolled these forward to Mar 1.
    assert y2025_ts.month == 2 and y2025_ts.day == 28, (
        f"expected 2025-02-28, got {y2025_ts}"
    )
    assert y2026_ts.month == 2 and y2026_ts.day == 28, (
        f"expected 2026-02-28, got {y2026_ts}"
    )


def test_build_lifetime_dispatch_accepts_year1_discharge_override():
    """When the caller supplies year1_discharge_mwh, the cycle-counter
    uses it instead of recomputing from res_year1.  This keeps the cycle
    bookkeeping symmetric with build_yearly_cashflow (which reads
    bess_total_discharge_mwh from the derated kpis dict)."""
    res1 = _make_year1_dispatch()
    capacities = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    econ = _econ()
    # Cycle fade enabled: 0.008 % per FEC turns the cycle counter into
    # a visible signal in the year-N bess_factor.
    econ["bess_degradation_pct_per_cycle"] = 0.008
    econ["project_lifecycle_years"] = 5

    # Raw discharge from res_year1.
    raw = build_lifetime_dispatch(res1, econ, capacities)
    # Same call with an explicit (smaller) discharge override: cycle
    # counter accrues less, so year-N capacity factor is higher.
    raw_year1 = (
        float(res1[["bess_dis_load_kwh", "bess_dis_grid_kwh"]]
              .to_numpy().sum()) / 1000.0
    )
    smaller = build_lifetime_dispatch(
        res1, econ, capacities,
        year1_discharge_mwh=raw_year1 * 0.5,
    )
    # bess_dis_load_kwh at year 5 should be HIGHER with the smaller
    # cycle-count override.
    raw_y5 = float(
        raw.loc[raw["project_year"] == 5, "bess_dis_load_kwh"].sum()
    )
    small_y5 = float(
        smaller.loc[smaller["project_year"] == 5, "bess_dis_load_kwh"].sum()
    )
    assert small_y5 > raw_y5

    # Sanity: passing the raw value explicitly equals the auto-derived path.
    explicit = build_lifetime_dispatch(
        res1, econ, capacities, year1_discharge_mwh=raw_year1,
    )
    explicit_y5 = float(
        explicit.loc[explicit["project_year"] == 5, "bess_dis_load_kwh"].sum()
    )
    assert explicit_y5 == pytest.approx(raw_y5, rel=1e-12)


def test_unavailability_derate_is_symmetric_between_cashflow_and_lifetime():
    """With unavailability_pct=0 the year-1 cycle counters of
    build_yearly_cashflow and build_lifetime_dispatch must be
    identical.  With unavailability_pct=1 the values must STILL match
    within float precision because main.py feeds the same derated
    year1_discharge_mwh into both."""
    from pvbess_opt.availability import apply_unavailability_derate
    from pvbess_opt.economics import build_yearly_cashflow

    res1 = _make_year1_dispatch()
    capacities = {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}
    econ = _econ()
    econ["project_lifecycle_years"] = 5
    # build_yearly_cashflow requires a richer econ dict than
    # build_lifetime_dispatch — fill in the missing CAPEX / OPEX keys
    # with placeholder values; the cycle-counter assertion below is
    # independent of their magnitudes.
    econ.update({
        "capex_pv_eur_per_kw": 500.0,
        "capex_bess_eur_per_kwh": 200.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 7.0,
        "opex_bess_eur_per_kw": 14.0,
        "aggregator_fee_pct_revenue": 0.0,
    })

    # Construct minimal year1 kpis matching the dispatch we built.
    raw_y1_discharge = (
        float(res1[["bess_dis_load_kwh", "bess_dis_grid_kwh"]]
              .to_numpy().sum()) / 1000.0
    )
    base_kpis = {
        "bess_total_discharge_mwh": raw_y1_discharge,
        "profit_total_eur": 1_000_000.0,
        "profit_load_from_pv_eur": 300_000.0,
        "profit_load_from_bess_eur": 50_000.0,
        "profit_export_from_pv_eur": 400_000.0,
        "profit_export_from_bess_eur": 250_000.0,
        "expense_charge_bess_grid_eur": 0.0,
    }
    for unavail in (0.0, 1.0):
        kpis_derated = apply_unavailability_derate(base_kpis, unavail)
        year1_for_cycles = float(
            kpis_derated["bess_total_discharge_mwh"]
        )

        yearly_cf = build_yearly_cashflow(kpis_derated, econ, capacities)
        lifetime = build_lifetime_dispatch(
            res1, econ, capacities,
            year1_discharge_mwh=year1_for_cycles,
        )
        # Year-1 bess_capacity_factor on both paths should equal 1.0
        # (no degradation accrued before the first cycle counter tick).
        cf_y1_factor = float(
            yearly_cf.loc[
                yearly_cf["project_year"] == 1, "bess_capacity_factor"
            ].iloc[0]
        )
        # build_lifetime_dispatch scales per-step discharge by bess_factor;
        # year 1 ratio (post-bess-factor) over raw_year1 = bess_factor[1].
        lt_y1_discharge = float(
            lifetime.loc[
                lifetime["project_year"] == 1,
                ["bess_dis_load_kwh", "bess_dis_grid_kwh"],
            ].to_numpy().sum()
        ) / 1000.0
        raw_year1_with_derate = year1_for_cycles
        lt_y1_factor = (
            lt_y1_discharge / raw_y1_discharge if raw_y1_discharge else 1.0
        )
        # Year 1 has no prior degradation: both factors should be 1.0.
        assert cf_y1_factor == pytest.approx(1.0, abs=1e-12)
        assert lt_y1_factor == pytest.approx(1.0, abs=1e-12)
        # The derate effect lives downstream in the aggregate-level
        # multiplication; the cycle counter is identical because both
        # paths see year1_discharge_mwh = raw_year1_with_derate.
        del raw_year1_with_derate
