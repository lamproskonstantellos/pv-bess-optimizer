"""Regression tests for the symmetry of :func:`apply_unavailability_derate`.

Locks two properties:

1. Every revenue-bearing top-level EUR key in the KPI dict is scaled
   by ``availability_factor`` -- per-stream profit components, the
   balancing per-product capacity / activation revenues, the balancing
   totals, and the canonical revenue aggregates (``revenue_*_eur``).
2. Once the derated KPIs feed :func:`build_yearly_cashflow`, the
   Year-1 ``balancing_revenue_eur`` row scales by the same factor, and
   the headline financial KPIs (NPV, IRR, ROI, BCR) all shift in the
   profit-reducing direction.
"""

from __future__ import annotations

import pytest

from pvbess_opt.availability import apply_unavailability_derate
from pvbess_opt.balancing import PRODUCTS_ALL, PRODUCTS_WITH_ACTIVATION
from pvbess_opt.economics import build_yearly_cashflow, compute_financial_kpis


def _year1_kpis_balancing_on() -> dict[str, float]:
    """Year-1 KPIs with non-trivial balancing revenue + canonical aggregates.

    Mirrors the shape :func:`pvbess_opt.kpis.compute_kpis` produces: raw
    per-stream profit components, per-product balancing capacity /
    activation revenues, balancing totals, and canonical revenue
    aggregates (``revenue_*_eur``).
    """
    profit_load_pv = 550_000.0
    profit_load_bess = 350_000.0
    profit_export_pv = 300_000.0
    profit_export_bess = 275_000.0
    expense_charge = 60_000.0

    # Per-product balancing capacity revenue (FCR is capacity-only).
    bm_cap = {
        "fcr": 40_000.0,
        "afrr_up": 37_500.0,
        "afrr_dn": 32_500.0,
        "mfrr_up": 45_000.0,
        "mfrr_dn": 35_000.0,
    }
    bm_act = {
        "afrr_up": 17_500.0,
        "afrr_dn": 14_000.0,
        "mfrr_up": 15_000.0,
        "mfrr_dn": 13_500.0,
    }
    bm_total_cap = sum(bm_cap.values())
    bm_total_act = sum(bm_act.values())

    kpis: dict[str, float] = {
        "profit_load_from_pv_eur": profit_load_pv,
        "profit_load_from_bess_eur": profit_load_bess,
        "profit_export_from_pv_eur": profit_export_pv,
        "profit_export_from_bess_eur": profit_export_bess,
        "expense_charge_bess_grid_eur": expense_charge,
        "profit_total_eur": (
            profit_load_pv + profit_load_bess
            + profit_export_pv + profit_export_bess
            - expense_charge
        ),
        "pv_generation_mwh": 7_200.0,
        "bess_total_discharge_mwh": 4_500.0,
        # Canonical revenue aggregates (compute_kpis emits these post
        # _compute_canonical_revenue_aggregates).
        "revenue_pv_dam_eur": profit_export_pv,
        "revenue_bess_dam_eur": profit_export_bess - expense_charge,
        "revenue_self_consumption_eur": profit_load_pv + profit_load_bess,
        # Balancing totals + per-product capacity/activation revenue.
        "bm_total_capacity_revenue_eur": bm_total_cap,
        "bm_total_activation_revenue_eur": bm_total_act,
        "bm_total_balancing_revenue_eur": bm_total_cap + bm_total_act,
    }
    for p in PRODUCTS_ALL:
        kpis[f"bm_{p}_capacity_revenue_eur"] = bm_cap[p]
    for p in PRODUCTS_WITH_ACTIVATION:
        kpis[f"bm_{p}_activation_revenue_eur"] = bm_act[p]
    # Per-product aggregates (capacity + activation; FCR is capacity-only).
    kpis["revenue_bess_fcr_eur"] = bm_cap["fcr"]
    for p in PRODUCTS_WITH_ACTIVATION:
        kpis[f"revenue_bess_{p}_eur"] = bm_cap[p] + bm_act[p]
    return kpis


