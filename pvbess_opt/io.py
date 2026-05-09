"""Excel input parsing and output writing for the PV+BESS optimizer.

The v0.8 schema is **seven sheets**, one logical theme per sheet:

* ``timeseries`` — per-step data with lowercase snake_case column names:
  ``timestamp``, ``load_kwh``, ``pv_kwh``, ``dam_price_eur_per_mwh``,
  optional ``retail_price_eur_per_mwh``.
* ``project`` — high-level run config (lifecycle horizon, mode,
  settlement, retail tariff, grid export limit, currency / title flags).
* ``pv`` — PV nameplate, specific production, degradation, CAPEX /
  DEVEX / OPEX.
* ``bess`` — BESS power and capacity, efficiency, SOC bounds, cycles,
  CAPEX / DEVEX / OPEX, replacement and degradation.
* ``economics`` — discount rate, inflation indices, aggregator fee,
  sensitivity deltas.
* ``simulation`` — uncertainty (rolling-horizon Monte Carlo) and plot
  scope flags.
* ``curtailment_profile`` — hour-of-day curtailment cap profile (24
  rows), optionally with one column per calendar month.  Missing →
  fall back to a constant 27 % (legacy v0.7 behaviour) and log INFO.

Public loader API
-----------------

* :func:`read_workbook` returns the typed nested dict:

  .. code-block:: python

     {
         "ts": pd.DataFrame,               # lowercase snake_case
         "project":            {...},
         "pv":                 {...},
         "bess":               {...},
         "economics":          {...},
         "simulation":         {...},
         "curtailment_profile": np.ndarray,  # shape (24,) or (24, 12)
         "dt_minutes": int,                # auto-detected from the timeseries
     }

* :func:`read_inputs` returns a flat ``(params, ts)`` tuple suitable for
  the optimizer / KPI / lifetime modules.

Mode-specific timeseries semantics
----------------------------------

* In ``vnb`` mode the ``load_kwh`` column is required; missing → ValueError.
* In ``merchant`` mode ``load_kwh`` is optional — if present, the loader
  logs an INFO message and the optimizer pins all load-coverage flows to 0.

Legacy v0.7 / v0.5 keys
-----------------------

A v0.7-style workbook (single ``project`` + ``economic`` sheets with
the v0.6 ``# system_sizing`` / ``# bess_operation`` group structure)
still loads — the loader logs a single WARNING listing the affected
file plus the migration command (``python scripts/build_input_xlsx.py``)
and reads what it can.  No silent translation.

The v0.5 ``# optimization`` group keys
(``weight_curtail_tiebreak``, ``weight_cycles_term``,
``solver_mip_gap``, ``solver_time_limit_seconds``) and the v0.5
``plot_daily_year1`` flag are still rejected with a WARNING.

Several v0.7 keys were dropped or renamed in v0.8:

* ``capex_licenses_eur_per_kw`` — replaced by per-asset DEVEX
  (``devex_pv_eur_per_kw`` / ``devex_bess_eur_per_kw``).
* ``battery_hours``, ``p_charge_max_kw``, ``p_dis_max_kw`` — replaced
  by the symmetric ``bess_power_kw`` and ``bess_capacity_kwh`` pair
  (industry standard; see Phase 2 of the v0.8 changelog).

Encountering any of these in a workbook triggers a friendly WARNING and
the value is ignored.
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

PROJECT_SHEET_DEFAULTS: dict[str, Any] = {
    "project_lifecycle_years": 25,
    "project_start_year": 2026,
    "mode": "vnb",
    "settlement_minutes": 15,
    "p_grid_export_max_kw": 5000.0,
    "retail_tariff_eur_per_mwh": 132.0,
    "allow_bess_grid_charging": False,
    "unavailability_pct": 1.0,
    "currency_format": "auto",
    "show_titles": False,
}

PV_SHEET_DEFAULTS: dict[str, Any] = {
    "pv_nameplate_kwp": 0.0,
    "specific_production_kwh_per_kwp": 1500.0,
    "pv_degradation_year1_pct": 2.5,
    "pv_degradation_annual_pct": 0.55,
    "capex_pv_eur_per_kw": 525.0,
    "devex_pv_eur_per_kw": 60.0,
    "opex_pv_eur_per_kwp": 7.0,
}

BESS_SHEET_DEFAULTS: dict[str, Any] = {
    "bess_power_kw": 0.0,
    "bess_capacity_kwh": 0.0,
    "efficiency_charge": 0.97,
    "efficiency_discharge": 0.97,
    "soc_min_frac": 0.20,
    "soc_max_frac": 0.95,
    "initial_soc_frac": 0.50,
    "terminal_soc_equal": True,
    "max_cycles_per_day": 1.0,
    "capex_bess_eur_per_kw": 200.0,
    "devex_bess_eur_per_kw": 30.0,
    "opex_bess_eur_per_kw": 14.0,
    "bess_replacement_year": 0,
    "bess_replacement_cost_pct": 50.0,
    "bess_degradation_annual_pct": 2.0,
}

ECONOMICS_SHEET_DEFAULTS: dict[str, Any] = {
    "discount_rate_pct": 7.0,
    "opex_inflation_pct": 1.0,
    "revenue_inflation_pct": 2.0,
    "aggregator_fee_pct_revenue": 10.0,
    "sensitivity_enabled": True,
    "sensitivity_capex_delta_pct": 10.0,
    "sensitivity_opex_delta_pct": 10.0,
    "sensitivity_revenue_delta_pct": 10.0,
    "sensitivity_discount_rate_delta_pp": 2.0,
}

SIMULATION_SHEET_DEFAULTS: dict[str, Any] = {
    "uncertainty_enabled": False,
    "uncertainty_compare_sources": False,
    "uncertainty_n_seeds": 30,
    "uncertainty_window_hours": 48,
    "uncertainty_commit_hours": 24,
    "uncertainty_dam_enabled": True,
    "uncertainty_pv_enabled": True,
    "uncertainty_load_enabled": True,
    "uncertainty_sigma_dam": 0.20,
    "uncertainty_sigma_pv": 0.12,
    "uncertainty_sigma_load": 0.05,
    "plot_daily_scope": "year1_only",
    "plot_monthly_scope": "all",
    "plot_yearly_scope": "all",
}

# Sheet → defaults map.  Used by the loader to validate keys per sheet.
_SHEET_DEFAULTS: dict[str, dict[str, Any]] = {
    "project": PROJECT_SHEET_DEFAULTS,
    "pv": PV_SHEET_DEFAULTS,
    "bess": BESS_SHEET_DEFAULTS,
    "economics": ECONOMICS_SHEET_DEFAULTS,
    "simulation": SIMULATION_SHEET_DEFAULTS,
}

_KEY_TO_SHEET: dict[str, str] = {}
for _sheet_name, _sheet_defaults in _SHEET_DEFAULTS.items():
    for _key in _sheet_defaults:
        _KEY_TO_SHEET[_key] = _sheet_name


# Legacy v0.5 ``# optimization`` group keys.
_LEGACY_OPTIMIZATION_KEYS: frozenset[str] = frozenset({
    "weight_curtail_tiebreak",
    "weight_cycles_term",
    "solver_mip_gap",
    "solver_time_limit_seconds",
})

# Keys removed in v0.8.  Each maps to a one-line user-facing hint.
_LEGACY_V08_REMOVED: dict[str, str] = {
    "capex_licenses_eur_per_kw": (
        "v0.8 dropped this — use devex_pv_eur_per_kw / "
        "devex_bess_eur_per_kw instead"
    ),
    "battery_hours": (
        "v0.8 dropped this — set bess_power_kw / bess_capacity_kwh "
        "instead (capacity is pinned to the workbook value)"
    ),
    "p_charge_max_kw": (
        "v0.8 dropped this — set bess_power_kw instead "
        "(symmetric charge / discharge limit)"
    ),
    "p_dis_max_kw": (
        "v0.8 dropped this — set bess_power_kw instead "
        "(symmetric charge / discharge limit)"
    ),
}


# ---------------------------------------------------------------------------
# Per-key parsing metadata
# ---------------------------------------------------------------------------

_BOOL_KEYS: frozenset[str] = frozenset({
    "show_titles",
    "allow_bess_grid_charging",
    "terminal_soc_equal",
    "sensitivity_enabled",
    "uncertainty_enabled",
    "uncertainty_compare_sources",
    "uncertainty_dam_enabled",
    "uncertainty_pv_enabled",
    "uncertainty_load_enabled",
})
_INT_KEYS: frozenset[str] = frozenset({
    "project_lifecycle_years",
    "project_start_year",
    "settlement_minutes",
    "bess_replacement_year",
    "uncertainty_n_seeds",
    "uncertainty_window_hours",
    "uncertainty_commit_hours",
})
_STR_KEYS: frozenset[str] = frozenset({
    "mode",
    "currency_format",
    "plot_daily_scope",
    "plot_monthly_scope",
    "plot_yearly_scope",
})
_ALLOWED_VALUES: dict[str, frozenset[str]] = {
    "mode": frozenset({"vnb", "merchant"}),
    "currency_format": frozenset({"auto", "millions", "raw"}),
    "plot_daily_scope": frozenset({"none", "year1_only", "all"}),
    "plot_monthly_scope": frozenset({"none", "year1_only", "all"}),
    "plot_yearly_scope": frozenset({"none", "year1_only", "all"}),
}


# ---------------------------------------------------------------------------
# Sheet row templates (shared with build_input_xlsx.py)
# ---------------------------------------------------------------------------

_PROJECT_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("project_lifecycle_years", 25, "years",
     "Total project horizon used to project Years 0..N."),
    ("project_start_year", 2026, "year",
     "Calendar year of Year 1 (first operating year). CAPEX is paid in "
     "Year 0 (calendar = project_start_year - 1)."),
    ("mode", "vnb", "enum",
     "vnb | merchant. vnb requires a co-located load and enforces load "
     "priority + no simultaneous grid I/O. merchant has no load; PV/BESS "
     "dispatch entirely to DAM."),
    ("settlement_minutes", 15, "int",
     "Greek VNB settles every 15 min per MD YPEN/DAPEEK/93976/2772/2024. "
     "Currently informational; the MILP timestep is auto-detected."),
    ("p_grid_export_max_kw", 5000, "kW",
     "Grid-connection export limit (kW)."),
    ("retail_tariff_eur_per_mwh", 132, "EUR/MWh",
     "Retail tariff used in vnb mode for load coverage."),
    ("allow_bess_grid_charging", False, "bool",
     "If TRUE the BESS may charge from the grid in periods with pv_kwh ~ 0."),
    ("unavailability_pct", 1.0, "%",
     "Annual unavailability (outages / scheduled maintenance) applied as "
     "a post-solve derate on PV generation, BESS discharge, and revenue."),
    ("currency_format", "auto", "enum",
     "auto | millions | raw — financial-axis label format."),
    ("show_titles", False, "bool",
     "Render plot titles. IEEE figures normally rely on the figure caption."),
)

_PV_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("pv_nameplate_kwp", 0, "kWp",
     "PV nameplate capacity. 0 = no PV in this project."),
    ("specific_production_kwh_per_kwp", 1500, "kWh/kWp/yr",
     "Annual specific production of the PV array. Used for documentation "
     "and as a sanity check; the MILP consumes the timeseries directly."),
    ("pv_degradation_year1_pct", 2.5, "%",
     "Initial light-induced degradation (LID) applied at start of Year 2."),
    ("pv_degradation_annual_pct", 0.55, "%",
     "Linear PV degradation after Year 1 (Tier-1 warranty)."),
    ("capex_pv_eur_per_kw", 525, "EUR/kWp",
     "Per-kWp PV CAPEX. Set 0 if PV already exists."),
    ("devex_pv_eur_per_kw", 60, "EUR/kWp",
     "Per-kWp PV DEVEX (development / permitting). Paid in Year 0."),
    ("opex_pv_eur_per_kwp", 7, "EUR/kWp/yr",
     "Annual O&M for PV."),
)

_BESS_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("bess_power_kw", 0, "kW",
     "BESS power rating (symmetric charge / discharge limit). "
     "0 = no BESS in this project."),
    ("bess_capacity_kwh", 0, "kWh",
     "BESS energy capacity. Pinned to the workbook value (industry "
     "standard for sizing-as-input projects)."),
    ("efficiency_charge", 0.97, "-",
     "Charge efficiency (0..1). Round-trip = "
     "efficiency_charge * efficiency_discharge."),
    ("efficiency_discharge", 0.97, "-",
     "Discharge efficiency (0..1)."),
    ("soc_min_frac", 0.20, "-",
     "Minimum SOC as fraction of nominal capacity (0.20 = 20 %)."),
    ("soc_max_frac", 0.95, "-",
     "Maximum SOC as fraction of nominal capacity (0.95 = 95 %)."),
    ("initial_soc_frac", 0.50, "-",
     "SOC at the first timestep, as a fraction of capacity."),
    ("terminal_soc_equal", True, "bool",
     "If TRUE, force final SOC == initial SOC (closed cycle)."),
    ("max_cycles_per_day", 1.0, "-",
     "Daily equivalent-cycle cap (sum of discharge / capacity)."),
    ("capex_bess_eur_per_kw", 200, "EUR/kW",
     "Per-kW BESS CAPEX (DC + PCS). Set 0 if BESS already exists."),
    ("devex_bess_eur_per_kw", 30, "EUR/kW",
     "Per-kW BESS DEVEX (development / permitting). Paid in Year 0."),
    ("opex_bess_eur_per_kw", 14, "EUR/kW/yr",
     "Annual O&M for BESS."),
    ("bess_replacement_year", 0, "year",
     "Year of BESS cell replacement (0 = no replacement). Typical 10 or 15."),
    ("bess_replacement_cost_pct", 50, "%",
     "Replacement cost as percent of original BESS CAPEX."),
    ("bess_degradation_annual_pct", 2.0, "%",
     "Linear BESS capacity fade. Approximate Tier-1 LFP cell warranty."),
)

_ECONOMICS_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("discount_rate_pct", 7.0, "%",
     "WACC. Typical EU RES band 6-8 %."),
    ("opex_inflation_pct", 1.0, "%",
     "Annual OPEX escalation rate."),
    ("revenue_inflation_pct", 2.0, "%",
     "Annual revenue escalation rate (ECB target). Set 0 to disable."),
    ("aggregator_fee_pct_revenue", 10.0, "%",
     "Aggregator fee on gross revenue (Gridcog convention; see public "
     "Gridcog cost / pricing docs)."),
    ("sensitivity_enabled", True, "bool",
     "Run a one-at-a-time tornado sensitivity after the base run."),
    ("sensitivity_capex_delta_pct", 10, "%",
     "Symmetric +/- delta on total CAPEX (incl. DEVEX)."),
    ("sensitivity_opex_delta_pct", 10, "%",
     "Symmetric +/- delta on total annual OPEX."),
    ("sensitivity_revenue_delta_pct", 10, "%",
     "Symmetric +/- delta on Year-1 revenue base."),
    ("sensitivity_discount_rate_delta_pp", 2.0, "pp",
     "Symmetric +/- delta on the discount rate, in percentage points. "
     "NPV tornado only - drops out of IRR tornado by definition."),
)

_SIMULATION_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("uncertainty_enabled", False, "bool",
     "Run rolling-horizon Monte Carlo. Default FALSE (perfect-foresight only)."),
    ("uncertainty_compare_sources", False, "bool",
     "When TRUE run 4 ensembles (DAM-only, PV-only, Load-only, "
     "All-combined) and emit a comparison plot."),
    ("uncertainty_n_seeds", 30, "int",
     "Monte Carlo seeds per ensemble."),
    ("uncertainty_window_hours", 48, "int",
     "Rolling window length."),
    ("uncertainty_commit_hours", 24, "int",
     "Commit slice."),
    ("uncertainty_dam_enabled", True, "bool",
     "Apply DAM noise."),
    ("uncertainty_pv_enabled", True, "bool",
     "Apply PV noise."),
    ("uncertainty_load_enabled", True, "bool",
     "Apply Load noise (ignored in merchant mode)."),
    ("uncertainty_sigma_dam", 0.20, "-",
     "Log-normal sigma for DAM. Default 0.20 (ENTSO-E D+1 benchmark)."),
    ("uncertainty_sigma_pv", 0.12, "-",
     "Log-normal sigma for PV. Default 0.12 (NREL day-ahead PV study)."),
    ("uncertainty_sigma_load", 0.05, "-",
     "Log-normal sigma for Load. Default 0.05 (predictable customer benchmark)."),
    ("plot_daily_scope", "year1_only", "scope",
     "none | year1_only | all. 'all' produces ~365 * N_years * 3 daily PDFs."),
    ("plot_monthly_scope", "all", "scope",
     "none | year1_only | all."),
    ("plot_yearly_scope", "all", "scope",
     "none | year1_only | all."),
)

_SHEET_ROW_TEMPLATES: dict[
    str, tuple[tuple[str, object, str, str], ...]
] = {
    "project": _PROJECT_ROWS,
    "pv": _PV_ROWS,
    "bess": _BESS_ROWS,
    "economics": _ECONOMICS_ROWS,
    "simulation": _SIMULATION_ROWS,
}

# Default constant 27 % curtailment cap (24 hourly rows) — reproduces
# v0.7 scalar behaviour exactly.
_DEFAULT_CURTAILMENT_PCT_HOURLY: float = 27.0
_LEGACY_DEFAULT_CURTAILMENT_PCT: float = 27.0


# ---------------------------------------------------------------------------
# Sheet builders
# ---------------------------------------------------------------------------


def _build_kv_sheet(
    typed_section: dict[str, Any],
    rows: tuple[tuple[str, object, str, str], ...],
) -> pd.DataFrame:
    out: list[dict[str, Any]] = []
    for key, default, unit, notes in rows:
        value = typed_section.get(key, default)
        out.append(
            {"key": key, "value": value, "unit": unit, "notes": notes},
        )
    return pd.DataFrame(out, columns=["key", "value", "unit", "notes"])


def _hour_interval_labels() -> list[str]:
    """24 strings of the form ``HH:00-HH:00`` covering 00:00 → 24:00."""
    return [f"{h:02d}:00-{(h + 1):02d}:00" for h in range(24)]


def _build_curtailment_sheet(profile: Any) -> pd.DataFrame:
    """Render the ``curtailment_profile`` sheet from a 1-D or 2-D array.

    Accepts:
    * shape ``(24,)`` → single ``curtailment_pct`` column.
    * shape ``(24, 12)`` → per-month columns (``curtailment_pct_jan`` ..
      ``curtailment_pct_dec``).

    The ``hour_of_day`` column is rendered as **24-hour interval
    strings** (``"00:00-01:00"`` … ``"23:00-24:00"``) for human
    readability.  The loader (:func:`_parse_curtailment_profile_sheet`)
    accepts both this string format and the legacy integer format.
    """
    arr = np.asarray(profile, dtype=float)
    hour_labels = _hour_interval_labels()
    if arr.ndim == 1:
        if arr.shape[0] != 24:
            raise ValueError(
                "curtailment_profile must have 24 rows "
                f"(got {arr.shape[0]})."
            )
        return pd.DataFrame({
            "hour_of_day": hour_labels,
            "curtailment_pct": arr,
        })
    if arr.ndim == 2:
        if arr.shape != (24, 12):
            raise ValueError(
                "curtailment_profile (2-D) must be shape (24, 12) "
                f"(got {arr.shape})."
            )
        cols: dict[str, Any] = {"hour_of_day": hour_labels}
        for m_idx, m_name in enumerate(_MONTH_TOKENS):
            cols[f"curtailment_pct_{m_name}"] = arr[:, m_idx]
        return pd.DataFrame(cols)
    raise ValueError(
        "curtailment_profile must be 1-D (24,) or 2-D (24, 12); "
        f"got shape {arr.shape}."
    )


_MONTH_TOKENS: tuple[str, ...] = (
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
)


def write_workbook(typed: dict[str, Any], dst: str | Path) -> Path:
    """Write a workbook from a typed nested dict (v0.8 seven-sheet schema)."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    project_df = _build_kv_sheet(typed["project"], _PROJECT_ROWS)
    pv_df = _build_kv_sheet(typed["pv"], _PV_ROWS)
    bess_df = _build_kv_sheet(typed["bess"], _BESS_ROWS)
    economics_df = _build_kv_sheet(typed["economics"], _ECONOMICS_ROWS)
    simulation_df = _build_kv_sheet(typed["simulation"], _SIMULATION_ROWS)

    profile = typed.get("curtailment_profile")
    if profile is None:
        profile = np.full(24, _DEFAULT_CURTAILMENT_PCT_HOURLY, dtype=float)
    curtailment_df = _build_curtailment_sheet(profile)

    with pd.ExcelWriter(dst, engine="openpyxl") as writer:
        typed["ts"].to_excel(writer, sheet_name="timeseries", index=False)
        project_df.to_excel(writer, sheet_name="project", index=False)
        pv_df.to_excel(writer, sheet_name="pv", index=False)
        bess_df.to_excel(writer, sheet_name="bess", index=False)
        economics_df.to_excel(writer, sheet_name="economics", index=False)
        simulation_df.to_excel(writer, sheet_name="simulation", index=False)
        curtailment_df.to_excel(
            writer, sheet_name="curtailment_profile", index=False,
        )
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
# Per-sheet typed parser
# ---------------------------------------------------------------------------


