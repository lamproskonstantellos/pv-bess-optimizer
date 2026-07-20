"""Batch scenario engine: run N scenarios in one invocation and compare.

Each scenario is a named set of overrides on a base config — sizes,
tariffs, balancing on/off, a CAPEX multiplier — and may ``inherits`` another
scenario to clone-and-override.  Every scenario is applied to the base typed
dict and run through the same path as a standalone run, so per-scenario
results match running each alone.

Scenario overrides vary on a shared base PV shape (rescaled per
``pv_nameplate_kwp``); per-scenario locations are not re-fetched — use
separate configs for different sites.
"""

from __future__ import annotations

import copy
import json
import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_PV_ALIASES = {"source": "pv_source", "nameplate_kwp": "pv_nameplate_kwp"}
_BESS_ALIASES = {"capacity_kwh": "bess_capacity_kwh", "power_kw": "bess_power_kw"}

_REVENUE_STREAMS: tuple[str, ...] = (
    "revenue_pv_dam_eur",
    "revenue_pv_ppa_eur",
    "revenue_bess_dam_eur",
    "revenue_self_consumption_eur",
    "revenue_bess_fcr_eur",
    "revenue_bess_afrr_up_eur",
    "revenue_bess_afrr_dn_eur",
    "revenue_bess_mfrr_up_eur",
    "revenue_bess_mfrr_dn_eur",
)

_COMPARISON_COLUMNS: tuple[str, ...] = (
    "name",
    "pv_nameplate_kwp",
    "bess_power_kw",
    "bess_capacity_kwh",
    "balancing_enabled",
    "npv_eur",
    "irr_pct",
    "simple_payback_years",
    "lcoe_eur_per_mwh",
    "lcos_eur_per_mwh",
    "profit_total_eur",
    *_REVENUE_STREAMS,
)


@dataclass
class ScenarioResult:
    """Outputs of a batch scenario run."""

    comparison: pd.DataFrame   # one row per scenario


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("on", "true", "yes", "1")


# ---------------------------------------------------------------------------
# Inheritance + override application
# ---------------------------------------------------------------------------

# Sectioned overrides accepted by _apply_scenario_overrides, and the bare
# specials that live next to them in a scenario spec.
_OVERRIDE_SECTIONS: tuple[str, ...] = (
    "project", "pv", "bess", "economics", "simulation", "balancing", "ppa",
    "intraday", "market_data", "scenario_engine",
)
_BARE_SPECIALS: frozenset[str] = frozenset({
    "name", "inherits", "capex_multiplier", "price_deck",
})


