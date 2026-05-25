"""Balancing market (FCR / aFRR / mFRR) data model and helpers.

This module contains pure-Python data structures and helper functions
for the European balancing market extension. Five products are
modelled:

* ``fcr``      — symmetric Frequency Containment Reserve. Capacity
  payment only (the duty cycle is implicit in the certification).
* ``afrr_up``  — automatic Frequency Restoration Reserve, upward.
  Capacity plus activation payments.
* ``afrr_dn``  — aFRR, downward. Capacity plus activation payments.
* ``mfrr_up``  — manual FRR, upward. Capacity plus activation payments.
* ``mfrr_dn``  — mFRR, downward. Capacity plus activation payments.

The MILP in :mod:`pvbess_opt.optimization` co-optimises a reservation
schedule against the existing DAM dispatch using deterministic
expected revenues; the Monte Carlo realisation lives in
:mod:`pvbess_opt.rolling_horizon`. Both consume the configuration
objects defined here.

No Pyomo or solver code lives in this module — only data structures,
validation helpers, and a synthetic-timeseries generator used by the
reference-workbook builder script under ``scripts/``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Product taxonomy
# ---------------------------------------------------------------------------

PRODUCTS_ALL: tuple[str, ...] = (
    "fcr", "afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn",
)

# Products that earn an activation payment in addition to the capacity
# payment. FCR is capacity-only by the ENTSO-E SAFA convention.
PRODUCTS_WITH_ACTIVATION: tuple[str, ...] = (
    "afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn",
)

PRODUCTS_UP: tuple[str, ...] = ("afrr_up", "mfrr_up")
PRODUCTS_DN: tuple[str, ...] = ("afrr_dn", "mfrr_dn")
PRODUCTS_SYMMETRIC: tuple[str, ...] = ("fcr",)


# ---------------------------------------------------------------------------
# Config + timeseries dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BalancingConfig:
    """Parsed balancing-market workbook section.

    Field names mirror the workbook keys verbatim so the round-trip from
    the ``balancing`` sheet is mechanical. Validation is performed by
    :func:`pvbess_opt.io._validate_balancing_config` at load time; this
    dataclass merely carries already-typed values.
    """

    balancing_enabled: bool = False

    dam_capacity_share_pct: float = 70.0
    fcr_capacity_share_pct: float = 10.0
    afrr_up_capacity_share_pct: float = 8.0
    afrr_dn_capacity_share_pct: float = 7.0
    mfrr_up_capacity_share_pct: float = 3.0
    mfrr_dn_capacity_share_pct: float = 2.0

    fcr_bid_acceptance_pct: float = 70.0
    afrr_up_bid_acceptance_pct: float = 55.0
    afrr_dn_bid_acceptance_pct: float = 55.0
    mfrr_up_bid_acceptance_pct: float = 40.0
    mfrr_dn_bid_acceptance_pct: float = 40.0

    fcr_activation_probability_pct: float = 15.0
    afrr_up_activation_probability_pct: float = 10.0
    afrr_dn_activation_probability_pct: float = 8.0
    mfrr_up_activation_probability_pct: float = 5.0
    mfrr_dn_activation_probability_pct: float = 4.0

    fcr_default_capacity_price_eur_per_mwh: float = 12.0
    afrr_up_default_capacity_price_eur_per_mwh: float = 18.0
    afrr_dn_default_capacity_price_eur_per_mwh: float = 15.0
    mfrr_up_default_capacity_price_eur_per_mwh: float = 6.0
    mfrr_dn_default_capacity_price_eur_per_mwh: float = 5.0

    afrr_up_default_activation_price_eur_per_mwh: float = 220.0
    afrr_dn_default_activation_price_eur_per_mwh: float = 25.0
    mfrr_up_default_activation_price_eur_per_mwh: float = 180.0
    mfrr_dn_default_activation_price_eur_per_mwh: float = 20.0

    fcr_required_duration_hours: float = 0.5
    bm_settlement_minutes: int = 15
    bm_soc_headroom_pct: float = 10.0
    bm_inflation_pct: float = 2.0
    bm_price_sigma_capacity_pct: float = 25.0
    bm_price_sigma_activation_pct: float = 35.0
    bm_random_seed: int = 1729


@dataclass(frozen=True, slots=True)
class BalancingTimeseries:
    """Per-step capacity and activation prices for every product (EUR/MWh).

    Capacity-price arrays are populated for every product in
    :data:`PRODUCTS_ALL`; activation-price arrays only for the products
    in :data:`PRODUCTS_WITH_ACTIVATION` (FCR is capacity-only).
    """

    fcr_capacity_price_eur_per_mwh: np.ndarray
    afrr_up_capacity_price_eur_per_mwh: np.ndarray
    afrr_dn_capacity_price_eur_per_mwh: np.ndarray
    mfrr_up_capacity_price_eur_per_mwh: np.ndarray
    mfrr_dn_capacity_price_eur_per_mwh: np.ndarray
    afrr_up_activation_price_eur_per_mwh: np.ndarray
    afrr_dn_activation_price_eur_per_mwh: np.ndarray
    mfrr_up_activation_price_eur_per_mwh: np.ndarray
    mfrr_dn_activation_price_eur_per_mwh: np.ndarray

    # Cached n_steps for cheap shape checks downstream.
    n_steps: int = field(init=False)

    def __post_init__(self) -> None:
        arrays = [getattr(self, f.name) for f in fields(self) if f.name != "n_steps"]
        lengths = {int(np.asarray(a).shape[0]) for a in arrays}
        if len(lengths) != 1:
            raise ValueError(
                "BalancingTimeseries arrays must share a single length; "
                f"got {sorted(lengths)}."
            )
        object.__setattr__(self, "n_steps", next(iter(lengths)))


# ---------------------------------------------------------------------------
# Loader / resolver helpers
# ---------------------------------------------------------------------------


def resolve_balancing_config(raw: dict[str, Any]) -> BalancingConfig:
    """Build a :class:`BalancingConfig` from the workbook dict.

    Unknown keys are ignored (the workbook loader already warns on
    them); missing keys fall back to the dataclass defaults. Booleans
    and integers are coerced explicitly because workbook readers may
    deliver ``numpy`` scalars.
    """
    fields_by_name = {f.name: f for f in fields(BalancingConfig)}
    kwargs: dict[str, Any] = {}
    for name, fld in fields_by_name.items():
        if name == "n_steps":
            continue
        if name not in raw:
            continue
        value = raw[name]
        if fld.type is bool or name == "balancing_enabled":
            kwargs[name] = bool(value)
        elif name in {"bm_settlement_minutes", "bm_random_seed"}:
            kwargs[name] = int(value)
        else:
            kwargs[name] = float(value)
    return BalancingConfig(**kwargs)


# Per-product timeseries column → BalancingConfig fallback attribute.
_PRODUCT_CAPACITY_TS_COLUMNS: dict[str, str] = {
    "fcr": "fcr_capacity_price_eur_per_mwh",
    "afrr_up": "afrr_up_capacity_price_eur_per_mwh",
    "afrr_dn": "afrr_dn_capacity_price_eur_per_mwh",
    "mfrr_up": "mfrr_up_capacity_price_eur_per_mwh",
    "mfrr_dn": "mfrr_dn_capacity_price_eur_per_mwh",
}

_PRODUCT_ACTIVATION_TS_COLUMNS: dict[str, str] = {
    "afrr_up": "afrr_up_activation_price_eur_per_mwh",
    "afrr_dn": "afrr_dn_activation_price_eur_per_mwh",
    "mfrr_up": "mfrr_up_activation_price_eur_per_mwh",
    "mfrr_dn": "mfrr_dn_activation_price_eur_per_mwh",
}

_PRODUCT_CAPACITY_DEFAULT_KEYS: dict[str, str] = {
    "fcr": "fcr_default_capacity_price_eur_per_mwh",
    "afrr_up": "afrr_up_default_capacity_price_eur_per_mwh",
    "afrr_dn": "afrr_dn_default_capacity_price_eur_per_mwh",
    "mfrr_up": "mfrr_up_default_capacity_price_eur_per_mwh",
    "mfrr_dn": "mfrr_dn_default_capacity_price_eur_per_mwh",
}

_PRODUCT_ACTIVATION_DEFAULT_KEYS: dict[str, str] = {
    "afrr_up": "afrr_up_default_activation_price_eur_per_mwh",
    "afrr_dn": "afrr_dn_default_activation_price_eur_per_mwh",
    "mfrr_up": "mfrr_up_default_activation_price_eur_per_mwh",
    "mfrr_dn": "mfrr_dn_default_activation_price_eur_per_mwh",
}


def _resolve_column(
    df: pd.DataFrame, column: str, fallback: float, n_steps: int,
) -> np.ndarray:
    if column in df.columns:
        values = df[column].to_numpy(dtype=float)
        if values.shape[0] != n_steps:
            raise ValueError(
                f"balancing timeseries column {column!r} has length "
                f"{values.shape[0]} but the dispatch timeseries has "
                f"{n_steps} rows."
            )
        return values
    return np.full(n_steps, float(fallback), dtype=float)


def resolve_balancing_timeseries(
    df: pd.DataFrame, cfg: BalancingConfig, n_steps: int,
) -> BalancingTimeseries:
    """Build a :class:`BalancingTimeseries` from the loaded timeseries frame.

    Each per-product column is read verbatim when present; missing
    columns fall back to the corresponding scalar in ``cfg`` (the
    workbook loader already broadcasts those defaults across all rows,
    so the column-absence path is mainly exercised by unit tests that
    bypass the loader).
    """
    kwargs: dict[str, np.ndarray] = {}
    for product, col in _PRODUCT_CAPACITY_TS_COLUMNS.items():
        fallback = float(getattr(cfg, _PRODUCT_CAPACITY_DEFAULT_KEYS[product]))
        kwargs[col] = _resolve_column(df, col, fallback, n_steps)
    for product, col in _PRODUCT_ACTIVATION_TS_COLUMNS.items():
        fallback = float(getattr(cfg, _PRODUCT_ACTIVATION_DEFAULT_KEYS[product]))
        kwargs[col] = _resolve_column(df, col, fallback, n_steps)
    return BalancingTimeseries(**kwargs)


# ---------------------------------------------------------------------------
# Product-level accessor helpers
# ---------------------------------------------------------------------------


_CAPACITY_SHARE_KEYS: dict[str, str] = {
    "fcr": "fcr_capacity_share_pct",
    "afrr_up": "afrr_up_capacity_share_pct",
    "afrr_dn": "afrr_dn_capacity_share_pct",
    "mfrr_up": "mfrr_up_capacity_share_pct",
    "mfrr_dn": "mfrr_dn_capacity_share_pct",
}

_BID_ACCEPTANCE_KEYS: dict[str, str] = {
    "fcr": "fcr_bid_acceptance_pct",
    "afrr_up": "afrr_up_bid_acceptance_pct",
    "afrr_dn": "afrr_dn_bid_acceptance_pct",
    "mfrr_up": "mfrr_up_bid_acceptance_pct",
    "mfrr_dn": "mfrr_dn_bid_acceptance_pct",
}

_ACTIVATION_PROB_KEYS: dict[str, str] = {
    "fcr": "fcr_activation_probability_pct",
    "afrr_up": "afrr_up_activation_probability_pct",
    "afrr_dn": "afrr_dn_activation_probability_pct",
    "mfrr_up": "mfrr_up_activation_probability_pct",
    "mfrr_dn": "mfrr_dn_activation_probability_pct",
}


def _check_product(product: str) -> None:
    if product not in PRODUCTS_ALL:
        raise ValueError(
            f"unknown balancing product {product!r}; "
            f"expected one of {PRODUCTS_ALL}."
        )


def capacity_share_kw(
    cfg: BalancingConfig, product: str, bess_power_kw: float,
) -> float:
    """Return the kW reservation cap for ``product``."""
    _check_product(product)
    share = float(getattr(cfg, _CAPACITY_SHARE_KEYS[product])) / 100.0
    return max(0.0, float(bess_power_kw)) * share


def acceptance_probability(cfg: BalancingConfig, product: str) -> float:
    """Return alpha_k in [0, 1] — P(bid clears the auction)."""
    _check_product(product)
    return float(getattr(cfg, _BID_ACCEPTANCE_KEYS[product])) / 100.0


def activation_probability(cfg: BalancingConfig, product: str) -> float:
    """Return beta_k in [0, 1] — P(activated | cleared)."""
    _check_product(product)
    return float(getattr(cfg, _ACTIVATION_PROB_KEYS[product])) / 100.0


def expected_capacity_revenue_per_kw_per_step(
    cfg: BalancingConfig,
    ts: BalancingTimeseries,
    product: str,
    t: int,
    dt_hours: float,
) -> float:
    """Expected € of capacity revenue per kW reserved at step ``t``.

    Equals ``alpha_k * p_cap_k(t) * dt_hours / 1000`` (the ``/1000``
    converts EUR/MWh to EUR/kWh).
    """
    _check_product(product)
    column = _PRODUCT_CAPACITY_TS_COLUMNS[product]
    price = float(getattr(ts, column)[t])
    alpha = acceptance_probability(cfg, product)
    return alpha * price * float(dt_hours) / 1000.0


def expected_activation_revenue_per_kw_per_step(
    cfg: BalancingConfig,
    ts: BalancingTimeseries,
    product: str,
    t: int,
    dt_hours: float,
) -> float:
    """Expected € of activation revenue per kW reserved at step ``t``.

    Zero for FCR (capacity-only). Otherwise equals
    ``alpha_k * beta_k * p_act_k(t) * dt_hours / 1000``.
    """
    _check_product(product)
    if product not in PRODUCTS_WITH_ACTIVATION:
        return 0.0
    column = _PRODUCT_ACTIVATION_TS_COLUMNS[product]
    price = float(getattr(ts, column)[t])
    alpha = acceptance_probability(cfg, product)
    beta = activation_probability(cfg, product)
    return alpha * beta * price * float(dt_hours) / 1000.0


# ---------------------------------------------------------------------------
# Synthetic timeseries generator (for the reference workbook builder)
# ---------------------------------------------------------------------------


_PEAK_HOURS: tuple[int, ...] = (18, 19, 20, 21)
_MIDDAY_HOURS: tuple[int, ...] = (11, 12, 13, 14)


def _diurnal_envelope(
    hours: np.ndarray,
    *,
    base: float,
    peak_factor: float,
    midday_factor: float,
) -> np.ndarray:
    """Multiplicative envelope used to shape synthetic prices.

    The envelope is ``base`` everywhere except across the canonical
    evening peak and midday window where it is scaled by
    ``peak_factor`` / ``midday_factor`` respectively.
    """
    env = np.full(hours.shape, float(base), dtype=float)
    env[np.isin(hours, list(_PEAK_HOURS))] *= float(peak_factor)
    env[np.isin(hours, list(_MIDDAY_HOURS))] *= float(midday_factor)
    return env


def generate_synthetic_balancing_timeseries(
    n_steps: int,
    dt_hours: float,
    cfg: BalancingConfig,
    *,
    seed: int = 1729,
) -> pd.DataFrame:
    """Generate a reproducible synthetic balancing-price timeseries.

    The output has one row per dispatch step and the nine columns from
    :data:`_PRODUCT_CAPACITY_TS_COLUMNS` and
    :data:`_PRODUCT_ACTIVATION_TS_COLUMNS`. The defaults from ``cfg``
    drive the per-product mean level; a weak diurnal envelope and
    multiplicative log-normal noise produce a plausible intraday shape.
    """
    rng = np.random.default_rng(int(seed))
    step_hours = float(dt_hours)
    if step_hours <= 0.0:
        raise ValueError(f"dt_hours must be > 0, got {dt_hours!r}.")
    hours = ((np.arange(int(n_steps)) * step_hours) % 24).astype(int)

    out: dict[str, np.ndarray] = {}

    # Capacity prices: weak diurnal (slight evening lift, slight midday
    # dip), log-normal sigma 0.15. Floor at zero.
    cap_sigma = 0.15
    cap_mu = -0.5 * cap_sigma * cap_sigma
    for product, col in _PRODUCT_CAPACITY_TS_COLUMNS.items():
        mean_price = float(
            getattr(cfg, _PRODUCT_CAPACITY_DEFAULT_KEYS[product])
        )
        envelope = _diurnal_envelope(
            hours, base=1.0, peak_factor=1.15, midday_factor=0.9,
        )
        noise = rng.lognormal(mean=cap_mu, sigma=cap_sigma, size=int(n_steps))
        out[col] = np.maximum(mean_price * envelope * noise, 0.0)

    # Activation prices: split into up and down shapes.
    up_sigma = 0.40
    dn_sigma = 0.40
    up_mu = -0.5 * up_sigma * up_sigma
    dn_mu = -0.5 * dn_sigma * dn_sigma

    for product in ("afrr_up", "mfrr_up"):
        col = _PRODUCT_ACTIVATION_TS_COLUMNS[product]
        mean_price = float(
            getattr(cfg, _PRODUCT_ACTIVATION_DEFAULT_KEYS[product])
        )
        envelope = _diurnal_envelope(
            hours, base=1.0, peak_factor=2.5, midday_factor=0.6,
        )
        noise = rng.lognormal(mean=up_mu, sigma=up_sigma, size=int(n_steps))
        out[col] = np.maximum(mean_price * envelope * noise, 30.0)

    for product in ("afrr_dn", "mfrr_dn"):
        col = _PRODUCT_ACTIVATION_TS_COLUMNS[product]
        mean_price = float(
            getattr(cfg, _PRODUCT_ACTIVATION_DEFAULT_KEYS[product])
        )
        envelope = _diurnal_envelope(
            hours, base=1.0, peak_factor=0.5, midday_factor=2.0,
        )
        noise = rng.lognormal(mean=dn_mu, sigma=dn_sigma, size=int(n_steps))
        out[col] = np.maximum(mean_price * envelope * noise, 0.0)

    return pd.DataFrame(out)
