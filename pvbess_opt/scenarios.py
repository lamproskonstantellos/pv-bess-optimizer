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
import math
import shutil
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
        if section == "capex_multiplier":
            if value is None:
                # A YAML ``capex_multiplier:`` stub — inert, like the
                # null-balancing shorthand: warn and keep the base CAPEX.
                logger.warning(
                    "scenario %r has an empty 'capex_multiplier' entry "
                    "(null); ignoring it (base CAPEX kept). Remove the "
                    "key or give it a value.",
                    name,
                )
                continue
            if isinstance(value, (bool, np.bool_)):
                # float(False) is 0.0: every CAPEX line silently zeroed
                # under the scenario's label.
                raise ValueError(
                    f"scenario {name!r}: capex_multiplier {value!r} is a "
                    "boolean, not a number; write the multiplier as a "
                    "numeric value (e.g. 0.8 for -20 % CAPEX)."
                )
            try:
                m = float(value)
            except (TypeError, ValueError):
                raise ValueError(
                    f"scenario {name!r}: capex_multiplier {value!r} is "
                    "not a number; write the multiplier as a numeric "
                    "value (e.g. 0.8 for -20 % CAPEX)."
                ) from None
            if not np.isfinite(m):
                # NaN would materialise blank CAPEX cells that re-parse
                # to the SHEET DEFAULTS on the scenario re-read — a
                # comparison row priced on defaults under the
                # scenario's label.
                raise ValueError(
                    f"scenario {name!r}: capex_multiplier must be "
                    f"finite, got {value!r}."
                )
            if m < 0.0:
                raise ValueError(
                    f"scenario {name!r}: capex_multiplier must be "
                    f">= 0, got {m} (a negative multiplier books CAPEX "
                    "as a cash credit)."
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
        for key, raw in value.items():
            canonical = aliases.get(str(key), str(key))
            if section == "pv" and canonical == "timeseries_path":
                # evaluate_scenario resolves the base PV profile ONCE and
                # clears the path from the materialised workbook, so this
                # override would be applied and then discarded — the
                # scenario would silently solve the BASE profile under the
                # override's label.  (pv_source is different: forcing it
                # to 'file' post-resolution is truthful, the materialised
                # workbook carries the data.)
                raise ValueError(
                    f"scenario {name!r}: 'pv.timeseries_path' cannot be "
                    "overridden per scenario — the base PV profile is "
                    "resolved once and carried into every scenario "
                    "workbook verbatim (per-scenario PV files are not "
                    "re-read). Vary PV via 'pv.nameplate_kwp' (profile "
                    "rescale) or run separate base workbooks."
                )
            if canonical in defaults:
                # Route the VALUE through the SAME parser the apply path
                # uses (_parsed_override_value): a scenarios file is the
                # second YAML surface for these keys, and the two paths
                # once diverged — the validator inlined _parse_value and
                # missed the grid-cap keys' _parse_grid_export_max
                # special case, rejecting the documented 'unlimited' /
                # 'inf' / 'disabled' tokens that the workbook and config
                # surfaces accept.  ``pv_nameplate_kwp`` keeps its
                # dedicated guards in ``_apply_scenario_overrides``
                # (profile rescale needs the raw form).
                if canonical != "pv_nameplate_kwp":
                    _parsed_override_value(section, key, canonical, raw, name)
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


def _debt_sizing_deck_to_keep(econ: dict[str, Any] | None) -> str | None:
    """The price-deck name a scenario must keep for its debt sizing.

    A base workbook with ``debt_sizing_case = low_price`` and
    ``debt_sizing_mode = target_dscr`` re-dispatches the named
    ``debt_sizing_deck`` (default ``low``) inside ``_build_financials``.
    That re-dispatch re-reads the MATERIALISED scenario workbook, so its
    ``<col>__<deck>`` variant columns must survive materialisation
    (stripping them all crashes the batch — the sizing sweep got its
    guard in an earlier round, the scenario batch did not).  Returns the
    deck name to preserve, or None when no deck re-dispatch will run.
    """
    if not isinstance(econ, dict):
        return None
    case = str(econ.get("debt_sizing_case", "base") or "base").strip().lower()
    mode = str(
        econ.get("debt_sizing_mode", "manual") or "manual"
    ).strip().lower()
    if case == "low_price" and mode == "target_dscr":
        return str(econ.get("debt_sizing_deck", "low") or "low").strip().lower()
    return None


def _strip_price_deck_variants(
    ts: pd.DataFrame, *, keep_deck: str | None = None,
) -> pd.DataFrame:
    """Drop every ``<base>__<deck>`` variant column from ``ts``.

    Variant columns are inert in a normal run; the per-scenario MILP
    never sees them (smaller materialized workbooks, and the balancing
    scalar fallback on the re-read operates on the canonical columns
    the deck resolution produced).  ``keep_deck`` preserves the
    ``<base>__<keep_deck>`` columns so a downstream low_price debt-sizing
    re-dispatch (which re-reads the materialised workbook) can still
    resolve its deck.
    """
    _keep = None if keep_deck is None else f"__{keep_deck}"
    variants = [
        c for c in ts.columns
        if "__" in str(c) and not (_keep is not None and str(c).endswith(_keep))
    ]
    return ts.drop(columns=variants) if variants else ts


def _apply_price_deck(
    ts: pd.DataFrame, deck: str, *, scenario_name: str,
    keep_deck: str | None = None,
) -> pd.DataFrame:
    """Resolve a named price deck onto the canonical price columns.

    Copies every ``<base>__<deck>`` variant onto ``<base>`` (partial
    decks allowed: a canonical column without a variant for this deck
    keeps its base values, INFO-logged), then strips the variant
    columns (except ``keep_deck``, preserved for a downstream low_price
    debt-sizing re-dispatch).  Raises when the deck matches no variant
    column — the batch runner calls this fail-fast before any solver
    time is spent.
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
    return _strip_price_deck_variants(ts, keep_deck=keep_deck)


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


def _canonicalise_scenario(scn: dict[str, Any]) -> dict[str, Any]:
    """Normalise the bare ``balancing`` scalar shorthand to its dict form.

    ``balancing: true`` becomes ``{"balancing": {"balancing_enabled": true}}``
    so ``_deep_merge`` always merges dict-with-dict across ``inherits`` — a
    scalar/dict cross would replace wholesale, silently dropping either the
    parent's enable or its dotted keys (the same silent-drop class the sheet
    parser rejects within one scenario).
    """
    if "balancing" not in scn:
        return scn
    bal = scn.get("balancing")
    if isinstance(bal, dict):
        return scn
    out = dict(scn)
    if bal is None:
        # An explicit null (YAML ``balancing:`` stub) must not reach
        # _deep_merge as a scalar — it would replace an inherited balancing
        # dict wholesale and the override applier would then skip it,
        # silently running the BASE workbook's balancing under the
        # scenario's label.  An empty dict is an inert merge instead.
        logger.warning(
            "scenario %r has an empty 'balancing' entry (null); ignoring "
            "it (inherited/base balancing settings are kept). Remove the "
            "key or give it a value.",
            scn.get("name", "scenario"),
        )
        out["balancing"] = {}
        return out
    out["balancing"] = {"balancing_enabled": bal}
    return out


def resolve_inheritance(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return scenarios with every ``inherits`` clause merged in."""
    canonical = [_canonicalise_scenario(s) for s in scenarios]
    # Duplicate names are always a drafting mistake: both rows would run
    # under one comparison label and an ``inherits`` clause would bind
    # silently to whichever definition came last.
    seen: set[str] = set()
    for scn in canonical:
        name = scn.get("name")
        if name is None:
            if "name" not in scn:
                # A scenario with NO name key keeps its historical
                # 'scenario' display label; only an explicit blank is
                # rejected below.
                continue
            # An explicit '- name:' (null) or blank name is a drafting
            # mistake: the comparison rows would all be labelled None
            # and duplicates of it bypassed the duplicate-name guard.
            raise ValueError(
                "scenario with an empty 'name': give every scenario a "
                "non-blank name (comparison rows and 'inherits' "
                "references need one)."
            )
        if isinstance(name, str) and not name.strip():
            raise ValueError(
                "scenario with a blank 'name': give every scenario a "
                "non-blank name (comparison rows and 'inherits' "
                "references need one)."
            )
        if name in seen:
            raise ValueError(
                f"duplicate scenario name {name!r}: comparison rows and "
                "'inherits' references need unique names; rename one of "
                "the definitions."
            )
        seen.add(name)
    by_name = {s["name"]: s for s in canonical if "name" in s}
    return [_resolve_one(scn, by_name, frozenset()) for scn in canonical]


