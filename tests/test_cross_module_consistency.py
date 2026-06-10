"""Cross-module single-source-of-truth sweep (the SOH bug class).

The BESS capacity factor (calendar + cycle fade, with optional scheduled
replacement) is derived independently in four places:

* ``economics.build_yearly_cashflow``   (cashflow projection),
* ``lifetime.build_lifetime_dispatch``  (hourly lifetime scaling),
* ``degradation.build_degradation_report`` (SOH diagnostic),
* ``economics.compute_financial_kpis`` (final-year fade decomposition).

Each keeps its own cumulative-cycle accumulator around the shared
``lifetime._bess_factor``.  This sweep asserts they agree numerically
across degradation rates, cycle fade, replacement years (unset / set /
beyond horizon), horizons, and availability derates — any drift is a
regression of the SOH-vs-finance divergence fixed pre-release.
"""

from __future__ import annotations

import itertools

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.degradation import build_degradation_report
from pvbess_opt.economics import build_yearly_cashflow, compute_financial_kpis
from pvbess_opt.lifetime import aggregate_lifetime_to_yearly, build_lifetime_dispatch


def _make_res(n_steps: int = 96) -> pd.DataFrame:
    ts = pd.date_range("2026-01-01", periods=n_steps, freq="15min")
    return pd.DataFrame({
        "timestamp": ts,
        "pv_kwh": np.full(n_steps, 30.0),
        "pv_to_load_kwh": np.full(n_steps, 15.0),
        "pv_to_grid_kwh": np.full(n_steps, 15.0),
        "bess_dis_load_kwh": np.full(n_steps, 25.0),
        "bess_dis_grid_kwh": np.full(n_steps, 25.0),
        "bess_charge_grid_kwh": np.zeros(n_steps),
        "pv_to_bess_kwh": np.zeros(n_steps),
        "soc_kwh": np.full(n_steps, 500.0),
        "profit_load_from_pv_eur": np.full(n_steps, 1.0),
        "profit_load_from_bess_eur": np.full(n_steps, 0.5),
        "profit_export_from_pv_eur": np.full(n_steps, 0.8),
        "profit_export_from_bess_eur": np.full(n_steps, 0.4),
        "expense_charge_bess_grid_eur": np.zeros(n_steps),
    })


def _econ(d_ann, d_cyc, repl, n_years, unav) -> dict:
    return {
        "project_lifecycle_years": n_years,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 1.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 500.0,
        "capex_bess_eur_per_kw": 200.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 7.0,
        "opex_bess_eur_per_kw": 14.0,
        "pv_degradation_year1_pct": 2.5,
        "pv_degradation_annual_pct": 0.55,
        "bess_degradation_annual_pct": d_ann,
        "bess_degradation_pct_per_cycle": d_cyc,
        "bess_replacement_year": repl,
        "bess_replacement_cost_pct": 50.0,
        "aggregator_fee_pct_revenue": 10.0,
        "unavailability_pct": unav,
    }


_SWEEP = list(itertools.product(
    [0.0, 2.0],          # bess_degradation_annual_pct
    [0.0, 0.1],          # bess_degradation_pct_per_cycle
    [0, 6, 25],          # bess_replacement_year (unset / set / beyond horizon)
    [20],                # n_years
    [0.0, 8.0],          # unavailability_pct
))


