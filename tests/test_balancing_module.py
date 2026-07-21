"""Unit tests for :mod:`pvbess_opt.balancing`."""

from __future__ import annotations

import numpy as np
import pytest

from pvbess_opt.balancing import (
    PRODUCTS_ALL,
    PRODUCTS_DN,
    PRODUCTS_UP,
    PRODUCTS_WITH_ACTIVATION,
    BalancingConfig,
    BalancingTimeseries,
    acceptance_probability,
    activation_probability,
    capacity_share_kw,
    generate_synthetic_balancing_timeseries,
    resolve_balancing_config,
)


def _cfg(**overrides) -> BalancingConfig:
    payload = dict(balancing_enabled=True, **overrides)
    return resolve_balancing_config(payload)


def test_products_constants():
    assert set(PRODUCTS_ALL) == {
        "fcr", "afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn",
    }
    # FCR is the only capacity-only product.
    assert "fcr" not in PRODUCTS_WITH_ACTIVATION
    assert set(PRODUCTS_UP) == {"afrr_up", "mfrr_up"}
    assert set(PRODUCTS_DN) == {"afrr_dn", "mfrr_dn"}


def test_dataclass_construction_uses_defaults():
    cfg = BalancingConfig()
    assert cfg.balancing_enabled is False
    assert cfg.fcr_capacity_share_pct == pytest.approx(10.0)
    assert cfg.bm_settlement_minutes == 15
    assert cfg.bm_random_seed == 1729


def test_capacity_share_kw_matches_workbook_share():
    cfg = _cfg(fcr_capacity_share_pct=12.5)
    assert capacity_share_kw(cfg, "fcr", 4000.0) == pytest.approx(500.0)
    # 0 % share returns zero, even with a positive nameplate.
    cfg_zero = _cfg(fcr_capacity_share_pct=0.0)
    assert capacity_share_kw(cfg_zero, "fcr", 4000.0) == 0.0


def test_probability_getters_clamp_to_unit_interval():
    cfg = _cfg(
        fcr_bid_acceptance_pct=70.0,
        fcr_activation_probability_pct=15.0,
    )
    assert acceptance_probability(cfg, "fcr") == pytest.approx(0.7)
    assert activation_probability(cfg, "fcr") == pytest.approx(0.15)


def test_synthetic_timeseries_shape_and_columns():
    cfg = _cfg()
    df = generate_synthetic_balancing_timeseries(96 * 7, 0.25, cfg, seed=1729)
    expected_cols = {
        f"{p}_capacity_price_eur_per_mwh" for p in PRODUCTS_ALL
    } | {
        f"{p}_activation_price_eur_per_mwh" for p in PRODUCTS_WITH_ACTIVATION
    }
    assert set(df.columns) == expected_cols
    assert len(df) == 96 * 7
    assert df.isna().sum().sum() == 0
    assert (df.to_numpy() >= 0).all()


def test_synthetic_timeseries_is_reproducible():
    cfg = _cfg()
    a = generate_synthetic_balancing_timeseries(96, 0.25, cfg, seed=2024)
    b = generate_synthetic_balancing_timeseries(96, 0.25, cfg, seed=2024)
    assert (a == b).all().all()


def test_synthetic_timeseries_up_activation_spike_around_evening_peak():
    cfg = _cfg()
    n = 96 * 7
    df = generate_synthetic_balancing_timeseries(n, 0.25, cfg, seed=7)
    hours = (np.arange(n) * 0.25) % 24
    peak_mask = np.isin(hours.astype(int), [18, 19, 20, 21])
    midday_mask = np.isin(hours.astype(int), [11, 12, 13, 14])
    afrr_up = df["afrr_up_activation_price_eur_per_mwh"].to_numpy()
    # Average evening-peak price clearly higher than the midday minimum.
    assert afrr_up[peak_mask].mean() > afrr_up[midday_mask].mean()


def test_unknown_product_raises():
    cfg = _cfg()
    with pytest.raises(ValueError, match="unknown balancing product"):
        capacity_share_kw(cfg, "fancy", 1000.0)
    with pytest.raises(ValueError, match="unknown balancing product"):
        acceptance_probability(cfg, "fancy")


def test_timeseries_length_mismatch_raises():
    arrs = {
        f"{p}_capacity_price_eur_per_mwh": np.ones(10, dtype=float)
        for p in PRODUCTS_ALL
    }
    for p in PRODUCTS_WITH_ACTIVATION:
        arrs[f"{p}_activation_price_eur_per_mwh"] = np.ones(10, dtype=float)
    # Corrupt one column to be a different length.
    arrs["fcr_capacity_price_eur_per_mwh"] = np.ones(11, dtype=float)
    with pytest.raises(ValueError, match="single length"):
        BalancingTimeseries(**arrs)