def _econ() -> dict:
    return {
        "project_lifecycle_years": 20,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 1.0,
        "retail_inflation_pct": 2.0,
        "dam_inflation_pct": 2.0,
        "bm_inflation_pct": 2.0,
        "capex_pv_eur_per_kw": 525.0,
        "capex_bess_eur_per_kw": 300.0,
        "devex_pv_eur_per_kw": 60.0,
        "devex_bess_eur_per_kw": 30.0,
        "opex_pv_eur_per_kwp": 7.0,
        "opex_bess_eur_per_kw": 14.0,
        "pv_degradation_year1_pct": 2.5,
        "pv_degradation_annual_pct": 0.55,
        "bess_degradation_annual_pct": 2.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 2.0,
    }


def _capacities() -> dict:
    return {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}


def _abs_tol(value: float) -> float:
    """Tolerance: max(0.02 EUR, 1e-6 * |value|)."""
    return max(0.02, 1e-6 * abs(value))


def _revenue_keys(kpis: dict[str, float]) -> list[str]:
    """Every revenue-bearing top-level EUR key the derate must scale."""
    keys: list[str] = [
        "profit_load_from_pv_eur",
        "profit_load_from_bess_eur",
        "profit_export_from_pv_eur",
        "profit_export_from_bess_eur",
        "expense_charge_bess_grid_eur",
        "profit_total_eur",
        "revenue_pv_dam_eur",
        "revenue_bess_dam_eur",
        "revenue_self_consumption_eur",
        "bm_total_capacity_revenue_eur",
        "bm_total_activation_revenue_eur",
        "bm_total_balancing_revenue_eur",
    ]
    for p in PRODUCTS_ALL:
        keys.append(f"bm_{p}_capacity_revenue_eur")
    for p in PRODUCTS_WITH_ACTIVATION:
        keys.append(f"bm_{p}_activation_revenue_eur")
    keys.append("revenue_bess_fcr_eur")
    for p in PRODUCTS_WITH_ACTIVATION:
        keys.append(f"revenue_bess_{p}_eur")
    return [k for k in keys if k in kpis]


def test_derate_scales_every_revenue_key_symmetrically():
    """All revenue-bearing EUR keys scale by ``availability_factor``.

    Compares pristine (0 % unavailability) vs. 10 % unavailability and
    asserts every revenue key in the 10 % case equals 0.9 * the
    pristine value within tolerance.
    """
    base = _year1_kpis_balancing_on()
    pristine = apply_unavailability_derate(base, 0.0)
    derated = apply_unavailability_derate(base, 10.0)

    assert pristine["availability_factor"] == pytest.approx(1.0)
    assert derated["availability_factor"] == pytest.approx(0.9)

    for key in _revenue_keys(base):
        expected = 0.9 * float(pristine[key])
        actual = float(derated[key])
        assert actual == pytest.approx(expected, abs=_abs_tol(expected)), (
            f"{key}: derated={actual}, expected 0.9 * pristine="
            f"{expected}"
        )