def _parse_value(key: str, raw: Any, default: Any) -> Any:
    if key in _BOOL_KEYS:
        return _parse_bool(raw, bool(default))
    if key in _STR_KEYS:
        return _parse_string_enum(
            raw, str(default), _ALLOWED_VALUES.get(key, frozenset()), key,
        )
    if key in _INT_KEYS:
        coerced = _coerce(raw, int, default)
        if coerced is _COERCE_FAILED:
            logger.warning(
                "Workbook value for %r could not be parsed as int "
                "(got %r); using default %r.", key, raw, default,
            )
            return default
        return coerced
    coerced = _coerce(raw, float, default)
    if coerced is _COERCE_FAILED:
        logger.warning(
            "Workbook value for %r could not be parsed as float "
            "(got %r); using default %r.", key, raw, default,
        )
        return default
    return coerced


def _parse_kv_sheet(
    sheet_name: str, flat: dict[str, Any],
) -> dict[str, Any]:
    defaults = _SHEET_DEFAULTS[sheet_name]
    out = dict(defaults)
    for key, raw in flat.items():
        if key in defaults:
            out[key] = _parse_value(key, raw, defaults[key])
            continue
        if key in _LEGACY_V08_REMOVED:
            logger.warning(
                "%s sheet key %r was dropped in v0.8: %s. Value %r ignored.",
                sheet_name, key, _LEGACY_V08_REMOVED[key], raw,
            )
            continue
        if key in _LEGACY_OPTIMIZATION_KEYS:
            logger.warning(
                "%s sheet contains legacy v0.5 '# optimization' key %r; "
                "no longer accepted (solver gap / time limit are CLI-only "
                "via --mip-gap / --time-limit; tie-breaker weights live as "
                "private constants in pvbess_opt.optimization). Value %r ignored.",
                sheet_name, key, raw,
            )
            continue
        if key == "plot_daily_year1":
            logger.warning(
                "%s sheet key 'plot_daily_year1' was renamed to "
                "'plot_daily_scope' in v0.6; use 'none' / 'year1_only' / "
                "'all' explicitly. Value %r ignored.", sheet_name, raw,
            )
            continue
        # Unknown key for this sheet — but maybe it belongs to another v0.8 sheet?
        if key in _KEY_TO_SHEET:
            logger.warning(
                "Key %r found on %r sheet but belongs to %r sheet; ignored.",
                key, sheet_name, _KEY_TO_SHEET[key],
            )
            continue
        logger.warning(
            "%s sheet key %r is unknown; ignored.", sheet_name, key,
        )
    return out