def _parsed_override_value(
    section: str, key: Any, canonical: str, raw: Any, name: Any,
) -> Any:
    """Route one scenario override through the loader's typed parser.

    Materialising the raw YAML value verbatim let native containers
    stringify into the workbook cell (``[1, 5]``) and deferred value
    errors to the scenario's re-read — mid-batch, after earlier
    scenarios' solver time, without naming the scenario.  Parsing here
    keeps the scenarios file and the structured-config surface (which
    already runs ``_parse_value``) in agreement.
    """
    from .io import _SHEET_DEFAULTS, _parse_grid_export_max, _parse_value

    defaults = _SHEET_DEFAULTS.get(section) or {}
    if canonical not in defaults:
        return raw
    try:
        if canonical in ("p_grid_export_max_kw", "p_grid_import_max_kw"):
            # The grid caps speak the documented 'unlimited' / 'inf' /
            # 'disabled' token dialect; the workbook kv layer and the
            # structured-config route both special-case them through
            # _parse_grid_export_max, and _parse_value's numeric branch
            # would reject the tokens — the third surface must agree.
            return _parse_grid_export_max(raw, canonical)
        return _parse_value(canonical, raw, defaults[canonical])
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"scenario {name!r}: override {section}.{key} = {raw!r}: {exc}"
        ) from exc


