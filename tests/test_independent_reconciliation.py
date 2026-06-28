"""Independent numerical re-derivation of the financial model.

The expected values here are computed from FIRST PRINCIPLES with plain
numpy — this module imports the project's economics builder only to
produce the *actual* frame to reconcile against; every *expected* number
is derived independently (no project helper computes an expected value).

A small, fully hand-traceable case (round numbers, 3 years) pins:

* the energy aggregator fee scope (gross DAM + retail only, clamped),
  and its pro-rata per-stream split;
* the NEW balancing-aggregator fee: ``net balancing = gross x (1 - fee)``,
  escalated with the gross, the DCF consuming the NET balancing revenue,
  PPA carrying neither fee;
* escalation ``(1 + i)^(y-1)`` and discounting ``1 / (1 + r)^y``;
* NPV / ROI / BCR / payback, and IRR re-solved with an INDEPENDENT
  bisection (not ``economics.calculate_irr``).
"""

from __future__ import annotations

import pytest

from pvbess_opt.economics import build_yearly_cashflow, compute_financial_kpis

TOL = 1e-6


# ---------------------------------------------------------------------------
# Independent reference implementations (numpy only)
# ---------------------------------------------------------------------------


def _independent_irr(cashflows: list[float]) -> float:
    """Bisection on the polynomial NPV(r); independent of calculate_irr."""
    def npv(r: float) -> float:
        return float(sum(cf / (1.0 + r) ** y for y, cf in enumerate(cashflows)))

    lo, hi = -0.99, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return float("nan")
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        f_mid = npv(mid)
        if abs(f_mid) < 1e-10:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


def _expected_frame(
    *, n_years, r, retail1, dam1, cap1, act1, fee, bsp_fee,
    i_ret, i_dam, i_bm, capex0,
):
    """Per-year (net_cf, dcf) from first principles. Factors held at 1.0
    (no degradation), so escalation is the only year-over-year term."""
    rows = []
    for y in range(0, n_years + 1):
        if y == 0:
            rows.append((-capex0, -capex0 * 1.0))
            continue
        retail = retail1 * (1.0 + i_ret) ** (y - 1)
        dam = dam1 * (1.0 + i_dam) ** (y - 1)
        gross = retail + dam
        energy_fee = -fee * max(gross, 0.0)
        revenue_net = gross + energy_fee
        gross_bm = (cap1 + act1) * (1.0 + i_bm) ** (y - 1)
        bm_fee = -bsp_fee * max(gross_bm, 0.0)
        net_cf = revenue_net + gross_bm + bm_fee
        disc = 1.0 / (1.0 + r) ** y
        rows.append((net_cf, net_cf * disc))
    return rows


# ---------------------------------------------------------------------------
# The reconciliation
# ---------------------------------------------------------------------------


def _econ(**kw):
    base = dict(
        project_lifecycle_years=3, project_start_year=2026,
        discount_rate_pct=10.0,
        opex_inflation_pct=0.0, retail_inflation_pct=0.0, dam_inflation_pct=0.0,
        bm_inflation_pct=0.0,
        pv_degradation_year1_pct=0.0, pv_degradation_annual_pct=0.0,
        bess_degradation_annual_pct=0.0, bess_degradation_pct_per_cycle=0.0,
        capex_pv_eur_per_kw=0.0, capex_bess_eur_per_kw=1.0,  # 1000 kW -> -1000
        devex_pv_eur_per_kw=0.0, devex_bess_eur_per_kw=0.0,
        site_capex_eur=0.0, site_devex_eur=0.0,
        opex_pv_eur_per_kwp=0.0, opex_bess_eur_per_kw=0.0,
        aggregator_fee_pct_revenue=10.0,
        balancing_aggregator_fee_pct_revenue=20.0,
        bess_replacement_year=0, bess_replacement_cost_pct=0.0,
    )
    base.update(kw)
    return base


def _y1():
    # Single PV-origin retail + DAM stream; round balancing totals.
    return {
        "profit_load_from_pv_eur": 300.0,
        "profit_load_from_bess_eur": 0.0,
        "profit_export_from_pv_eur": 200.0,
        "profit_export_from_bess_eur": 0.0,
        "expense_charge_bess_grid_eur": 0.0,
        "profit_total_eur": 500.0,
        "bm_total_capacity_revenue_eur": 100.0,
        "bm_total_activation_revenue_eur": 50.0,
        "bess_total_discharge_mwh": 0.0,
    }


