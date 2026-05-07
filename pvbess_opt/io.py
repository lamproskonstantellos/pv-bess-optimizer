"""Excel input parsing and output writing for the PV+BESS optimizer.

The schema is three sheets:

* ``timeseries`` — per-step data with lowercase snake_case column
  names: ``timestamp``, ``load_kwh``, ``pv_kwh``,
  ``dam_price_eur_per_mwh``, optional ``retail_price_eur_per_mwh``.
* ``project``    — physical system + regulatory framework +
  optimization behavior, in three logical groups (separator rows
  allowed).  Keys: see :data:`PROJECT_DEFAULTS`.
* ``economic``   — project finance + plot preferences, in six
  logical groups.  Mandatory.

Public loader API
-----------------

* :func:`read_workbook` returns the typed nested dict:

  .. code-block:: python

     {
         "ts": pd.DataFrame,           # lowercase snake_case
         "project": {
             "system":         {...},  # efficiency_charge, p_charge_max_kw, ...
             "regulatory":     {...},  # mode, retail_tariff_eur_per_mwh, ...
             "optimization":   {...},  # solver_mip_gap, weight_curtail_tiebreak, ...
         },
         "economic": {...},
         "dt_minutes": int,            # auto-detected from the timeseries
     }

* :func:`read_inputs` returns a flat ``(params, ts)`` tuple suitable for
  the optimizer / KPI / lifetime modules.

Mode-specific timeseries semantics
----------------------------------

* In ``vnb`` mode the ``load_kwh`` column is required; missing → ValueError.
* In ``merchant`` mode ``load_kwh`` is optional — if present, the loader
  logs an INFO message and the optimizer pins all load-coverage flows to 0.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

TRUTHY = {"true", "1", "yes", "y", "t"}
FALSY = {"false", "0", "no", "n", "f"}

_COERCE_FAILED = object()


# ---------------------------------------------------------------------------
# Canonical defaults (single source of truth)
# ---------------------------------------------------------------------------

_SYSTEM_DEFAULTS: dict[str, Any] = {
    "pv_nameplate_kwp": 0.0,
    "bess_power_kw": 0.0,
    "bess_capacity_kwh": 0.0,
    "efficiency_charge": 0.97,
    "efficiency_discharge": 0.97,
    "soc_min_frac": 0.20,
    "soc_max_frac": 0.95,
    "initial_soc_frac": 0.50,
    "terminal_soc_equal": True,
    "p_charge_max_kw": 0.0,
    "p_dis_max_kw": 0.0,
    "battery_hours": 4.0,
    "max_cycles_per_day": 1.0,
    "p_grid_export_max_kw": 8000.0,
}

_REGULATORY_DEFAULTS: dict[str, Any] = {
    "mode": "vnb",
    "retail_tariff_eur_per_mwh": 132.0,
    "curtailment_pct": 27.0,
    "allow_bess_grid_charging": False,
    "settlement_minutes": 15,
}

_OPTIMIZATION_DEFAULTS: dict[str, Any] = {
    "weight_curtail_tiebreak": 1.0e-5,
    "weight_cycles_term": 0.0,
    "solver_mip_gap": 0.001,
    "solver_time_limit_seconds": 1800,
}

PROJECT_DEFAULTS: dict[str, dict[str, Any]] = {
    "system": dict(_SYSTEM_DEFAULTS),
    "regulatory": dict(_REGULATORY_DEFAULTS),
    "optimization": dict(_OPTIMIZATION_DEFAULTS),
}

ECON_DEFAULTS: dict[str, Any] = {
    "project_lifecycle_years": 25,
    "project_start_year": 2026,
    "discount_rate_pct": 7.0,
    "opex_inflation_pct": 1.0,
    "revenue_inflation_pct": 2.0,
    "capex_pv_eur_per_kw": 525.0,
    "capex_bess_eur_per_kw": 200.0,
    "capex_licenses_eur_per_kw": 90.0,
    "opex_pv_eur_per_kwp": 7.0,
    "opex_bess_eur_per_kw": 14.0,
    "pv_degradation_year1_pct": 2.5,
    "pv_degradation_annual_pct": 0.55,
    "bess_degradation_annual_pct": 2.0,
    "bess_replacement_year": 0,
    "bess_replacement_cost_pct": 50.0,
    "sensitivity_enabled": True,
    "sensitivity_capex_delta_pct": 10.0,
    "sensitivity_opex_delta_pct": 10.0,
    "sensitivity_revenue_delta_pct": 10.0,
    "sensitivity_discount_rate_delta_pp": 2.0,
    "show_titles": False,
    "currency_format": "auto",
    "plot_daily_year1": True,
    "plot_monthly_scope": "all",
    "plot_yearly_scope": "all",
}

_ECON_INT_KEYS: frozenset[str] = frozenset({
    "project_lifecycle_years",
    "project_start_year",
    "bess_replacement_year",
})
_ECON_BOOL_KEYS: frozenset[str] = frozenset({
    "sensitivity_enabled",
    "show_titles",
    "plot_daily_year1",
})
_ECON_STR_KEYS: frozenset[str] = frozenset({
    "currency_format",
    "plot_monthly_scope",
    "plot_yearly_scope",
})
_ECON_ALLOWED_VALUES: dict[str, frozenset[str]] = {
    "currency_format": frozenset({"auto", "millions", "raw"}),
    "plot_monthly_scope": frozenset({"none", "year1_only", "all"}),
    "plot_yearly_scope": frozenset({"none", "all"}),
}

_PROJECT_BOOL_KEYS: frozenset[str] = frozenset({
    "terminal_soc_equal",
    "allow_bess_grid_charging",
})
_PROJECT_INT_KEYS: frozenset[str] = frozenset({
    "settlement_minutes",
    "solver_time_limit_seconds",
})
_PROJECT_STR_KEYS: frozenset[str] = frozenset({"mode"})
_PROJECT_ALLOWED_VALUES: dict[str, frozenset[str]] = {
    "mode": frozenset({"vnb", "merchant"}),
}

_PROJECT_KEY_TO_GROUP: dict[str, str] = {}
for _grp, _keys in PROJECT_DEFAULTS.items():
    for _k in _keys:
        _PROJECT_KEY_TO_GROUP[_k] = _grp


# ---------------------------------------------------------------------------
# Sheet row templates (shared with build_input_xlsx.py)
# ---------------------------------------------------------------------------

_PROJECT_SYSTEM_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("pv_nameplate_kwp", 0, "kWp",
     "Declared PV nameplate capacity. If 0, inferred from max(pv_kwh)/dt_h."),
    ("bess_power_kw", 0, "kW",
     "Declared BESS power. If 0, falls back to p_dis_max_kw."),
    ("bess_capacity_kwh", 0, "kWh",
     "Declared BESS energy capacity. If 0, derived from "
     "bess_power_kw * battery_hours."),
    ("efficiency_charge", 0.97, "-",
     "Battery charging efficiency (0-1). Round-trip = efficiency_charge * efficiency_discharge."),
    ("efficiency_discharge", 0.97, "-",
     "Battery discharging efficiency (0-1)."),
    ("soc_min_frac", 0.20, "-",
     "Minimum SOC as fraction of nominal capacity (0.20 = 20 %)."),
    ("soc_max_frac", 0.95, "-",
     "Maximum SOC as fraction of nominal capacity (0.95 = 95 %)."),
    ("initial_soc_frac", 0.50, "-",
     "SOC at the first timestep, as a fraction of capacity."),
    ("terminal_soc_equal", True, "bool",
     "If TRUE, force final SOC == initial SOC (closed cycle)."),
    ("p_charge_max_kw", 8000, "kW",
     "Maximum battery charge power (kW)."),
    ("p_dis_max_kw", 8000, "kW",
     "Maximum battery discharge power (kW)."),
    ("battery_hours", 4, "h",
     "E/P ratio cap; sets bess_capacity_kwh if not given."),
    ("max_cycles_per_day", 1, "-",
     "Daily equivalent-cycle cap (sum of discharge / capacity)."),
    ("p_grid_export_max_kw", 8000, "kW",
     "Grid-connection export limit (kW)."),
)

_PROJECT_REGULATORY_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("mode", "vnb", "enum",
     "vnb | merchant. vnb requires a co-located load and enforces load "
     "priority + no simultaneous grid I/O. merchant has no load; PV/BESS "
     "dispatch entirely to DAM."),
    ("retail_tariff_eur_per_mwh", 132, "EUR/MWh",
     "Retail tariff used in vnb mode for load coverage."),
    ("curtailment_pct", 27, "%",
     "Static curtailment cap as percent. Default 27 = distribution-"
     "connected per MD YPEN/DAPEEK/53563/1556/2023. Applies to grid-"
     "bound flows in BOTH vnb and merchant modes."),
    ("allow_bess_grid_charging", False, "bool",
     "If TRUE the BESS may charge from the grid in periods with pv_kwh ~ 0."),
    ("settlement_minutes", 15, "int",
     "Greek VNB settles every 15 min per MD YPEN/DAPEEK/93976/2772/2024. "
     "Currently informational; the MILP timestep is auto-detected."),
)

_PROJECT_OPTIMIZATION_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("weight_curtail_tiebreak", 1.0e-5, "EUR/kWh",
     "Tiny tie-breaker on pv_curtail for determinism under degeneracy. "
     "NOT a constraint substitute. Set 0 to disable."),
    ("weight_cycles_term", 0.0, "EUR/MWh",
     "Optional bonus per discharged MWh."),
    ("solver_mip_gap", 0.001, "-",
     "Solver MIP relative gap (CLI --mip-gap overrides)."),
    ("solver_time_limit_seconds", 1800, "s",
     "Solver wall-time limit (CLI --time-limit overrides)."),
)

_PROJECT_GROUPS_TEMPLATE: tuple[
    tuple[str, str, tuple[tuple[str, object, str, str], ...]], ...
] = (
    ("# system",       "system",       _PROJECT_SYSTEM_ROWS),
    ("# regulatory",   "regulatory",   _PROJECT_REGULATORY_ROWS),
    ("# optimization", "optimization", _PROJECT_OPTIMIZATION_ROWS),
)

_ECON_HORIZON_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("project_lifecycle_years", 25, "years",
     "Total project horizon used to project Years 0..N."),
    ("project_start_year", 2026, "year",
     "Calendar year of Year 1 (commissioning) AND Year 0 (CAPEX, paid at "
     "COD). HOMER / Gridcog / Aurora convention."),
    ("discount_rate_pct", 7.0, "%",
     "WACC. Typical EU RES band 6-8 %."),
    ("opex_inflation_pct", 1.0, "%",
     "Annual OPEX escalation rate."),
    ("revenue_inflation_pct", 2.0, "%",
     "Annual revenue escalation rate (ECB target). Set 0 to disable."),
)

_ECON_CAPEX_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("capex_pv_eur_per_kw", 525, "EUR/kWp",
     "Per-kWp PV CAPEX. Set 0 if PV already exists."),
    ("capex_bess_eur_per_kw", 200, "EUR/kW",
     "Per-kW BESS CAPEX (DC + PCS). Set 0 if BESS already exists."),
    ("capex_licenses_eur_per_kw", 90, "EUR/kW",
     "Licensing / permitting CAPEX, applied to (pv_kwp + bess_kw)."),
)

_ECON_OPEX_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("opex_pv_eur_per_kwp", 7, "EUR/kWp/yr",
     "Annual O&M for PV."),
    ("opex_bess_eur_per_kw", 14, "EUR/kW/yr",
     "Annual O&M for BESS."),
)

_ECON_DEGRADATION_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("pv_degradation_year1_pct", 2.5, "%",
     "Initial light-induced degradation (LID) applied at start of Year 2."),
    ("pv_degradation_annual_pct", 0.55, "%",
     "Linear PV degradation after Year 1 (Tier-1 warranty)."),
    ("bess_degradation_annual_pct", 2.0, "%",
     "Linear BESS capacity fade. Approximate Tier-1 LFP cell warranty."),
    ("bess_replacement_year", 0, "year",
     "Year of BESS cell replacement (0 = no replacement). Typical 10 or 15."),
    ("bess_replacement_cost_pct", 50, "%",
     "Replacement cost as percent of original BESS CAPEX."),
)

_ECON_SENSITIVITY_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("sensitivity_enabled", True, "bool",
     "Run a one-at-a-time tornado sensitivity after the base run."),
    ("sensitivity_capex_delta_pct", 10, "%",
     "Symmetric +/- delta on total CAPEX."),
    ("sensitivity_opex_delta_pct", 10, "%",
     "Symmetric +/- delta on total annual OPEX."),
    ("sensitivity_revenue_delta_pct", 10, "%",
     "Symmetric +/- delta on Year-1 revenue base."),
    ("sensitivity_discount_rate_delta_pp", 2.0, "pp",
     "Symmetric +/- delta on the discount rate, in percentage points. "
     "NPV tornado only - drops out of IRR tornado by definition."),
)

_ECON_OUTPUT_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("show_titles", False, "bool",
     "Render plot titles. IEEE figures normally rely on the figure caption."),
    ("currency_format", "auto", "enum",
     "auto | millions | raw."),
    ("plot_daily_year1", True, "bool",
     "Render Year-1 daily plots (~1100 PDFs). FALSE for fast iterations."),
    ("plot_monthly_scope", "all", "scope",
     "none | year1_only | all."),
    ("plot_yearly_scope", "all", "scope",
     "none | all."),
)

_ECON_GROUPS_TEMPLATE: tuple[
    tuple[str, tuple[tuple[str, object, str, str], ...]], ...
] = (
    ("# horizon", _ECON_HORIZON_ROWS),
    ("# capex", _ECON_CAPEX_ROWS),
    ("# opex", _ECON_OPEX_ROWS),
    ("# degradation_replacement", _ECON_DEGRADATION_ROWS),
    ("# sensitivity", _ECON_SENSITIVITY_ROWS),
    ("# output", _ECON_OUTPUT_ROWS),
)


def _build_project_sheet(typed_project: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """Assemble the ``project`` sheet from a typed project dict."""
    rows: list[dict[str, Any]] = []
    for label, group, group_rows in _PROJECT_GROUPS_TEMPLATE:
        rows.append({"key": label, "value": "", "unit": "", "notes": ""})
        for key, _default, unit, notes in group_rows:
            value = typed_project[group].get(key, _default)
            rows.append({"key": key, "value": value, "unit": unit, "notes": notes})
    return pd.DataFrame(rows, columns=["key", "value", "unit", "notes"])


def _build_economic_sheet(typed_econ: dict[str, Any]) -> pd.DataFrame:
    """Assemble the ``economic`` sheet from a typed economic dict."""
    rows: list[dict[str, Any]] = []
    for label, group_rows in _ECON_GROUPS_TEMPLATE:
        rows.append({"key": label, "value": "", "unit": "", "notes": ""})
        for key, _default, unit, notes in group_rows:
            value = typed_econ.get(key, _default)
            rows.append({"key": key, "value": value, "unit": unit, "notes": notes})
    return pd.DataFrame(rows, columns=["key", "value", "unit", "notes"])


def write_workbook(typed: dict[str, Any], dst: str | Path) -> Path:
    """Write a workbook from a typed nested dict.

    Output sheets are ``timeseries``, ``project`` (three logical groups),
    and ``economic`` (six logical groups).  Separator rows are inserted
    between groups for human readability — the loader skips any row whose
    ``key`` is empty / NaN or starts with ``#``.
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    project = _build_project_sheet(typed["project"])
    econ_df = _build_economic_sheet(typed["economic"])
    with pd.ExcelWriter(dst, engine="openpyxl") as writer:
        typed["ts"].to_excel(writer, sheet_name="timeseries", index=False)
        project.to_excel(writer, sheet_name="project", index=False)
        econ_df.to_excel(writer, sheet_name="economic", index=False)
    return dst