# ---------------------------------------------------------------------------
# Curtailment-profile parser
# ---------------------------------------------------------------------------


import re as _re  # noqa: E402

_HOUR_PARSE_RE = _re.compile(r"^\s*(\d{1,2})")


def _parse_hour_of_day(value: Any) -> int:
    """Coerce an ``hour_of_day`` cell into an integer 0..23.

    Accepts the legacy integer format and the v0.8 24-hour interval
    string format (``"00:00-01:00"`` … ``"23:00-24:00"``).  The
    parser is forgiving: any leading 1-2 digit run is taken as the
    start hour.  Out-of-range values raise ``ValueError``.
    """
    if isinstance(value, (int, np.integer)):
        h = int(value)
    elif isinstance(value, (float, np.floating)):
        if np.isnan(value):
            raise ValueError("hour_of_day cell is NaN")
        h = int(value)
    else:
        s = str(value).strip()
        m = _HOUR_PARSE_RE.match(s)
        if not m:
            raise ValueError(
                f"cannot parse hour_of_day value {value!r}; "
                "expected an integer 0..23 or an interval like '00:00-01:00'"
            )
        h = int(m.group(1))
    if h < 0 or h > 23:
        raise ValueError(
            f"hour_of_day must be in 0..23 (got {h} from {value!r})"
        )
    return h


