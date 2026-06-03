"""Cross-cutting correctness contracts C1-C6 for the PPA (with merchant
tail) and zero-feed-in features.

C1  ppa off                       -> existing run unchanged; only the new
                                     zero-valued PPA KPI keys are added.
C2  ppa on, dispatch_aware off    -> physical dispatch + LCOE/LCOS
                                     bit-identical to ppa-off; revenue /
                                     NPV change.
C3  ppa on, dispatch_aware on     -> dispatch may change; invariants hold
    (pay_as_produced only).
C4  zero_feed_in                  -> off bit-identical; on forces export 0
                                     with curtailment, invariants hold.
C5  PPA premium is a parallel     -> not in profit_total_eur, the
    revenue stream                   aggregator fee, the Year-1 breakdown,
                                     or LCOE/LCOS.
C6  lifetime.py unchanged         -> PPA is projected analytically in the
                                     cashflow, never in the per-step
                                     lifetime frame.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.availability import apply_unavailability_derate
from pvbess_opt.economics import (
    build_yearly_cashflow,
    compute_financial_kpis,
)
from pvbess_opt.io import (
    BESS_SHEET_DEFAULTS,
    ECONOMICS_SHEET_DEFAULTS,
    PPA_SHEET_DEFAULTS,
    PROJECT_SHEET_DEFAULTS,
    PV_SHEET_DEFAULTS,
)
from pvbess_opt.kpis import (
    ECONOMIC_COLUMNS,
    ENERGY_TOLERANCE,
    add_economic_columns,
    compute_kpis,
)
from pvbess_opt.lifetime import (
    aggregate_lifetime_to_yearly,
    build_lifetime_dispatch,
)
from pvbess_opt.optimization import run_scenario, verify_dispatch_invariants

_TOL = ENERGY_TOLERANCE

_PPA_ZERO_KPI_KEYS = (
    "ppa_premium_total_eur",
    "ppa_premium_pv_eur",
    "ppa_premium_bess_eur",
    "ppa_contracted_mwh",
    "ppa_merchant_mwh",
    "revenue_ppa_premium_eur",
)
_PPA_STEP_COLUMNS = (
    "ppa_contracted_kwh", "ppa_merchant_kwh", "ppa_premium_eur",
    "ppa_premium_pv_eur", "ppa_premium_bess_eur",
)


def _ts(n: int = 48) -> pd.DataFrame:
    t = pd.date_range("2026-06-01", periods=n, freq="h")
    h = np.arange(n).astype(float) % 24
    pv = 7000.0 * np.where((h >= 6) & (h <= 18),
                           np.sin(np.pi * (h - 6) / 12.0), 0.0)
    return pd.DataFrame({
        "timestamp": t,
        "pv_kwh": np.maximum(pv, 0.0),
        "load_kwh": np.full(n, 1500.0),
        "dam_price_eur_per_mwh": 50.0 - 15.0 * np.sin(np.pi * (h - 6) / 12.0),
    })


def _params(**over) -> dict:
    base = dict(
        dt_minutes=60, efficiency_charge=0.97, efficiency_discharge=0.97,
        soc_min_frac=0.20, soc_max_frac=0.95, initial_soc_frac=0.50,
        terminal_soc_equal=True, max_cycles_per_day=2.0,
        p_grid_export_max_kw=5000.0, retail_tariff_eur_per_mwh=120.0,
        settlement_minutes=15, mode="self_consumption",
        allow_bess_grid_charging=False, show_titles=False,
        unavailability_pct=0.0,
        pv_nameplate_kwp=6000.0, bess_power_kw=2000.0, bess_capacity_kwh=8000.0,
    )
    base.update(over)
    return base


def _ppa(**over) -> dict:
    cfg = dict(PPA_SHEET_DEFAULTS)
    cfg["ppa_enabled"] = True
    cfg["ppa_price_eur_per_mwh"] = 90.0  # above DAM -> positive premium
    cfg["ppa_coverage_fraction"] = 1.0
    cfg.update(over)
    return cfg


def _econ(params: dict) -> dict:
    econ: dict = {}
    for d in (
        PROJECT_SHEET_DEFAULTS, PV_SHEET_DEFAULTS, BESS_SHEET_DEFAULTS,
        ECONOMICS_SHEET_DEFAULTS, PPA_SHEET_DEFAULTS,
    ):
        econ.update(d)
    econ["project_lifecycle_years"] = 8
    econ["project_start_year"] = 2026
    if params.get("ppa"):
        econ.update(params["ppa"])
    return econ


def _frames_identical(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    shared = [c for c in a.columns if c in b.columns
              and pd.api.types.is_numeric_dtype(a[c])]
    return all(
        np.array_equal(a[c].to_numpy(dtype=float), b[c].to_numpy(dtype=float))
        for c in shared
    )


def _financials(res: pd.DataFrame, params: dict):
    """Mirror main._build_financials: derive KPIs, cashflow, lifetime, fin KPIs.

    The per-step PPA columns are dropped before build_lifetime_dispatch,
    exactly as main does, so the lifetime frame carries no PPA.
    """
    res = res.copy()
    add_economic_columns(res, params)
    kpis = compute_kpis(res, params, verify_balance=False)
    kpis = apply_unavailability_derate(
        kpis, float(params.get("unavailability_pct", 0.0) or 0.0),
    )
    econ = _econ(params)
    capacities = {
        "pv_kwp": params["pv_nameplate_kwp"],
        "bess_kw": params["bess_power_kw"],
        "bess_kwh": params["bess_capacity_kwh"],
    }
    yearly_cf = build_yearly_cashflow(kpis, econ, capacities)
    res_lt = res.drop(
        columns=[c for c in _PPA_STEP_COLUMNS if c in res.columns],
    )
    lifetime_df = build_lifetime_dispatch(
        res_lt, econ, capacities,
        year1_discharge_mwh=float(kpis.get("bess_total_discharge_mwh", 0.0)),
    )
    lifetime_yearly = aggregate_lifetime_to_yearly(lifetime_df)
    fin = compute_financial_kpis(
        yearly_cf, econ, capacities=capacities,
        lifetime_yearly=lifetime_yearly, year1_kpis=kpis,
    )
    return kpis, yearly_cf, lifetime_df, lifetime_yearly, fin


def _solve(params: dict) -> pd.DataFrame:
    res, _ = run_scenario(params, _ts(), solver_name="highs", mip_gap=0.0)
    return res


def _invariants_ok(inv: dict[str, float]) -> bool:
    for name, value in inv.items():
        if name == "invariant_5_no_sim_grid_io_max_product_kwh2":
            if value > _TOL ** 2:
                return False
        elif name == "invariant_b1_capacity_share_sum_pct_excess":
            if value > 0.5:
                return False
        elif value > _TOL:
            return False
    return True


# ---------------------------------------------------------------------------
# C1 — ppa off is unchanged, only zero PPA keys added
# ---------------------------------------------------------------------------


def test_C1_ppa_absent_equals_ppa_off():
    res_absent = _solve(_params())                       # no "ppa" key
    res_off = _solve(_params(ppa=_ppa(ppa_enabled=False)))
    assert _frames_identical(res_absent, res_off)
    k_absent = compute_kpis(res_absent.copy(), _params(), verify_balance=False)
    k_off = compute_kpis(
        res_off.copy(), _params(ppa=_ppa(ppa_enabled=False)),
        verify_balance=False,
    )
    assert set(k_absent) == set(k_off)
    for key in k_absent:
        assert k_absent[key] == k_off[key], key


def test_C1_ppa_off_zero_keys_and_no_step_columns():
    params = _params(ppa=_ppa(ppa_enabled=False))
    res = _solve(params)
    add_economic_columns(res, params)
    assert not any(c.startswith("ppa_") for c in res.columns)
    kpis = compute_kpis(res, params, verify_balance=False)
    for key in _PPA_ZERO_KPI_KEYS:
        assert kpis[key] == 0.0, key
    assert kpis["project_revenue_total_eur"] == pytest.approx(
        kpis["profit_total_eur"] + kpis["bm_total_balancing_revenue_eur"],
        abs=1e-6,
    )


# ---------------------------------------------------------------------------
# C2 — dispatch_aware off: physical dispatch + LCOE/LCOS identical; NPV moves
# ---------------------------------------------------------------------------


def test_C2_dispatch_and_lcoe_identical_revenue_changes():
    params_off = _params(ppa=_ppa(ppa_enabled=False))
    params_on = _params(ppa=_ppa(ppa_dispatch_aware=False))
    res_off = _solve(params_off)
    res_on = _solve(params_on)
    # Physical dispatch bit-identical (raw run_scenario frames).
    assert _frames_identical(res_off, res_on)

    _, _, _, _, fin_off = _financials(res_off, params_off)
    k_on, _, _, _, fin_on = _financials(res_on, params_on)
    assert fin_off["lcoe_eur_per_mwh"] == fin_on["lcoe_eur_per_mwh"]
    assert fin_off["lcos_eur_per_mwh"] == fin_on["lcos_eur_per_mwh"]
    # Revenue / NPV change because the PPA premium folds into the cashflow.
    assert k_on["ppa_premium_total_eur"] > 0.0
    assert fin_on["npv_eur"] != fin_off["npv_eur"]


# ---------------------------------------------------------------------------
# C3 — dispatch_aware on (pay_as_produced) may change dispatch; invariants hold
# ---------------------------------------------------------------------------


def test_C3_dispatch_aware_changes_dispatch_invariants_hold():
    # Strong diurnal DAM swing + merchant so the flat PPA price reshapes
    # dispatch; pay_as_produced only.
    ts = _ts(72)
    h = np.arange(len(ts)) % 24
    ts["dam_price_eur_per_mwh"] = 50.0 - 30.0 * np.sin(np.pi * (h - 6) / 12.0)
    params_off = _params(
        mode="merchant", allow_bess_grid_charging=True,
        bess_power_kw=3000.0, bess_capacity_kwh=12000.0,
        ppa=_ppa(ppa_enabled=False),
    )
    params_on = _params(
        mode="merchant", allow_bess_grid_charging=True,
        bess_power_kw=3000.0, bess_capacity_kwh=12000.0,
        ppa=_ppa(ppa_price_eur_per_mwh=200.0, ppa_dispatch_aware=True),
    )
    res_off, _ = run_scenario(params_off, ts, solver_name="highs", mip_gap=0.0)
    res_on, _ = run_scenario(params_on, ts, solver_name="highs", mip_gap=0.0)
    assert not _frames_identical(res_off, res_on)
    inv = verify_dispatch_invariants(res_on, params_on, mode="merchant")
    assert _invariants_ok(inv)


# ---------------------------------------------------------------------------
# C4 — zero_feed_in
# ---------------------------------------------------------------------------


def test_C4_zero_feed_in_off_identical_on_forces_zero_export():
    res_off = _solve(_params(zero_feed_in=False))
    res_absent = _solve(_params())
    assert _frames_identical(res_off, res_absent)

    res_on = _solve(_params(zero_feed_in=True))
    export = (res_on["pv_to_grid_kwh"] + res_on["bess_dis_grid_kwh"]).to_numpy()
    assert np.all(np.abs(export) <= _TOL)
    assert float(res_on["pv_curtail_kwh"].sum()) > 1.0  # surplus curtailed
    inv = verify_dispatch_invariants(
        res_on, _params(zero_feed_in=True), mode="self_consumption",
    )
    assert _invariants_ok(inv)
    # The off run does export, so the feature is what removes it.
    assert float(
        (res_off["pv_to_grid_kwh"] + res_off["bess_dis_grid_kwh"]).sum()
    ) > 1.0


# ---------------------------------------------------------------------------
# C5 — PPA premium is a parallel revenue stream
# ---------------------------------------------------------------------------


def test_C5_ppa_excluded_from_profit_fee_breakdown_lcoe():
    params_off = _params(ppa=_ppa(ppa_enabled=False))
    params_on = _params(ppa=_ppa())
    res_off = _solve(params_off)
    res_on = _solve(params_on)

    k_off, cf_off, _, _, fin_off = _financials(res_off, params_off)
    k_on, cf_on, _, _, fin_on = _financials(res_on, params_on)

    # profit_total_eur unchanged.
    assert k_on["profit_total_eur"] == k_off["profit_total_eur"]
    # aggregator fee per year unchanged.
    assert np.allclose(
        cf_off["aggregator_fee_eur"].to_numpy(),
        cf_on["aggregator_fee_eur"].to_numpy(),
    )
    # Year-1 retail/DAM breakdown KPIs unchanged.
    for key in (
        "revenue_breakdown_y1_export_pv_eur",
        "revenue_breakdown_y1_export_bess_eur",
    ):
        assert fin_on[key] == fin_off[key], key
    # LCOE / LCOS unchanged; lifetime PPA total appears only when on.
    assert fin_on["lcoe_eur_per_mwh"] == fin_off["lcoe_eur_per_mwh"]
    assert fin_on["lcos_eur_per_mwh"] == fin_off["lcos_eur_per_mwh"]
    assert fin_off["lifetime_ppa_revenue_total_eur"] == 0.0
    assert fin_on["lifetime_ppa_revenue_total_eur"] != 0.0


# ---------------------------------------------------------------------------
# C6 — PPA never enters the per-step lifetime frame
# ---------------------------------------------------------------------------


def test_C6_ppa_not_in_economic_columns():
    for col in _PPA_STEP_COLUMNS:
        assert col not in ECONOMIC_COLUMNS


def test_C6_ppa_absent_from_lifetime_frames():
    params_on = _params(ppa=_ppa())
    res_on = _solve(params_on)
    _, cf, lifetime_df, lifetime_yearly, _ = _financials(res_on, params_on)
    # The premium IS projected analytically in the yearly cashflow.
    assert "ppa_revenue_eur" in cf.columns
    assert float(cf.loc[cf.project_year >= 1, "ppa_revenue_eur"].sum()) > 0.0
    # ... but never in the per-step lifetime frame nor its aggregate.
    assert not any(c.startswith("ppa_") for c in lifetime_df.columns)
    assert not any(c.startswith("ppa_") for c in lifetime_yearly.columns)