# ---------------------------------------------------------------------------
# Generic value-coercion helpers
# ---------------------------------------------------------------------------


def _coerce(value: Any, cast: type, default: Any) -> Any:
    """Cast ``value`` to ``cast``; return ``default`` on empty/NaN; sentinel on error."""
    if value is None:
        return default
    if isinstance(value, float) and np.isnan(value):
        return default
    if isinstance(value, str) and value.strip() == "":
        return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        return _COERCE_FAILED


def _get_param(
    params: dict[str, Any],
    keys: str | Iterable[str],
    default: Any = None,
    cast: type = float,
) -> Any:
    """Look up the first non-empty param under any of ``keys``, casting to ``cast``."""
    if isinstance(keys, str):
        keys = (keys,)
    for key in keys:
        if key in params:
            value = params[key]
            if isinstance(value, float) and np.isnan(value):
                continue
            if isinstance(value, str) and value.strip() == "":
                continue
            coerced = _coerce(value, cast, default)
            if coerced is _COERCE_FAILED:
                logger.warning(
                    "Param %r could not be parsed as %s (got %r); using default %r.",
                    key, getattr(cast, "__name__", str(cast)), value, default,
                )
                return default
            return coerced
    return default


def _parse_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and np.isnan(value):
            return default
        return value != 0
    token = str(value).strip().lower()
    if token == "":
        return default
    if token in TRUTHY:
        return True
    if token in FALSY:
        return False
    return default