def _parse_curtailment_profile_sheet(df: pd.DataFrame) -> np.ndarray:
    """Parse a curtailment_profile sheet into a (24,) or (24, 12) array."""
    if df is None or df.empty:
        raise ValueError("curtailment_profile sheet is empty.")

    cols = {c.strip().lower() for c in df.columns}
    if "hour_of_day" not in cols:
        raise ValueError(
            "curtailment_profile sheet must contain a 'hour_of_day' column."
        )

    df_norm = df.rename(columns={c: c.strip().lower() for c in df.columns})
    df_norm["hour_of_day"] = df_norm["hour_of_day"].map(_parse_hour_of_day)
    df_norm = df_norm.sort_values("hour_of_day").reset_index(drop=True)
    if len(df_norm) != 24:
        raise ValueError(
            "curtailment_profile sheet must have exactly 24 rows "
            f"(got {len(df_norm)})."
        )
    hours = df_norm["hour_of_day"].astype(int).to_numpy()
    if not np.array_equal(hours, np.arange(24)):
        raise ValueError(
            "curtailment_profile 'hour_of_day' column must cover 0..23 "
            f"exactly once; got {hours.tolist()}."
        )

    monthly_cols = [f"curtailment_pct_{m}" for m in _MONTH_TOKENS]
    has_monthly = all(col in df_norm.columns for col in monthly_cols)
    if has_monthly:
        arr = np.zeros((24, 12), dtype=float)
        for m_idx, m_name in enumerate(_MONTH_TOKENS):
            arr[:, m_idx] = (
                df_norm[f"curtailment_pct_{m_name}"].astype(float).to_numpy()
            )
        return arr
    if "curtailment_pct" in df_norm.columns:
        return df_norm["curtailment_pct"].astype(float).to_numpy()
    raise ValueError(
        "curtailment_profile sheet must contain either a "
        "'curtailment_pct' column (24x1) or all 12 "
        "'curtailment_pct_<month>' columns (24x12)."
    )


