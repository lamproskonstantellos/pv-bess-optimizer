"""Structured (YAML / JSON) config loading + JSON Schema for the optimizer.

The canonical input is the Excel workbook
(:mod:`pvbess_opt.io`).  This module adds an equivalent machine-friendly
format: a YAML/JSON config whose sections mirror the workbook sheets, with
the time-series referenced by ``timeseries_path`` (CSV/Parquet) instead of a
35 040-row inline column.

A structured config is *materialized* to a workbook
(:func:`materialize_to_xlsx`) and then read through the same well-tested
Excel path, so YAML and Excel inputs produce identical results.

This module also owns the single PV-source resolution rule
(:func:`resolve_pv_source`): a blank / ``auto`` ``pv_source`` uses the
``pv_kwh`` column (or an external ``timeseries_path``) when it carries data
and otherwise fetches the profile from ``latitude`` / ``longitude`` via
PVGIS.  Both the Excel reader and the structured-config loader funnel
through it, so an Excel workbook with a location resolves exactly like the
equivalent YAML config.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .io import (
    _KEY_TO_SHEET,
    _SHEET_DEFAULTS,
    _normalise_trajectories_block,
    _parse_grid_export_max,
    _parse_value,
    reject_legacy_bess_capex_key,
    write_workbook,
)

logger = logging.getLogger(__name__)

STRUCTURED_SUFFIXES: frozenset[str] = frozenset({".yaml", ".yml", ".json"})

# Top-level config keys accepted besides the seven sheet sections.
_TOP_LEVEL_EXTRAS: frozenset[str] = frozenset({
    "timeseries",
    "timeseries_path",
    "financing",
    "grid",
    "max_injection_profile",
    "max_injection_profile_pv",
    "max_injection_profile_bess",
    "sizing",  # read separately by pvbess_opt.sizing.read_sizing_block
    "trajectories",  # per-year stream multipliers (Eq. E24)
    "price_decks",  # named price-deck files merged as __ variant columns
})

# Reference specific yield (kWh/kWp/yr) and divergence threshold for the PV
# consistency check.  EU-representative; the resource layer (PVGIS) calls it.
_REFERENCE_SPECIFIC_YIELD: float = 1200.0
_PV_CONSISTENCY_THRESHOLD_PCT: float = 30.0

# Europe/Athens standard-time offset applied to PVGIS (UTC) profiles —
# fixed, no DST, so the uniform 35 040-step grid is preserved.
_PVGIS_UTC_OFFSET_HOURS: int = 2


def is_structured_config(path: str | Path) -> bool:
    """True if ``path`` is a YAML/JSON config (vs an Excel workbook)."""
    return Path(path).suffix.lower() in STRUCTURED_SUFFIXES


def _load_raw(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data: Any = json.loads(text)
    else:
        import yaml

        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(
            f"Config {path} must be a mapping at the top level, got "
            f"{type(data).__name__}."
        )
    return data


def _read_timeseries_file(path: Path) -> pd.DataFrame:
    """Load an external time-series CSV/Parquet, parsing ``timestamp``."""
    if not path.exists():
        raise FileNotFoundError(f"timeseries_path not found: {path}")
    if path.suffix.lower() in (".parquet", ".pq"):
        df = pd.read_parquet(path)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df
    return pd.read_csv(path, parse_dates=["timestamp"])


def _resolve_timeseries(raw: dict[str, Any], base_dir: Path) -> pd.DataFrame:
    ref = raw.get("timeseries_path")
    if ref is None:
        pv_section = raw.get("pv")
        if isinstance(pv_section, dict):
            ref = pv_section.get("timeseries_path")
    if ref:
        ts_path = Path(str(ref))
        if not ts_path.is_absolute():
            ts_path = base_dir / ts_path
        return _read_timeseries_file(ts_path)
    inline = raw.get("timeseries")
    if isinstance(inline, list) and inline:
        df = pd.DataFrame(inline)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df
    raise ValueError(
        "Config provides no time-series: set 'timeseries_path' to a "
        "CSV/Parquet file (or an inline 'timeseries' list)."
    )


def _apply_financing_block(raw: dict[str, Any], typed: dict[str, Any]) -> None:
    """Map an optional top-level ``financing:`` block onto economics keys.

    ``gearing`` / ``interest_rate`` are fractions (0..1); they map to the
    percentage economics keys ``gearing_pct`` / ``debt_interest_rate_pct``.
    """
    fin = raw.get("financing")
    if not isinstance(fin, dict):
        return
    econ = typed["economics"]
    if "gearing" in fin:
        econ["gearing_pct"] = float(fin["gearing"]) * 100.0
    if "interest_rate" in fin:
        econ["debt_interest_rate_pct"] = float(fin["interest_rate"]) * 100.0
    if "tenor_years" in fin:
        econ["debt_tenor_years"] = int(fin["tenor_years"])
    elif "tenor" in fin:
        econ["debt_tenor_years"] = int(fin["tenor"])
    if "repayment" in fin:
        econ["debt_repayment"] = str(fin["repayment"])


def _apply_grid_block(raw: dict[str, Any], typed: dict[str, Any]) -> None:
    """Map an optional top-level ``grid:`` block onto economics keys.

    ``co2_intensity`` is kg/MWh (the economics-key unit); ``co2_annual_decline``
    is a fraction (0..1) mapped to the percentage key.
    """
    grid = raw.get("grid")
    if not isinstance(grid, dict):
        return
    econ = typed["economics"]
    if "co2_intensity" in grid:
        econ["grid_co2_intensity_kg_per_mwh"] = float(grid["co2_intensity"])
    if "co2_annual_decline" in grid:
        econ["grid_co2_annual_decline_pct"] = float(grid["co2_annual_decline"]) * 100.0


def load_structured_config(path: str | Path) -> dict[str, Any]:
    """Load a YAML/JSON config into the typed nested dict that
    :func:`pvbess_opt.io.read_workbook` produces."""
    path = Path(path)
    raw = _load_raw(path)
    typed: dict[str, Any] = {}
    for key in raw:
        if key in _SHEET_DEFAULTS or key in _TOP_LEVEL_EXTRAS:
            continue
        if key == "scenarios":
            # Honoured only via --scenarios <file>; flag the no-op loudly.
            logger.warning(
                "Config %s carries a 'scenarios' block, which is ignored "
                "for config files — pass it via --scenarios instead.", path,
            )
            continue
        logger.warning(
            "Config top-level key %r is unknown; ignored.", key,
        )
    for section, defaults in _SHEET_DEFAULTS.items():
        user = raw.get(section)
        if user is None:
            user = {}
        if not isinstance(user, dict):
            raise ValueError(f"Config section {section!r} must be a mapping.")
        if section == "bess":
            reject_legacy_bess_capex_key(
                user, source=f"Config {path} (bess section)",
            )
        known: dict[str, Any] = {}
        for key, value in user.items():
            if key in defaults:
                # Route every known key through the SAME typed parser the
                # workbook loader uses (io._parse_value / the grid-export
                # special case), so a YAML/JSON config validates and
                # normalises identically to the workbook — invalid enums
                # and unparseable types fail loudly at load instead of
                # slipping through as raw strings until the xlsx
                # round-trip (three-surface parity).
                if key == "p_grid_export_max_kw":
                    known[key] = _parse_grid_export_max(value, defaults[key])
                else:
                    known[key] = _parse_value(key, value, defaults[key])
                continue
            if section == "pv" and key == "timeseries_path":
                # Config-only hint consumed by _resolve_timeseries; keep it
                # out of the workbook pv sheet.
                continue
            # Mirror the workbook loader's unknown-key semantics
            # (io._parse_kv_sheet): warn and ignore, naming the owning
            # sheet when the key merely sits in the wrong section.
            if key in _KEY_TO_SHEET:
                logger.warning(
                    "Key %r found in %r section but belongs to %r; ignored.",
                    key, section, _KEY_TO_SHEET[key],
                )
                continue
            logger.warning(
                "%s section key %r is unknown; ignored.", section, key,
            )
        typed[section] = {**defaults, **known}
        if section == "bess" and "bess_degradation_pct_per_cycle" not in known:
            # Mirror io.read_workbook: a config that omits the cycle-fade
            # coefficient runs calendar-only, exactly like a workbook that
            # omits the row.
            typed["bess"]["bess_degradation_pct_per_cycle"] = 0.0
            logger.info(
                "[bess] bess_degradation_pct_per_cycle not found in "
                "config; defaulting to 0.0 (calendar-only mode)."
            )
    _apply_financing_block(raw, typed)
    _apply_grid_block(raw, typed)
    # Optional per-year trajectory block, normalised through the SAME
    # helper the workbook parser uses (three-surface parity: one
    # parse/validate path).  Lifecycle-aware invariants (coverage, the
    # m_1 == 1 anchor) are enforced by validate_workbook_params on the
    # materialize_to_xlsx round-trip.
    typed["trajectories"] = _normalise_trajectories_block(
        raw.get("trajectories"), source=f"Config {path}",
    )
    typed["ts"] = _resolve_timeseries(raw, path.parent)
    _resolve_price_decks(raw, path.parent, typed)
    mip = raw.get("max_injection_profile")
    if mip is not None:
        typed["max_injection_profile"] = np.asarray(mip, dtype=float)
    for _src in ("pv", "bess"):
        _mip_src = raw.get(f"max_injection_profile_{_src}")
        if _mip_src is not None:
            typed[f"max_injection_profile_{_src}"] = np.asarray(
                _mip_src, dtype=float,
            )
    resolve_pv_source(typed, base_dir=path.parent)
    return typed


def _resolve_price_decks(
    raw: dict[str, Any], base_dir: Path, typed: dict[str, Any],
) -> None:
    """Merge named price-deck files as ``<col>__<deck>`` variant columns.

    ``price_decks: {name: file.csv}`` keeps 35k-row decks out of the
    YAML: each file carries canonical price column names (plus an
    optional ``timestamp``), is row-count-checked against the model
    grid, and lands as suffix columns on ``typed['ts']`` — exactly the
    workbook variant-column convention, so the scenario runner's
    ``price_deck`` special works identically on both surfaces.  The
    merged columns ride ``typed['ts']`` through ``dump_structured_config``
    (the ts CSV keeps them), so the round-trip needs no separate block.
    """
    decks = raw.get("price_decks")
    if decks is None:
        return
    if not isinstance(decks, dict):
        raise ValueError(
            "'price_decks' must be a mapping of deck name to a "
            "CSV/Parquet path."
        )
    from .io import PRICE_DECK_BASE_COLUMNS

    ts = typed["ts"]
    for deck_name, ref in decks.items():
        deck = str(deck_name).strip().lower()
        deck_path = Path(str(ref))
        if not deck_path.is_absolute():
            deck_path = base_dir / deck_path
        if not deck_path.exists():
            raise FileNotFoundError(
                f"price deck {deck!r}: file not found: {deck_path}",
            )
        if deck_path.suffix.lower() in (".parquet", ".pq"):
            df = pd.read_parquet(deck_path)
        else:
            # timestamp is optional in a deck file (positional alignment
            # against the model grid; the row count is checked below).
            df = pd.read_csv(deck_path)
        price_cols = [c for c in df.columns if c != "timestamp"]
        if not price_cols:
            raise ValueError(
                f"price deck {deck!r} ({deck_path}) carries no price "
                f"columns."
            )
        for col in price_cols:
            if col not in PRICE_DECK_BASE_COLUMNS:
                raise ValueError(
                    f"price deck {deck!r} ({deck_path}): column {col!r} "
                    f"is not a recognised price column; expected one of "
                    f"{', '.join(PRICE_DECK_BASE_COLUMNS)}."
                )
            if len(df) != len(ts):
                raise ValueError(
                    f"price deck {deck!r} ({deck_path}) has {len(df)} "
                    f"rows but the model grid has {len(ts)}."
                )
            ts[f"{col}__{deck}"] = df[col].to_numpy(dtype=float)


def _resolve_pvgis(typed: dict[str, Any]) -> None:
    """Resolve ``pv_source='pvgis'`` in place.

    Fetch the per-kWp PVGIS profile, scale it by ``pv_nameplate_kwp``,
    upsample it onto the model grid, shift it to Europe/Athens standard
    time, and write it into ``ts['pv_kwh']``.  The section is then switched
    to ``file`` mode so the materialized workbook reads the resolved profile
    back through the standard path.
    """
    from .resource import fetch_pv_profile
    from .resource.resample import upsample_hourly_to_grid
    from .timeutils import apply_fixed_utc_offset

    pv = typed["pv"]
    ts = typed["ts"]
    lat = pv.get("latitude")
    lon = pv.get("longitude")
    nameplate = float(pv.get("pv_nameplate_kwp", 0.0) or 0.0)
    if lat is None or lon is None:
        raise ValueError(
            "pv_source='pvgis' requires 'latitude' and 'longitude' in the "
            "pv section."
        )
    if nameplate <= 0.0:
        raise ValueError("pv_source='pvgis' requires pv_nameplate_kwp > 0.")

    result = fetch_pv_profile(
        float(lat), float(lon),
        tilt=pv.get("tilt", "optimal"),
        azimuth=float(pv.get("azimuth", 0.0) or 0.0),
        losses_pct=float(pv.get("losses_pct", 14.0) or 14.0),
        weather_year=pv.get("weather_year", 2019),
        raddatabase=pv.get("raddatabase") or None,
    )
    scaled = result.per_kwp_kwh * nameplate
    n_steps = len(ts)
    hours = int(result.per_kwp_kwh.size)
    if hours <= 0 or n_steps % hours != 0:
        raise ValueError(
            f"time-series has {n_steps} steps, not a whole-hour multiple of "
            f"the {hours}-hour PVGIS profile; use a 15-minute non-leap-year "
            "grid (35 040 steps)."
        )
    steps_per_hour = n_steps // hours
    grid = upsample_hourly_to_grid(scaled, steps_per_hour)
    grid = apply_fixed_utc_offset(grid, _PVGIS_UTC_OFFSET_HOURS, steps_per_hour)

    ts = ts.copy()
    ts["pv_kwh"] = grid
    typed["ts"] = ts
    validate_pv_consistency(scaled, nameplate)
    pv["pv_source"] = "file"


# ---------------------------------------------------------------------------
# Single PV-source resolution rule (auto | file | pvgis)
# ---------------------------------------------------------------------------
#
# One rule, shared by the Excel reader (:func:`pvbess_opt.io.read_workbook`)
# and the structured-config loader, decides where the PV profile comes from:
#
#   pv_source | pv_kwh / timeseries_path | latitude+longitude | result
#   --------- | ---------------------- | ------------------ | --------------
#   auto      | has data               | (any)              | file (+warn if
#             |                        |                    | location too)
#   auto      | empty                  | present            | pvgis
#   auto      | empty                  | missing            | error
#   file      | has data               | (any)              | file
#   file      | empty                  | (any)              | error
#   pvgis     | (any)                  | present            | pvgis (+warn if
#             |                        |                    | column has data)
#   pvgis     | (any)                  | missing            | error
#
# A blank ``pv_source`` maps to ``auto``.  The deprecated ``pv_kwh_override``
# column counts as data only as a fallback when ``pv_kwh`` is empty.


def _normalize_pv_source(raw: Any) -> str:
    """Lower-case ``pv_source``; a blank / missing value maps to ``auto``."""
    if raw is None:
        return "auto"
    if isinstance(raw, float) and np.isnan(raw):
        return "auto"
    return str(raw).strip().lower() or "auto"


def _is_present_number(value: Any) -> bool:
    if value is None:
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return not np.isnan(number)


def _pv_has_location(pv: dict[str, Any]) -> bool:
    return _is_present_number(pv.get("latitude")) and _is_present_number(
        pv.get("longitude")
    )


def _column_has_data(ts: pd.DataFrame, column: str) -> bool:
    if column not in ts.columns:
        return False
    return bool(ts[column].notna().any())


def _pv_external_path(pv: dict[str, Any], base_dir: Path | None) -> Path | None:
    ref = pv.get("timeseries_path")
    if ref is None:
        return None
    text = str(ref).strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    return path


def _decide_pv_source(
    source: str, *, has_file_data: bool, has_location: bool,
) -> str:
    """Return ``'file'`` or ``'pvgis'`` per the resolution table, or raise."""
    if source not in ("auto", "file", "pvgis"):
        raise ValueError(
            f"pv_source must be one of auto / file / pvgis; got {source!r}."
        )
    if source == "file":
        if has_file_data:
            return "file"
        raise ValueError("pv_source=file but no pv_kwh/timeseries_path provided.")
    if source == "pvgis":
        if has_location:
            return "pvgis"
        raise ValueError("pv_source=pvgis but no latitude/longitude provided.")
    # auto
    if has_file_data:
        return "file"
    if has_location:
        return "pvgis"
    raise ValueError(
        "Provide pv_kwh data (or timeseries_path), or set latitude+longitude "
        "for PVGIS."
    )


def resolve_pv_source(
    typed: dict[str, Any], *, base_dir: Path | None = None,
) -> dict[str, Any]:
    """Resolve ``typed['ts']['pv_kwh']`` from the configured PV source.

    The single PV-source rule shared by the Excel reader and the
    structured-config loader: a blank ``pv_source`` maps to ``auto``;
    ``auto`` uses the ``pv_kwh`` column / ``timeseries_path`` when it carries
    data and otherwise fetches from ``latitude`` / ``longitude`` via PVGIS.
    Mutates and returns ``typed`` (``ts`` / ``pv``).
    """
    from .io import validate_pv_location_fields

    pv = typed["pv"]
    ts = typed["ts"]
    validate_pv_location_fields(pv)

    source = _normalize_pv_source(pv.get("pv_source"))
    has_location = _pv_has_location(pv)
    pv_kwh_has_data = _column_has_data(ts, "pv_kwh")
    override_has_data = _column_has_data(ts, "pv_kwh_override")
    ts_path = _pv_external_path(pv, base_dir)
    has_file_data = pv_kwh_has_data or ts_path is not None or override_has_data

    decision = _decide_pv_source(
        source, has_file_data=has_file_data, has_location=has_location,
    )

    if decision == "pvgis":
        if pv_kwh_has_data or override_has_data:
            logger.warning(
                "pv_source resolves to PVGIS but the timeseries already "
                "carries PV data; the location wins and the column is ignored."
            )
        _resolve_pvgis(typed)
        if "pv_kwh_override" in typed["ts"].columns:
            typed["ts"] = typed["ts"].drop(columns=["pv_kwh_override"])
        # The fetched PVGIS profile is already absolute kWh; it is written
        # into the materialized workbook and re-read verbatim, so the Excel
        # and YAML paths resolve to the identical pv_kwh.
        return typed

    if source == "auto" and has_location:
        logger.warning(
            "Both PV column/path data and a latitude/longitude are set; using "
            "the file PV data (the location is ignored)."
        )
    typed["ts"] = _resolve_pv_file_column(
        ts, pv, ts_path=ts_path,
        pv_kwh_has_data=pv_kwh_has_data,
        override_has_data=override_has_data,
    )
    return typed


def _resolve_pv_file_column(
    ts: pd.DataFrame,
    pv: dict[str, Any],
    *,
    ts_path: Path | None,
    pv_kwh_has_data: bool,
    override_has_data: bool,
) -> pd.DataFrame:
    """File-branch PV resolution: source ``pv_kwh`` and use it verbatim.

    ``pv_kwh`` is sourced (in priority order) from the column, an external
    ``timeseries_path`` file, or the deprecated ``pv_kwh_override`` column
    (fallback only).  The series is the absolute PV generation per step and
    is consumed as-is; ``pv_nameplate_kwp`` is metadata (per-kW CAPEX/OPEX
    and the sizing-sweep axis), not a rescale target.
    """
    nameplate = float(pv.get("pv_nameplate_kwp", 0.0) or 0.0)
    has_override_col = "pv_kwh_override" in ts.columns
    if has_override_col:
        logger.warning(
            "pv_kwh_override is deprecated; use the single 'pv_kwh' column. "
            "It is read only as a fallback when 'pv_kwh' is empty."
        )

    if pv_kwh_has_data:
        return ts.drop(columns=["pv_kwh_override"]) if has_override_col else ts

    if ts_path is not None:
        external = _read_timeseries_file(ts_path)
        if "pv_kwh" not in external.columns:
            raise ValueError(
                f"timeseries_path file has no 'pv_kwh' column: {ts_path}."
            )
        if len(external) != len(ts):
            raise ValueError(
                f"timeseries_path file has {len(external)} rows but the model "
                f"grid has {len(ts)}; resample it to match."
            )
        out = ts.drop(columns=["pv_kwh_override"]) if has_override_col else ts.copy()
        out["pv_kwh"] = external["pv_kwh"].astype(float).to_numpy()
        return out

    if override_has_data:
        return _apply_override_fallback(ts, nameplate)

    raise ValueError(
        "pv_source resolves to file but no pv_kwh / timeseries_path data is "
        "available."
    )


def _apply_override_fallback(
    ts: pd.DataFrame, nameplate_kwp: float,
) -> pd.DataFrame:
    """Use the deprecated ``pv_kwh_override`` column as ``pv_kwh`` verbatim.

    Backward-compatible read for old files: only reached when ``pv_kwh`` is
    empty.  Partial-NaN overrides raise; a fully-populated override is used
    as-is (with an implied-specific-production sanity warning).
    """
    override = ts["pv_kwh_override"]
    n_total = len(override)
    n_null = int(override.isna().sum())
    if n_null > 0:
        raise ValueError(
            f"pv_kwh_override has {n_null} NaN values out of {n_total}. "
            "Fill every row, or leave the column empty and use 'pv_kwh'."
        )
    out = ts.copy()
    out["pv_kwh"] = override.astype(float)
    out = out.drop(columns=["pv_kwh_override"])
    annual_sum = float(override.sum())
    if nameplate_kwp > 0.0:
        implied_sp = annual_sum / nameplate_kwp
        logger.info(
            "PV column: using deprecated pv_kwh_override verbatim (annual sum "
            "%.1f kWh, implied specific production %.1f kWh/kWp at "
            "pv_nameplate_kwp=%.1f).",
            annual_sum, implied_sp, nameplate_kwp,
        )
        if implied_sp < 500.0 or implied_sp > 2500.0:
            logger.warning(
                "PV column: implied specific production %.1f kWh/kWp at "
                "pv_nameplate_kwp=%.1f falls outside the plausible "
                "500-2500 kWh/kWp band. Check pv_nameplate_kwp.",
                implied_sp, nameplate_kwp,
            )
    else:
        logger.info(
            "PV column: using deprecated pv_kwh_override verbatim (annual sum "
            "%.1f kWh). pv_nameplate_kwp = 0 — no implied SP check.",
            annual_sum,
        )
    return out


def materialize_to_xlsx(input_path: str | Path, dst_dir: str | Path) -> Path:
    """Return an Excel path for ``input_path``.

    If it is already a workbook, return it unchanged.  If it is a structured
    config, load it and write an equivalent workbook into ``dst_dir`` so the
    canonical Excel read path produces identical results.
    """
    input_path = Path(input_path)
    if not is_structured_config(input_path):
        return input_path
    typed = load_structured_config(input_path)
    dst = Path(dst_dir) / f"{input_path.stem}.xlsx"
    write_workbook(typed, dst)
    return dst


def _yaml_scalar(value: Any) -> Any:
    """Coerce numpy scalars to built-in types for YAML/JSON dumping."""
    if isinstance(value, np.generic):
        return value.item()
    return value


def dump_structured_config(
    typed: dict[str, Any],
    config_path: str | Path,
    *,
    timeseries_path: str | Path | None = None,
) -> Path:
    """Serialise a typed dict to a YAML/JSON config plus a time-series CSV.

    The inverse of :func:`load_structured_config`; the round-trip is exact.
    """
    config_path = Path(config_path)
    if timeseries_path is None:
        timeseries_path = config_path.with_name(
            config_path.stem + "_timeseries.csv"
        )
    timeseries_path = Path(timeseries_path)
    typed["ts"].to_csv(timeseries_path, index=False)

    out: dict[str, Any] = {}
    for section in _SHEET_DEFAULTS:
        section_dict = typed.get(section)
        if isinstance(section_dict, dict):
            out[section] = {k: _yaml_scalar(v) for k, v in section_dict.items()}
    try:
        out["timeseries_path"] = str(
            timeseries_path.relative_to(config_path.parent)
        )
    except ValueError:
        out["timeseries_path"] = str(timeseries_path)
    mip = typed.get("max_injection_profile")
    if mip is not None:
        out["max_injection_profile"] = np.asarray(mip, dtype=float).tolist()
    for _src in ("pv", "bess"):
        _mip_src = typed.get(f"max_injection_profile_{_src}")
        if _mip_src is not None:
            out[f"max_injection_profile_{_src}"] = np.asarray(
                _mip_src, dtype=float,
            ).tolist()
    trajectories = typed.get("trajectories")
    if trajectories:
        # Emitted only when set so an untouched config round-trips with
        # zero diff.
        out["trajectories"] = {
            stream: {
                "mode": str(spec["mode"]),
                "values": [float(v) for v in spec["values"]],
            }
            for stream, spec in trajectories.items()
        }

    if config_path.suffix.lower() == ".json":
        config_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    else:
        import yaml

        config_path.write_text(
            yaml.safe_dump(out, sort_keys=False), encoding="utf-8",
        )
    return config_path


# ---------------------------------------------------------------------------
# JSON Schema emission + validation
# ---------------------------------------------------------------------------


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def _pv_schema_overrides() -> dict[str, Any]:
    """JSON-Schema property specs for the PVGIS location / geometry keys.

    ``latitude`` / ``longitude`` default to None and ``tilt`` / ``weather_year``
    accept a number *or* a string token, so the auto-derived single-type specs
    would be wrong; these explicit specs replace them.
    """
    return {
        "latitude": {"type": "number", "minimum": -90, "maximum": 90},
        "longitude": {"type": "number", "minimum": -180, "maximum": 180},
        "tilt": {"description": "degrees in [0, 90], or 'optimal'"},
        "azimuth": {"type": "number", "minimum": -180, "maximum": 360},
        "losses_pct": {"type": "number", "minimum": 0, "maximum": 100},
        "weather_year": {"description": "non-leap calendar year, or 'tmy'"},
        "raddatabase": {"type": "string"},
        "timeseries_path": {"type": "string"},
    }


def config_json_schema() -> dict[str, Any]:
    """Emit a JSON Schema (draft 2020-12) describing the config structure."""
    from .io import _ALLOWED_VALUES

    properties: dict[str, Any] = {}
    for section, defaults in _SHEET_DEFAULTS.items():
        props: dict[str, Any] = {}
        for key, value in defaults.items():
            spec: dict[str, Any] = {"type": _json_type(value)}
            if key in _ALLOWED_VALUES:
                spec["enum"] = sorted(_ALLOWED_VALUES[key])
            props[key] = spec
        if section == "pv":
            props.update(_pv_schema_overrides())
        if section == "bess":
            # Three-way replacement semantics: a non-negative integer
            # (0 = never, N = scheduled year) or the literal 'auto'.
            props["bess_replacement_year"] = {
                "type": ["integer", "string"],
            }
        properties[section] = {
            "type": "object",
            "properties": props,
            "additionalProperties": True,
        }
    properties["timeseries_path"] = {"type": "string"}
    properties["max_injection_profile"] = {"type": "array"}
    properties["max_injection_profile_pv"] = {"type": "array"}
    properties["max_injection_profile_bess"] = {"type": "array"}
    properties["price_decks"] = {
        "type": "object",
        "description": (
            "Named price decks: mapping of deck name to a CSV/Parquet "
            "path whose canonical price columns are merged as "
            "<column>__<deck> variant columns; scenarios select a deck "
            "with the price_deck special."
        ),
    }
    properties["trajectories"] = {
        "type": "object",
        "description": (
            "Per-year stream multipliers (Eq. E24): mapping of stream "
            "name (revenue_dam, revenue_retail, balancing_capacity, "
            "balancing_activation, opex, opex_pv, opex_bess) to a "
            "values list or a {mode: replace|overlay, values: [...]} "
            "block; year-1 value must be 1.0."
        ),
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "pvbess-optimizer configuration",
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
    }


def _type_matches(value: Any, json_type: str | list[str] | None) -> bool:
    if isinstance(json_type, (list, tuple)):
        return any(_type_matches(value, jt) for jt in json_type)
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "array":
        return isinstance(value, (list, tuple))
    return True


def validate_config(
    raw: dict[str, Any], schema: dict[str, Any] | None = None,
) -> list[str]:
    """Validate a raw config mapping against :func:`config_json_schema`.

    Returns a list of human-readable error strings (empty == valid).  A
    dependency-free subset of JSON Schema: per-key type and ``enum`` checks
    on known section properties; unknown keys are permitted.
    """
    schema = schema or config_json_schema()
    errors: list[str] = []
    section_props: dict[str, Any] = schema["properties"]
    for section, section_schema in section_props.items():
        if section not in raw or section_schema.get("type") != "object":
            continue
        value = raw[section]
        if not isinstance(value, dict):
            errors.append(
                f"{section}: expected object, got {type(value).__name__}"
            )
            continue
        props = section_schema.get("properties", {})
        for key, item in value.items():
            spec = props.get(key)
            if spec is None or item is None:
                # Unknown keys are permitted; a None value means "absent"
                # (an optional field left blank) and is not type-checked.
                continue
            if "enum" in spec and item not in spec["enum"]:
                errors.append(f"{section}.{key}: {item!r} not in {spec['enum']}")
            elif not _type_matches(item, spec.get("type")):
                errors.append(
                    f"{section}.{key}: expected {spec.get('type')}, "
                    f"got {type(item).__name__}"
                )
    return errors


def validate_pv_consistency(
    pv_kwh: Any,
    nameplate_kwp: float,
    *,
    reference_specific_yield: float = _REFERENCE_SPECIFIC_YIELD,
    threshold_pct: float = _PV_CONSISTENCY_THRESHOLD_PCT,
) -> float | None:
    """Warn when an absolute PV series is inconsistent with the nameplate.

    Computes the implied kWp (annual energy / reference specific yield) and
    logs a WARNING when it diverges from ``nameplate_kwp`` by more than
    ``threshold_pct``.  Returns the implied kWp (or None when inputs are
    degenerate).  Used by the PVGIS ingestion path (resource layer).
    """
    annual = float(np.asarray(pv_kwh, dtype=float).sum())
    if nameplate_kwp <= 0.0 or reference_specific_yield <= 0.0 or annual <= 0.0:
        return None
    implied_kwp = annual / reference_specific_yield
    rel_pct = abs(implied_kwp - nameplate_kwp) / nameplate_kwp * 100.0
    if rel_pct > threshold_pct:
        logger.warning(
            "PV consistency: annual PV energy %.0f kWh implies ~%.0f kWp at "
            "%.0f kWh/kWp, but pv_nameplate_kwp=%.0f (%.0f%% off). Check the "
            "PV series or the declared nameplate.",
            annual, implied_kwp, reference_specific_yield, nameplate_kwp,
            rel_pct,
        )
    return implied_kwp