def _parse_curtailment(raw: Any) -> float:
    """Accept curtailment as fraction (0.27) or percent (27 -> 0.27)."""
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if value > 1.0:
        value /= 100.0
    return float(np.clip(value, 0.0, 1.0))


def _parse_string_enum(
    value: Any, default: str, allowed: frozenset[str], key: str,
) -> str:
    if value is None:
        return default
    if isinstance(value, float) and np.isnan(value):
        return default
    token = str(value).strip().lower()
    if token == "":
        return default
    if token not in allowed:
        logger.warning(
            "Workbook value for %r is %r which is not in %s; using default %r.",
            key, value, sorted(allowed), default,
        )
        return default
    return token


# ---------------------------------------------------------------------------
# Sheet → flat-dict reduction (skips separator rows)
# ---------------------------------------------------------------------------


def _flat_dict_from_sheet(df: pd.DataFrame) -> dict[str, Any]:
    """Reduce a (key, value, ...) sheet to ``{key: value}``, skipping separators."""
    if "key" not in df.columns or "value" not in df.columns:
        return {}
    out: dict[str, Any] = {}
    for _, row in df.iterrows():
        raw_key = row.get("key")
        if not isinstance(raw_key, str):
            continue
        key = raw_key.strip()
        if not key or key.startswith("#"):
            continue
        out[key] = row.get("value")
    return out