# ---------------------------------------------------------------------------
# Timeseries normalisation + dt auto-detection
# ---------------------------------------------------------------------------


def _normalise_timeseries(ts: pd.DataFrame, *, mode: str) -> pd.DataFrame:
    """Validate timeseries columns and forward-fill numeric NaNs."""
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
    """Auto-detect the MILP timestep (in minutes) from the timeseries."""
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


_V08_REQUIRED_SHEETS: frozenset[str] = frozenset({
    "timeseries", "project", "pv", "bess", "economics", "simulation",
})
_V07_LEGACY_SHEETS: frozenset[str] = frozenset({
    "timeseries", "project", "economic",
})


# Tolerance for "the workbook PV total already matches the user's
# pv_nameplate_kwp × specific_production_kwh_per_kwp target" — below
# this relative threshold the loader does **not** rescale.
_PV_RESCALE_REL_TOLERANCE: float = 1.0e-12


def _rescale_pv_to_user_target(
    ts: pd.DataFrame,
    *,
    pv_nameplate_kwp: float,
    specific_production_kwh_per_kwp: float,
) -> pd.DataFrame:
    """Rescale ``ts['pv_kwh']`` to match the user's
    ``pv_nameplate_kwp × specific_production_kwh_per_kwp`` target.

    The shape is preserved exactly (multiplicative scaling).  Returns
    a new DataFrame.  Skipped (pass-through) when:

    * either knob is zero or negative (PV is "absent" or unspecified);
    * the workbook PV column sums to zero;
    * the current annual total already matches the target within
      ``1e-12`` relative.
    """
    if "pv_kwh" not in ts.columns:
        return ts
    if pv_nameplate_kwp <= 0.0 or specific_production_kwh_per_kwp <= 0.0:
        return ts

    current_total = float(ts["pv_kwh"].astype(float).sum())
    if current_total <= 0.0:
        return ts

    target_total = (
        float(pv_nameplate_kwp) * float(specific_production_kwh_per_kwp)
    )
    rel_diff = abs(current_total - target_total) / max(target_total, 1.0e-9)
    if rel_diff <= _PV_RESCALE_REL_TOLERANCE:
        return ts

    factor = target_total / current_total
    out = ts.copy()
    out["pv_kwh"] = out["pv_kwh"].astype(float) * factor
    logger.info(
        "PV column rescaled: workbook annual %.1f kWh → user target %.1f "
        "kWh (factor %.6f) from pv_nameplate_kwp=%.1f kWp × "
        "specific_production=%.4f kWh/kWp.",
        current_total, target_total, factor,
        pv_nameplate_kwp, specific_production_kwh_per_kwp,
    )
    return out