def _apply_scenario_overrides(
    base_typed: dict[str, Any], scenario: dict[str, Any],
    *, prevalidated: bool = False,
) -> dict[str, Any]:
    # ``prevalidated`` lets the batch (which validates every scenario in
    # its explicit fail-fast loop) skip the re-validation here — the
    # validator's warning side effects (capex_multiplier stubs, no-shape
    # nameplate) previously fired three times per scenario per batch.
    if not prevalidated:
        validate_scenario_overrides(scenario)
    typed = copy.deepcopy(base_typed)
    _scn_name = scenario.get("name", "scenario")
    _base_nameplate = float(
        (base_typed.get("pv") or {}).get("pv_nameplate_kwp", 0.0) or 0.0
    )
    for key, value in (scenario.get("pv") or {}).items():
        canonical = _PV_ALIASES.get(key, key)
        if canonical != "pv_nameplate_kwp":
            value = _parsed_override_value("pv", key, canonical, value, _scn_name)
        typed["pv"][canonical] = value
    # A nameplate override changes the CAPEX/OPEX basis, so the resolved
    # PV profile must scale with it (shape preserved) or the scenario
    # would solve the BASE plant's generation against the OVERRIDDEN
    # plant's cost — the module contract ("shared base PV shape, rescaled
    # per pv_nameplate_kwp") and the sizing sweep's identical treatment
    # (evaluate_sizing_point).  Skipped when the base has no nameplate
    # (no shape to scale) or no resolved pv_kwh column.
    _has_np_override = any(
        _PV_ALIASES.get(str(k), str(k)) == "pv_nameplate_kwp"
        for k in (scenario.get("pv") or {})
    )
    _raw_np = typed.get("pv", {}).get("pv_nameplate_kwp", 0.0)
    if _has_np_override and (
        isinstance(_raw_np, (bool, np.bool_))
        or _raw_np is None
        or (isinstance(_raw_np, str) and not _raw_np.strip())
    ):
        # bool (incl. numpy bool) would float() to 1.0/0.0 (a silent
        # 1-kWp or zeroed plant); a blank override would fall into the
        # `or 0.0` base-default path and zero the profile — both are
        # drafting mistakes, not sizes.
        raise ValueError(
            f"scenario {scenario.get('name', 'scenario')!r}: "
            f"pv nameplate override {_raw_np!r} is not a number."
        )
    try:
        _new_nameplate = float(_raw_np or 0.0)
    except (TypeError, ValueError) as exc:
        # Name the scenario and key — a locale-formatted override
        # ('30,000') would otherwise surface as a bare float() error.
        raise ValueError(
            f"scenario {scenario.get('name', 'scenario')!r}: "
            f"pv nameplate override "
            f"{typed.get('pv', {}).get('pv_nameplate_kwp')!r} is not a "
            "number."
        ) from exc
    if _has_np_override and not math.isfinite(_new_nameplate):
        # NaN rides through float() (JSON's json.dump emits bare NaN by
        # default; YAML has .nan) and would either silently zero the
        # comparison row on a PV-less base (NaN > 0.0 is False on BOTH
        # rescale branches, then the NaN materialises as a blank cell
        # re-parsed to the default) or fail late and unnamed at the
        # scenario's re-read.  Fail fast naming the scenario instead.
        raise ValueError(
            f"scenario {scenario.get('name', 'scenario')!r}: "
            f"pv nameplate override {_raw_np!r} is not a finite number."
        )
    if _has_np_override and _new_nameplate < 0.0:
        # A negative override would rescale pv_kwh NEGATIVE and die at
        # the scenario's re-read — late (after earlier scenarios solved),
        # unnamed, and blaming the timeseries column instead of the
        # override.  Fail fast naming the scenario, like non-finite.
        raise ValueError(
            f"scenario {scenario.get('name', 'scenario')!r}: "
            f"pv nameplate override {_raw_np!r} must be >= 0."
        )
    if (
        _base_nameplate > 0.0
        and _new_nameplate != _base_nameplate
        and isinstance(typed.get("ts"), pd.DataFrame)
        and "pv_kwh" in typed["ts"].columns
    ):
        typed["ts"] = typed["ts"].copy()
        typed["ts"]["pv_kwh"] = (
            typed["ts"]["pv_kwh"].astype(float)
            * (_new_nameplate / _base_nameplate)
        )
    elif _base_nameplate <= 0.0 and _new_nameplate > 0.0:
        logger.warning(
            "scenario %r sets pv_nameplate_kwp=%.6g on a base with no PV "
            "nameplate: there is no PV shape to scale, so the CAPEX/OPEX "
            "basis grows while generation stays at the base profile "
            "(likely zero). Provide a base PV profile if PV output is "
            "intended.",
            scenario.get("name", "scenario"), _new_nameplate,
        )
    for key, value in (scenario.get("bess") or {}).items():
        canonical = _BESS_ALIASES.get(key, key)
        typed["bess"][canonical] = _parsed_override_value(
            "bess", key, canonical, value, _scn_name,
        )
    for section in (
        "project", "economics", "simulation", "ppa", "intraday",
        "market_data", "scenario_engine",
    ):
        overrides = scenario.get(section) or {}
        if overrides:
            target = typed.setdefault(section, {})
            for key, value in overrides.items():
                target[key] = _parsed_override_value(
                    section, key, str(key), value, _scn_name,
                )

    bal = scenario.get("balancing")
    if isinstance(bal, dict):
        typed["balancing"].update({
            key: _parsed_override_value(
                "balancing", key, str(key), val, _scn_name,
            )
            for key, val in bal.items()
        })
    elif bal is not None:
        typed["balancing"]["balancing_enabled"] = _as_bool(bal)

    # Columns the BASE read's scalar fallback materialised hold the
    # base's default prices, not workbook data.  Left in place they
    # would survive into the scenario's temp workbook, where the
    # re-read finds them "present" and never re-applies the fallback —
    # so a scenario override of e.g.
    # balancing.fcr_default_capacity_price_eur_per_mwh would be
    # accepted, land in the scenario's BalancingConfig, and still
    # settle on the BASE price (and the identical spec behaved
    # differently depending on the base workbook's enable toggle).
    # Dropping them lets the scenario's own re-read re-materialise
    # them from the scenario's (possibly overridden) scalars; columns
    # that carried real workbook data are untouched.
    _fallback_cols = typed.pop("balancing_fallback_columns", None) or []
    if _fallback_cols and "ts" in typed:
        typed["ts"] = typed["ts"].drop(
            columns=[c for c in _fallback_cols if c in typed["ts"].columns],
        )

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
    # Preserve the debt-sizing deck's variants (if the base config runs a
    # low_price target-DSCR sizing) so the nested re-dispatch — which
    # re-reads THIS materialised workbook — can still resolve its deck.
    _keep_deck = _debt_sizing_deck_to_keep(typed.get("economics"))
    deck = scenario.get("price_deck")
    if deck is not None and "ts" in typed:
        typed["ts"] = _apply_price_deck(
            typed["ts"], str(deck),
            scenario_name=str(scenario.get("name", "<unnamed>")),
            keep_deck=_keep_deck,
        )
    elif "ts" in typed:
        typed["ts"] = _strip_price_deck_variants(
            typed["ts"], keep_deck=_keep_deck,
        )

    # The base PV profile is already resolved into typed['ts'] (a nameplate
    # override rescales it in _apply_scenario_overrides, NOT on re-read —
    # the loader treats pv_kwh as absolute), so force file mode and drop
    # any external-path reference: the materialised temp workbook already
    # CARRIES the resolved column, and a surviving timeseries_path would
    # point at a path relative to the temp dir and misfire the loud
    # column-vs-file conflict warning on every scenario load.
    typed["pv"]["pv_source"] = "file"
    if typed.get("pv", {}).get("timeseries_path"):
        typed["pv"]["timeseries_path"] = None
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
    prevalidated: bool = False,
) -> dict[str, Any]:
    """Run one scenario and return its comparison row."""
    from .availability import apply_operating_derates
    from .io import read_inputs, write_workbook
    from .kpis import compute_kpis
    from .optimization import run_scenario
    from .pipeline import _build_financials

    typed = _apply_scenario_overrides(
        base_typed, scenario, prevalidated=prevalidated,
    )
    scn_name = str(scenario.get("name", "scenario"))
    tmp = Path(tempfile.mkdtemp(prefix="pvbess_scn_"))
    try:
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
            xlsx, params, ts, kpis, res,
            solver_opts=solver_opts, base_dir=base_dir,
        )
    except (ValueError, RuntimeError) as exc:
        # A failure from the materialised workbook's re-read or the
        # solve carries no scenario context of its own — a ten-scenario
        # batch died with "'bess_capacity_kwh' must be non-negative"
        # and the client had to bisect to find which scenario.
        if str(exc).startswith("scenario "):
            raise
        raise type(exc)(f"scenario {scn_name!r}: {exc}") from exc
    finally:
        # The scenario workbook has no consumer past its comparison row;
        # drop the temp dir so a batch does not leak one dir per scenario.
        shutil.rmtree(tmp, ignore_errors=True)
    fin = bundle.get("fin_kpis") or {}

    row: dict[str, Any] = {
        "name": scenario.get("name", "scenario"),
        "price_deck": str(scenario.get("price_deck") or ""),
        "pv_nameplate_kwp": _to_float(params.get("pv_nameplate_kwp", 0.0)),
        "bess_power_kw": _to_float(params.get("bess_power_kw", 0.0)),
        "bess_capacity_kwh": _to_float(params.get("bess_capacity_kwh", 0.0)),
        # Source the display flag from the PARSED params (what was actually
        # solved), not the pre-materialize ``typed`` dict: an Excel
        # scenarios-sheet override written as the dotted target
        # ``balancing.balancing_enabled = FALSE`` lands in ``typed`` as the
        # unparsed string 'FALSE', and ``bool('FALSE')`` is True — so the row
        # would report balancing ENABLED for a run that solved with it
        # DISABLED.  ``params`` re-reads the materialised workbook, where the
        # loader has parsed 'FALSE' -> False.
        "balancing_enabled": bool(
            (params.get("balancing") or {}).get("balancing_enabled", False)
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
    from .io import read_inputs, write_workbook

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
    # Key-level checks above cannot see VALUE-level mistakes that only
    # the materialised workbook's re-read rejects (a negative capacity,
    # a replacement year beyond an overridden lifecycle, ...).  Dry-run
    # every scenario through materialise + read BEFORE the solve loop:
    # seconds per scenario against hours of solver work previously
    # discarded when scenario N crashed the batch after N-1 solves.
    for scn in resolved:
        scn_name = str(scn.get("name", "<unnamed>"))
        tmp = Path(tempfile.mkdtemp(prefix="pvbess_scn_val_"))
        try:
            typed = _apply_scenario_overrides(
                base_typed, scn, prevalidated=True,
            )
            xlsx = tmp / "scenario.xlsx"
            write_workbook(typed, xlsx)
            _params_chk, _ts_chk = read_inputs(xlsx)
            # An ARMED price-scenario engine's own inputs (store_path
            # resolution, meta.yaml, curve cadence) previously escaped
            # this pre-pass and surfaced only inside _build_financials —
            # after each scenario's full MILP; with the arming in the
            # base workbook, EVERY scenario burned its solve and the
            # batch ended in the all-fail error.  Validate them here,
            # against the same base_dir the evaluation threads.
            from .economics import read_economic_params
            from .pricedata.engine import preflight_price_scenarios

            preflight_price_scenarios(
                read_economic_params(xlsx), _ts_chk,
                base_dir=(
                    Path(base_dir) if base_dir is not None else xlsx.parent
                ),
            )
        except (ValueError, KeyError) as exc:
            if str(exc).startswith("scenario "):
                raise
            raise ValueError(f"scenario {scn_name!r}: {exc}") from exc
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    # The dry pre-pass has rejected everything the loaders can catch, so
    # a failure past this point is solver-level (infeasible, solver
    # crash).  One sick scenario must not discard its siblings' finished
    # solves: log it, keep going, and report the failures alongside the
    # completed comparison.
    rows = []
    failures: list[dict[str, str]] = []
    for scn in resolved:
        scn_name = str(scn.get("name", "scenario"))
        try:
            rows.append(evaluate_scenario(
                base_typed, scn, solver_opts=solver_opts, base_dir=base_dir,
                prevalidated=True,
            ))
        except (ValueError, RuntimeError) as exc:
            logger.error(
                "scenario %r failed (%s); continuing with the remaining "
                "scenarios — the comparison will list it on the "
                "failed_scenarios sheet.", scn_name, exc,
            )
            failures.append({"name": scn_name, "error": str(exc)})
    if failures and not rows:
        raise RuntimeError(
            f"all {len(failures)} scenarios failed; first error: "
            f"{failures[0]['error']}"
        )
    # The comparison gains a price_deck column ONLY when at least one
    # scenario names a deck, keeping deck-free batches bit-identical.
    columns = list(_COMPARISON_COLUMNS)
    if any(r.get("price_deck") for r in rows):
        columns.insert(1, "price_deck")
    else:
        for r in rows:
            r.pop("price_deck", None)
    comparison = pd.DataFrame(rows, columns=columns)
    # pandas .attrs is the side channel to the orchestrator; the sheet
    # writer surfaces it as a failed_scenarios sheet only when non-empty
    # so healthy batches stay bit-identical.
    comparison.attrs["failed_scenarios"] = failures
    return comparison


# ---------------------------------------------------------------------------
# Output + orchestration
# ---------------------------------------------------------------------------


def write_scenario_comparison_workbook(
    out_path: str | Path, comparison: pd.DataFrame,
    *, failures: list[dict[str, str]] | None = None,
) -> Path:
    """Write the scenario-comparison table to a styled workbook.

    ``failures`` (mid-batch solver-level casualties) land on a separate
    ``failed_scenarios`` sheet — added only when non-empty, so healthy
    batches keep the single-sheet layout bit-identical.
    """
    from .io import atomic_workbook_path
    from .io_style import style_workbook

    if failures is None:
        failures = list(comparison.attrs.get("failed_scenarios") or [])
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_workbook_path(out_path) as tmp_path, pd.ExcelWriter(
        tmp_path, engine="openpyxl",
    ) as writer:
        comparison.to_excel(writer, sheet_name="scenario_comparison", index=False)
        if failures:
            pd.DataFrame(failures, columns=["name", "error"]).to_excel(
                writer, sheet_name="failed_scenarios", index=False,
            )
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
    entries = raw["scenarios"]
    for pos, entry in enumerate(entries):
        # A malformed entry (a bare string from a missed '- name:' key,
        # a stray list, ...) was previously dropped silently, so the
        # batch ran with fewer scenarios than the file lists.
        if not isinstance(entry, dict):
            raise ValueError(
                f"{path}: scenarios[{pos}] is not a mapping "
                f"(got {type(entry).__name__}: {entry!r}); each entry "
                "must be a '- name: ...' block of overrides."
            )
    return list(entries)


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
    _collisions: list[str] = []
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
        if target == "balancing":
            # Canonicalise the documented bare shorthand to its dict form
            # ({'balancing_enabled': value}) at parse time, so it composes
            # with dotted ``balancing.<key>`` rows in either order AND
            # deep-merges correctly across ``inherits`` (a scalar/dict
            # cross would otherwise replace wholesale, silently dropping
            # the other side's overrides).
            if value is None:
                # A blank value cell would canonicalise to
                # balancing_enabled=None, which materialises as a blank
                # cell and re-parses to the sheet DEFAULT — silently
                # resetting an inherited/base enable instead of leaving
                # it alone.  Treat the row as inert, loudly.
                logger.warning(
                    "scenarios sheet: scenario %r has a bare 'balancing' "
                    "row with a blank value cell; the row is ignored "
                    "(base/inherited balancing settings are kept). Fill "
                    "the value cell to use it.",
                    current,
                )
                continue
            bucket = scn.setdefault("balancing", {})
            bucket["balancing_enabled"] = value
        elif "." in target:
            section, key = target.split(".", 1)
            existing = scn.get(section)
            if existing is not None and not isinstance(existing, dict):
                # A bare scalar and a dotted target on the same section
                # cannot coexist (only ``balancing`` has a documented
                # scalar shorthand, canonicalised above): silently skipping
                # either row would solve a DIFFERENT scenario than the
                # sheet describes while labelling the comparison row as
                # the requested one.
                _collisions.append(
                    f"scenario {current!r}: target {target!r} conflicts "
                    f"with the earlier bare {section!r} scalar override; "
                    f"use dotted '{section}.<key>' targets only."
                )
                continue
            bucket = scn.setdefault(section, {})
            bucket[key] = value
        else:
            existing = scn.get(target)
            if isinstance(existing, dict):
                _collisions.append(
                    f"scenario {current!r}: bare target {target!r} would "
                    f"overwrite the earlier dotted "
                    f"'{target}.<key>' override(s) for the same section; "
                    f"use dotted '{target}.<key>' targets only."
                )
                continue
            scn[target] = value
    if _collisions:
        # A DISABLED sheet is documented as inert ("a normal run
        # proceeds"), so its drafting mistakes must not kill the base
        # run — warn instead; an enabled batch fails fast.
        if enabled:
            raise ValueError("; ".join(_collisions))
        for _msg in _collisions:
            logger.warning(
                "scenarios sheet (disabled): %s (ignored because the "
                "sheet's enabled toggle is FALSE).", _msg,
            )
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
        with pd.ExcelFile(path) as _xl:
            sheets = set(_xl.sheet_names)
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
    # The materialised base workbook is fully in ``base_typed`` now and each
    # scenario re-materialises its own; drop the temp dir so it does not leak.
    shutil.rmtree(tmp, ignore_errors=True)
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

    from .io import ensure_writable_outdir

    # Fail on an unusable --outdir BEFORE the solve loop: the output
    # directory was previously first touched after every scenario had
    # solved, so a path collision or read-only filesystem discarded the
    # entire batch at the very end.
    ensure_writable_outdir(Path(config.outdir))

    apply_ieee_style()
    comparison = run_scenario_batch(
        base_typed, scenarios, solver_opts=solver_opts,
        base_dir=src.parent,
    )
    result = ScenarioResult(comparison=comparison)

    from .io import unique_output_dir

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = unique_output_dir(
        Path(config.outdir) / f"{src.stem}_scenarios_{stamp}",
    )
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
        # comparison_sheet may be a fresh concat (risk rows), which
        # does not reliably carry .attrs — hand the failures over
        # explicitly from the batch frame.
        failures=list(comparison.attrs.get("failed_scenarios") or []),
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
