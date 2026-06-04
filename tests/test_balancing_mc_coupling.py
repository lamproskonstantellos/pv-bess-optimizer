"""Regression tests for the coupled-draw fix in :func:`realise_balancing_scenario`.

Before the fix, the SOC-trajectory pass opened an independent child RNG and
resampled activation outcomes; a scenario could therefore report revenue from
activations that never appeared in its SOC trace, and "SOC OK" for a trace that
never accrued the matching revenue. The pre-fix behaviour reported
``bm_soc_constrained_scenarios_pct = 0.0`` across every balancing-ON case
precisely because of this decoupling.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from pvbess_opt.balancing import (
    PRODUCTS_ALL,
    PRODUCTS_WITH_ACTIVATION,
    BalancingConfig,
)
from pvbess_opt.io import BALANCING_SHEET_DEFAULTS
from pvbess_opt.rolling_horizon import (
    monte_carlo_balancing,
    realise_balancing_scenario,
)


def _toy_cfg(**overrides) -> BalancingConfig:
    base = dict(BALANCING_SHEET_DEFAULTS, balancing_enabled=True)
    base.update(overrides)
    fields = {k: v for k, v in base.items() if k in BalancingConfig.__dataclass_fields__}
    return BalancingConfig(**fields)


def test_soc_check_couples_to_revenue_draws():
    """A deterministic single-product scenario must report SOC-violation
    iff that product's activation actually happens — proving the SOC pass
    consumes the same Bernoulli draws as the revenue pass.

    Setup: one product (``afrr_up``) with alpha=beta=1.0 (always activated),
    every other product zeroed.  Reservation is sized so a single
    activation step would push the SOC below the floor.  With the pre-fix
    decoupled sampling, the SOC violation flag is decided by a fresh draw
    and is therefore independent of revenue; with the fix it is forced to
    True whenever the (identical) Bernoulli draw produced revenue.
    """
    n = 8
    dt = 1.0
    eta_c = 1.0
    eta_d = 1.0
    soc_min = 0.0
    soc_max = 100.0
    soc_path = np.full(n, 5.0)  # 5 kWh above floor

    reservations = {p: np.zeros(n) for p in PRODUCTS_ALL}
    # 10 kW reservation × 1 h / eta_d = 10 kWh draw — twice the headroom.
    reservations["afrr_up"] = np.full(n, 10.0)

    prices = {
        f"{p}_capacity_price_eur_per_mwh": np.full(n, 10.0) for p in PRODUCTS_ALL
    }
    prices.update({
        f"{p}_activation_price_eur_per_mwh": np.full(n, 100.0)
        for p in PRODUCTS_WITH_ACTIVATION
    })

    # alpha = beta = 1.0 on every product so each Bernoulli draw is True
    # for every step, regardless of seed.  Zero noise sigmas to keep the
    # numbers deterministic.
    cfg = _toy_cfg(
        balancing_enabled=True,
        fcr_bid_acceptance_pct=100.0, afrr_up_bid_acceptance_pct=100.0,
        afrr_dn_bid_acceptance_pct=100.0, mfrr_up_bid_acceptance_pct=100.0,
        mfrr_dn_bid_acceptance_pct=100.0,
        fcr_activation_probability_pct=100.0,
        afrr_up_activation_probability_pct=100.0,
        afrr_dn_activation_probability_pct=100.0,
        mfrr_up_activation_probability_pct=100.0,
        mfrr_dn_activation_probability_pct=100.0,
        bm_price_sigma_capacity_pct=0.0, bm_price_sigma_activation_pct=0.0,
    )

    rng = np.random.default_rng(123)
    outcome = realise_balancing_scenario(
        reservations, cfg, prices, dt_hours=dt, rng=rng,
        soc_path_kwh=soc_path,
        soc_min_kwh=soc_min, soc_max_kwh=soc_max,
        eta_charge=eta_c, eta_discharge=eta_d,
    )
    # Revenue passed through because activation is guaranteed.
    assert outcome["per_product_activation_revenue_eur"]["afrr_up"] > 0.0
    # And the SOC pass — driven by the SAME activation array — sees the
    # violation: 5 kWh headroom − 10 kWh draw < 0.
    assert outcome["soc_constrained"] is True


def test_soc_check_no_violation_when_no_reservation():
    """Mirror of the above: when no product is reserved the SOC trajectory
    stays at the planned path and the violation flag is False."""
    n = 8
    reservations = {p: np.zeros(n) for p in PRODUCTS_ALL}
    prices = {f"{p}_capacity_price_eur_per_mwh": np.full(n, 10.0) for p in PRODUCTS_ALL}
    prices.update({
        f"{p}_activation_price_eur_per_mwh": np.full(n, 100.0)
        for p in PRODUCTS_WITH_ACTIVATION
    })
    cfg = _toy_cfg(balancing_enabled=True)
    soc_path = np.full(n, 50.0)

    outcome = realise_balancing_scenario(
        reservations, cfg, prices, dt_hours=1.0,
        rng=np.random.default_rng(0),
        soc_path_kwh=soc_path, soc_min_kwh=0.0, soc_max_kwh=100.0,
        eta_charge=1.0, eta_discharge=1.0,
    )
    assert outcome["soc_constrained"] is False


def test_revenue_implies_violation_when_planned_path_at_floor():
    """Synthetic check at the full-stack ``monte_carlo_balancing`` layer:
    a planned path sitting *at* the SOC floor with a non-zero ``afrr_up``
    reservation cannot accrue revenue without breaching the floor on the
    realised trace — every scenario where revenue lands must also flag a
    constrained SOC.

    Pre-fix this implication was broken: the SOC pass resampled the
    activation Booleans, so revenue draws and SOC draws disagreed and
    the constrained fraction collapsed to zero.  Post-fix the two views
    consume the same array.
    """
    n_steps = 24
    timestamps = pd.date_range("2026-06-01", periods=n_steps, freq="h")
    res = pd.DataFrame({
        "timestamp": timestamps,
        # Planned SOC sits exactly at the floor every step.
        "soc_kwh": np.full(n_steps, 0.0),
    })
    # Reserve afrr_up only; zero on every other product.
    for p in PRODUCTS_ALL:
        res[f"bm_reservation_{p}_kw"] = 0.0
    res["bm_reservation_afrr_up_kw"] = 100.0
    # Flat capacity / activation price columns (only the activation pass
    # drives the SOC trace, but the helper expects every column).
    for p in PRODUCTS_ALL:
        res[f"{p}_capacity_price_eur_per_mwh"] = 10.0
    for p in ("afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn"):
        res[f"{p}_activation_price_eur_per_mwh"] = 100.0

    params = {
        "dt_minutes": 60,
        "efficiency_charge": 1.0,
        "efficiency_discharge": 1.0,
        "soc_min_frac": 0.0,
        "soc_max_frac": 1.0,
        "bess_capacity_kwh": 1000.0,
        "balancing": dict(
            BALANCING_SHEET_DEFAULTS,
            balancing_enabled=True,
            bm_settlement_minutes=60,
            # Force every reserved step to clear AND activate so revenue is
            # guaranteed in every Monte Carlo scenario.
            afrr_up_bid_acceptance_pct=100.0,
            afrr_up_activation_probability_pct=100.0,
            # Zero noise on prices for determinism.
            bm_price_sigma_capacity_pct=0.0,
            bm_price_sigma_activation_pct=0.0,
        ),
    }
    mc = monte_carlo_balancing(res, params, n_scenarios=30, seed=2026)
    # Every scenario clears+activates → every scenario must report a
    # constrained SOC trace (planned path was at the floor, draw subtracts
    # 100 kWh from it).
    assert mc["bm_soc_constrained_scenarios_pct"] == 100.0


