"""Structured (YAML / JSON) config loading + JSON Schema for the optimizer.

The canonical input is the eight-sheet Excel workbook
(:mod:`pvbess_opt.io`).  This module adds an equivalent machine-friendly
format: a YAML/JSON config whose sections mirror the workbook sheets, with
the time-series referenced by ``timeseries_path`` (CSV/Parquet) instead of a
35 040-row inline column.

A structured config is *materialized* to a workbook
(:func:`materialize_to_xlsx`) and then read through the same well-tested
Excel path, so YAML and Excel inputs produce identical results.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .io import _SHEET_DEFAULTS, write_workbook

logger = logging.getLogger(__name__)

STRUCTURED_SUFFIXES: frozenset[str] = frozenset({".yaml", ".yml", ".json"})

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


def load_structured_config(path: str | Path) -> dict[str, Any]:
    """Load a YAML/JSON config into the typed nested dict that
    :func:`pvbess_opt.io.read_workbook` produces."""
    path = Path(path)
    raw = _load_raw(path)
    typed: dict[str, Any] = {}
    for section, defaults in _SHEET_DEFAULTS.items():
        user = raw.get(section)
        if user is None:
            user = {}
        if not isinstance(user, dict):
            raise ValueError(f"Config section {section!r} must be a mapping.")
        # Drop the config-only timeseries_path hint from the pv section so it
        # does not leak into the workbook pv sheet.
        user = {k: v for k, v in user.items() if k != "timeseries_path"}
        typed[section] = {**defaults, **user}
    _apply_financing_block(raw, typed)
    typed["ts"] = _resolve_timeseries(raw, path.parent)
    mip = raw.get("max_injection_profile")
    if mip is not None:
        typed["max_injection_profile"] = np.asarray(mip, dtype=float)
    if str(typed["pv"].get("pv_source", "file")).strip().lower() == "pvgis":
        _resolve_pvgis(typed)
    return typed


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
        properties[section] = {
            "type": "object",
            "properties": props,
            "additionalProperties": True,
        }
    properties["timeseries_path"] = {"type": "string"}
    properties["max_injection_profile"] = {"type": "array"}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "pvbess-optimizer configuration",
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
    }


def _type_matches(value: Any, json_type: str | None) -> bool:
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
            if spec is None:
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
