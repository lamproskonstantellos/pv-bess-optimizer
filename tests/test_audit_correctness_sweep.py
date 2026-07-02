"""Cross-module correctness locks from the release audit sweep.

Four findings locked here:

1. ``derive_monthly_cashflow`` dropped investment EVENTS — in a BESS
   replacement year, the monthly (and quarterly) net summed exactly one
   replacement CAPEX above the yearly row, silently contradicting the
   yearly sheet in the shipped default configuration
   (``bess_replacement_year = 10``).  The frames now carry
   ``capex_eur`` / ``devex_eur`` booked in month 12 (the end-of-year
   placement matches the yearly ``1/(1+r)^y`` discounting), so monthly
   sums reconcile to the yearly net in EVERY operating year.
2. ``aggregate_lifetime_to_yearly``'s revenue scope: the column is the
   PRE-fee gross (the old docstring claimed post-fee).  Locked as
   ``revenue_eur_dam_retail == revenue_eur - aggregator_fee_eur`` at
   zero DAM/retail indexation with a non-zero fee.
3. The BESS degradation curve has one owner (``lifetime._bess_factor``)
   and three consumers; a sweep across calendar fade x cycle fade x
   replacement year (set / unset / at horizon / beyond horizon) keeps
   the cashflow factor, the lifetime-frame scaling, and the SOH
   diagnostic numerically identical.
4. ``monte_carlo_balancing`` joins the pipeline (the README's report
   list promised the reservation profile + MC distribution, but no
   caller existed): the ``availability_factor`` hook scales the whole
   distribution into the same derated scope as the deterministic
   ``bm_*`` KPIs, and ``bm_mc_scenarios`` parameterizes the ensemble.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import (
    build_yearly_cashflow,
    derive_monthly_cashflow,
)
from pvbess_opt.lifetime import (
    _bess_factor,
    aggregate_lifetime_to_yearly,
    build_lifetime_dispatch,
)


def _econ(**overrides) -> dict:
    out = {
        "project_lifecycle_years": 12,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "bm_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 525.0,
        # 50 EUR/kWh x 4,000 kWh == the original 200 EUR/kW x 1,000 kW
        # = 200,000 EUR, keeping the replacement-CAPEX expectation intact.
        "capex_bess_eur_per_kwh": 50.0,
        "devex_pv_eur_per_kw": 60.0,
        "devex_bess_eur_per_kw": 30.0,
        "opex_pv_eur_per_kwp": 7.0,
        "opex_bess_eur_per_kw": 14.0,
        "pv_degradation_year1_pct": 2.5,
        "pv_degradation_annual_pct": 0.55,
        "bess_degradation_annual_pct": 2.0,
        "bess_degradation_pct_per_cycle": 0.008,
        "bess_replacement_year": 6,
        "bess_replacement_cost_pct": 50.0,
        "aggregator_fee_pct_revenue": 10.0,
    }
    out.update(overrides)
    return out


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 1000.0, "bess_kwh": 4000.0}


def _kpis() -> dict:
    return {
        "profit_total_eur": 900_000.0,
        "profit_load_from_pv_eur": 500_000.0,
        "profit_load_from_bess_eur": 150_000.0,
        "profit_export_from_pv_eur": 200_000.0,
        "profit_export_from_bess_eur": 80_000.0,
        "expense_charge_bess_grid_eur": 30_000.0,
        "pv_generation_mwh": 1_500.0,
        "bess_total_discharge_mwh": 1_200.0,
    }


def _res_year1(n_days: int = 4) -> pd.DataFrame:
    """Tiny Year-1 dispatch frame with the per-step EUR columns."""
    n = 24 * n_days
    rng = np.random.default_rng(0)
    ts = pd.date_range("2026-01-01", periods=n, freq="h")
    return pd.DataFrame({
        "timestamp": ts,
        "pv_kwh": rng.uniform(0, 100, n),
        "pv_to_load_kwh": rng.uniform(0, 40, n),
        "pv_to_grid_kwh": rng.uniform(0, 40, n),
        "pv_to_bess_kwh": rng.uniform(0, 10, n),
        "pv_curtail_kwh": np.zeros(n),
        "bess_dis_load_kwh": rng.uniform(0, 20, n),
        "bess_dis_grid_kwh": rng.uniform(0, 20, n),
        "bess_charge_grid_kwh": np.zeros(n),
        "grid_to_load_kwh": rng.uniform(0, 30, n),
        "grid_export_total_kwh": np.zeros(n),
        "soc_kwh": rng.uniform(500, 3500, n),
        "profit_load_from_pv_eur": rng.uniform(0, 5, n),
        "profit_load_from_bess_eur": rng.uniform(0, 2, n),
        "profit_export_from_pv_eur": rng.uniform(0, 4, n),
        "profit_export_from_bess_eur": rng.uniform(0, 2, n),
        "expense_charge_bess_grid_eur": rng.uniform(0, 0.5, n),
    })


# ---------------------------------------------------------------------------
# 1. Monthly / quarterly frames reconcile through a replacement year
# ---------------------------------------------------------------------------


def test_monthly_and_quarterly_reconcile_in_replacement_year():
    econ, caps, kpis = _econ(), _caps(), _kpis()
    yearly = build_yearly_cashflow(kpis, econ, caps)
    monthly, quarterly = derive_monthly_cashflow(_res_year1(), yearly, econ)

    op = yearly[yearly["project_year"] >= 1].set_index("project_year")
    mo_net = monthly.groupby("project_year")["net_cashflow_eur"].sum()
    qt_net = quarterly.groupby("project_year")["net_cashflow_eur"].sum()
    for y in op.index:
        assert mo_net.loc[y] == pytest.approx(
            float(op.loc[y, "net_cashflow_eur"]), abs=0.01,
        ), f"monthly net diverges from yearly in year {y}"
        assert qt_net.loc[y] == pytest.approx(
            float(op.loc[y, "net_cashflow_eur"]), abs=0.01,
        ), f"quarterly net diverges from yearly in year {y}"

    # The replacement CAPEX books in month 12 of the replacement year.
    repl = int(econ["bess_replacement_year"])
    dec = monthly[(monthly["project_year"] == repl) & (monthly["period"] == 12)]
    expected_capex = -50.0 * 4000.0 * 0.50  # EUR/kWh x kWh x repl. pct
    assert float(dec["capex_eur"].iloc[0]) == pytest.approx(expected_capex)
    other = monthly[(monthly["project_year"] == repl) & (monthly["period"] != 12)]
    assert float(other["capex_eur"].abs().sum()) == 0.0


def test_monthly_december_event_discounts_like_the_yearly_row():
    """Month-12 placement carries exactly the yearly 1/(1+r)^y factor."""
    econ, caps, kpis = _econ(), _caps(), _kpis()
    yearly = build_yearly_cashflow(kpis, econ, caps)
    monthly, _q = derive_monthly_cashflow(_res_year1(), yearly, econ)
    repl = int(econ["bess_replacement_year"])
    dec = monthly[(monthly["project_year"] == repl) & (monthly["period"] == 12)]
    net = float(dec["net_cashflow_eur"].iloc[0])
    disc = float(dec["discounted_cf_eur"].iloc[0])
    assert disc == pytest.approx(net / 1.07 ** repl, abs=1e-9)


def test_monthly_sums_still_reconcile_without_replacement():
    econ = _econ(bess_replacement_year=0)
    yearly = build_yearly_cashflow(_kpis(), econ, _caps())
    monthly, _q = derive_monthly_cashflow(_res_year1(), yearly, econ)
    op = yearly[yearly["project_year"] >= 1].set_index("project_year")
    mo_net = monthly.groupby("project_year")["net_cashflow_eur"].sum()
    for y in op.index:
        assert mo_net.loc[y] == pytest.approx(
            float(op.loc[y, "net_cashflow_eur"]), abs=0.01,
        )
    assert float(monthly["capex_eur"].abs().sum()) == 0.0


# ---------------------------------------------------------------------------
# 2. Lifetime aggregate is pre-fee gross
# ---------------------------------------------------------------------------


def test_lifetime_aggregate_is_pre_fee_gross():
    """revenue_eur_dam_retail == cashflow revenue_eur - aggregator_fee_eur
    (fee signed negative) at zero DAM/retail indexation, fee = 10 %."""
    econ, caps = _econ(), _caps()
    res = _res_year1()
    # Build the Year-1 KPI dict from the SAME frame so the two paths
    # share one revenue base.
    kpis = {
        "profit_load_from_pv_eur": float(res["profit_load_from_pv_eur"].sum()),
        "profit_load_from_bess_eur": float(res["profit_load_from_bess_eur"].sum()),
        "profit_export_from_pv_eur": float(res["profit_export_from_pv_eur"].sum()),
        "profit_export_from_bess_eur": float(
            res["profit_export_from_bess_eur"].sum()
        ),
        "expense_charge_bess_grid_eur": float(
            res["expense_charge_bess_grid_eur"].sum()
        ),
        "bess_total_discharge_mwh": float(
            (res["bess_dis_load_kwh"] + res["bess_dis_grid_kwh"]).sum() / 1000.0
        ),
    }
    kpis["profit_total_eur"] = (
        kpis["profit_load_from_pv_eur"] + kpis["profit_load_from_bess_eur"]
        + kpis["profit_export_from_pv_eur"]
        + kpis["profit_export_from_bess_eur"]
        - kpis["expense_charge_bess_grid_eur"]
    )
    yearly = build_yearly_cashflow(kpis, econ, caps)
    lifetime = build_lifetime_dispatch(
        res, econ, caps, year1_discharge_mwh=kpis["bess_total_discharge_mwh"],
    )
    agg = aggregate_lifetime_to_yearly(lifetime)
    merged = agg.merge(
        yearly[yearly["project_year"] >= 1][
            ["project_year", "revenue_eur", "aggregator_fee_eur"]
        ],
        on="project_year",
    )
    gross = merged["revenue_eur"] - merged["aggregator_fee_eur"]
    assert np.allclose(merged["revenue_eur_dam_retail"], gross, atol=0.01), (
        merged[["project_year", "revenue_eur_dam_retail"]].assign(gross=gross)
    )
    # And it is NOT the post-fee net (the old docstring's claim).
    assert (
        (merged["revenue_eur_dam_retail"] - merged["revenue_eur"]).abs().max()
        > 1.0
    )


# ---------------------------------------------------------------------------
# 3. Degradation-curve three-way agreement across a parameter sweep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("d_annual,d_cycle,repl", [
    (2.0, 0.0, 0),       # calendar only, no replacement
    (2.0, 0.008, 6),     # shipped-style: both fades, mid-horizon swap
    (3.0, 0.02, 12),     # replacement in the final year
    (2.0, 0.008, 25),    # replacement beyond the horizon (no reset)
])
def test_degradation_owner_consumers_agree(d_annual, d_cycle, repl):
    from pvbess_opt.degradation import build_degradation_report

    econ = _econ(
        bess_degradation_annual_pct=d_annual,
        bess_degradation_pct_per_cycle=d_cycle,
        bess_replacement_year=repl,
    )
    caps, kpis = _caps(), _kpis()
    res = _res_year1()
    y1_dis = float(kpis["bess_total_discharge_mwh"])

    cf = build_yearly_cashflow(kpis, econ, caps)
    op = cf[cf["project_year"] >= 1].set_index("project_year")

    lifetime = build_lifetime_dispatch(
        res, econ, caps, year1_discharge_mwh=y1_dis,
    )
    base_dis = float(res["bess_dis_grid_kwh"].sum())
    lt_factor = {
        int(y): float(
            grp["bess_dis_grid_kwh"].sum() / base_dis
        )
        for y, grp in lifetime.groupby("project_year")
    }

    soh = build_degradation_report(
        res["soc_kwh"],
        capacity_kwh=caps["bess_kwh"],
        soc_min_frac=0.1,
        soc_max_frac=0.95,
        degradation_pct_per_cycle=d_cycle,
        degradation_annual_pct=d_annual,
        year1_discharge_mwh=y1_dis,
        project_years=int(econ["project_lifecycle_years"]),
        start_year=int(econ["project_start_year"]),
        replacement_year=repl,
    ).set_index("project_year")

    # Re-derive the owner curve by hand for reference.
    cum = 0.0
    for y in op.index:
        if repl > 0 and y == repl:
            cum = 0.0
        owner = _bess_factor(
            int(y), d_annual / 100.0, replacement_year=repl,
            d_bess_per_cycle=d_cycle / 100.0,
            cumulative_cycles_through=cum,
        )
        cum += y1_dis * owner / (caps["bess_kwh"] / 1000.0)
        assert float(op.loc[y, "bess_capacity_factor"]) == pytest.approx(
            owner, abs=1e-12,
        ), f"cashflow factor diverges at year {y}"
        assert lt_factor[int(y)] == pytest.approx(owner, abs=1e-9), (
            f"lifetime scaling diverges at year {y}"
        )
        # The SOH diagnostic resets to a fresh pack on EoL swaps it
        # schedules itself when repl == 0; restrict the equality to the
        # scheduled-replacement configurations.  The sheet rounds
        # soh_pct to 4 dp, so agreement is asserted to half an ulp.
        if repl > 0:
            assert float(soh.loc[y, "soh_pct"]) == pytest.approx(
                owner * 100.0, abs=5e-5,
            ), f"SOH diagnostic diverges at year {y}"


# ---------------------------------------------------------------------------
# 4. Balancing Monte Carlo joins the headline scope
# ---------------------------------------------------------------------------


def _bm_res_and_params() -> tuple[pd.DataFrame, dict]:
    from pvbess_opt.io import BALANCING_SHEET_DEFAULTS

    n = 96
    res = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="15min"),
        "soc_kwh": np.full(n, 2000.0),
        **{f"bm_reservation_{p}_kw": np.full(n, 50.0)
           for p in ("fcr", "afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn")},
        **{f"{p}_capacity_price_eur_per_mwh": np.full(n, 15.0)
           for p in ("fcr", "afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn")},
        **{f"{p}_activation_price_eur_per_mwh": np.full(n, 100.0)
           for p in ("afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn")},
    })
    params = {
        "dt_minutes": 15,
        "bess_capacity_kwh": 4000.0,
        "soc_min_frac": 0.1,
        "soc_max_frac": 0.95,
        "efficiency_charge": 0.97,
        "efficiency_discharge": 0.97,
        "balancing": dict(BALANCING_SHEET_DEFAULTS, balancing_enabled=True),
    }
    return res, params


def test_balancing_mc_availability_factor_scales_the_distribution():
    from pvbess_opt.rolling_horizon import monte_carlo_balancing

    res, params = _bm_res_and_params()
    raw = monte_carlo_balancing(res, params, n_scenarios=40, seed=7)
    derated = monte_carlo_balancing(
        res, params, n_scenarios=40, seed=7, availability_factor=0.9,
    )
    assert derated["bm_total_balancing_revenue_p50_eur"] == pytest.approx(
        0.9 * raw["bm_total_balancing_revenue_p50_eur"], abs=0.05,
    )
    np.testing.assert_allclose(
        derated["bm_mc_total_realised_eur"],
        0.9 * np.asarray(raw["bm_mc_total_realised_eur"]),
        atol=1e-6,
    )


def test_bm_mc_scenarios_key_reaches_the_config():
    from pvbess_opt.balancing import resolve_balancing_config
    from pvbess_opt.io import BALANCING_SHEET_DEFAULTS

    assert BALANCING_SHEET_DEFAULTS["bm_mc_scenarios"] == 200
    cfg = resolve_balancing_config({"bm_mc_scenarios": 64})
    assert cfg.bm_mc_scenarios == 64


def test_pipeline_emits_balancing_mc_kpis_and_plots(tmp_path):
    """A balancing-enabled pipeline run carries the MC quantile KPIs and
    writes the two README-promised balancing plots."""
    from pvbess_opt.io import (
        BALANCING_SHEET_DEFAULTS,
        BESS_SHEET_DEFAULTS,
        ECONOMICS_SHEET_DEFAULTS,
        PROJECT_SHEET_DEFAULTS,
        PV_SHEET_DEFAULTS,
        SIMULATION_SHEET_DEFAULTS,
        write_workbook,
    )
    from pvbess_opt.pipeline import RunConfig, run

    n = 48
    h = np.arange(n) % 24
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=n, freq="h"),
        "pv_kwh": np.clip(4000 * np.sin(np.pi * (h - 6) / 12.0), 0, None)
        * ((h >= 6) & (h <= 18)),
        "load_kwh": 1000.0 + 500 * np.exp(-((h - 9) ** 2) / 8.0),
        "dam_price_eur_per_mwh": 100.0 - 50 * np.sin(np.pi * (h - 6) / 12.0),
    })
    typed = {
        "ts": ts,
        "project": dict(PROJECT_SHEET_DEFAULTS, unavailability_pct=10.0),
        "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=4500.0),
        "bess": dict(
            BESS_SHEET_DEFAULTS, bess_power_kw=2000.0,
            bess_capacity_kwh=8000.0,
        ),
        "economics": dict(ECONOMICS_SHEET_DEFAULTS, sensitivity_enabled=False),
        "simulation": dict(SIMULATION_SHEET_DEFAULTS),
        "balancing": dict(
            BALANCING_SHEET_DEFAULTS, balancing_enabled=True,
            bm_settlement_minutes=60, bm_mc_scenarios=25,
        ),
        "max_injection_profile": np.full(24, 100.0),
    }
    xlsx = tmp_path / "bm.xlsx"
    write_workbook(typed, xlsx)
    result = run(RunConfig(excel=xlsx, outdir=tmp_path / "out", mip_gap=0.01))
    kpis = result.kpis
    assert "bm_total_balancing_revenue_p50_eur" in kpis
    assert "bm_total_balancing_revenue_p10_eur" in kpis
    # Same derated scope as the deterministic expected-value KPI.
    assert kpis["availability_factor"] == pytest.approx(0.9)
    p50 = float(kpis["bm_total_balancing_revenue_p50_eur"])
    deterministic = float(kpis["bm_total_balancing_revenue_eur"])
    assert 0.5 * deterministic < p50 < 1.5 * deterministic
    plots = result.out_dir / "04_financial_plots"
    assert (plots / "balancing_reservation_profile.pdf").exists()
    assert (plots / "balancing_mc_distribution.pdf").exists()
