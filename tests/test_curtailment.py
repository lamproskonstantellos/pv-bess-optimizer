"""Exogenous curtailment: quota derate + compensation + signal mode
(Eqs. E48/E49).

Quota mode scales the export-side KPIs post-solve (a second derate
after availability) and pays the compensated share of the curtailed
volume at an administered price; signal mode multiplies a per-step
[0, 1] column into the export caps so the MILP re-dispatches around
the curtailment.  The two modes answer different questions and are
mutually exclusive by validation.  Locked: zero-default bit-identity,
the export-only derate classification with preserved aggregate
identities, the E49 cent-level compensation and ordering invariance,
the cashflow column with monthly reconciliation and sensitivity
identities, signal-mode dispatch response, and the exclusivity error.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.availability import (
    apply_curtailment_derate,
    apply_operating_derates,
    apply_unavailability_derate,
)
from pvbess_opt.economics import build_yearly_cashflow, compute_financial_kpis


def _kpis() -> dict:
    return {
        "profit_load_from_pv_eur": 60_000.0,
        "profit_load_from_bess_eur": 10_000.0,
        "profit_export_from_pv_eur": 40_000.0,
        "profit_export_from_bess_eur": 40_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 135_000.0,
        "system_total_export_mwh": 1_600.0,
        "pv_export_mwh": 1_000.0,
        "bess_export_mwh": 600.0,
        "revenue_pv_dam_eur": 40_000.0,
        "revenue_bess_dam_eur": 35_000.0,
        "revenue_self_consumption_eur": 70_000.0,
        "load_energy_mwh": 2_000.0,
        "system_total_import_mwh": 500.0,
    }


def test_zero_default_is_identity():
    kpis = _kpis()
    out = apply_curtailment_derate(kpis, 0.0)
    assert out == kpis  # no new keys, no factor records
    out2 = apply_operating_derates(kpis, {"unavailability_pct": 0.0})
    ref = apply_unavailability_derate(kpis, 0.0)
    assert out2 == ref


def test_quota_derate_symmetry():
    """Export keys scale by (1 - q); the rest are untouched; the
    profit recomposition matches the component deltas."""
    kpis = _kpis()
    out = apply_curtailment_derate(kpis, 10.0)
    for key in ("system_total_export_mwh", "pv_export_mwh",
                "bess_export_mwh", "profit_export_from_pv_eur",
                "profit_export_from_bess_eur", "revenue_pv_dam_eur"):
        assert out[key] == pytest.approx(0.9 * kpis[key]), key
    for key in ("profit_load_from_pv_eur", "profit_load_from_bess_eur",
                "expense_charge_bess_grid_eur",
                "revenue_self_consumption_eur", "load_energy_mwh",
                "system_total_import_mwh"):
        assert out[key] == kpis[key], key
    # revenue_bess_dam_eur is the NET BESS-DAM aggregate (export profit minus
    # the curtailment-EXEMPT grid-charging withdrawal), so it must NOT scale
    # monolithically by (1 - q): curtailment scales the export leg but leaves
    # the withdrawal untouched, so the aggregate is recomposed from its
    # components and must keep its documented identity.  Regression: it used
    # to be scaled as a whole, handing back q * expense_charge_bess_grid_eur
    # (here 0.1 * 5_000 = 500 EUR of over-statement, 31_500 instead of 31_000).
    assert out["revenue_bess_dam_eur"] == pytest.approx(
        out["profit_export_from_bess_eur"]
        - out["expense_charge_bess_grid_eur"]
    )
    assert out["revenue_bess_dam_eur"] == pytest.approx(
        0.9 * kpis["profit_export_from_bess_eur"]
        - kpis["expense_charge_bess_grid_eur"]
    )  # = 31_000, not the pre-fix 0.9 * 35_000 = 31_500
    # profit_total drops by exactly the export-profit deltas (no
    # compensation configured).
    assert out["profit_total_eur"] == pytest.approx(
        kpis["profit_total_eur"]
        - 0.1 * (kpis["profit_export_from_pv_eur"]
                 + kpis["profit_export_from_bess_eur"]),
    )
    assert out["curtailment_factor"] == pytest.approx(0.9)
    assert out["curtailment_compensation_eur"] == 0.0


def test_compensation_line_cent_level_and_ordering():
    """E49: R = q x E_export x c x p on the availability-derated base;
    availability x curtailment ordering is multiplicative."""
    kpis = _kpis()
    via_helper = apply_operating_derates(kpis, {
        "unavailability_pct": 2.0,
        "curtailment_pct": 5.0,
        "curtailment_compensated_pct": 60.0,
        "curtailment_compensation_price_eur_per_mwh": 45.0,
    })
    export_after_avail = 0.98 * kpis["system_total_export_mwh"]
    expected = 0.05 * export_after_avail * 0.60 * 45.0
    assert via_helper["curtailment_compensation_eur"] == pytest.approx(
        expected, abs=0.01,
    )
    # Ordering invariance of the export scaling itself.
    assert via_helper["system_total_export_mwh"] == pytest.approx(
        kpis["system_total_export_mwh"] * 0.98 * 0.95,
    )


def test_cashflow_column_and_lifetime_total():
    econ = {
        "project_lifecycle_years": 6,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 2.0,
        "capex_pv_eur_per_kw": 400.0,
        "capex_bess_eur_per_kwh": 100.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 5.0,
        "opex_bess_eur_per_kw": 5.0,
        "pv_degradation_year1_pct": 2.0,
        "pv_degradation_annual_pct": 1.0,
        "bess_degradation_annual_pct": 1.5,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
    }
    caps = {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}
    kpis = dict(_kpis(), curtailment_compensation_eur=3_000.0)
    cf = build_yearly_cashflow(kpis, econ, caps)
    col = cf["curtailment_compensation_eur"]
    assert float(col.iloc[0]) == 0.0  # Year 0
    # Year 1: base x blend(=1.0) x index(=1.0).
    assert float(col.iloc[1]) == pytest.approx(3_000.0)
    # Year 2 fades on the export-split blend and indexes on dam
    # inflation.
    w_pv, w_bess = 1_000.0, 600.0
    pv_f2 = 0.98
    bess_f2 = float(cf["bess_capacity_factor"].iloc[2])
    blend2 = (w_pv * pv_f2 + w_bess * bess_f2) / (w_pv + w_bess)
    assert float(col.iloc[2]) == pytest.approx(
        3_000.0 * blend2 * 1.02, rel=1e-9,
    )
    # Included in the net and the lifetime KPI total.
    fin = compute_financial_kpis(cf, econ)
    assert fin["lifetime_curtailment_compensation_eur"] == pytest.approx(
        float(col.iloc[1:].sum()), abs=0.01,
    )
    # Zero-base run keeps an all-zero column (bit-identity).
    cf_off = build_yearly_cashflow(_kpis(), econ, caps)
    assert (cf_off["curtailment_compensation_eur"] == 0.0).all()


def test_sensitivity_lists_carry_the_column():
    from pvbess_opt.economics import TAX_LAYER_COLUMNS
    from pvbess_opt.sensitivity import _recompute_net, _scale_revenue

    econ = {
        "project_lifecycle_years": 6, "project_start_year": 2026,
        "discount_rate_pct": 7.0, "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0, "dam_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 400.0, "capex_bess_eur_per_kwh": 100.0,
        "devex_pv_eur_per_kw": 0.0, "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 5.0, "opex_bess_eur_per_kw": 5.0,
        "pv_degradation_year1_pct": 2.0, "pv_degradation_annual_pct": 1.0,
        "bess_degradation_annual_pct": 1.5, "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0, "aggregator_fee_pct_revenue": 0.0,
    }
    caps = {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}
    kpis = dict(_kpis(), curtailment_compensation_eur=3_000.0)
    cf = build_yearly_cashflow(kpis, econ, caps)
    scaled = _scale_revenue(cf, 1.1, econ)
    # Price-linked: the administered payment scales with the driver.
    assert scaled["curtailment_compensation_eur"].iloc[1] == pytest.approx(
        1.1 * float(cf["curtailment_compensation_eur"].iloc[1]),
    )
    # The recomputed net matches the builder's on the unscaled frame.
    base = cf.drop(columns=list(TAX_LAYER_COLUMNS))
    recomputed = _recompute_net(base.copy())
    pd.testing.assert_series_equal(
        recomputed["net_cashflow_eur"], base["net_cashflow_eur"],
    )


def test_monthly_reconciles_yearly():
    from pvbess_opt.economics import derive_monthly_cashflow

    econ = {
        "project_lifecycle_years": 4, "project_start_year": 2026,
        "discount_rate_pct": 7.0, "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0, "dam_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 400.0, "capex_bess_eur_per_kwh": 100.0,
        "devex_pv_eur_per_kw": 0.0, "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 5.0, "opex_bess_eur_per_kw": 5.0,
        "pv_degradation_year1_pct": 2.0, "pv_degradation_annual_pct": 1.0,
        "bess_degradation_annual_pct": 1.5, "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0, "aggregator_fee_pct_revenue": 0.0,
    }
    caps = {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}
    kpis = dict(_kpis(), curtailment_compensation_eur=3_000.0)
    cf = build_yearly_cashflow(kpis, econ, caps)
    n = 96
    res = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "savings_self_consumption_eur": np.full(n, 10.0),
        "profit_export_from_pv_eur": np.full(n, 5.0),
        "profit_export_from_bess_eur": np.full(n, 5.0),
        "expense_charge_bess_grid_eur": np.full(n, 1.0),
        "pv_kwh": np.full(n, 100.0),
    })
    monthly, quarterly = derive_monthly_cashflow(res, cf, econ)
    for y in range(1, 5):
        y_total = float(cf.loc[
            cf["project_year"] == y, "curtailment_compensation_eur",
        ].iloc[0])
        m_total = float(monthly.loc[
            monthly["project_year"] == y, "curtailment_compensation_eur",
        ].sum())
        assert m_total == pytest.approx(y_total, abs=1e-6), y
    assert "curtailment_compensation_eur" in quarterly.columns


# ---------------------------------------------------------------------------
# Signal mode + validation
# ---------------------------------------------------------------------------


def _highs_available() -> bool:
    try:
        import highspy
    except ImportError:
        return False
    return bool(highspy)


@pytest.mark.skipif(not _highs_available(), reason="requires HiGHS")
def test_signal_mode_redispatch():
    """A zeroed export cap in high-PV hours forces the MILP to charge
    or curtail instead of exporting; invariant_7 stays green because
    the reported cap reflects the signal."""
    from pvbess_opt.optimization import (
        build_model,
        run_scenario,
        verify_dispatch_invariants,
    )

    n = 6
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=n, freq="h"),
        "pv_kwh": [0.0, 500.0, 500.0, 0.0, 0.0, 0.0],
        "dam_price_eur_per_mwh": [50.0, 60.0, 60.0, 90.0, 90.0, 90.0],
        "curtailment_signal": [1.0, 0.0, 0.0, 1.0, 1.0, 1.0],
    })
    params = {
        "dt_minutes": 60, "mode": "merchant",
        "pv_nameplate_kwp": 500.0,
        "bess_power_kw": 300.0, "bess_capacity_kwh": 800.0,
        "efficiency_charge": 1.0, "efficiency_discharge": 1.0,
        "soc_min_frac": 0.0, "soc_max_frac": 1.0,
        "initial_soc_frac": 0.0, "terminal_soc_equal": False,
        "max_cycles_per_day": 10.0,
        "p_grid_export_max_kw": 5000.0,
        "retail_tariff_eur_per_mwh": 120.0,
        "allow_bess_grid_charging": False,
    }
    _res, _s, full = run_scenario(params, ts, return_unrounded=True)
    exports = (
        full["pv_to_grid_kwh"] + full["bess_dis_grid_kwh"]
    ).to_numpy(dtype=float)
    assert exports[1] <= 1e-6 and exports[2] <= 1e-6
    # PV re-routes into the BESS in the curtailed hours and sells at
    # the evening peak.
    assert float(full["pv_to_bess_kwh"].iloc[1:3].sum()) > 0.0
    assert float(full["bess_dis_grid_kwh"].iloc[3:].sum()) > 0.0
    inv = verify_dispatch_invariants(full, params)
    assert inv["invariant_7_curtail_behavior_count"] == 0.0
    # Absent column: caps identical to a signal of all-ones.
    m_plain = build_model(params, ts.drop(columns=["curtailment_signal"]))
    m_ones = build_model(
        params, ts.assign(curtailment_signal=np.ones(n)),
    )
    import pyomo.environ as pyo

    for t in range(n):
        assert pyo.value(
            m_plain.EXPORT_CAP[t].upper
        ) == pytest.approx(pyo.value(m_ones.EXPORT_CAP[t].upper))


def test_exclusivity_and_signal_range(tmp_path):
    from pvbess_opt.io import (
        BALANCING_SHEET_DEFAULTS,
        BESS_SHEET_DEFAULTS,
        ECONOMICS_SHEET_DEFAULTS,
        PROJECT_SHEET_DEFAULTS,
        PV_SHEET_DEFAULTS,
        SIMULATION_SHEET_DEFAULTS,
        read_workbook,
        write_workbook,
    )

    def _typed(signal, quota):
        n = 24
        ts = pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
            "pv_kwh": np.full(n, 100.0),
            "load_kwh": np.full(n, 50.0),
            "dam_price_eur_per_mwh": np.full(n, 60.0),
        })
        if signal is not None:
            ts["curtailment_signal"] = signal
        return {
            "ts": ts,
            "project": dict(
                PROJECT_SHEET_DEFAULTS, curtailment_pct=quota,
            ),
            "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=100.0),
            "bess": dict(BESS_SHEET_DEFAULTS),
            "economics": dict(ECONOMICS_SHEET_DEFAULTS),
            "simulation": dict(SIMULATION_SHEET_DEFAULTS),
            "balancing": dict(BALANCING_SHEET_DEFAULTS),
        }

    both = tmp_path / "both.xlsx"
    write_workbook(_typed(np.full(24, 0.5), 10.0), both)
    with pytest.raises(ValueError, match="double-count"):
        read_workbook(both)

    bad_range = tmp_path / "range.xlsx"
    write_workbook(_typed(np.full(24, 1.5), 0.0), bad_range)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        read_workbook(bad_range)

    ok = tmp_path / "ok.xlsx"
    write_workbook(_typed(np.full(24, 0.5), 0.0), ok)
    read_workbook(ok)

    bad_quota = tmp_path / "quota.xlsx"
    write_workbook(_typed(None, 150.0), bad_quota)
    with pytest.raises(ValueError, match="curtailment_pct"):
        read_workbook(bad_quota)