def _read_v07_legacy_workbook(xlsx_path: Path) -> dict[str, Any]:
    """Best-effort read of a v0.7 workbook (project + economic).

    The loader logs a single WARNING listing the affected file plus the
    migration command, then translates the v0.7 grouped flat dict into
    v0.8 typed sections.  No silent translation: keys it cannot place
    are surfaced via the normal warning channels.
    """
    logger.warning(
        "%s uses the legacy v0.7 two-sheet layout (project + economic). "
        "v0.8 expects seven sheets — please regenerate via "
        "`python scripts/build_input_xlsx.py`. Proceeding with best-effort "
        "translation; values that don't map will be ignored with a per-key "
        "warning.",
        xlsx_path,
    )

    project_flat = _flat_dict_from_sheet(
        pd.read_excel(xlsx_path, sheet_name="project"),
    )
    econ_flat = _flat_dict_from_sheet(
        pd.read_excel(xlsx_path, sheet_name="economic"),
    )

    routed: dict[str, dict[str, Any]] = {
        "project": {}, "pv": {}, "bess": {}, "economics": {}, "simulation": {},
    }
    unplaced: list[tuple[str, Any]] = []
    for src in (project_flat, econ_flat):
        for key, value in src.items():
            sheet = _KEY_TO_SHEET.get(key)
            if sheet is not None:
                routed[sheet][key] = value
            else:
                unplaced.append((key, value))

    typed: dict[str, Any] = {}
    for sheet_name in ("project", "pv", "bess", "economics", "simulation"):
        typed[sheet_name] = _parse_kv_sheet(sheet_name, routed[sheet_name])

    # Run unplaced keys through the normal per-sheet path so the
    # legacy / removed warnings are emitted exactly once.
    for key, value in unplaced:
        # Pretend they came from the most likely sheet (project) just
        # for the warning message.
        _parse_kv_sheet("project", {key: value})

    profile = np.full(24, _LEGACY_DEFAULT_CURTAILMENT_PCT, dtype=float)
    legacy_curtailment_pct = econ_flat.get("curtailment_pct")
    if legacy_curtailment_pct is None:
        legacy_curtailment_pct = project_flat.get("curtailment_pct")
    if legacy_curtailment_pct is not None:
        try:
            profile = np.full(24, float(legacy_curtailment_pct), dtype=float)
        except (TypeError, ValueError):
            pass

    mode = str(typed["project"]["mode"]).lower()
    ts = _normalise_timeseries(
        pd.read_excel(xlsx_path, sheet_name="timeseries", parse_dates=["timestamp"]),
        mode=mode,
    )
    ts = _rescale_pv_to_user_target(
        ts,
        pv_nameplate_kwp=float(typed["pv"].get("pv_nameplate_kwp", 0.0) or 0.0),
        specific_production_kwh_per_kwp=float(
            typed["pv"].get("specific_production_kwh_per_kwp", 0.0) or 0.0,
        ),
    )
    out: dict[str, Any] = {
        "ts": ts,
        "curtailment_profile": profile,
        "dt_minutes": detect_timestep_minutes(ts),
    }
    out.update(typed)
    return out