def test_derate_preserves_canonical_aggregate_identities():
    """The canonical aggregates remain consistent with their components.

    e.g. ``revenue_bess_dam_eur ==
    profit_export_from_bess_eur - expense_charge_bess_grid_eur``
    before AND after the derate.
    """
    base = _year1_kpis_balancing_on()
    derated = apply_unavailability_derate(base, 10.0)

    # revenue_bess_dam_eur = profit_export_from_bess_eur
    #                       - expense_charge_bess_grid_eur
    rebuilt_bess_dam = (
        derated["profit_export_from_bess_eur"]
        - derated["expense_charge_bess_grid_eur"]
    )
    assert derated["revenue_bess_dam_eur"] == pytest.approx(
        rebuilt_bess_dam, abs=_abs_tol(rebuilt_bess_dam),
    )

    # revenue_pv_dam_eur == profit_export_from_pv_eur
    assert derated["revenue_pv_dam_eur"] == pytest.approx(
        derated["profit_export_from_pv_eur"],
        abs=_abs_tol(derated["profit_export_from_pv_eur"]),
    )

    # revenue_self_consumption_eur ==
    #   profit_load_from_pv_eur + profit_load_from_bess_eur
    rebuilt_self = (
        derated["profit_load_from_pv_eur"]
        + derated["profit_load_from_bess_eur"]
    )
    assert derated["revenue_self_consumption_eur"] == pytest.approx(
        rebuilt_self, abs=_abs_tol(rebuilt_self),
    )

    # bm_total_capacity_revenue_eur == sum of per-product capacity.
    rebuilt_cap = sum(
        derated[f"bm_{p}_capacity_revenue_eur"] for p in PRODUCTS_ALL
    )
    assert derated["bm_total_capacity_revenue_eur"] == pytest.approx(
        rebuilt_cap, abs=_abs_tol(rebuilt_cap),
    )

    # bm_total_activation_revenue_eur == sum of per-product activation.
    rebuilt_act = sum(
        derated[f"bm_{p}_activation_revenue_eur"]
        for p in PRODUCTS_WITH_ACTIVATION
    )
    assert derated["bm_total_activation_revenue_eur"] == pytest.approx(
        rebuilt_act, abs=_abs_tol(rebuilt_act),
    )

    # bm_total_balancing_revenue_eur == capacity_total + activation_total.
    assert derated["bm_total_balancing_revenue_eur"] == pytest.approx(
        rebuilt_cap + rebuilt_act, abs=_abs_tol(rebuilt_cap + rebuilt_act),
    )


def test_cashflow_balancing_row_scales_with_derate():
    """Year-1 ``balancing_revenue_eur`` is 0.9 * the pristine value."""
    base = _year1_kpis_balancing_on()
    pristine = apply_unavailability_derate(base, 0.0)
    derated = apply_unavailability_derate(base, 10.0)

    econ, caps = _econ(), _capacities()
    cf_pristine = build_yearly_cashflow(pristine, econ, caps)
    cf_derated = build_yearly_cashflow(derated, econ, caps)

    y1_pristine = float(
        cf_pristine.loc[
            cf_pristine["project_year"] == 1, "balancing_revenue_eur"
        ].iloc[0]
    )
    y1_derated = float(
        cf_derated.loc[
            cf_derated["project_year"] == 1, "balancing_revenue_eur"
        ].iloc[0]
    )
    assert y1_pristine > 0.0
    assert y1_derated == pytest.approx(
        0.9 * y1_pristine, abs=_abs_tol(0.9 * y1_pristine),
    )


def test_derate_shifts_financial_kpis_downward():
    """NPV / IRR / ROI / BCR all drop when availability is reduced."""
    base = _year1_kpis_balancing_on()
    pristine = apply_unavailability_derate(base, 0.0)
    derated = apply_unavailability_derate(base, 10.0)

    econ, caps = _econ(), _capacities()
    cf_pristine = build_yearly_cashflow(pristine, econ, caps)
    cf_derated = build_yearly_cashflow(derated, econ, caps)
    fin_pristine = compute_financial_kpis(cf_pristine, econ)
    fin_derated = compute_financial_kpis(cf_derated, econ)

    # Baseline scenario is NPV-positive — the derate must reduce, not
    # zero out, every cashflow-driven KPI.
    assert fin_pristine["npv_eur"] > 0.0
    assert fin_derated["npv_eur"] < fin_pristine["npv_eur"]
    assert fin_derated["irr_pct"] < fin_pristine["irr_pct"]
    assert fin_derated["roi_pct"] < fin_pristine["roi_pct"]
    assert fin_derated["bcr"] < fin_pristine["bcr"]
