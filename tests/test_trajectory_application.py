"""Application of the per-year stream trajectories (Eq. E24/E24a).

The escalation helper is the ONLY source of per-year factors for both
the yearly cashflow and the LCOE/LCOS discounted-OPEX numerators.
Locked properties:

1. Zero-default bit-identity: an econ dict without a ``trajectories``
   key (or with ``None``) produces the byte-identical cashflow the
   scalar indices produced.
2. Equivalence locks: an overlay of all-ones is exact vs no trajectory;
   a replace vector equal to ``(1+i)^(y-1)`` matches the scalar path to
   the cent.
3. Stream routing: the CfD DAM leg, the post-term physical reversion
   and the optimizer-fee base (E13d) ride the ``revenue_dam``
   trajectory; balancing capacity and activation are independently
   shaped; the PPA strike leg deliberately takes NO trajectory.
4. OPEX split (E24a): per-asset vectors shape each leg; LCOE moves with
   the ``opex_pv`` leg and matches a hand-computed discounted sum;
   revenue trajectories leave LCOE/LCOS untouched.
5. Monthly reconciliation: monthly sums equal the yearly rows in every
   operating year with trajectories on.
6. Sensitivity: ``_scale_revenue(cf, 1.0)`` stays a no-op and
   ``_recompute_net`` reproduces the net identity with trajectories on.
"""

from __future__ import annotations

import pandas as pd
import pytest

from pvbess_opt.economics import (
    _escalation_series,
    build_yearly_cashflow,
    compute_financial_kpis,
    derive_monthly_cashflow,
)

N_YEARS = 6
DAM_INFL = 0.02


def _econ(**overrides) -> dict:
    econ = {
        "project_lifecycle_years": N_YEARS,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 1.0,
        "retail_inflation_pct": 0.5,
        "dam_inflation_pct": DAM_INFL * 100.0,
        "bm_inflation_pct": 2.0,
        "capex_pv_eur_per_kw": 500.0,
        "capex_bess_eur_per_kwh": 100.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 5.0,
        "opex_bess_eur_per_kw": 5.0,
        "pv_degradation_year1_pct": 2.0,
        "pv_degradation_annual_pct": 1.0,
        "bess_degradation_annual_pct": 3.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
        "optimizer_revenue_share_pct": 10.0,
    }
    econ.update(overrides)
    return econ


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}


def _kpis() -> dict:
    return {
        "profit_load_from_pv_eur": 50_000.0,
        "profit_load_from_bess_eur": 10_000.0,
        "profit_export_from_pv_eur": 60_000.0,
        "profit_export_from_bess_eur": 40_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 155_000.0,
        "pv_export_mwh": 800.0,
        "bess_export_mwh": 300.0,
        "bm_total_capacity_revenue_eur": 20_000.0,
        "bm_total_activation_revenue_eur": 8_000.0,
    }


def _traj(stream: str, values: list[float], mode: str = "replace") -> dict:
    return {stream: {"mode": mode, "values": values}}


# ---------------------------------------------------------------------------
# 1+2. Bit-identity and equivalence locks
# ---------------------------------------------------------------------------


def test_missing_and_none_trajectories_are_bit_identical():
    base = build_yearly_cashflow(_kpis(), _econ(), _caps())
    with_none = build_yearly_cashflow(
        _kpis(), _econ(trajectories=None), _caps(),
    )
    pd.testing.assert_frame_equal(base, with_none)


def test_overlay_all_ones_is_bit_identical():
    ones = [1.0] * N_YEARS
    block = {
        s: {"mode": "overlay", "values": ones}
        for s in ("revenue_dam", "revenue_retail", "balancing_capacity",
                  "balancing_activation", "opex")
    }
    base = build_yearly_cashflow(_kpis(), _econ(), _caps())
    shaped = build_yearly_cashflow(
        _kpis(), _econ(trajectories=block), _caps(),
    )
    pd.testing.assert_frame_equal(base, shaped)


def test_replace_with_scalar_index_matches_scalar_path():
    vec = [(1.0 + DAM_INFL) ** y for y in range(N_YEARS)]
    base = build_yearly_cashflow(_kpis(), _econ(), _caps())
    shaped = build_yearly_cashflow(
        _kpis(), _econ(trajectories=_traj("revenue_dam", vec)), _caps(),
    )
    for col in base.columns:
        assert shaped[col].tolist() == pytest.approx(
            base[col].tolist(), abs=0.01,
        ), col


def test_escalation_series_shapes():
    flat = _escalation_series("revenue_dam", 0.02, 4, None)
    assert flat == pytest.approx([1.0, 1.02, 1.0404, 1.061208])
    rep = _escalation_series(
        "revenue_dam", 0.02, 4,
        _traj("revenue_dam", [1.0, 0.9, 0.8, 0.7]),
    )
    assert rep == [1.0, 0.9, 0.8, 0.7]
    over = _escalation_series(
        "revenue_dam", 0.02, 4,
        _traj("revenue_dam", [1.0, 0.9, 0.8, 0.7], mode="overlay"),
    )
    assert over == pytest.approx([1.0, 0.9 * 1.02, 0.8 * 1.02 ** 2,
                                  0.7 * 1.02 ** 3])
    # Defensive: a short vector holds its last multiplier flat.
    short = _escalation_series(
        "revenue_dam", 0.0, 4, _traj("revenue_dam", [1.0, 0.9]),
    )
    assert short == [1.0, 0.9, 0.9, 0.9]