def read_workbook(xlsx_path: str | Path) -> dict[str, Any]:
    """Read the input workbook and return the typed nested dict."""
    xlsx_path = Path(xlsx_path)
    sheets = set(pd.ExcelFile(xlsx_path).sheet_names)

    missing = _V08_REQUIRED_SHEETS - sheets
    if missing and _V07_LEGACY_SHEETS.issubset(sheets):
        return _read_v07_legacy_workbook(xlsx_path)
    if missing:
        raise ValueError(
            f"Workbook {xlsx_path!s} is missing required sheets: "
            f"{sorted(missing)}. Found: {sorted(sheets)}."
        )

    typed: dict[str, Any] = {}
    for sheet_name in ("project", "pv", "bess", "economics", "simulation"):
        flat = _flat_dict_from_sheet(
            pd.read_excel(xlsx_path, sheet_name=sheet_name),
        )
        typed[sheet_name] = _parse_kv_sheet(sheet_name, flat)

    if "curtailment_profile" in sheets:
        try:
            profile = _parse_curtailment_profile_sheet(
                pd.read_excel(xlsx_path, sheet_name="curtailment_profile"),
            )
        except ValueError as exc:
            raise ValueError(f"curtailment_profile: {exc}") from exc
    else:
        logger.info(
            "curtailment_profile sheet not found in %s; falling back to "
            "constant %.1f %% for every hour (legacy v0.7 default).",
            xlsx_path, _LEGACY_DEFAULT_CURTAILMENT_PCT,
        )
        profile = np.full(24, _LEGACY_DEFAULT_CURTAILMENT_PCT, dtype=float)

    mode = str(typed["project"]["mode"]).lower()
    ts = _normalise_timeseries(
        pd.read_excel(xlsx_path, sheet_name="timeseries", parse_dates=["timestamp"]),
        mode=mode,
    )
    ts = _rescale_pv_to_user_target(
        ts,
        pv_nameplate_kwp=float(typed["pv"].get("pv_nameplate_kwp", 0.0) or 0.0),
        specific_production_kwh_per_kwp=float(
            typed["pv"].get("specific_production_kwh_per_kwp", 0.0) or 0.0,
        ),
    )
    out: dict[str, Any] = {
        "ts": ts,
        "curtailment_profile": profile,
        "dt_minutes": detect_timestep_minutes(ts),
    }
    out.update(typed)
    return out