@pytest.mark.parametrize("d_ann,d_cyc,repl,n_years,unav", _SWEEP)
def test_bess_factor_single_source_of_truth(d_ann, d_cyc, repl, n_years, unav):
    avail = 1.0 - unav / 100.0
    res = _make_res()
    cap_kwh = 1000.0
    raw_dis_mwh = float(
        (res["bess_dis_load_kwh"] + res["bess_dis_grid_kwh"]).sum()
    ) / 1000.0
    derated_dis_mwh = raw_dis_mwh * avail

    econ = _econ(d_ann, d_cyc, repl, n_years, unav)
    caps = {"pv_kwp": 100.0, "bess_kw": 100.0, "bess_kwh": cap_kwh}
    kpis = {
        "profit_total_eur": 1000.0 * avail,
        "profit_load_from_pv_eur": 400.0 * avail,
        "profit_load_from_bess_eur": 200.0 * avail,
        "profit_export_from_pv_eur": 300.0 * avail,
        "profit_export_from_bess_eur": 100.0 * avail,
        "expense_charge_bess_grid_eur": 0.0,
        "bess_total_discharge_mwh": derated_dis_mwh,
        "pv_generation_mwh": float(res["pv_kwh"].sum()) / 1000.0 * avail,
    }

    ycf = build_yearly_cashflow(kpis, econ, caps)
    fac_econ = ycf.set_index("project_year")["bess_capacity_factor"]
    pvf_econ = ycf.set_index("project_year")["pv_production_factor"]

    # Lifetime hourly scaling — the pipeline passes the derated discharge.
    lt = build_lifetime_dispatch(
        res, econ, caps, year1_discharge_mwh=derated_dis_mwh,
    )
    y1_dis = float((res["bess_dis_load_kwh"] + res["bess_dis_grid_kwh"]).sum())
    y1_pv = float(res["pv_kwh"].sum())
    for y in range(1, n_years + 1):
        sub = lt[lt["project_year"] == y]
        f_lt = float(
            (sub["bess_dis_load_kwh"] + sub["bess_dis_grid_kwh"]).sum()
        ) / y1_dis
        assert f_lt == pytest.approx(float(fac_econ.loc[y]), abs=1e-9), (
            f"lifetime vs cashflow bess factor drift at year {y}"
        )
        pv_lt = float(sub["pv_kwh"].sum()) / y1_pv
        assert pv_lt == pytest.approx(float(pvf_econ.loc[y]), abs=1e-9), (
            f"lifetime vs cashflow pv factor drift at year {y}"
        )

    # SOH diagnostic (scheduled mode; the advisory EoL reset is disabled
    # so the curve tracks the finance factor exactly).
    rep = build_degradation_report(
        res["soc_kwh"], capacity_kwh=cap_kwh, soc_min_frac=0.0,
        soc_max_frac=1.0, degradation_pct_per_cycle=d_cyc,
        degradation_annual_pct=d_ann, year1_discharge_mwh=derated_dis_mwh,
        project_years=n_years, start_year=2026, replacement_year=repl,
        end_of_life_soh_pct=-1.0,
    )
    soh = rep.set_index("project_year")["soh_pct"]
    for y in range(1, n_years + 1):
        assert float(soh.loc[y]) / 100.0 == pytest.approx(
            float(fac_econ.loc[y]), abs=5e-7,  # soh is rounded to 4 dp
        ), f"SOH diagnostic vs cashflow bess factor drift at year {y}"

    # Final-year fade decomposition.
    lty = aggregate_lifetime_to_yearly(lt)
    for col in ("pv_generation_mwh", "bess_discharge_mwh"):
        lty[col] = lty[col] * avail
    fin = compute_financial_kpis(
        ycf, econ, capacities=caps, lifetime_yearly=lty, year1_kpis=kpis,
    )
    expected_fade = (1.0 - float(fac_econ.loc[n_years])) * 100.0
    assert fin["bess_total_fade_pct_y_final"] == pytest.approx(
        expected_fade, abs=1e-6,
    )
    assert (
        fin["bess_calendar_fade_pct_y_final"]
        + fin["bess_cycle_fade_pct_y_final"]
    ) == pytest.approx(fin["bess_total_fade_pct_y_final"], abs=1e-6)


@pytest.mark.parametrize("repl,expect_charge", [(0, False), (6, True), (25, False)])
def test_replacement_capex_charged_only_in_horizon(repl, expect_charge):
    """Replacement CAPEX lands in exactly the replacement year — never when
    unset, never when scheduled beyond the horizon — and the capacity
    factor resets in the same year the CAPEX is charged."""
    econ = _econ(2.0, 0.0, repl, 20, 0.0)
    caps = {"pv_kwp": 100.0, "bess_kw": 100.0, "bess_kwh": 1000.0}
    kpis = {"profit_total_eur": 1000.0, "bess_total_discharge_mwh": 0.0}
    ycf = build_yearly_cashflow(kpis, econ, caps)

    op = ycf[ycf["project_year"] >= 1]
    charged_years = op.loc[op["capex_eur"] < 0, "project_year"].tolist()
    if expect_charge:
        assert charged_years == [repl]
        expected = -(econ["capex_bess_eur_per_kw"] * caps["bess_kw"]) * (
            econ["bess_replacement_cost_pct"] / 100.0
        )
        actual = float(
            op.loc[op["project_year"] == repl, "capex_eur"].iloc[0]
        )
        assert actual == pytest.approx(expected)
        # Fade resets in the SAME year the CAPEX is charged.
        assert float(
            op.loc[op["project_year"] == repl, "bess_capacity_factor"].iloc[0]
        ) == pytest.approx(1.0)
    else:
        assert charged_years == []
        # No reset anywhere: the factor decays monotonically.
        factors = op["bess_capacity_factor"].to_numpy(dtype=float)
        assert (np.diff(factors) <= 1e-12).all()