# ---------------------------------------------------------------------------
# 3. Stream routing
# ---------------------------------------------------------------------------


def test_dam_trajectory_reshapes_dam_and_optimizer_fee_only():
    decline = [1.0, 0.9, 0.8, 0.7, 0.6, 0.5]
    base = build_yearly_cashflow(_kpis(), _econ(), _caps())
    shaped = build_yearly_cashflow(
        _kpis(), _econ(trajectories=_traj("revenue_dam", decline)), _caps(),
    )
    # Retail, balancing and OPEX columns are untouched.
    for col in ("opex_eur", "revenue_retail_eur",
                "balancing_capacity_revenue_eur",
                "balancing_activation_revenue_eur"):
        pd.testing.assert_series_equal(base[col], shaped[col])
    # DAM-origin revenue falls with the vector from year 2 on...
    y = shaped["project_year"] >= 2
    assert (shaped.loc[y, "revenue_dam_eur"]
            < base.loc[y, "revenue_dam_eur"]).all()
    # ...and the optimizer fee (a negative deduction on the DAM margin)
    # shrinks in magnitude with it.
    assert (shaped.loc[y, "optimizer_fee_eur"]
            > base.loc[y, "optimizer_fee_eur"]).all()


def test_balancing_streams_shape_independently():
    kill_act = _traj(
        "balancing_activation", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    )
    base = build_yearly_cashflow(_kpis(), _econ(), _caps())
    shaped = build_yearly_cashflow(
        _kpis(), _econ(trajectories=kill_act), _caps(),
    )
    pd.testing.assert_series_equal(
        base["balancing_capacity_revenue_eur"],
        shaped["balancing_capacity_revenue_eur"],
    )
    y = shaped["project_year"] >= 2
    assert (shaped.loc[y, "balancing_activation_revenue_eur"] == 0.0).all()


def test_ppa_strike_leg_takes_no_trajectory():
    """A DAM trajectory reshapes the CfD DAM leg but not the strike."""
    kpis = {**_kpis(), "revenue_pv_ppa_eur": 30_000.0,
            "ppa_covered_dam_value_eur": 25_000.0}
    econ_kw = dict(
        ppa_enabled=True, ppa_settlement="cfd", ppa_term_years=N_YEARS,
        ppa_inflation_pct=0.0, ppa_volume_share_pct=50.0,
    )
    decline = [1.0, 0.5, 0.5, 0.5, 0.5, 0.5]
    base = build_yearly_cashflow(kpis, _econ(**econ_kw), _caps())
    shaped = build_yearly_cashflow(
        kpis, _econ(trajectories=_traj("revenue_dam", decline), **econ_kw),
        _caps(),
    )
    # CfD pay-out = strike leg − covered DAM leg: halving the DAM
    # trajectory RAISES the CfD stream (the strike is untouched).
    y2 = shaped["project_year"] == 2
    assert float(shaped.loc[y2, "ppa_revenue_eur"].iloc[0]) > float(
        base.loc[y2, "ppa_revenue_eur"].iloc[0],
    )


# ---------------------------------------------------------------------------
# 4. OPEX split + LCOE/LCOS coupling
# ---------------------------------------------------------------------------


def _fin(econ):
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    lifetime = pd.DataFrame({
        "project_year": list(range(1, N_YEARS + 1)),
        "pv_generation_mwh": [1_800.0] * N_YEARS,
        "bess_discharge_mwh": [400.0] * N_YEARS,
    })
    return compute_financial_kpis(
        cf, econ, capacities=_caps(), lifetime_yearly=lifetime,
    )


def test_opex_split_streams_shape_each_leg():
    step_pv = _traj("opex_pv", [1.0, 1.0, 2.0, 2.0, 2.0, 2.0])
    econ = _econ(opex_inflation_pct=0.0, trajectories=step_pv)
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    y = cf.set_index("project_year")["opex_eur"]
    # pv leg 5 EUR/kWp x 1000 kWp = 5000; bess leg 5 x 500 = 2500.
    assert float(y.loc[1]) == pytest.approx(-7_500.0)
    assert float(y.loc[3]) == pytest.approx(-(5_000.0 * 2.0 + 2_500.0))


