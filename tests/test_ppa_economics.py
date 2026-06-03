"""PPA (with merchant tail) per-step economics, KPIs, and dispatch-aware
pay-as-produced repricing.

The PPA reprices the grid-export stream (pv_to_grid + bess_dis_grid) as a
parallel revenue stream: an additive premium = contracted MWh x
(PPA price - DAM price).  It is never folded into profit_total_eur, never
part of the retail/DAM breakdown, and (by default) never touches the
MILP — only the optional dispatch-aware pay-as-produced mode changes the
objective coefficients.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.availability import apply_unavailability_derate
from pvbess_opt.kpis import (
    ENERGY_TOLERANCE,
    add_economic_columns,
    compute_kpis,
)
from pvbess_opt.optimization import run_scenario, verify_dispatch_invariants

_TOL = ENERGY_TOLERANCE

_PPA_KPI_KEYS = (
    "ppa_premium_total_eur",
    "ppa_premium_pv_eur",
    "ppa_premium_bess_eur",
    "ppa_contracted_mwh",
    "ppa_merchant_mwh",
)
_PPA_STEP_COLUMNS = (
    "ppa_contracted_kwh",
    "ppa_merchant_kwh",
    "ppa_premium_eur",
    "ppa_premium_pv_eur",
    "ppa_premium_bess_eur",
)


def _mk_ts(n: int = 48) -> pd.DataFrame:
    t = pd.date_range("2026-06-01", periods=n, freq="h")
    h = np.arange(n).astype(float) % 24
    pv = 7000.0 * np.where((h >= 6) & (h <= 18),
                           np.sin(np.pi * (h - 6) / 12.0), 0.0)
    pv = np.maximum(pv, 0.0)
    load = np.full(n, 1500.0)
    dam = 50.0 - 15.0 * np.sin(np.pi * (h - 6) / 12.0)  # ~35..65 EUR/MWh
    return pd.DataFrame({
        "timestamp": t,
        "pv_kwh": pv,
        "load_kwh": load,
        "dam_price_eur_per_mwh": dam,
    })


def _base_params(**over) -> dict:
    base = dict(
        dt_minutes=60,
        efficiency_charge=0.97,
        efficiency_discharge=0.97,
        soc_min_frac=0.20,
        soc_max_frac=0.95,
        initial_soc_frac=0.50,
        terminal_soc_equal=True,
        max_cycles_per_day=2.0,
        p_grid_export_max_kw=5000.0,
        retail_tariff_eur_per_mwh=120.0,
        settlement_minutes=15,
        mode="self_consumption",
        allow_bess_grid_charging=False,
        show_titles=False,
        pv_nameplate_kwp=6000.0,
        bess_power_kw=2000.0,
        bess_capacity_kwh=8000.0,
    )
    base.update(over)
    return base


def _ppa(**over) -> dict:
    cfg = dict(
        ppa_enabled=True,
        ppa_structure="pay_as_produced",
        ppa_price_eur_per_mwh=80.0,
        ppa_coverage_fraction=1.0,
        ppa_baseload_mw=0.0,
        ppa_escalation_pct=0.0,
        ppa_dispatch_aware=False,
    )
    cfg.update(over)
    return cfg


@pytest.fixture(scope="module")
def solved_base():
    """Solve a self-consumption hybrid scenario once (PPA off).

    The PPA repricing tests reprice this fixed dispatch frame with
    different PPA configs — valid because the (non-dispatch-aware) PPA is
    a post-dispatch repricing.
    """
    ts = _mk_ts()
    params = _base_params()
    res, _ = run_scenario(params, ts, solver_name="highs", mip_gap=0.0)
    export_mwh = float(
        (res["pv_to_grid_kwh"] + res["bess_dis_grid_kwh"]).sum()
    ) / 1000.0
    assert export_mwh > 1.0  # the scenario must export for these tests
    return res, params, export_mwh


def _kpis_with(solved_base, ppa_cfg) -> tuple[dict, pd.DataFrame]:
    res0, base, _ = solved_base
    res = res0.copy()
    params = dict(base)
    params["ppa"] = ppa_cfg
    add_economic_columns(res, params)
    kpis = compute_kpis(res, params, verify_balance=False)
    return kpis, res


# ---------------------------------------------------------------------------
# OFF state: always-emitted zero KPIs, no per-step columns
# ---------------------------------------------------------------------------


def test_ppa_off_emits_zero_kpis(solved_base):
    kpis, _ = _kpis_with(solved_base, _ppa(ppa_enabled=False))
    for key in _PPA_KPI_KEYS:
        assert kpis[key] == 0.0, key
    assert kpis["revenue_ppa_premium_eur"] == 0.0
    # project_revenue_total_eur with no PPA / balancing equals profit.
    assert kpis["project_revenue_total_eur"] == pytest.approx(
        kpis["profit_total_eur"], abs=1e-6,
    )


def test_ppa_off_adds_no_per_step_columns(solved_base):
    _, res = _kpis_with(solved_base, _ppa(ppa_enabled=False))
    for col in _PPA_STEP_COLUMNS:
        assert col not in res.columns, col


def test_ppa_on_adds_per_step_columns(solved_base):
    _, res = _kpis_with(solved_base, _ppa())
    for col in _PPA_STEP_COLUMNS:
        assert col in res.columns, col


# ---------------------------------------------------------------------------
# Premium sign and coverage-fraction linearity
# ---------------------------------------------------------------------------


def test_premium_positive_when_price_beats_dam(solved_base):
    kpis, _ = _kpis_with(solved_base, _ppa(ppa_price_eur_per_mwh=80.0))
    assert kpis["ppa_premium_total_eur"] > 0.0


def test_premium_negative_when_price_below_dam(solved_base):
    kpis, _ = _kpis_with(solved_base, _ppa(ppa_price_eur_per_mwh=20.0))
    assert kpis["ppa_premium_total_eur"] < 0.0


def test_coverage_fraction_linearity(solved_base):
    k0, _ = _kpis_with(solved_base, _ppa(ppa_coverage_fraction=0.0))
    k1, _ = _kpis_with(solved_base, _ppa(ppa_coverage_fraction=1.0))
    kh, _ = _kpis_with(solved_base, _ppa(ppa_coverage_fraction=0.5))
    assert k0["ppa_premium_total_eur"] == pytest.approx(0.0, abs=1e-6)
    assert k0["ppa_contracted_mwh"] == pytest.approx(0.0, abs=1e-9)
    assert kh["ppa_premium_total_eur"] == pytest.approx(
        0.5 * k1["ppa_premium_total_eur"], rel=1e-6,
    )


def test_coverage_f1_contracts_full_export(solved_base):
    _, _, export_mwh = solved_base
    k1, _ = _kpis_with(solved_base, _ppa(ppa_coverage_fraction=1.0))
    assert k1["ppa_contracted_mwh"] == pytest.approx(export_mwh, rel=1e-6)
    assert k1["ppa_merchant_mwh"] == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Baseload split: contracted = min(export, target); sum identity
# ---------------------------------------------------------------------------


def test_baseload_split_sum_identity(solved_base):
    _, _, export_mwh = solved_base
    kb, res = _kpis_with(
        solved_base, _ppa(ppa_structure="baseload", ppa_baseload_mw=1.0),
    )
    assert kb["ppa_contracted_mwh"] + kb["ppa_merchant_mwh"] == pytest.approx(
        export_mwh, rel=1e-6,
    )
    # Per-step contracted never exceeds the per-step target (1 MW * 1 h).
    target_kwh = 1.0 * 1000.0 * 1.0
    assert (res["ppa_contracted_kwh"].to_numpy() <= target_kwh + 1e-6).all()
    # Some steps export above the target -> a non-zero merchant tail.
    assert kb["ppa_merchant_mwh"] > 0.0


def test_baseload_large_target_contracts_everything(solved_base):
    _, _, export_mwh = solved_base
    kb, _ = _kpis_with(
        solved_base, _ppa(ppa_structure="baseload", ppa_baseload_mw=999.0),
    )
    assert kb["ppa_contracted_mwh"] == pytest.approx(export_mwh, rel=1e-6)
    assert kb["ppa_merchant_mwh"] == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Pro-rata PV / BESS split + parallel-stream contracts
# ---------------------------------------------------------------------------


def test_pv_bess_premium_sums_to_total(solved_base):
    kpis, _ = _kpis_with(solved_base, _ppa())
    assert kpis["ppa_premium_pv_eur"] + kpis["ppa_premium_bess_eur"] == (
        pytest.approx(kpis["ppa_premium_total_eur"], abs=0.02)
    )


def test_profit_total_unchanged_by_ppa(solved_base):
    k_off, _ = _kpis_with(solved_base, _ppa(ppa_enabled=False))
    k_on, _ = _kpis_with(solved_base, _ppa())
    assert k_on["profit_total_eur"] == k_off["profit_total_eur"]


def test_project_revenue_total_is_sum(solved_base):
    kpis, _ = _kpis_with(solved_base, _ppa())
    expected = (
        kpis["profit_total_eur"]
        + kpis["ppa_premium_total_eur"]
        + kpis["bm_total_balancing_revenue_eur"]
    )
    assert kpis["project_revenue_total_eur"] == pytest.approx(expected, abs=0.02)


def test_revenue_ppa_premium_canonical_equals_total(solved_base):
    kpis, _ = _kpis_with(solved_base, _ppa())
    assert kpis["revenue_ppa_premium_eur"] == kpis["ppa_premium_total_eur"]


# ---------------------------------------------------------------------------
# Availability derate preserves the PPA identities
# ---------------------------------------------------------------------------


def test_availability_derate_preserves_ppa_identities(solved_base):
    kpis, _ = _kpis_with(solved_base, _ppa())
    derated = apply_unavailability_derate(dict(kpis), 5.0)  # 95 % available
    factor = 0.95
    assert derated["ppa_premium_total_eur"] == pytest.approx(
        kpis["ppa_premium_total_eur"] * factor, rel=1e-6,
    )
    assert (
        derated["ppa_premium_pv_eur"] + derated["ppa_premium_bess_eur"]
    ) == pytest.approx(derated["ppa_premium_total_eur"], abs=0.02)
    # project_revenue stays the sum of its (now derated) components.
    assert derated["project_revenue_total_eur"] == pytest.approx(
        derated["profit_total_eur"]
        + derated["ppa_premium_total_eur"]
        + derated["bm_total_balancing_revenue_eur"],
        abs=0.05,
    )


# ---------------------------------------------------------------------------
# Optional dispatch-aware pay-as-produced (objective coefficients only)
# ---------------------------------------------------------------------------


def _solve(params: dict, ts: pd.DataFrame):
    res, _ = run_scenario(params, ts, solver_name="highs", mip_gap=0.0)
    return res


def _frames_identical(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    cols = [c for c in a.columns if pd.api.types.is_numeric_dtype(a[c])]
    return all(
        np.array_equal(a[c].to_numpy(dtype=float), b[c].to_numpy(dtype=float))
        for c in cols
    )


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


def test_dispatch_aware_off_is_identical_to_ppa_off():
    ts = _mk_ts(72)
    params_off = _base_params(mode="merchant", allow_bess_grid_charging=True,
                              bess_power_kw=3000.0, bess_capacity_kwh=12000.0)
    params_da_off = dict(params_off)
    params_da_off["ppa"] = _ppa(ppa_price_eur_per_mwh=200.0,
                                ppa_dispatch_aware=False)
    res_off = _solve(params_off, ts)
    res_da_off = _solve(params_da_off, ts)
    assert _frames_identical(res_off, res_da_off)


def test_dispatch_aware_on_changes_dispatch_and_invariants_hold():
    ts = _mk_ts(72)
    # Strong diurnal DAM swing so the flat PPA price reshapes dispatch.
    h = np.arange(len(ts)) % 24
    ts["dam_price_eur_per_mwh"] = 50.0 - 30.0 * np.sin(np.pi * (h - 6) / 12.0)
    params_off = _base_params(mode="merchant", allow_bess_grid_charging=True,
                              bess_power_kw=3000.0, bess_capacity_kwh=12000.0)
    params_da_on = dict(params_off)
    params_da_on["ppa"] = _ppa(ppa_price_eur_per_mwh=200.0,
                               ppa_dispatch_aware=True)
    res_off = _solve(params_off, ts)
    res_da_on = _solve(params_da_on, ts)
    assert not _frames_identical(res_off, res_da_on)
    inv = verify_dispatch_invariants(res_da_on, params_da_on, mode="merchant")
    assert _invariants_ok(inv)


def test_baseload_dispatch_aware_is_financial_only():
    ts = _mk_ts(72)
    params_off = _base_params(mode="merchant", allow_bess_grid_charging=True,
                              bess_power_kw=3000.0, bess_capacity_kwh=12000.0)
    params_bl = dict(params_off)
    # dispatch_aware requested but baseload is out of scope -> no MILP change.
    params_bl["ppa"] = _ppa(ppa_structure="baseload", ppa_baseload_mw=1.0,
                            ppa_price_eur_per_mwh=200.0, ppa_dispatch_aware=True)
    res_off = _solve(params_off, ts)
    res_bl = _solve(params_bl, ts)
    assert _frames_identical(res_off, res_bl)
