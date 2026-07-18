"""Canonical per-scenario price store: schema, loader, validators.

A price scenario is a **directory** referenced from the workbook's
``price_scenarios`` sheet (``store_path``, relative paths resolved
against the workbook), carrying:

* ``meta.yaml`` — provider, vintage, zone, currency, basis
  (``nominal`` | ``real``), ``base_year`` (required for ``real``),
  license, plus provider-specific blocks (the ``parametric`` knobs, the
  TYNDP milestone file map);
* ``dam.csv`` / ``dam.parquet`` — tidy per-year hourly (or model-cadence)
  curves: ``year, step, dam_price_eur_per_mwh`` with an optional
  ``ida_price_eur_per_mwh`` column (``year`` is the OPERATING year
  1..N; ``step`` is 1-based within the year).  CSV is first-class
  (Parquet needs an optional engine, exactly like the price-deck
  loader);
* ``balancing_annual.csv`` — per-year per-product scalars:
  ``year, product, capacity_price_eur_per_mwh,
  activation_price_eur_per_mwh`` with products drawn from
  fcr / afrr_up / afrr_dn / mfrr_up / mfrr_dn (FCR has no activation
  by design — its cell stays empty).

Calendar rules follow the market-data contract
(:mod:`pvbess_opt.marketdata.base`): a curve year must carry a whole
non-leap year at some cadence commensurable with the model grid and is
laid on by :func:`pvbess_opt.marketdata.resample_intensive` (step-hold
finer, mean coarser).  Years past the last declared one hold the last
curve (``hold_last``, logged); missing INTERIOR years are a hard error.

The real→nominal bridge: vendor curves are typically real EUR of a
base year while this repo's cashflow is nominal.  The loader converts
every store's basis to the engine basis (``scenario_engine`` sheet:
``price_basis`` + ``price_base_year`` + ``cpi_pct``) so every deck the
projection engine sees is on ONE declared basis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pvbess_opt.marketdata import resample_intensive

logger = logging.getLogger(__name__)

#: Balancing products a store may price (FCR carries no activation).
BALANCING_PRODUCTS: tuple[str, ...] = (
    "fcr", "afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn",
)

#: Providers accepted on the ``price_scenarios`` sheet.  ``file`` reads
#: a ready-made store directory; ``parametric`` / ``tyndp`` generate or
#: adapt one; the vendor adapters are documented stubs until sample
#: deliverables exist (their GR coverage is unconfirmed).
SCENARIO_PROVIDERS: tuple[str, ...] = (
    "retwin", "ffe", "maon", "afry", "tyndp", "parametric", "file",
)

_STUB_PROVIDERS: frozenset[str] = frozenset({"retwin", "ffe", "maon", "afry"})


class PriceDataError(ValueError):
    """A price-scenario store, adapter, or validation failure."""


@dataclass
class ScenarioDeck:
    """One scenario's loaded price paths, on the engine basis.

    ``dam[y]`` (and optionally ``ida[y]``) is the operating-year-``y``
    curve on the model grid (``n_steps`` values); ``balancing`` is the
    per-year per-product scalar table indexed ``(year, product)`` with
    ``capacity_price_eur_per_mwh`` / ``activation_price_eur_per_mwh``
    columns (None when the store carries no balancing file).
    """

    name: str
    provider: str
    vintage: str
    weight_pct: float
    dam: dict[int, np.ndarray]
    ida: dict[int, np.ndarray] | None = None
    balancing: pd.DataFrame | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def dam_curve(self, year: int) -> np.ndarray:
        """Year-``year`` DAM curve with the documented hold-last rule."""
        last = max(self.dam)
        return self.dam[min(year, last)]


def _read_meta(store_dir: Path) -> dict[str, Any]:
    meta_path = store_dir / "meta.yaml"
    if not meta_path.exists():
        raise PriceDataError(
            f"price-scenario store {store_dir} carries no meta.yaml."
        )
    import yaml

    raw = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PriceDataError(
            f"{meta_path}: meta.yaml must be a mapping, got "
            f"{type(raw).__name__}."
        )
    currency = str(raw.get("currency", "EUR")).strip().upper()
    if currency != "EUR":
        raise PriceDataError(
            f"{meta_path}: currency {currency!r} is not supported; "
            "stores must price in EUR."
        )
    basis = str(raw.get("basis", "nominal")).strip().lower()
    if basis not in ("nominal", "real"):
        raise PriceDataError(
            f"{meta_path}: basis {basis!r} must be 'nominal' or 'real'."
        )
    if basis == "real" and not raw.get("base_year"):
        raise PriceDataError(
            f"{meta_path}: basis 'real' requires a base_year (the price "
            "level's reference year)."
        )
    raw["basis"] = basis
    raw["currency"] = currency
    return raw


def _read_dam_table(store_dir: Path) -> pd.DataFrame:
    csv_path = store_dir / "dam.csv"
    parquet_path = store_dir / "dam.parquet"
    if csv_path.exists():
        df = pd.read_csv(csv_path)
        source = csv_path
    elif parquet_path.exists():
        df = pd.read_parquet(parquet_path)
        source = parquet_path
    else:
        raise PriceDataError(
            f"price-scenario store {store_dir} carries neither dam.csv "
            "nor dam.parquet."
        )
    required = {"year", "step", "dam_price_eur_per_mwh"}
    missing = required - set(df.columns)
    if missing:
        raise PriceDataError(
            f"{source}: missing column(s) {', '.join(sorted(missing))}; "
            "expected year, step, dam_price_eur_per_mwh"
            " [, ida_price_eur_per_mwh]."
        )
    return df


def _curves_from_table(
    df: pd.DataFrame,
    column: str,
    *,
    n_steps: int,
    dt_minutes: int,
    source: str,
) -> dict[int, np.ndarray]:
    """Per-year curves from the tidy table, resampled onto the grid."""
    curves: dict[int, np.ndarray] = {}
    steps_per_day_model = (24 * 60) // dt_minutes
    year_index = df["year"].astype(int)
    for y in sorted(year_index.unique().tolist()):
        group = df.loc[year_index == y]
        if y < 1:
            raise PriceDataError(
                f"{source}: operating years start at 1; got {y}."
            )
        ordered = group.sort_values("step")
        steps = ordered["step"].to_numpy(dtype=int)
        if steps[0] != 1 or not np.array_equal(
            steps, np.arange(1, len(steps) + 1),
        ):
            raise PriceDataError(
                f"{source}: year {y} steps must be contiguous from 1; "
                f"got {len(steps)} rows spanning "
                f"{steps.min()}..{steps.max()}."
            )
        values = ordered[column].to_numpy(dtype=float)
        if np.isnan(values).any():
            raise PriceDataError(
                f"{source}: year {y} column {column!r} carries NaN "
                "prices."
            )
        if len(values) % 365 != 0:
            raise PriceDataError(
                f"{source}: year {y} carries {len(values)} steps, not a "
                "whole non-leap year at any cadence (need a multiple "
                "of 365 daily blocks)."
            )
        native_steps_per_day = len(values) // 365
        if (24 * 60) % native_steps_per_day != 0:
            raise PriceDataError(
                f"{source}: year {y}: {native_steps_per_day} steps/day "
                "does not divide the day into whole minutes."
            )
        native_minutes = (24 * 60) // native_steps_per_day
        notes: set[str] = set()
        curve = resample_intensive(
            values, native_minutes, dt_minutes,
            column=column, notes=notes,
            context=f"scenario year {y}",
        )
        if len(curve) != n_steps:
            raise PriceDataError(
                f"{source}: year {y} resamples to {len(curve)} steps; "
                f"the model grid carries {n_steps} "
                f"({steps_per_day_model}/day)."
            )
        for note in notes:
            logger.info("[pricedata] %s: %s.", source, note)
        curves[y] = curve
    if not curves:
        raise PriceDataError(f"{source}: no curve years found.")
    return curves


def _validate_year_coverage(
    years: list[int], n_years: int, *, source: str,
) -> None:
    """Years must be contiguous from 1; hold_last covers the tail only."""
    expected = list(range(1, max(years) + 1))
    if sorted(years) != expected:
        missing = sorted(set(expected) - set(years))
        raise PriceDataError(
            f"{source}: curve years must be contiguous from 1; missing "
            f"year(s) {missing}."
        )
    if max(years) < n_years:
        logger.info(
            "[pricedata] %s: curves cover years 1..%d of %d; later "
            "years hold the year-%d curve (hold_last).",
            source, max(years), n_years, max(years),
        )


def _basis_factor(
    year_calendar: int,
    *,
    store_basis: str,
    store_base_year: int,
    engine_basis: str,
    engine_base_year: int,
    cpi_pct: float,
) -> float:
    """Deflator bridge between the store's and the engine's basis.

    real→nominal inflates from the store base year to the curve's
    calendar year; nominal→real deflates to the engine base year;
    real→real rebases between the two base years.  With a zero CPI all
    factors collapse to 1 (the bridge is inert).
    """
    g = 1.0 + float(cpi_pct) / 100.0
    if store_basis == engine_basis:
        if store_basis == "real":
            return float(g ** (store_base_year - engine_base_year))
        return 1.0
    if store_basis == "real":  # → nominal
        return float(g ** (year_calendar - store_base_year))
    # nominal → real
    return float(g ** (engine_base_year - year_calendar))


def _read_balancing_annual(store_dir: Path) -> pd.DataFrame | None:
    path = store_dir / "balancing_annual.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    required = {
        "year", "product",
        "capacity_price_eur_per_mwh", "activation_price_eur_per_mwh",
    }
    missing = required - set(df.columns)
    if missing:
        raise PriceDataError(
            f"{path}: missing column(s) {', '.join(sorted(missing))}."
        )
    bad = set(df["product"].astype(str)) - set(BALANCING_PRODUCTS)
    if bad:
        raise PriceDataError(
            f"{path}: unknown product(s) {', '.join(sorted(bad))}; "
            f"expected {', '.join(BALANCING_PRODUCTS)}."
        )
    fcr_act = df.loc[
        (df["product"] == "fcr"),
        "activation_price_eur_per_mwh",
    ]
    if fcr_act.notna().any() and (fcr_act.fillna(0.0) != 0.0).any():
        raise PriceDataError(
            f"{path}: FCR carries no activation price by design; leave "
            "the cell empty or 0."
        )
    duplicated = df.duplicated(subset=["year", "product"])
    if duplicated.any():
        first = df[duplicated].iloc[0]
        raise PriceDataError(
            f"{path}: duplicate (year, product) row "
            f"({int(first['year'])}, {first['product']})."
        )
    return df


def load_scenario_store(
    store_dir: str | Path,
    *,
    name: str,
    provider: str,
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
    """Load and validate one ``file``-provider scenario store directory."""
    store_dir = Path(store_dir)
    if not store_dir.is_dir():
        raise PriceDataError(
            f"price-scenario store directory not found: {store_dir}."
        )
    meta = _read_meta(store_dir)
    dam_table = _read_dam_table(store_dir)
    dam = _curves_from_table(
        dam_table, "dam_price_eur_per_mwh",
        n_steps=n_steps, dt_minutes=dt_minutes,
        source=f"{store_dir.name}/dam",
    )
    _validate_year_coverage(
        sorted(dam), n_years, source=f"{store_dir.name}/dam",
    )
    ida: dict[int, np.ndarray] | None = None
    if "ida_price_eur_per_mwh" in dam_table.columns and (
        dam_table["ida_price_eur_per_mwh"].notna().any()
    ):
        ida = _curves_from_table(
            dam_table.dropna(subset=["ida_price_eur_per_mwh"]),
            "ida_price_eur_per_mwh",
            n_steps=n_steps, dt_minutes=dt_minutes,
            source=f"{store_dir.name}/ida",
        )
    balancing = _read_balancing_annual(store_dir)

    # Basis bridge (real→nominal etc.), applied per curve year on its
    # CALENDAR year.
    store_basis = str(meta["basis"])
    store_base_year = int(meta.get("base_year") or 0)
    engine_basis = str(engine_basis).strip().lower()
    if engine_basis not in ("nominal", "real"):
        raise PriceDataError(
            f"price_basis {engine_basis!r} must be 'nominal' or 'real'."
        )
    if engine_basis == "real" and not engine_base_year:
        raise PriceDataError(
            "price_basis 'real' requires price_base_year on the "
            "scenario_engine sheet."
        )
    if store_basis != engine_basis or store_basis == "real":
        for y in list(dam):
            factor = _basis_factor(
                start_year + y - 1,
                store_basis=store_basis,
                store_base_year=store_base_year,
                engine_basis=engine_basis,
                engine_base_year=int(engine_base_year),
                cpi_pct=cpi_pct,
            )
            dam[y] = dam[y] * factor
            if ida is not None and y in ida:
                ida[y] = ida[y] * factor
        if balancing is not None:
            factors = balancing["year"].astype(int).map(
                lambda y: _basis_factor(
                    start_year + y - 1,
                    store_basis=store_basis,
                    store_base_year=store_base_year,
                    engine_basis=engine_basis,
                    engine_base_year=int(engine_base_year),
                    cpi_pct=cpi_pct,
                )
            )
            for price_col in (
                "capacity_price_eur_per_mwh",
                "activation_price_eur_per_mwh",
            ):
                balancing[price_col] = (
                    balancing[price_col].astype(float) * factors
                )
        logger.info(
            "[pricedata] %s: bridged store basis %r (base %s) to engine "
            "basis %r at %.2f %%/yr CPI.",
            store_dir.name, store_basis, store_base_year or "-",
            engine_basis, cpi_pct,
        )

    return ScenarioDeck(
        name=name,
        provider=provider,
        vintage=vintage,
        weight_pct=float(weight_pct),
        dam=dam,
        ida=ida,
        balancing=balancing,
        meta=meta,
    )


def stub_provider_error(provider: str) -> PriceDataError:
    """The documented not-yet-shipped vendor adapters."""
    return PriceDataError(
        f"provider {provider!r} is a documented stub: the adapter ships "
        "once sample deliverables exist (its GR coverage is "
        "unconfirmed). Use provider 'file' with a store directory in "
        "the canonical schema, or 'parametric' / 'tyndp'."
    )
