"""Scenario-deck adapters: parametric generator + TYNDP milestone files.

Two working adapters ship first, per the design's bankable-practice
note (per-year hourly curves per scenario from fundamental models):

* ``parametric`` — generates per-year curves FROM the workbook's own
  Year-1 price column with three interpretable knobs (annual level
  drift, PV-weighted capture decline, intra-day spread evolution) plus
  per-product balancing price paths.  No external files: the knobs
  live in the store's ``meta.yaml`` under a ``parametric`` block.
* ``tyndp`` — adapts the free ENTSO-E TYNDP hourly marginal-cost
  proxies (CC-BY 4.0; milestone years 2030/2040/2050) named in
  ``meta.yaml`` under a ``tyndp.files`` map; curves between milestones
  interpolate linearly per step, before/after the range hold the
  nearest milestone.

The vendor adapters (``retwin`` / ``ffe`` / ``maon`` / ``afry``) stay
documented stubs until sample deliverables exist — their GR coverage is
unconfirmed (:func:`pvbess_opt.pricedata.store.stub_provider_error`).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pvbess_opt.marketdata import resample_intensive

from .store import (
    BALANCING_PRODUCTS,
    PriceDataError,
    ScenarioDeck,
    _basis_factor,
    _read_meta,
    validate_engine_basis,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parametric generator
# ---------------------------------------------------------------------------


def _parametric_block(meta: dict[str, Any], source: str) -> dict[str, Any]:
    block = meta.get("parametric")
    if not isinstance(block, dict):
        raise PriceDataError(
            f"{source}: provider 'parametric' needs a 'parametric' "
            "mapping in meta.yaml (knobs: dam_level_pct_per_yr, "
            "pv_capture_decline_pct_per_yr, spread_evolution_pct_per_yr, "
            "balancing)."
        )
    return block


def build_parametric_deck(
    store_dir: str | Path,
    *,
    name: str,
    vintage: str,
    weight_pct: float,
    year1_dam: np.ndarray,
    pv_kwh: np.ndarray | None,
    year1_balancing: dict[str, tuple[float, float]] | None,
    n_years: int,
    engine_basis: str = "nominal",
) -> ScenarioDeck:
    """Generate per-year curves from the Year-1 column and three knobs.

    For operating year ``y`` (Year 1 is the anchor, factors apply from
    year 2 on):

    ``level_y = (1 + dam_level_pct/100)^(y-1)`` — overall price drift.

    ``capture_y(t)``: the PV-weighted cannibalization shape.  With
    ``w(t) = pv(t)/max(pv)`` (0 outside daylight) the year-``y`` curve
    loses ``(1 - (1 - d)^(y-1)) · w(t)`` of its DAILY-MEAN level in
    PV-heavy steps, ``d = pv_capture_decline_pct/100`` — solar-hour
    prices fall faster than the average, which is exactly the
    capture-rate story.  The haircut applies to the daily-mean
    component only (an additive subtraction), so negative solar-hour
    prices DEEPEN under cannibalization instead of shrinking toward
    zero, as a multiplicative factor on the signed price would.

    ``spread_y``: intra-day deviations from the DAILY mean scale by
    ``(1 + s/100)^(y-1)`` — the BESS arbitrage spread path, independent
    of the level path.

    Balancing: per-product ``{capacity,activation}_pct_per_yr`` paths
    applied to the Year-1 per-product prices (from the workbook
    balancing columns / scalars), producing the ``balancing_annual``
    table of the canonical schema.
    """
    store_dir = Path(store_dir)
    meta = _read_meta(store_dir)
    # Parametric curves derive from the workbook's own Year-1 prices,
    # which are already on the engine basis — a declared foreign basis
    # would invite a double-counting bridge, so it is rejected.
    engine_basis = str(engine_basis).strip().lower()
    if str(meta["basis"]) != engine_basis:
        raise PriceDataError(
            f"{store_dir.name}: provider 'parametric' derives its "
            "curves from the workbook's own Year-1 prices, which are "
            f"already on the engine basis ({engine_basis!r}); "
            f"meta.yaml declares basis {meta['basis']!r} — declare the "
            "engine basis (or omit the key) instead of bridging."
        )
    block = _parametric_block(meta, store_dir.name)
    level_pct = float(block.get("dam_level_pct_per_yr", 0.0) or 0.0)
    capture_pct = float(
        block.get("pv_capture_decline_pct_per_yr", 0.0) or 0.0
    )
    spread_pct = float(
        block.get("spread_evolution_pct_per_yr", 0.0) or 0.0
    )
    if capture_pct and pv_kwh is None:
        raise PriceDataError(
            f"{store_dir.name}: pv_capture_decline_pct_per_yr needs the "
            "workbook PV profile (pv_kwh) to weight the decline."
        )

    base = np.asarray(year1_dam, dtype=float)
    n_steps = len(base)
    steps_per_day = n_steps // 365
    if n_steps % 365 != 0:
        raise PriceDataError(
            f"{store_dir.name}: Year-1 curve carries {n_steps} steps, "
            "not a whole non-leap year."
        )
    weight = (
        np.zeros(n_steps)
        if pv_kwh is None or float(np.max(pv_kwh)) <= 0.0
        else np.asarray(pv_kwh, dtype=float) / float(np.max(pv_kwh))
    )
    daily_mean = base.reshape(365, steps_per_day).mean(axis=1)
    daily_mean_steps = np.repeat(daily_mean, steps_per_day)
    deviation = base - daily_mean_steps

    dam: dict[int, np.ndarray] = {}
    for y in range(1, n_years + 1):
        level = (1.0 + level_pct / 100.0) ** (y - 1)
        capture_loss = 1.0 - (
            1.0 - capture_pct / 100.0
        ) ** (y - 1)
        spread = (1.0 + spread_pct / 100.0) ** (y - 1)
        # The capture haircut subtracts from the daily-mean component
        # only: scaling the SIGNED price would move negative solar-hour
        # prices toward zero — the opposite of cannibalization.
        curve = (
            daily_mean_steps * (1.0 - capture_loss * weight)
            + deviation * spread
        ) * level
        dam[y] = curve

    balancing: pd.DataFrame | None = None
    bal_block = block.get("balancing")
    if bal_block is not None:
        if not isinstance(bal_block, dict):
            raise PriceDataError(
                f"{store_dir.name}: parametric.balancing must map "
                "product to {capacity_pct_per_yr, activation_pct_per_yr}."
            )
        if year1_balancing is None:
            raise PriceDataError(
                f"{store_dir.name}: parametric.balancing needs Year-1 "
                "per-product prices from the workbook balancing inputs."
            )
        rows: list[dict[str, Any]] = []
        for product, knobs in bal_block.items():
            if product not in BALANCING_PRODUCTS:
                raise PriceDataError(
                    f"{store_dir.name}: unknown balancing product "
                    f"{product!r}; expected "
                    f"{', '.join(BALANCING_PRODUCTS)}."
                )
            if not isinstance(knobs, dict):
                raise PriceDataError(
                    f"{store_dir.name}: parametric.balancing.{product} "
                    "must be a mapping."
                )
            cap1, act1 = year1_balancing.get(product, (0.0, 0.0))
            cap_pct = float(knobs.get("capacity_pct_per_yr", 0.0) or 0.0)
            act_pct = float(knobs.get("activation_pct_per_yr", 0.0) or 0.0)
            for y in range(1, n_years + 1):
                rows.append({
                    "year": y,
                    "product": product,
                    "capacity_price_eur_per_mwh":
                        cap1 * (1.0 + cap_pct / 100.0) ** (y - 1),
                    "activation_price_eur_per_mwh":
                        0.0 if product == "fcr"
                        else act1 * (1.0 + act_pct / 100.0) ** (y - 1),
                })
        balancing = pd.DataFrame(rows)

    return ScenarioDeck(
        name=name,
        provider="parametric",
        vintage=vintage,
        weight_pct=float(weight_pct),
        dam=dam,
        balancing=balancing,
        meta=meta,
    )


# ---------------------------------------------------------------------------
# TYNDP milestone adapter
# ---------------------------------------------------------------------------


def build_tyndp_deck(
    store_dir: str | Path,
    *,
    name: str,
    vintage: str,
    weight_pct: float,
    n_steps: int,
    dt_minutes: int,
    n_years: int,
    start_year: int,
    engine_basis: str = "nominal",
    engine_base_year: int = 0,
    cpi_pct: float = 0.0,
) -> ScenarioDeck:
    """Adapt TYNDP hourly marginal-cost milestone files to a deck.

    ``meta.yaml`` names the files::

        tyndp:
          files:
            2030: tyndp_2030.csv
            2040: tyndp_2040.csv
            2050: tyndp_2050.csv

    Each file carries one non-leap year of hourly (or model-cadence)
    prices in a single ``dam_price_eur_per_mwh`` column (a ``step``
    column is optional and only checked for contiguity).  A project
    calendar year between two milestones interpolates linearly per
    step; before/after the milestone range holds the nearest curve.
    """
    store_dir = Path(store_dir)
    meta = _read_meta(store_dir)
    block = meta.get("tyndp")
    if not isinstance(block, dict) or not isinstance(
        block.get("files"), dict,
    ):
        raise PriceDataError(
            f"{store_dir.name}: provider 'tyndp' needs a 'tyndp.files' "
            "mapping of milestone year to a curve file in meta.yaml."
        )
    milestones: dict[int, np.ndarray] = {}
    for raw_year, ref in block["files"].items():
        milestone = int(raw_year)
        path = store_dir / str(ref)
        if not path.exists():
            raise PriceDataError(
                f"{store_dir.name}: tyndp milestone file not found: "
                f"{path}."
            )
        df = pd.read_csv(path)
        if "dam_price_eur_per_mwh" not in df.columns:
            raise PriceDataError(
                f"{path}: needs a dam_price_eur_per_mwh column."
            )
        if "step" in df.columns:
            steps = df["step"].to_numpy(dtype=int)
            if not np.array_equal(steps, np.arange(1, len(steps) + 1)):
                raise PriceDataError(
                    f"{path}: step column must be contiguous from 1."
                )
        values = df["dam_price_eur_per_mwh"].to_numpy(dtype=float)
        if np.isnan(values).any():
            raise PriceDataError(f"{path}: NaN prices.")
        if len(values) % 365 != 0:
            raise PriceDataError(
                f"{path}: {len(values)} steps is not a whole non-leap "
                "year at any cadence."
            )
        native_steps_per_day = len(values) // 365
        if (24 * 60) % native_steps_per_day != 0:
            raise PriceDataError(
                f"{path}: {native_steps_per_day} steps/day does not "
                "divide the day into whole minutes."
            )
        native_minutes = (24 * 60) // native_steps_per_day
        notes: set[str] = set()
        curve = resample_intensive(
            values, native_minutes, dt_minutes,
            column="dam_price_eur_per_mwh", notes=notes,
            context=f"milestone {milestone}",
        )
        if len(curve) != n_steps:
            raise PriceDataError(
                f"{path}: resamples to {len(curve)} steps; the model "
                f"grid carries {n_steps}."
            )
        milestones[milestone] = curve
    if not milestones:
        raise PriceDataError(
            f"{store_dir.name}: tyndp.files named no milestone curves."
        )

    ordered = sorted(milestones)
    dam: dict[int, np.ndarray] = {}
    for y in range(1, n_years + 1):
        calendar = start_year + y - 1
        if calendar <= ordered[0]:
            dam[y] = milestones[ordered[0]].copy()
        elif calendar >= ordered[-1]:
            dam[y] = milestones[ordered[-1]].copy()
        else:
            upper = min(m for m in ordered if m >= calendar)
            lower = max(m for m in ordered if m <= calendar)
            if upper == lower:
                dam[y] = milestones[lower].copy()
            else:
                t = (calendar - lower) / (upper - lower)
                dam[y] = (
                    (1.0 - t) * milestones[lower]
                    + t * milestones[upper]
                )
    logger.info(
        "[pricedata] %s: TYNDP milestones %s mapped onto calendar "
        "years %d..%d (linear between, hold outside).",
        store_dir.name, ordered, start_year, start_year + n_years - 1,
    )

    # Basis bridge onto the engine basis, per operating year on its
    # calendar year — the same guarded bridge the file loader applies
    # (TYNDP marginal-cost curves are typically real EUR of the
    # milestone study's base year).  The milestone mapping above fills
    # every operating year, so the bridge covers the whole horizon.
    store_basis = str(meta["basis"])
    store_base_year = int(meta.get("base_year") or 0)
    engine_basis = validate_engine_basis(engine_basis, int(engine_base_year))
    if store_basis != engine_basis or store_basis == "real":
        for y in list(dam):
            dam[y] = dam[y] * _basis_factor(
                start_year + y - 1,
                store_basis=store_basis,
                store_base_year=store_base_year,
                engine_basis=engine_basis,
                engine_base_year=int(engine_base_year),
                cpi_pct=cpi_pct,
            )
        logger.info(
            "[pricedata] %s: bridged store basis %r (base %s) to "
            "engine basis %r at %.2f %%/yr CPI.",
            store_dir.name, store_basis, store_base_year or "-",
            engine_basis, cpi_pct,
        )
    return ScenarioDeck(
        name=name,
        provider="tyndp",
        vintage=vintage,
        weight_pct=float(weight_pct),
        dam=dam,
        meta=meta,
    )