# ---------------------------------------------------------------------------
# Sheet parsers
# ---------------------------------------------------------------------------


def _parse_project_value(group: str, key: str, raw: Any) -> Any:
    """Type-coerce a single project-sheet value into its canonical type."""
    default = PROJECT_DEFAULTS[group][key]
    if key in _PROJECT_BOOL_KEYS:
        return _parse_bool(raw, bool(default))
    if key in _PROJECT_INT_KEYS:
        coerced = _coerce(raw, int, default)
        if coerced is _COERCE_FAILED:
            logger.warning(
                "Workbook value for %r could not be parsed as int (got %r); "
                "using default %r.", key, raw, default,
            )
            return default
        return coerced
    if key in _PROJECT_STR_KEYS:
        return _parse_string_enum(
            raw, str(default), _PROJECT_ALLOWED_VALUES.get(key, frozenset()),
            key,
        )
    coerced = _coerce(raw, float, default)
    if coerced is _COERCE_FAILED:
        logger.warning(
            "Workbook value for %r could not be parsed as float (got %r); "
            "using default %r.", key, raw, default,
        )
        return default
    return coerced


def _parse_project_sheet(flat: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build the typed ``project`` dict from a flat ``key: value`` mapping."""
    project: dict[str, dict[str, Any]] = {
        "system": dict(_SYSTEM_DEFAULTS),
        "regulatory": dict(_REGULATORY_DEFAULTS),
        "optimization": dict(_OPTIMIZATION_DEFAULTS),
    }
    for key, raw in flat.items():
        group = _PROJECT_KEY_TO_GROUP.get(key)
        if group is None:
            logger.warning("Project sheet key %r is unknown; ignored.", key)
            continue
        project[group][key] = _parse_project_value(group, key, raw)
    return project


def _parse_economic_sheet(flat: dict[str, Any]) -> dict[str, Any]:
    """Build the typed ``economic`` dict from a flat ``key: value`` mapping."""
    out = dict(ECON_DEFAULTS)
    for key, raw in flat.items():
        if key not in ECON_DEFAULTS:
            logger.warning("Economic sheet key %r is unknown; ignored.", key)
            continue
        if key in _ECON_BOOL_KEYS:
            out[key] = _parse_bool(raw, bool(ECON_DEFAULTS[key]))
        elif key in _ECON_STR_KEYS:
            out[key] = _parse_string_enum(
                raw, str(ECON_DEFAULTS[key]),
                _ECON_ALLOWED_VALUES.get(key, frozenset()),
                key,
            )
        else:
            cast: type = int if key in _ECON_INT_KEYS else float
            coerced = _coerce(raw, cast, ECON_DEFAULTS[key])
            if coerced is _COERCE_FAILED:
                logger.warning(
                    "Economic sheet %r could not be parsed as %s (got %r); "
                    "using default %r.",
                    key, cast.__name__, raw, ECON_DEFAULTS[key],
                )
                out[key] = ECON_DEFAULTS[key]
            else:
                out[key] = coerced
    return out


# ---------------------------------------------------------------------------
# Timeseries normalisation + dt auto-detection
# ---------------------------------------------------------------------------


def _normalise_timeseries(ts: pd.DataFrame, *, mode: str) -> pd.DataFrame:
    """Validate timeseries columns and forward-fill numeric NaNs.

    In ``vnb`` mode the ``load_kwh`` column is required.
    In ``merchant`` mode ``load_kwh`` is optional; if present the loader
    logs an INFO message and the column is preserved (the optimizer pins
    all load-coverage flows to zero).
    """
    if "timestamp" not in ts.columns:
        raise ValueError("timeseries sheet must contain a 'timestamp' column.")
    if "pv_kwh" not in ts.columns:
        raise ValueError("timeseries sheet must contain a 'pv_kwh' column.")

    if mode == "vnb" and "load_kwh" not in ts.columns:
        raise ValueError(
            "timeseries sheet must contain a 'load_kwh' column when mode='vnb'."
        )
    if mode == "merchant" and "load_kwh" in ts.columns:
        logger.info("merchant mode: load_kwh column ignored")

    for col in ("load_kwh", "pv_kwh", "dam_price_eur_per_mwh", "retail_price_eur_per_mwh"):
        if col in ts.columns:
            ts[col] = ts[col].astype(float).ffill().bfill()
    return ts


def detect_timestep_minutes(ts: pd.DataFrame) -> int:
    """Auto-detect the MILP timestep (in minutes) from the timeseries.

    Raises ``ValueError`` when timestamps are irregular.
    """
    idx = pd.to_datetime(ts["timestamp"]).sort_values()
    diffs = idx.diff().dropna()
    if diffs.empty:
        raise ValueError(
            "timeseries has fewer than 2 rows; cannot determine timestep."
        )
    if diffs.nunique() > 1:
        sample = diffs.value_counts().head().to_dict()
        raise ValueError(
            "Irregular timestep detected in 'timeseries' "
            f"(distinct step sizes: {sample}). Run "
            "`python scripts/resample_timeseries.py <workbook>` to harmonise "
            "the resolution before optimising."
        )
    delta = diffs.iloc[0]
    return int(delta.total_seconds() / 60)


# ---------------------------------------------------------------------------
# Public loader API
# ---------------------------------------------------------------------------


def read_workbook(xlsx_path: str | Path) -> dict[str, Any]:
    """Read the input workbook and return the typed nested dict."""
    xlsx_path = Path(xlsx_path)
    sheets = set(pd.ExcelFile(xlsx_path).sheet_names)

    required = {"project", "timeseries", "economic"}
    missing = required - sheets
    if missing:
        raise ValueError(
            f"Workbook {xlsx_path!s} is missing required sheets: {sorted(missing)}. "
            f"Found: {sorted(sheets)}."
        )

    project_flat = _flat_dict_from_sheet(
        pd.read_excel(xlsx_path, sheet_name="project"),
    )
    project = _parse_project_sheet(project_flat)
    econ_flat = _flat_dict_from_sheet(
        pd.read_excel(xlsx_path, sheet_name="economic"),
    )
    economic = _parse_economic_sheet(econ_flat)
    mode = str(project["regulatory"]["mode"]).lower()
    ts = _normalise_timeseries(
        pd.read_excel(xlsx_path, sheet_name="timeseries", parse_dates=["timestamp"]),
        mode=mode,
    )
    return {
        "ts": ts,
        "project": project,
        "economic": economic,
        "dt_minutes": detect_timestep_minutes(ts),
    }


def _typed_to_flat(
    typed: dict[str, Any],
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Translate the typed dict to the flat ``(params, ts)`` shape."""
    project = typed["project"]
    sys_ = project["system"]
    reg = project["regulatory"]
    opt = project["optimization"]
    econ = typed["economic"]
    ts = typed["ts"]

    params: dict[str, Any] = {
        "dt_minutes": int(typed["dt_minutes"]),
        "efficiency_charge": float(sys_["efficiency_charge"]),
        "efficiency_discharge": float(sys_["efficiency_discharge"]),
        "soc_min_frac": float(sys_["soc_min_frac"]),
        "soc_max_frac": float(sys_["soc_max_frac"]),
        "initial_soc_frac": float(sys_["initial_soc_frac"]),
        "terminal_soc_equal": bool(sys_["terminal_soc_equal"]),
        "p_charge_max_kw": float(sys_["p_charge_max_kw"]),
        "p_dis_max_kw": float(sys_["p_dis_max_kw"]),
        "battery_hours": float(sys_["battery_hours"]),
        "max_cycles_per_day": float(sys_["max_cycles_per_day"]),
        "p_grid_export_max_kw": float(sys_["p_grid_export_max_kw"]),
        "pv_nameplate_kwp": float(sys_["pv_nameplate_kwp"]),
        "bess_power_kw": float(sys_["bess_power_kw"]),
        "bess_capacity_kwh": float(sys_["bess_capacity_kwh"]),
        "curtailment_frac": _parse_curtailment(reg["curtailment_pct"]),
        "retail_tariff_eur_per_mwh": float(reg["retail_tariff_eur_per_mwh"]),
        "settlement_minutes": int(reg["settlement_minutes"]),
        "mode": str(reg["mode"]),
        "allow_bess_grid_charging": bool(reg["allow_bess_grid_charging"]),
        "weight_curtail_tiebreak": float(opt["weight_curtail_tiebreak"]),
        "weight_cycles_term": float(opt["weight_cycles_term"]),
        "solver_mip_gap": float(opt["solver_mip_gap"]),
        "solver_time_limit_seconds": int(opt["solver_time_limit_seconds"]),
        "show_titles": bool(econ.get("show_titles", False)),
    }
    return params, ts


def read_inputs(xlsx_path: str | Path) -> tuple[dict[str, Any], pd.DataFrame]:
    """Return ``(params, ts)`` — the flat shape used by the optimizer."""
    typed = read_workbook(xlsx_path)
    return _typed_to_flat(typed)


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


def _format_assumptions(econ: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, value in econ.items():
        rows.append({"key": key, "value": value, "unit": _ECON_UNITS.get(key, "")})
    return pd.DataFrame(rows, columns=["key", "value", "unit"])


_ECON_UNITS: dict[str, str] = {
    "project_lifecycle_years": "years",
    "project_start_year": "year",
    "discount_rate_pct": "%",
    "opex_inflation_pct": "%",
    "revenue_inflation_pct": "%",
    "capex_pv_eur_per_kw": "EUR/kWp",
    "capex_bess_eur_per_kw": "EUR/kW",
    "capex_licenses_eur_per_kw": "EUR/kW",
    "opex_pv_eur_per_kwp": "EUR/kWp/yr",
    "opex_bess_eur_per_kw": "EUR/kW/yr",
    "pv_degradation_year1_pct": "%",
    "pv_degradation_annual_pct": "%",
    "bess_degradation_annual_pct": "%",
    "bess_replacement_year": "year",
    "bess_replacement_cost_pct": "%",
    "sensitivity_enabled": "bool",
    "sensitivity_capex_delta_pct": "%",
    "sensitivity_opex_delta_pct": "%",
    "sensitivity_revenue_delta_pct": "%",
    "sensitivity_discount_rate_delta_pp": "pp",
    "show_titles": "bool",
    "currency_format": "enum",
    "plot_daily_year1": "bool",
    "plot_monthly_scope": "scope",
    "plot_yearly_scope": "scope",
}


def copy_input_snapshot(src_xlsx: Path, out_dir: Path, tag: str) -> Path | None:
    """Copy the input workbook into ``out_dir`` with a tag suffix."""
    src_xlsx = Path(src_xlsx)
    if not src_xlsx.exists():
        return None
    dst = out_dir / f"{src_xlsx.stem}_{tag}{src_xlsx.suffix}"
    dst.write_bytes(src_xlsx.read_bytes())
    return dst


# ---------------------------------------------------------------------------
# 00..05 numbered output layout
# ---------------------------------------------------------------------------

LAYOUT_SUBDIRS: tuple[str, ...] = (
    "00_summary",
    "01_inputs",
    "02_dispatch",
    "04_financial_plots",
    "05_energy_plots",
    "06_uncertainty_plots",
)


def make_run_layout(out_dir: Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name in LAYOUT_SUBDIRS:
        sub = out_dir / name
        sub.mkdir(parents=True, exist_ok=True)
        paths[name.split("_", 1)[1]] = sub
    paths["root"] = out_dir
    return paths


def write_assumptions_summary(
    out_path: Path,
    params: dict[str, Any],
    econ: dict[str, Any] | None,
) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("PV+BESS optimizer - assumptions snapshot")
    lines.append("=" * 60)
    lines.append("")
    lines.append("[params]")
    for key in sorted(params):
        if key.startswith("_"):
            continue
        lines.append(f"  {key} = {params[key]!r}")
    lines.append("")
    lines.append("[economic]")
    if econ:
        for key in sorted(econ):
            lines.append(f"  {key} = {econ[key]!r}")
    out_path.write_text("\n".join(lines) + "\n")
    return out_path


def write_dispatch_artifacts(
    dispatch_dir: Path,
    res_year1: pd.DataFrame,
    lifetime_df: pd.DataFrame | None,
    *,
    project_start_year: int = 2026,
) -> dict[str, Path]:
    """Write the ``02_dispatch/`` artefacts.

    The single file ``dispatch_hourly.xlsx`` carries one sheet per
    calendar year (sheet name = the calendar year as a 4-digit string).
    """
    dispatch_dir = Path(dispatch_dir)
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    out = dispatch_dir / "dispatch_hourly.xlsx"

    if lifetime_df is not None and not lifetime_df.empty:
        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            for cy in sorted(lifetime_df["calendar_year"].unique()):
                sheet = str(int(cy))
                lifetime_df.loc[lifetime_df["calendar_year"] == cy].to_excel(
                    writer, sheet_name=sheet, index=False,
                )
    else:
        if pd.api.types.is_datetime64_any_dtype(res_year1["timestamp"]):
            cal_year = int(
                pd.to_datetime(res_year1["timestamp"]).dt.year.iloc[0]
            )
        else:
            cal_year = int(project_start_year)
        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            res_year1.to_excel(writer, sheet_name=str(cal_year), index=False)

    return {"hourly_xlsx": out}


def write_results_workbook(
    out_path: Path,
    res_year1: pd.DataFrame,
    kpis_year1: dict[str, Any],
    kpis_monthly_year1: pd.DataFrame | None,
    *,
    yearly_cf: pd.DataFrame | None = None,
    monthly_cf: pd.DataFrame | None = None,
    quarterly_cf: pd.DataFrame | None = None,
    financial_kpis: dict[str, Any] | None = None,
    sensitivity: pd.DataFrame | None = None,
    lifetime_yearly: pd.DataFrame | None = None,
    economic_assumptions: dict[str, Any] | None = None,
    rolling_horizon_mc: pd.DataFrame | None = None,
) -> Path:
    """Write the consolidated ``03_results.xlsx`` workbook."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        pd.DataFrame(
            list(kpis_year1.items()), columns=["metric", "value"],
        ).to_excel(writer, sheet_name="kpis_year1", index=False)
        if kpis_monthly_year1 is not None and not kpis_monthly_year1.empty:
            kpis_monthly_year1.reset_index(names="month").to_excel(
                writer, sheet_name="kpis_monthly_year1", index=False,
            )
        res_year1.to_excel(writer, sheet_name="dispatch_year1", index=False)
        if yearly_cf is not None and not yearly_cf.empty:
            yearly_cf.to_excel(writer, sheet_name="cashflow_yearly", index=False)
        if quarterly_cf is not None and not quarterly_cf.empty:
            quarterly_cf.to_excel(
                writer, sheet_name="cashflow_quarterly", index=False,
            )
        if monthly_cf is not None and not monthly_cf.empty:
            monthly_cf.to_excel(writer, sheet_name="cashflow_monthly", index=False)
        if financial_kpis:
            pd.DataFrame(
                list(financial_kpis.items()), columns=["metric", "value"],
            ).to_excel(writer, sheet_name="financial_kpis", index=False)
        if sensitivity is not None and not sensitivity.empty:
            sensitivity.to_excel(
                writer, sheet_name="sensitivity_analysis", index=False,
            )
        if lifetime_yearly is not None and not lifetime_yearly.empty:
            lifetime_yearly.to_excel(
                writer, sheet_name="lifetime_dispatch_yearly", index=False,
            )
        if rolling_horizon_mc is not None and not rolling_horizon_mc.empty:
            rolling_horizon_mc.to_excel(
                writer, sheet_name="rolling_horizon_mc", index=False,
            )
        if economic_assumptions:
            _format_assumptions(economic_assumptions).to_excel(
                writer, sheet_name="economic_assumptions", index=False,
            )
    return out_path