def _typed_to_flat(
    typed: dict[str, Any],
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Translate the typed dict to the flat ``(params, ts)`` shape."""
    project = typed["project"]
    pv = typed["pv"]
    bess = typed["bess"]
    sim = typed["simulation"]
    ts = typed["ts"]

    bess_power_kw = float(bess["bess_power_kw"])
    bess_capacity_kwh = float(bess["bess_capacity_kwh"])

    profile = typed.get("curtailment_profile")
    if profile is not None:
        curtailment_frac = float(np.mean(np.asarray(profile, dtype=float))) / 100.0
    else:
        curtailment_frac = _LEGACY_DEFAULT_CURTAILMENT_PCT / 100.0
    curtailment_frac = float(np.clip(curtailment_frac, 0.0, 1.0))

    params: dict[str, Any] = {
        "dt_minutes": int(typed["dt_minutes"]),
        # bess
        "efficiency_charge": float(bess["efficiency_charge"]),
        "efficiency_discharge": float(bess["efficiency_discharge"]),
        "soc_min_frac": float(bess["soc_min_frac"]),
        "soc_max_frac": float(bess["soc_max_frac"]),
        "initial_soc_frac": float(bess["initial_soc_frac"]),
        "terminal_soc_equal": bool(bess["terminal_soc_equal"]),
        "max_cycles_per_day": float(bess["max_cycles_per_day"]),
        "bess_power_kw": bess_power_kw,
        "bess_capacity_kwh": bess_capacity_kwh,
        # pv
        "pv_nameplate_kwp": float(pv["pv_nameplate_kwp"]),
        # project
        "p_grid_export_max_kw": float(project["p_grid_export_max_kw"]),
        "retail_tariff_eur_per_mwh": float(project["retail_tariff_eur_per_mwh"]),
        "settlement_minutes": int(project["settlement_minutes"]),
        "mode": str(project["mode"]),
        "allow_bess_grid_charging": bool(project["allow_bess_grid_charging"]),
        "unavailability_pct": float(project["unavailability_pct"]),
        "show_titles": bool(project["show_titles"]),
        # curtailment — scalar fraction for Phase 2; per-step profile for Phase 3.
        "curtailment_frac": curtailment_frac,
        "curtailment_profile": typed.get("curtailment_profile"),
        # simulation
        "plot_daily_scope": str(sim["plot_daily_scope"]),
        "plot_monthly_scope": str(sim["plot_monthly_scope"]),
        "plot_yearly_scope": str(sim["plot_yearly_scope"]),
    }
    return params, ts


def read_inputs(xlsx_path: str | Path) -> tuple[dict[str, Any], pd.DataFrame]:
    """Return ``(params, ts)`` — the flat shape used by the optimizer.

    Raises ``ValueError`` when both ``pv_nameplate_kwp`` and
    ``bess_power_kw`` are zero (no asset to optimise).
    """
    typed = read_workbook(xlsx_path)
    params, ts = _typed_to_flat(typed)
    if (
        float(params.get("pv_nameplate_kwp", 0.0) or 0.0) <= 0.0
        and float(params.get("bess_power_kw", 0.0) or 0.0) <= 0.0
    ):
        raise ValueError(
            "Both pv_nameplate_kwp and bess_power_kw are zero — "
            "nothing to optimise."
        )
    return params, ts


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


_ECON_UNITS: dict[str, str] = {}
for _rows in _SHEET_ROW_TEMPLATES.values():
    for _key, _default, _unit, _notes in _rows:
        _ECON_UNITS.setdefault(_key, _unit)


def _format_assumptions(econ: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, value in econ.items():
        rows.append({"key": key, "value": value, "unit": _ECON_UNITS.get(key, "")})
    return pd.DataFrame(rows, columns=["key", "value", "unit"])


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
        # Hide the array-valued curtailment_profile from the snapshot —
        # it's already in the workbook's curtailment_profile sheet.
        if key == "curtailment_profile":
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
    """Write the ``02_dispatch/`` artefacts."""
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
    rolling_horizon_compare_mc: pd.DataFrame | None = None,
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
        if (
            rolling_horizon_compare_mc is not None
            and not rolling_horizon_compare_mc.empty
        ):
            rolling_horizon_compare_mc.to_excel(
                writer, sheet_name="rolling_horizon_compare_mc", index=False,
            )
        if economic_assumptions:
            _format_assumptions(economic_assumptions).to_excel(
                writer, sheet_name="economic_assumptions", index=False,
            )
    return out_path