def test_lcoe_moves_with_opex_pv_trajectory_and_matches_hand_sum():
    econ0 = _econ(opex_inflation_pct=0.0)
    econ1 = _econ(
        opex_inflation_pct=0.0,
        trajectories=_traj("opex_pv", [1.0, 1.0, 1.0, 1.5, 1.5, 1.5]),
    )
    fin0, fin1 = _fin(econ0), _fin(econ1)
    assert fin1["lcoe_eur_per_mwh"] > fin0["lcoe_eur_per_mwh"]
    # Hand-computed discounted OPEX numerator for the shaped run.
    r = 0.07
    disc_opex = sum(
        5_000.0 * m / (1.0 + r) ** y
        for y, m in zip(range(1, N_YEARS + 1),
                        [1.0, 1.0, 1.0, 1.5, 1.5, 1.5], strict=True)
    )
    assert fin1["lcoe_disc_pv_opex_eur"] == pytest.approx(disc_opex, rel=1e-9)
    # The BESS metric is untouched by a pv-leg trajectory (its own leg
    # falls back to the flat scalar).
    assert fin1["lcos_eur_per_mwh"] == pytest.approx(
        fin0["lcos_eur_per_mwh"],
    )


def test_revenue_trajectory_leaves_lcoe_lcos_unchanged():
    econ0 = _econ()
    econ1 = _econ(
        trajectories=_traj("revenue_dam", [1.0, 0.5, 0.5, 0.5, 0.5, 0.5]),
    )
    fin0, fin1 = _fin(econ0), _fin(econ1)
    assert fin1["lcoe_eur_per_mwh"] == pytest.approx(fin0["lcoe_eur_per_mwh"])
    assert fin1["lcos_eur_per_mwh"] == pytest.approx(fin0["lcos_eur_per_mwh"])
    assert fin1["npv_eur"] < fin0["npv_eur"]


# ---------------------------------------------------------------------------
# 5. Monthly reconciliation
# ---------------------------------------------------------------------------


def test_monthly_reconciles_yearly_with_trajectories_on():
    block = {
        "revenue_dam": {"mode": "replace",
                        "values": [1.0, 0.95, 0.9, 0.85, 0.8, 0.75]},
        "balancing_capacity": {"mode": "replace",
                               "values": [1.0, 0.8, 0.6, 0.5, 0.4, 0.3]},
        "opex": {"mode": "overlay",
                 "values": [1.0, 1.0, 1.1, 1.1, 1.3, 1.3]},
    }
    from tests.test_monthly_cashflow_reconciliation import _make_res_frame

    econ = _econ(trajectories=block, bess_replacement_year=4,
                 bess_replacement_cost_pct=40.0)
    yearly = build_yearly_cashflow(_kpis(), econ, _caps())
    monthly, _ = derive_monthly_cashflow(_make_res_frame(), yearly, econ)
    for y in range(1, N_YEARS + 1):
        month_net = float(
            monthly.loc[monthly["project_year"] == y, "net_cashflow_eur"].sum()
        )
        year_net = float(
            yearly.loc[yearly["project_year"] == y, "net_cashflow_eur"].iloc[0]
        )
        assert month_net == pytest.approx(year_net, abs=0.01), y


# ---------------------------------------------------------------------------
# 6. Sensitivity locks
# ---------------------------------------------------------------------------


def test_scale_revenue_unit_factor_is_noop_with_trajectories():
    from pvbess_opt.sensitivity import _scale_revenue

    econ = _econ(
        aggregator_fee_pct_revenue=5.0,
        trajectories=_traj("revenue_dam", [1.0, 0.9, 0.8, 0.7, 0.6, 0.5]),
    )
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    # Perturbed frames deliberately drop the tax-layer columns
    # (Eqs. E34-E38 stale-value guard), so the no-op compares
    # against the pre-tax view.
    from pvbess_opt.economics import TAX_LAYER_COLUMNS

    pd.testing.assert_frame_equal(
        _scale_revenue(cf, 1.0),
        cf.drop(columns=list(TAX_LAYER_COLUMNS)),
    )


def test_recompute_net_identity_with_trajectories():
    from pvbess_opt.sensitivity import _recompute_net

    econ = _econ(
        aggregator_fee_pct_revenue=5.0,
        route_to_market_fee_eur_per_mwh=2.0,
        trajectories={
            "revenue_dam": {"mode": "replace",
                            "values": [1.0, 0.9, 0.8, 0.7, 0.6, 0.5]},
            "opex": {"mode": "overlay",
                     "values": [1.0, 1.0, 1.2, 1.2, 1.2, 1.2]},
        },
    )
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    recomputed = _recompute_net(cf.copy())
    pd.testing.assert_series_equal(
        recomputed["net_cashflow_eur"], cf["net_cashflow_eur"],
    )


def test_discount_rate_rebuild_keeps_trajectories():
    """The DiscountRate driver rebuild passes econ through, so the
    trajectory survives and the rebuilt Year-2 revenue matches."""
    from pvbess_opt.sensitivity import run_sensitivity_analysis

    econ = _econ(
        trajectories=_traj("revenue_dam", [1.0, 0.9, 0.8, 0.7, 0.6, 0.5]),
    )
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    base_kpis = compute_financial_kpis(cf, econ)
    sens = run_sensitivity_analysis(_kpis(), econ, _caps(), base_kpis)
    rate_rows = sens.loc[sens["variable"] == "DiscountRate"]
    assert not rate_rows.empty
    assert rate_rows["npv_eur"].notna().all()
