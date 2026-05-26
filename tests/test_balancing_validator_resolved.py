"""Phase 6 regression: balancing share-cap validation uses resolved config.

``_validate_balancing_config`` previously summed raw workbook share
values directly, treating missing keys as 0.  ``dam_capacity_share_pct``
defaults to 70 %, so a workbook that omitted the DAM line and supplied
product shares summing to 60 % passed the validator at 60 % and then
materialised a 130 % allocation downstream.

Lock the post-default cap on the resolved :class:`BalancingConfig` so
the cap check sees the same numbers the optimiser will.
"""

from __future__ import annotations

import pytest

from pvbess_opt.balancing import BalancingConfig
from pvbess_opt.io import BALANCING_SHEET_DEFAULTS, _validate_balancing_config


def _enabled(**overrides) -> dict:
    """Build a balancing dict that is enabled and includes only ``overrides``.

    Starts from BALANCING_SHEET_DEFAULTS so the probability /
    duration / settlement-cadence checks pass while we exercise the
    capacity-share cap.  The DAM share is explicitly REMOVED so the
    dataclass default (70 %) is the value the validator sees -- that
    is the configuration that flagged the original bug.
    """
    out = dict(BALANCING_SHEET_DEFAULTS)
    out["balancing_enabled"] = True
    out["bm_settlement_minutes"] = 15
    out.pop("dam_capacity_share_pct", None)
    # Wipe the per-product shares too so each test can declare its own
    # mix without inheriting the defaults' 70 / 10 / 8 / 7 / 3 / 2 split.
    for k in (
        "fcr_capacity_share_pct",
        "afrr_up_capacity_share_pct",
        "afrr_dn_capacity_share_pct",
        "mfrr_up_capacity_share_pct",
        "mfrr_dn_capacity_share_pct",
    ):
        out.pop(k, None)
    out.update(overrides)
    return out


def test_default_dam_share_plus_product_shares_over_cap_rejected():
    """Omit DAM, sum product shares to 40 % -> 110 % resolved (default DAM=70)."""
    raw = _enabled(
        fcr_capacity_share_pct=10.0,
        afrr_up_capacity_share_pct=10.0,
        afrr_dn_capacity_share_pct=10.0,
        mfrr_up_capacity_share_pct=5.0,
        mfrr_dn_capacity_share_pct=5.0,
        # Total products = 40 %; default DAM = 70 % => resolved sum 110 %.
    )
    with pytest.raises(ValueError, match=r"capacity shares sum to"):
        _validate_balancing_config(raw, dt_minutes=15)


def test_resolved_share_sum_at_or_below_cap_accepted():
    """Explicit DAM=30 + product shares summing to 30 % (total 60 %) is OK."""
    raw = _enabled(
        dam_capacity_share_pct=30.0,
        fcr_capacity_share_pct=10.0,
        afrr_up_capacity_share_pct=8.0,
        afrr_dn_capacity_share_pct=6.0,
        mfrr_up_capacity_share_pct=4.0,
        mfrr_dn_capacity_share_pct=2.0,
    )
    _validate_balancing_config(raw, dt_minutes=15)


def test_resolved_share_sum_exactly_100_accepted():
    """Exactly 100 % is valid; the cap is an inclusive upper bound."""
    raw = _enabled(
        dam_capacity_share_pct=60.0,
        fcr_capacity_share_pct=10.0,
        afrr_up_capacity_share_pct=10.0,
        afrr_dn_capacity_share_pct=10.0,
        mfrr_up_capacity_share_pct=5.0,
        mfrr_dn_capacity_share_pct=5.0,
    )
    _validate_balancing_config(raw, dt_minutes=15)


def test_resolved_share_sum_above_100_below_eps_accepted():
    """A 0.4 % overshoot is within the workbook-rounding epsilon."""
    raw = _enabled(
        dam_capacity_share_pct=70.0,
        fcr_capacity_share_pct=10.4,  # 100.4 % total
        afrr_up_capacity_share_pct=8.0,
        afrr_dn_capacity_share_pct=7.0,
        mfrr_up_capacity_share_pct=3.0,
        mfrr_dn_capacity_share_pct=2.0,
    )
    # Sum = 70 + 10.4 + 8 + 7 + 3 + 2 = 100.4 -> within 0.5 % eps.
    _validate_balancing_config(raw, dt_minutes=15)


def test_resolved_share_sum_above_eps_rejected():
    """A 1 % overshoot is outside the epsilon and rejected."""
    raw = _enabled(
        dam_capacity_share_pct=70.0,
        fcr_capacity_share_pct=11.0,  # 101 % total
        afrr_up_capacity_share_pct=8.0,
        afrr_dn_capacity_share_pct=7.0,
        mfrr_up_capacity_share_pct=3.0,
        mfrr_dn_capacity_share_pct=2.0,
    )
    with pytest.raises(ValueError, match=r"capacity shares sum to"):
        _validate_balancing_config(raw, dt_minutes=15)


def test_disabled_balancing_skipped_entirely():
    """The validator is a no-op when balancing_enabled=False."""
    raw = {
        "balancing_enabled": False,
        # Even if shares are absurd, validation skips them.
        "dam_capacity_share_pct": 80.0,
        "fcr_capacity_share_pct": 90.0,
    }
    _validate_balancing_config(raw, dt_minutes=15)


def test_negative_share_rejected_before_resolve():
    """Negative workbook shares are caught by the pre-default sanity check."""
    raw = _enabled(fcr_capacity_share_pct=-5.0)
    with pytest.raises(ValueError, match="must be non-negative"):
        _validate_balancing_config(raw, dt_minutes=15)


def test_default_config_passes():
    """The dataclass defaults sum to at most 100 % (sanity guard)."""
    defaults = BalancingConfig()
    total = sum(
        getattr(defaults, k) for k in (
            "dam_capacity_share_pct",
            "fcr_capacity_share_pct",
            "afrr_up_capacity_share_pct",
            "afrr_dn_capacity_share_pct",
            "mfrr_up_capacity_share_pct",
            "mfrr_dn_capacity_share_pct",
        )
    )
    assert total <= 100.0, f"defaults sum to {total}, must be <= 100 %"
