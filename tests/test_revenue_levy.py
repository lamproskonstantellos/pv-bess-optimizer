"""Revenue levy on gross market turnover (Eq. E33).

A percentage levy on invoiced MARKET turnover: wholesale DAM export
revenue gross of the aggregator fee, both balancing legs gross of the
BSP fee, and the PPA contract leg — e.g. the 3 % special RES turnover
levy applied in Greece.  Retail/self-consumption savings (avoided
cost, not invoiced turnover), the contracted streams (E29-E32) and the
imbalance settlement are excluded by construction; negative turnover
never yields a rebate (clamp).  Locked: zero-default bit-identity, the
per-year algebra with per-origin degradation and stream inflation, the
base scope (retail exclusion, contracted exclusion, PPA post-term
reversion), the clamp, fee_share monthly reconciliation, sensitivity
(Revenue driver SCALES it exactly; net recompute folds it), LCOE/LCOS
invariance and the registries.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import (
    build_yearly_cashflow,
    compute_financial_kpis,
    derive_monthly_cashflow,
)
from pvbess_opt.io import _SHEET_DEFAULTS, validate_workbook_params

N_YEARS = 5
LEVY_PCT = 3.0


def _econ(**o) -> dict:
    econ = {
        "project_lifecycle_years": N_YEARS,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 1.0,
        "dam_inflation_pct": 2.0,
        "bm_inflation_pct": 1.5,
        "unavailability_pct": 1.0,
        "capex_pv_eur_per_kw": 500.0,
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
        "aggregator_fee_pct_revenue": 5.0,
        "balancing_aggregator_fee_pct_revenue": 10.0,
        "bess_power_kw": 500.0,
        "revenue_levy_pct": LEVY_PCT,
    }
    econ.update(o)
    return econ


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}


def _kpis(**o) -> dict:
    base = {
        "profit_load_from_pv_eur": 50_000.0,
        "profit_load_from_bess_eur": 10_000.0,
        "profit_export_from_pv_eur": 30_000.0,
        "profit_export_from_bess_eur": 40_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 125_000.0,
        "bm_total_capacity_revenue_eur": 12_000.0,
        "bm_total_activation_revenue_eur": 3_000.0,
    }
    base.update(o)
    return base


def test_zero_default_bit_identity():
    base = build_yearly_cashflow(
        _kpis(), _econ(revenue_levy_pct=0.0), _caps(),
    )
    kw = _econ(revenue_levy_pct=0.0)
    kw.pop("revenue_levy_pct")
    without_key = build_yearly_cashflow(_kpis(), kw, _caps())
    pd.testing.assert_frame_equal(base, without_key)
    assert (base["revenue_levy_eur"] == 0.0).all()


def test_per_year_algebra_lock():
    """The levy tracks per-origin degradation and per-stream inflation
    through its base: gross DAM (pre-fee) + gross balancing + PPA leg,
    never the retail stream."""
    cf = build_yearly_cashflow(_kpis(), _econ(), _caps()).set_index(
        "project_year",
    )
    lam = LEVY_PCT / 100.0
    assert float(cf.loc[0, "revenue_levy_eur"]) == 0.0
    for y in range(1, N_YEARS + 1):
        pv_f = float(cf.loc[y, "pv_production_factor"])
        bess_f = float(cf.loc[y, "bess_capacity_factor"])
        g_dam = 1.02 ** (y - 1)
        g_bm = 1.015 ** (y - 1)
        gross_dam = (30_000.0 * pv_f + 35_000.0 * bess_f) * g_dam
        gross_bm = 15_000.0 * bess_f * g_bm
        expected = -lam * (gross_dam + gross_bm)
        assert float(cf.loc[y, "revenue_levy_eur"]) == pytest.approx(
            expected, abs=0.01,
        ), y
    # The base is PRE-aggregator-fee: with the 5 % fee on, the levy is
    # strictly larger in magnitude than 3 % of the net DAM column.
    y = 1
    net_dam = float(cf.loc[y, "revenue_dam_eur"])
    assert abs(float(cf.loc[y, "revenue_levy_eur"])) > lam * (
        net_dam + 15_000.0 * 0.90
    )


def test_base_scope_retail_ppa_and_contracted_exclusions():
    base_levy = build_yearly_cashflow(
        _kpis(), _econ(), _caps(),
    ).set_index("project_year")["revenue_levy_eur"]
    # Retail-heavy fixture: the levy never charges the retail stream.
    retail_heavy = build_yearly_cashflow(
        _kpis(profit_load_from_pv_eur=90_000.0,
              profit_total_eur=165_000.0),
        _econ(), _caps(),
    ).set_index("project_year")["revenue_levy_eur"]
    pd.testing.assert_series_equal(base_levy, retail_heavy)
    # Contracted streams are excluded by construction: state support
    # and capacity market leave the levy untouched.
    contracted = build_yearly_cashflow(
        _kpis(),
        _econ(
            state_support_eur_per_mw_year=40_000.0,
            capacity_market_eur_per_mw_year=50_000.0,
        ),
        _caps(),
    ).set_index("project_year")["revenue_levy_eur"]
    pd.testing.assert_series_equal(base_levy, contracted)
    # The PPA contract leg joins the base while in term; the physical
    # post-term reversion joins it through the DAM stream.
    ppa = build_yearly_cashflow(
        _kpis(revenue_pv_ppa_eur=20_000.0,
              ppa_covered_dam_value_eur=18_000.0,
              profit_total_eur=145_000.0),
        _econ(ppa_enabled=True, ppa_settlement="physical",
              ppa_term_years=2, ppa_inflation_pct=0.0),
        _caps(),
    ).set_index("project_year")
    lam = LEVY_PCT / 100.0
    for y in (1, 2):  # in term: contract leg in the base
        pv_f = float(ppa.loc[y, "pv_production_factor"])
        assert float(ppa.loc[y, "revenue_levy_eur"]) == pytest.approx(
            float(base_levy.loc[y]) - lam * 20_000.0 * pv_f, abs=0.01,
        ), y
    for y in (3, 4, 5):  # post term: covered DAM value rides the base
        pv_f = float(ppa.loc[y, "pv_production_factor"])
        g_dam = 1.02 ** (y - 1)
        assert float(ppa.loc[y, "revenue_levy_eur"]) == pytest.approx(
            float(base_levy.loc[y]) - lam * 18_000.0 * pv_f * g_dam,
            abs=0.01,
        ), y


def test_clamp_negative_turnover_never_rebates():
    """A deeply negative CfD difference leg can flip the turnover
    negative — the levy clamps at zero instead of rebating."""
    cf = build_yearly_cashflow(
        _kpis(
            profit_export_from_pv_eur=1_000.0,
            profit_export_from_bess_eur=5_000.0,
            bm_total_capacity_revenue_eur=0.0,
            bm_total_activation_revenue_eur=0.0,
            revenue_pv_ppa_eur=-20_000.0,
            ppa_covered_dam_value_eur=1_000.0,
            profit_total_eur=41_000.0,
        ),
        _econ(ppa_enabled=True, ppa_settlement="cfd", ppa_term_years=5),
        _caps(),
    ).set_index("project_year")
    for y in range(1, N_YEARS + 1):
        assert float(cf.loc[y, "revenue_levy_eur"]) == 0.0, y


def test_monthly_fee_share_reconciliation():
    from tests.test_monthly_cashflow_reconciliation import _make_res_frame

    res = _make_res_frame()
    econ = _econ()
    yearly = build_yearly_cashflow(_kpis(), econ, _caps())
    monthly, _q = derive_monthly_cashflow(res, yearly, econ)
    yearly_indexed = yearly.set_index("project_year")
    for y in range(1, N_YEARS + 1):
        sub = monthly.loc[monthly["project_year"] == y]
        assert float(sub["revenue_levy_eur"].sum()) == pytest.approx(
            float(yearly_indexed.loc[y, "revenue_levy_eur"]), abs=1e-6,
        ), y
        assert float(sub["net_cashflow_eur"].sum()) == pytest.approx(
            float(yearly_indexed.loc[y, "net_cashflow_eur"]), abs=1e-6,
        ), y


def test_sensitivity_scales_with_revenue_driver():
    from pvbess_opt.sensitivity import _recompute_net, _scale_revenue

    econ = _econ()
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    recomputed = _recompute_net(cf.copy())
    pd.testing.assert_series_equal(
        recomputed["net_cashflow_eur"], cf["net_cashflow_eur"],
    )
    pd.testing.assert_frame_equal(_scale_revenue(cf, 1.0), cf)
    scaled = _scale_revenue(cf, 1.1, econ)
    pd.testing.assert_series_equal(
        scaled["revenue_levy_eur"], cf["revenue_levy_eur"] * 1.1,
    )
    # CAPEX/OPEX drivers leave the levy untouched.
    from pvbess_opt.sensitivity import _scale_capex, _scale_opex

    pd.testing.assert_series_equal(
        _scale_capex(cf, 1.2)["revenue_levy_eur"], cf["revenue_levy_eur"],
    )
    pd.testing.assert_series_equal(
        _scale_opex(cf, 1.2)["revenue_levy_eur"], cf["revenue_levy_eur"],
    )


def _typed(**econ_overrides) -> dict:
    typed = {
        sheet: dict(defaults) for sheet, defaults in _SHEET_DEFAULTS.items()
    }
    typed["economics"].update(econ_overrides)
    return typed


@pytest.mark.parametrize("bad", [150.0, -3.0])
def test_levy_out_of_range_rejected(bad):
    with pytest.raises(ValueError, match="revenue_levy_pct"):
        validate_workbook_params(
            _typed(revenue_levy_pct=bad), dt_minutes=15,
        )


def _lifetime_yearly() -> pd.DataFrame:
    return pd.DataFrame({
        "project_year": range(1, N_YEARS + 1),
        "pv_generation_mwh": [1500.0] * N_YEARS,
        "bess_discharge_mwh": [300.0] * N_YEARS,
    })


def test_kpi_summary_theme_and_lcoe_invariance():
    from pvbess_opt.io import _SUMMARY_OPTIONAL_FINANCIAL_KEYS
    from pvbess_opt.theme import (
        FINANCIAL_COLORS,
        FINANCIAL_LABELS,
        FINANCIAL_LEGEND_ORDER,
    )

    base_fin = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), _econ(revenue_levy_pct=0.0),
                              _caps()),
        _econ(revenue_levy_pct=0.0), capacities=_caps(),
        lifetime_yearly=_lifetime_yearly(), year1_kpis=_kpis(),
    )
    cf = build_yearly_cashflow(_kpis(), _econ(), _caps())
    fin = compute_financial_kpis(
        cf, _econ(), capacities=_caps(),
        lifetime_yearly=_lifetime_yearly(), year1_kpis=_kpis(),
    )
    expected = float(
        cf.loc[cf["project_year"] >= 1, "revenue_levy_eur"].sum()
    )
    assert expected < 0.0
    assert fin["total_revenue_levy_eur_lifecycle"] == pytest.approx(
        expected, abs=0.01,
    )
    assert np.isclose(
        fin["lcoe_eur_per_mwh"], base_fin["lcoe_eur_per_mwh"],
    )
    assert np.isclose(
        fin["lcos_eur_per_mwh"], base_fin["lcos_eur_per_mwh"],
    )
    assert ("total_revenue_levy_eur_lifecycle",
            "Lifetime revenue levy [EUR]") in (
        _SUMMARY_OPTIONAL_FINANCIAL_KEYS
    )
    assert "Revenue levy" in FINANCIAL_LABELS
    assert "Revenue levy" in FINANCIAL_LEGEND_ORDER
    assert FINANCIAL_COLORS["revenue_levy"] == "#EC407A"