def validate_scenario_overrides(scenario: dict[str, Any]) -> None:
    """Reject unknown scenario override sections or keys with guidance.

    A typo'd override would otherwise be dropped silently at workbook
    materialization (``io._build_kv_sheet`` writes only template keys),
    producing a comparison row identical to the base case — actively
    misleading.  Every target must therefore be a ``<sheet>.<key>`` pair
    from the workbook schema (aliases ``pv.source``, ``pv.nameplate_kwp``,
    ``bess.power_kw``, ``bess.capacity_kwh`` included), the bare
    ``balancing`` on/off scalar, or the ``capex_multiplier`` special.
    """
    from .io import _KEY_TO_SHEET, _SHEET_DEFAULTS

    name = scenario.get("name", "<unnamed>")
    for section, value in scenario.items():
        if section == "price_deck":
            # A deck is a NAME into the base timeseries' variant
            # columns; existence of matching columns is checked
            # fail-fast in run_scenario_batch (needs the base ts).
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"scenario {name!r}: 'price_deck' must be a "
                    f"non-empty deck name (matching the "
                    f"<column>__<deck> variant columns of the base "
                    f"timeseries)."
                )
            continue
        if section in _BARE_SPECIALS:
            continue
        if section == "balancing" and not isinstance(value, dict):
            continue  # bare on/off scalar
        if section == "trajectories":
            # Per-year stream multipliers (Eq. E24) — YAML scenario
            # files only: a single scenarios-sheet cell cannot carry a
            # per-year vector.
            from .io import _normalise_trajectories_block

            if not isinstance(value, dict):
                raise ValueError(
                    f"scenario {name!r}: 'trajectories' must be a "
                    f"mapping of stream name to a values list or "
                    f"{{mode, values}} block."
                )
            for stream, spec in value.items():
                if not isinstance(spec, (list, tuple, dict)):
                    raise ValueError(
                        f"scenario {name!r}: trajectories.{stream} needs "
                        "a per-year vector, which a single "
                        "scenarios-sheet cell cannot carry; declare the "
                        "override in a YAML scenarios file passed with "
                        "--scenarios."
                    )
            _normalise_trajectories_block(
                value, source=f"scenario {name!r}",
            )
            continue
        if section not in _OVERRIDE_SECTIONS:
            owner = _KEY_TO_SHEET.get(section)
            hint = (
                f"; did you mean target '{owner}.{section}'?"
                if owner else
                f"; known sections: {', '.join(_OVERRIDE_SECTIONS)}; bare "
                "specials: balancing, capex_multiplier"
            )
            raise ValueError(
                f"scenario {name!r}: unknown override target {section!r}{hint}"
            )
        if not isinstance(value, dict):
            raise ValueError(
                f"scenario {name!r}: section {section!r} must be a mapping "
                f"of <key>: <value> overrides, got {type(value).__name__}."
            )
        aliases = (
            _PV_ALIASES if section == "pv"
            else _BESS_ALIASES if section == "bess"
            else {}
        )
        defaults = _SHEET_DEFAULTS[section]
        for key in value:
            canonical = aliases.get(str(key), str(key))
            if canonical in defaults:
                continue
            owner = _KEY_TO_SHEET.get(canonical)
            hint = (
                f"; key {canonical!r} belongs to the {owner!r} sheet — use "
                f"target '{owner}.{canonical}'"
                if owner else ""
            )
            raise ValueError(
                f"scenario {name!r}: unknown key {section}.{key!r}{hint}"
            )


def _strip_price_deck_variants(ts: pd.DataFrame) -> pd.DataFrame:
    """Drop every ``<base>__<deck>`` variant column from ``ts``.

    Variant columns are inert in a normal run; the per-scenario MILP
    never sees them (smaller materialized workbooks, and the balancing
    scalar fallback on the re-read operates on the canonical columns
    the deck resolution produced).
    """
    variants = [c for c in ts.columns if "__" in str(c)]
    return ts.drop(columns=variants) if variants else ts


def _apply_price_deck(
    ts: pd.DataFrame, deck: str, *, scenario_name: str,
) -> pd.DataFrame:
    """Resolve a named price deck onto the canonical price columns.

    Copies every ``<base>__<deck>`` variant onto ``<base>`` (partial
    decks allowed: a canonical column without a variant for this deck
    keeps its base values, INFO-logged), then strips ALL variant
    columns.  Raises when the deck matches no variant column — the
    batch runner calls this fail-fast before any solver time is spent.
    """
    from .io import PRICE_DECK_BASE_COLUMNS

    deck = str(deck).strip().lower()
    ts = ts.copy()
    hits = 0
    for base in PRICE_DECK_BASE_COLUMNS:
        variant = f"{base}__{deck}"
        if variant in ts.columns:
            ts[base] = ts[variant].astype(float)
            hits += 1
        elif base in ts.columns:
            logger.info(
                "scenario %r price deck %r: no %s variant column; the "
                "base column's values are kept.",
                scenario_name, deck, base,
            )
    if hits == 0:
        available = sorted({
            str(c).split("__", 1)[1]
            for c in ts.columns if "__" in str(c)
        })
        raise ValueError(
            f"scenario {scenario_name!r}: price deck {deck!r} matches no "
            f"<column>__{deck} variant column in the base timeseries; "
            f"decks available: {available or 'none'}."
        )
    return _strip_price_deck_variants(ts)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if key == "inherits":
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _resolve_one(
    scn: dict[str, Any], by_name: dict[str, dict[str, Any]], seen: frozenset[str],
) -> dict[str, Any]:
    parent_name = scn.get("inherits")
    if not parent_name:
        return copy.deepcopy(scn)
    if parent_name in seen:
        raise ValueError(f"circular scenario inheritance via {parent_name!r}")
    parent = by_name.get(parent_name)
    if parent is None:
        raise ValueError(
            f"scenario {scn.get('name')!r} inherits unknown {parent_name!r}"
        )
    merged = _deep_merge(
        _resolve_one(parent, by_name, seen | {parent_name}), scn,
    )
    merged.pop("inherits", None)
    merged["name"] = scn.get("name")
    return merged