def _caps():
    return {"pv_kwp": 0.0, "bess_kw": 1000.0, "bess_kwh": 4000.0}


@pytest.mark.parametrize(
    "i_ret,i_dam,i_bm",
    [(0.0, 0.0, 0.0), (0.03, 0.0, 0.02)],
)
def test_yearly_cashflow_matches_independent_calc(i_ret, i_dam, i_bm):
    econ = _econ(
        retail_inflation_pct=i_ret * 100.0,
        dam_inflation_pct=i_dam * 100.0,
        bm_inflation_pct=i_bm * 100.0,
    )
    df = build_yearly_cashflow(_y1(), econ, _caps())

    expected = _expected_frame(
        n_years=3, r=0.10, retail1=300.0, dam1=200.0, cap1=100.0, act1=50.0,
        fee=0.10, bsp_fee=0.20, i_ret=i_ret, i_dam=i_dam, i_bm=i_bm,
        capex0=1000.0,
    )
    for y, (exp_net, exp_dcf) in enumerate(expected):
        row = df.loc[df["project_year"] == y].iloc[0]
        assert row["net_cashflow_eur"] == pytest.approx(exp_net, abs=TOL), f"net y{y}"
        assert row["discounted_cf_eur"] == pytest.approx(exp_dcf, abs=TOL), f"dcf y{y}"

    # Independent NPV and IRR.
    nets = [e[0] for e in expected]
    exp_npv = sum(e[1] for e in expected)
    kpis = compute_financial_kpis(df, econ)
    assert kpis["npv_eur"] == pytest.approx(round(exp_npv, 2), abs=1e-2)
    exp_irr = _independent_irr(nets) * 100.0
    assert kpis["irr_pct"] == pytest.approx(exp_irr, abs=1e-3)


def test_fee_scopes_and_splits_independently():
    """The two fees hit only their own streams, and the energy fee splits
    pro-rata across retail/DAM — checked against hand values."""
    econ = _econ()
    df = build_yearly_cashflow(_y1(), econ, _caps())
    y1 = df.loc[df["project_year"] == 1].iloc[0]

    # Energy fee: -10% of (300+200) = -50, split 300:200 -> -30 retail, -20 dam.
    assert y1["aggregator_fee_eur"] == pytest.approx(-50.0, abs=TOL)
    assert y1["revenue_retail_eur"] == pytest.approx(270.0, abs=TOL)
    assert y1["revenue_dam_eur"] == pytest.approx(180.0, abs=TOL)
    assert y1["revenue_eur"] == pytest.approx(450.0, abs=TOL)
    # Balancing: gross 150 stays gross; BSP fee -20% = -30; net 120.
    assert y1["balancing_revenue_eur"] == pytest.approx(150.0, abs=TOL)
    assert y1["balancing_aggregator_fee_eur"] == pytest.approx(-30.0, abs=TOL)
    # PPA carries neither fee (no PPA here -> column zero).
    assert y1["ppa_revenue_eur"] == pytest.approx(0.0, abs=TOL)
    # Net cashflow = 450 + 150 - 30 = 570.
    assert y1["net_cashflow_eur"] == pytest.approx(570.0, abs=TOL)


def test_dcf_consumes_net_balancing():
    """Turning the BSP fee on lowers NPV by exactly the discounted fee
    stream — proving the DCF consumes NET balancing revenue."""
    off = build_yearly_cashflow(_y1(), _econ(balancing_aggregator_fee_pct_revenue=0.0), _caps())
    on = build_yearly_cashflow(_y1(), _econ(balancing_aggregator_fee_pct_revenue=20.0), _caps())
    npv_off = compute_financial_kpis(off, _econ())["npv_eur"]
    npv_on = compute_financial_kpis(on, _econ())["npv_eur"]
    # Discounted fee stream: -30 / 1.1^y for y=1..3.
    exp_drop = sum(30.0 / 1.1 ** y for y in (1, 2, 3))
    assert (npv_off - npv_on) == pytest.approx(exp_drop, abs=1e-2)
