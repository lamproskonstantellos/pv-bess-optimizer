"""aFRR / mFRR merit-order activation-probability curve (Eq. B10).

`bm_merit_order_enabled` swaps the scalar per-product activation
probability for a piecewise price-to-probability curve evaluated at
each step's activation price — deterministic per-step coefficients,
so the MILP stays linear.  Locked here: zero-default bit-identity, the
flat-curve equivalence to the scalar path, the objective / SOC-drift /
KPI consistency on the same beta(t), the MC realisation coupling, and
the sheet's schema / monotonicity validation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.balancing import (
    BalancingConfig,
    activation_probability,
    activation_probability_curve,
)
from pvbess_opt.io import parse_merit_order_sheet


def _curve_df(rows) -> pd.DataFrame:
    return pd.DataFrame(
        rows,
        columns=["product", "price_eur_per_mwh",
                 "activation_probability_pct"],
    )


# ---------------------------------------------------------------------------
# Parser / validation
# ---------------------------------------------------------------------------


def test_parser_schema_and_monotonicity():
    curve = parse_merit_order_sheet(_curve_df([
        ("afrr_up", 100.0, 20.0),
        ("afrr_up", 300.0, 5.0),
        ("mfrr_up", 150.0, 10.0),
    ]))
    assert curve["afrr_up"] == [(100.0, 20.0), (300.0, 5.0)]
    assert curve["mfrr_up"] == [(150.0, 10.0)]

    with pytest.raises(ValueError, match="missing column"):
        parse_merit_order_sheet(pd.DataFrame({"product": ["afrr_up"]}))
    with pytest.raises(ValueError, match="unknown product"):
        parse_merit_order_sheet(_curve_df([("fcr", 10.0, 10.0)]))
    with pytest.raises(ValueError, match="NON-INCREASING"):
        parse_merit_order_sheet(_curve_df([
            ("afrr_up", 100.0, 5.0), ("afrr_up", 300.0, 20.0),
        ]))
    with pytest.raises(ValueError, match=r"\[0, 100\]"):
        parse_merit_order_sheet(_curve_df([("afrr_up", 100.0, 150.0)]))
    with pytest.raises(ValueError, match="duplicate"):
        parse_merit_order_sheet(_curve_df([
            ("afrr_up", 100.0, 20.0), ("afrr_up", 100.0, 10.0),
        ]))
    with pytest.raises(ValueError, match="no data rows"):
        parse_merit_order_sheet(_curve_df([]))


def test_curve_interpolation_and_scalar_fallback():
    cfg = BalancingConfig(afrr_up_activation_probability_pct=10.0)
    prices = np.array([50.0, 100.0, 200.0, 300.0, 500.0])
    curve = {"afrr_up": [(100.0, 20.0), (300.0, 5.0)]}
    beta = activation_probability_curve(cfg, curve, "afrr_up", prices)
    # Clamped below/above the curve ends; linear in between.
    assert beta[0] == pytest.approx(0.20)
    assert beta[1] == pytest.approx(0.20)
    assert beta[2] == pytest.approx(0.125)
    assert beta[3] == pytest.approx(0.05)
    assert beta[4] == pytest.approx(0.05)
    # Missing curve / product falls back to the scalar path.
    flat = activation_probability_curve(cfg, None, "afrr_up", prices)
    assert np.allclose(flat, activation_probability(cfg, "afrr_up"))
    flat2 = activation_probability_curve(cfg, curve, "mfrr_dn", prices)
    assert np.allclose(flat2, activation_probability(cfg, "mfrr_dn"))


# ---------------------------------------------------------------------------
# MILP + KPI coupling
# ---------------------------------------------------------------------------


def _highs_available() -> bool:
    try:
        import highspy
    except ImportError:
        return False
    return bool(highspy)


def _balancing_params(curve=None, enabled=False, **o) -> dict:
    balancing = {
        "balancing_enabled": True,
        "dam_capacity_share_pct": 50.0,
        "fcr_capacity_share_pct": 10.0,
        "afrr_up_capacity_share_pct": 20.0,
        "afrr_dn_capacity_share_pct": 10.0,
        "mfrr_up_capacity_share_pct": 5.0,
        "mfrr_dn_capacity_share_pct": 5.0,
        "bm_settlement_minutes": 60,
        "bm_merit_order_enabled": enabled,
    }
    if curve is not None:
        balancing["bm_merit_order_curve"] = curve
    p = {
        "dt_minutes": 60,
        "mode": "merchant",
        "pv_nameplate_kwp": 0.0,
        "bess_power_kw": 1000.0,
        "bess_capacity_kwh": 2000.0,
        "efficiency_charge": 1.0,
        "efficiency_discharge": 1.0,
        "soc_min_frac": 0.0,
        "soc_max_frac": 1.0,
        "initial_soc_frac": 0.5,
        "terminal_soc_equal": False,
        "max_cycles_per_day": 10.0,
        "p_grid_export_max_kw": 5000.0,
        "retail_tariff_eur_per_mwh": 120.0,
        "allow_bess_grid_charging": True,
        "balancing": balancing,
    }
    p.update(o)
    return p


def _ts(n_hours: int = 24) -> pd.DataFrame:
    hours = np.arange(n_hours) % 24
    price = np.where(hours < 12, 40.0, 120.0)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n_hours, freq="h"),
        "pv_kwh": np.zeros(n_hours),
        "dam_price_eur_per_mwh": price.astype(float),
        # Two activation-price regimes so the curve actually varies.
        "afrr_up_activation_price_eur_per_mwh": np.where(
            hours < 12, 100.0, 300.0,
        ).astype(float),
    })


@pytest.mark.skipif(not _highs_available(), reason="requires HiGHS")
def test_disabled_and_flat_curve_reproduce_scalar_path():
    from pvbess_opt.optimization import run_scenario

    ts = _ts()
    res_off, _s1, _f1 = run_scenario(
        _balancing_params(), ts, return_unrounded=True,
    )
    # Enabled with a FLAT curve at the scalar probability for every
    # product: the per-step beta equals the scalar everywhere, so the
    # dispatch and KPIs must match the scalar path.
    cfg_defaults = BalancingConfig()
    flat_curve = {
        k: [(0.0, getattr(
            cfg_defaults, f"{k}_activation_probability_pct",
        ))]
        for k in ("afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn")
    }
    res_flat, _s2, _f2 = run_scenario(
        _balancing_params(curve=flat_curve, enabled=True), ts,
        return_unrounded=True,
    )
    for col in (c for c in res_off.columns if c.startswith("bm_reservation")):
        np.testing.assert_allclose(
            res_flat[col].to_numpy(dtype=float),
            res_off[col].to_numpy(dtype=float),
            atol=1e-5, err_msg=col,
        )


@pytest.mark.skipif(not _highs_available(), reason="requires HiGHS")
def test_kpis_and_invariants_use_the_same_beta():
    from pvbess_opt.kpis import compute_kpis
    from pvbess_opt.optimization import run_scenario

    curve = {"afrr_up": [(100.0, 20.0), (300.0, 5.0)]}
    params = _balancing_params(curve=curve, enabled=True)
    ts = _ts()
    res, _s, res_full = run_scenario(params, ts, return_unrounded=True)
    # The energy-balance verification embeds the SOC-dynamics mirror;
    # a drift mismatch between the MILP's per-step beta and the KPI
    # reconstruction would surface as a residual here.
    from pvbess_opt.kpis import verify_energy_balance
    residuals = verify_energy_balance(res_full, params)
    assert residuals["max_soc_dynamics_residual_kwh"] <= 1e-3

    kpis = compute_kpis(res, params, verify_balance=False)
    # Analytic cross-check of the aFRR-up activation revenue with the
    # interpolated beta at the two price regimes.
    r = res["bm_reservation_afrr_up_kw"].to_numpy(dtype=float)
    act_price = res[
        "afrr_up_activation_price_eur_per_mwh"
    ].to_numpy(dtype=float)
    cfg = BalancingConfig(**{
        k: v for k, v in params["balancing"].items()
        if k != "bm_merit_order_curve"
    })
    from pvbess_opt.balancing import acceptance_probability
    beta_t = activation_probability_curve(cfg, curve, "afrr_up", act_price)
    alpha = acceptance_probability(cfg, "afrr_up")
    expected = float(alpha * (beta_t * act_price * r).sum() / 1000.0)
    assert kpis["bm_afrr_up_activation_revenue_eur"] == pytest.approx(
        expected, abs=0.05,
    )


def test_mc_realisation_uses_per_step_beta():
    from pvbess_opt.rolling_horizon import realise_balancing_scenario

    n = 2000
    cfg = BalancingConfig(
        balancing_enabled=True,
        afrr_up_bid_acceptance_pct=100.0,
        bm_price_sigma_capacity_pct=0.0,
        bm_price_sigma_activation_pct=0.0,
        bm_merit_order_enabled=True,
    )
    curve = {"afrr_up": [(100.0, 100.0), (300.0, 0.0)]}
    products = ("fcr", "afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn")
    reservations = {
        p: (np.ones(n) if p == "afrr_up" else np.zeros(n))
        for p in products
    }
    prices = {
        f"{p}_capacity_price_eur_per_mwh": np.zeros(n) for p in products
    }
    prices.update({
        f"{p}_activation_price_eur_per_mwh": np.zeros(n)
        for p in products if p != "fcr"
    })
    prices["afrr_up_activation_price_eur_per_mwh"] = np.where(
        np.arange(n) % 2 == 0, 100.0, 300.0,
    ).astype(float)
    rng = np.random.default_rng(7)
    out = realise_balancing_scenario(
        reservations, cfg, prices, dt_hours=1.0, rng=rng,
        merit_curve=curve,
    )
    # beta(100) = 1 and beta(300) = 0: only the 100-EUR steps activate,
    # so the realised activation revenue equals exactly their count
    # x 1 kW x 1 h x 100 EUR/MWh / 1000.
    n_cheap = int((np.arange(n) % 2 == 0).sum())
    assert out["per_product_activation_revenue_eur"][
        "afrr_up"
    ] == pytest.approx(n_cheap * 100.0 / 1000.0)


def test_loader_requires_sheet_when_enabled(tmp_path):
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
    n = 24
    typed = {
        "ts": pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
            "pv_kwh": np.full(n, 100.0),
            "dam_price_eur_per_mwh": np.full(n, 60.0),
        }),
        "project": dict(PROJECT_SHEET_DEFAULTS, mode="merchant"),
        "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=1000.0),
        "bess": dict(
            BESS_SHEET_DEFAULTS, bess_power_kw=500.0,
            bess_capacity_kwh=1000.0,
        ),
        "economics": dict(ECONOMICS_SHEET_DEFAULTS),
        "simulation": dict(SIMULATION_SHEET_DEFAULTS),
        "balancing": dict(
            BALANCING_SHEET_DEFAULTS, balancing_enabled=True,
            bm_settlement_minutes=60, bm_merit_order_enabled=True,
        ),
    }
    missing = tmp_path / "missing.xlsx"
    write_workbook(typed, missing)
    with pytest.raises(ValueError, match="bm_merit_order"):
        read_workbook(missing)

    # With the curve attached the workbook round-trips: the writer
    # rebuilds the sheet from the parsed dict and the reader restores
    # the same curve.
    typed["balancing"]["bm_merit_order_curve"] = {
        "afrr_up": [(100.0, 20.0), (300.0, 5.0)],
    }
    ok = tmp_path / "ok.xlsx"
    write_workbook(typed, ok)
    back = read_workbook(ok)
    assert back["balancing"]["bm_merit_order_curve"] == {
        "afrr_up": [(100.0, 20.0), (300.0, 5.0)],
    }