def resolve_inheritance(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return scenarios with every ``inherits`` clause merged in."""
    by_name = {s["name"]: s for s in scenarios if "name" in s}
    return [_resolve_one(scn, by_name, frozenset()) for scn in scenarios]


def _apply_scenario_overrides(
    base_typed: dict[str, Any], scenario: dict[str, Any],
) -> dict[str, Any]:
    validate_scenario_overrides(scenario)
    typed = copy.deepcopy(base_typed)
    for key, value in (scenario.get("pv") or {}).items():
        typed["pv"][_PV_ALIASES.get(key, key)] = value
    for key, value in (scenario.get("bess") or {}).items():
        typed["bess"][_BESS_ALIASES.get(key, key)] = value
    for section in (
        "project", "economics", "simulation", "ppa", "intraday",
        "market_data", "scenario_engine",
    ):
        overrides = scenario.get(section) or {}
        if overrides:
            target = typed.setdefault(section, {})
            for key, value in overrides.items():
                target[key] = value

    bal = scenario.get("balancing")
    if isinstance(bal, dict):
        typed["balancing"].update(bal)
    elif bal is not None:
        typed["balancing"]["balancing_enabled"] = _as_bool(bal)

    mult = scenario.get("capex_multiplier")
    if mult is not None:
        m = float(mult)
        typed["pv"]["capex_pv_eur_per_kw"] = (
            _to_float(typed["pv"].get("capex_pv_eur_per_kw", 0.0)) * m
        )
        typed["bess"]["capex_bess_eur_per_kwh"] = (
            _to_float(typed["bess"].get("capex_bess_eur_per_kwh", 0.0)) * m
        )
        typed["project"]["site_capex_eur"] = (
            _to_float(typed["project"].get("site_capex_eur", 0.0)) * m
        )

    # Trajectory overrides merge per stream: an overridden stream
    # replaces the base workbook's vector wholesale, untouched base
    # streams are kept.  Lifecycle coverage and the Year-1 anchor are
    # re-validated on the materialize round-trip (read_workbook →
    # validate_workbook_params), so a scenario that also overrides
    # project_lifecycle_years is checked against the NEW length.
    traj_override = scenario.get("trajectories")
    if traj_override is not None:
        from .io import _normalise_trajectories_block

        block = _normalise_trajectories_block(
            traj_override,
            source=f"scenario {scenario.get('name', '<unnamed>')!r}",
        )
        base_block = typed.get("trajectories") or {}
        typed["trajectories"] = {
            **copy.deepcopy(base_block), **(block or {}),
        } or None

    # Price deck resolution happens BEFORE the workbook is written, so
    # the balancing scalar fallback on the re-read sees the deck values
    # in the canonical columns; without a deck the inert variant
    # columns are stripped so the per-scenario MILP never sees them.
    deck = scenario.get("price_deck")
    if deck is not None and "ts" in typed:
        typed["ts"] = _apply_price_deck(
            typed["ts"], str(deck),
            scenario_name=str(scenario.get("name", "<unnamed>")),
        )
    elif "ts" in typed:
        typed["ts"] = _strip_price_deck_variants(typed["ts"])

    # The base PV profile is already resolved; scenarios rescale it by
    # nameplate through the standard read path, so force file mode.
    typed["pv"]["pv_source"] = "file"
    # Same rule for the market-data bypass: the base read already
    # resolved any fetched price columns into typed['ts'], so the
    # materialised temp workbook must NOT re-trigger the fetch on
    # re-read — a re-fetch REPLACES the canonical columns and would
    # silently clobber a price_deck override (and needs network/token
    # again).  Mirrors materialize_bypassed_workbook: sources flip to
    # 'file', the token cell is blanked.  A scenario that explicitly
    # overrides market_data keys keeps its configuration verbatim —
    # the deliberate re-fetch is then the scenario's own semantics.
    if not scenario.get("market_data"):
        market_cfg = typed.get("market_data")
        if isinstance(market_cfg, dict):
            for source_key in (
                "price_source", "balancing_source", "imbalance_source",
                "intraday_source",
            ):
                market_cfg[source_key] = "file"
            market_cfg["entsoe_token"] = ""
    return typed


# ---------------------------------------------------------------------------
# Per-scenario evaluation + batch
# ---------------------------------------------------------------------------


def evaluate_scenario(
    base_typed: dict[str, Any], scenario: dict[str, Any], *,
    solver_opts: dict[str, Any],
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Run one scenario and return its comparison row."""
    from .availability import apply_operating_derates
    from .io import read_inputs, write_workbook
    from .kpis import compute_kpis
    from .optimization import run_scenario
    from .pipeline import _build_financials

    typed = _apply_scenario_overrides(base_typed, scenario)
    tmp = Path(tempfile.mkdtemp(prefix="pvbess_scn_"))
    xlsx = tmp / "scenario.xlsx"
    write_workbook(typed, xlsx)

    params, ts = read_inputs(xlsx)
    res, _solver, _res_full = run_scenario(
        params, ts, return_unrounded=True, **solver_opts,
    )
    kpis = compute_kpis(res, params, verify_balance=False)
    kpis = apply_operating_derates(kpis, params)
    # base_dir: an armed price-scenario engine resolves relative
    # store_path entries against the ORIGINAL workbook's directory,
    # never the throwaway temp dir the scenario materialised into.
    bundle = _build_financials(
        xlsx, params, ts, kpis, res, base_dir=base_dir,
    )
    fin = bundle.get("fin_kpis") or {}

    row: dict[str, Any] = {
        "name": scenario.get("name", "scenario"),
        "price_deck": str(scenario.get("price_deck") or ""),
        "pv_nameplate_kwp": _to_float(params.get("pv_nameplate_kwp", 0.0)),
        "bess_power_kw": _to_float(params.get("bess_power_kw", 0.0)),
        "bess_capacity_kwh": _to_float(params.get("bess_capacity_kwh", 0.0)),
        "balancing_enabled": bool(
            typed["balancing"].get("balancing_enabled", False)
        ),
        "npv_eur": _to_float(fin.get("npv_eur")),
        "irr_pct": _to_float(fin.get("irr_pct")),
        "simple_payback_years": _to_float(fin.get("simple_payback_years")),
        "lcoe_eur_per_mwh": _to_float(fin.get("lcoe_eur_per_mwh")),
        "lcos_eur_per_mwh": _to_float(fin.get("lcos_eur_per_mwh")),
        "profit_total_eur": _to_float(kpis.get("profit_total_eur")),
    }
    for stream in _REVENUE_STREAMS:
        row[stream] = _to_float(kpis.get(stream))
    return row


def run_scenario_batch(
    base_typed: dict[str, Any],
    scenarios: list[dict[str, Any]],
    *,
    solver_opts: dict[str, Any],
    base_dir: Path | None = None,
) -> pd.DataFrame:
    """Evaluate every (inheritance-resolved) scenario into a comparison table."""
    resolved = resolve_inheritance(scenarios)
    # Fail fast on a typo'd override BEFORE any solver time is spent —
    # scenario N failing after N-1 solves wastes minutes per batch.
    for scn in resolved:
        validate_scenario_overrides(scn)
        deck = scn.get("price_deck")
        if deck is not None and "ts" in base_typed:
            # Raises on a deck with no matching variant columns; the
            # resolved frame itself is discarded here.
            _apply_price_deck(
                base_typed["ts"], str(deck),
                scenario_name=str(scn.get("name", "<unnamed>")),
            )
    rows = [
        evaluate_scenario(
            base_typed, scn, solver_opts=solver_opts, base_dir=base_dir,
        )
        for scn in resolved
    ]
    # The comparison gains a price_deck column ONLY when at least one
    # scenario names a deck, keeping deck-free batches bit-identical.
    columns = list(_COMPARISON_COLUMNS)
    if any(r.get("price_deck") for r in rows):
        columns.insert(1, "price_deck")
    else:
        for r in rows:
            r.pop("price_deck", None)
    return pd.DataFrame(rows, columns=columns)


# ---------------------------------------------------------------------------
# Output + orchestration
# ---------------------------------------------------------------------------


def write_scenario_comparison_workbook(
    out_path: str | Path, comparison: pd.DataFrame,
) -> Path:
    """Write the scenario-comparison table to a styled workbook."""
    from .io_style import style_workbook

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        comparison.to_excel(writer, sheet_name="scenario_comparison", index=False)
        style_workbook(writer.book)
    return out_path


def read_scenarios_file(path: str | Path) -> list[dict[str, Any]]:
    """Load the ``scenarios`` list from a YAML/JSON scenarios file."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        raw = json.loads(text)
    else:
        import yaml

        raw = yaml.safe_load(text)
    if not isinstance(raw, dict) or not isinstance(raw.get("scenarios"), list):
        raise ValueError(
            f"{path}: expected a mapping with a 'scenarios' list."
        )
    return [s for s in raw["scenarios"] if isinstance(s, dict)]


def _cell(value: Any) -> Any:
    """Normalise a sheet cell: blank/NaN to None, numpy scalar to Python."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, np.generic):
        return value.item()
    return value


def _parse_scenarios_sheet(
    df: pd.DataFrame,
) -> tuple[bool, list[dict[str, Any]]]:
    """Parse the columnar ``scenarios`` sheet into ``(enabled, scenarios)``.

    The sheet is tidy/long: each row is one override.  Consecutive rows
    that share a ``name`` (blank ``name`` cells inherit the row above) form
    one scenario.  A dotted ``target`` such as ``project.mode`` nests the
    ``value`` under that section; a bare ``target`` (``capex_multiplier``,
    ``balancing``) sets a top-level key.  The ``inherits`` cell clones
    another scenario.  ``enabled`` is read from the first non-blank cell of
    the ``enabled`` column.  The returned list matches the shape consumed by
    :func:`run_scenarios`.
    """
    from .io import _parse_bool

    cols = {str(c).strip().lower(): c for c in df.columns}

    def col(row: Any, name: str) -> Any:
        key = cols.get(name)
        return _cell(row[key]) if key is not None else None

    enabled = False
    enabled_key = cols.get("enabled")
    if enabled_key is not None:
        nonnull = df[enabled_key].dropna()
        if not nonnull.empty:
            enabled = _parse_bool(nonnull.iloc[0], False)

    by_name: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    current: str | None = None
    for _, row in df.iterrows():
        name_val = col(row, "name")
        if name_val is not None and str(name_val).strip():
            current = str(name_val).strip()
        if not current:
            continue
        if current not in by_name:
            by_name[current] = {"name": current}
            order.append(current)
        scn = by_name[current]
        inherits = col(row, "inherits")
        if inherits is not None and str(inherits).strip():
            scn["inherits"] = str(inherits).strip()
        target = col(row, "target")
        if target is None or not str(target).strip():
            continue
        target = str(target).strip()
        value = col(row, "value")
        if "." in target:
            section, key = target.split(".", 1)
            bucket = scn.setdefault(section, {})
            if isinstance(bucket, dict):
                bucket[key] = value
        else:
            scn[target] = value
    return enabled, [by_name[name] for name in order]


def read_scenarios_block(path: str | Path) -> list[dict[str, Any]] | None:
    """Return the scenario list from an Excel ``scenarios`` sheet.

    Returns the parsed scenarios when the sheet is present and its
    ``enabled`` toggle is TRUE, otherwise None (so a normal run proceeds).
    Non-Excel paths return None — YAML/JSON batches use ``--scenarios`` with
    :func:`read_scenarios_file`.
    """
    path = Path(path)
    if path.suffix.lower() not in (".xlsx", ".xls") or not path.exists():
        return None
    try:
        sheets = set(pd.ExcelFile(path).sheet_names)
    except (ValueError, OSError):
        return None
    if "scenarios" not in sheets:
        return None
    enabled, scenarios = _parse_scenarios_sheet(
        pd.read_excel(path, sheet_name="scenarios"),
    )
    return scenarios if (enabled and scenarios) else None


def run_scenarios(config: Any, scenarios: list[dict[str, Any]]) -> ScenarioResult:
    """Run a batch of scenarios for ``config`` and write the comparison
    workbook + plots under the output directory."""
    from .io import read_workbook
    from .io_read import is_structured_config, materialize_to_xlsx
    from .plotting import (
        apply_ieee_style,
        plot_scenario_comparison_bars,
        plot_scenario_revenue_bridge,
    )

    if not scenarios:
        raise ValueError("no scenarios to run")

    src = Path(config.excel)
    tmp = Path(tempfile.mkdtemp(prefix="pvbess_scn_base_"))
    base_xlsx = materialize_to_xlsx(src, tmp) if is_structured_config(src) else src
    base_typed = read_workbook(base_xlsx)
    # Apply the CLI ``--mode`` override to the batch base, mirroring
    # ``pipeline.run`` and ``sizing.run_sizing`` so the three dispatch
    # surfaces agree.  Per-scenario ``project.mode`` targets still override
    # this base.
    if getattr(config, "mode", None) is not None:
        base_typed["project"]["mode"] = config.mode
    solver_opts = {
        "solver_name": config.solver,
        "mip_gap": config.mip_gap,
        "time_limit_seconds": config.time_limit,
        "tee": config.tee,
    }

    apply_ieee_style()
    comparison = run_scenario_batch(
        base_typed, scenarios, solver_opts=solver_opts,
        base_dir=src.parent,
    )
    result = ScenarioResult(comparison=comparison)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(config.outdir) / f"{src.stem}_scenarios_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    # NPV tail risk over the scenario set (Eqs. U10/U11): appended to
    # the WORKBOOK table only (equal-weight scenarios - a deck is a
    # scenario list, not a probability distribution; documented), so
    # the comparison plots keep one bar per real scenario.
    comparison_sheet = comparison
    _sim_cfg = base_typed.get("simulation") or {}
    if (
        bool(_sim_cfg.get("risk_metrics_enabled", False))
        and len(comparison) >= 2
        and "npv_eur" in comparison.columns
    ):
        from .economics import var_cvar

        _alpha = float(_sim_cfg.get("risk_alpha_pct", 5.0) or 5.0)
        _var, _cvar = var_cvar(
            comparison["npv_eur"].astype(float).tolist(), _alpha,
        )
        comparison_sheet = pd.concat(
            [
                comparison,
                pd.DataFrame([
                    {"name": f"npv_var_{_alpha:g}pct", "npv_eur": _var},
                    {"name": f"npv_cvar_{_alpha:g}pct", "npv_eur": _cvar},
                ]),
            ],
            ignore_index=True,
        )
        logger.info(
            "[risk] scenario-set NPV tail (equal weights, %d rows): "
            "VaR_%.3g%% = %.0f EUR, CVaR_%.3g%% = %.0f EUR.",
            len(comparison), _alpha, _var, _alpha, _cvar,
        )
    write_scenario_comparison_workbook(
        out_dir / "scenario_comparison.xlsx", comparison_sheet,
    )
    plot_scenario_comparison_bars(comparison, out_dir / "scenario_comparison.pdf")
    if len(comparison) >= 2:
        plot_scenario_revenue_bridge(
            comparison, out_dir / "scenario_revenue_bridge.pdf",
        )
    logger.info(
        "[scenarios] %d scenarios -> %s", len(comparison), out_dir,
    )
    return result
