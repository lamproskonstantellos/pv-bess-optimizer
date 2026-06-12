"""Input-surface parity locks: workbook == YAML/JSON == scenario targets.

The three configuration surfaces are defined to be exact mirrors of one
another, all flowing through the same read path
(``load_structured_config`` / scenario overrides materialize to a real
workbook that re-enters ``read_inputs``):

* every key in the seven kv-sheet defaults tables has a row in the
  shipped workbook, in template order;
* a YAML/JSON config accepts exactly the same section/key pairs, warns
  on (and ignores) unknown or misplaced keys exactly like the workbook
  loader, and mirrors the absent-row semantics of
  ``bess_degradation_pct_per_cycle`` (absent -> 0.0, calendar-only);
* every ``<sheet>.<key>`` dotted target resolves as a scenario
  override (plus the documented aliases and bare specials), and an
  unknown target raises instead of silently producing a comparison row
  identical to the base case.

These tests derive each surface programmatically from the live schema
tables so the three surfaces cannot drift apart unnoticed.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest
import yaml

from pvbess_opt.io import _SHEET_DEFAULTS, _SHEET_ROW_TEMPLATES
from pvbess_opt.io_read import load_structured_config
from pvbess_opt.scenarios import (
    _BESS_ALIASES,
    _PV_ALIASES,
    _parse_scenarios_sheet,
    read_scenarios_file,
    resolve_inheritance,
    validate_scenario_overrides,
)

ROOT = Path(__file__).resolve().parent.parent
WORKBOOK = ROOT / "inputs" / "input.xlsx"

# Two 15-minute steps: the minimum inline timeseries the loader accepts.
_TS_INLINE = [
    {
        "timestamp": "2026-01-01 00:00",
        "pv_kwh": 1.0,
        "load_kwh": 2.0,
        "dam_price_eur_per_mwh": 50.0,
    },
    {
        "timestamp": "2026-01-01 00:15",
        "pv_kwh": 1.5,
        "load_kwh": 2.0,
        "dam_price_eur_per_mwh": 55.0,
    },
]


def _write_yaml(tmp_path: Path, raw: dict) -> Path:
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return cfg


def _full_sections() -> dict:
    """A config carrying EVERY schema key at its canonical default."""
    return {sec: dict(defaults) for sec, defaults in _SHEET_DEFAULTS.items()}


# ---------------------------------------------------------------------------
# Surface 1: the shipped workbook
# ---------------------------------------------------------------------------


def test_shipped_workbook_rows_match_templates_exactly():
    """Every kv sheet of inputs/input.xlsx carries exactly the template
    keys, in template order — the workbook exposes the full surface."""
    for sheet, rows in _SHEET_ROW_TEMPLATES.items():
        frame = pd.read_excel(WORKBOOK, sheet_name=sheet)
        sheet_keys = [
            str(k).strip() for k in frame["key"].tolist()
            if isinstance(k, str) and str(k).strip()
            and not str(k).strip().startswith("#")
        ]
        template_keys = [r[0] for r in rows]
        assert sheet_keys == template_keys, (
            f"{sheet}: shipped workbook rows diverge from the template "
            f"(workbook {sheet_keys} vs template {template_keys})"
        )


# ---------------------------------------------------------------------------
# Surface 2: YAML/JSON structured config
# ---------------------------------------------------------------------------


def test_yaml_accepts_every_workbook_key_without_warning(tmp_path, caplog):
    raw: dict = _full_sections()
    raw["timeseries"] = _TS_INLINE
    cfg = _write_yaml(tmp_path, raw)
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io_read"):
        typed = load_structured_config(cfg)
    assert not [
        r for r in caplog.records if "unknown" in r.getMessage().lower()
    ], "schema keys must never trigger the unknown-key warning"
    for sec, defaults in _SHEET_DEFAULTS.items():
        assert set(typed[sec]) == set(defaults), f"section {sec!r}"


def test_yaml_unknown_key_warns_and_is_ignored(tmp_path, caplog):
    raw: dict = _full_sections()
    raw["timeseries"] = _TS_INLINE
    raw["project"]["definitely_not_a_key"] = 1
    cfg = _write_yaml(tmp_path, raw)
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io_read"):
        typed = load_structured_config(cfg)
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "definitely_not_a_key" in m and "unknown" in m.lower() for m in messages
    ), f"expected an unknown-key warning, got {messages}"
    assert "definitely_not_a_key" not in typed["project"]


def test_yaml_misplaced_key_warns_with_owning_section(tmp_path, caplog):
    """A key placed in the wrong section warns and names the owning
    sheet, mirroring the workbook loader's misplaced-key warning."""
    raw: dict = _full_sections()
    raw["timeseries"] = _TS_INLINE
    raw["project"]["ppa_enabled"] = True
    del raw["project"]["mode"]  # keep the section otherwise canonical
    cfg = _write_yaml(tmp_path, raw)
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io_read"):
        typed = load_structured_config(cfg)
    messages = [r.getMessage() for r in caplog.records]
    assert any("ppa_enabled" in m and "ppa" in m for m in messages), messages
    # The misplaced value must NOT leak into either section.
    assert typed["ppa"]["ppa_enabled"] is False
    assert "ppa_enabled" not in typed["project"]


def test_yaml_unknown_top_level_section_warns(tmp_path, caplog):
    raw: dict = _full_sections()
    raw["timeseries"] = _TS_INLINE
    raw["ecnomics"] = {"discount_rate_pct": 9.0}  # typo'd section
    cfg = _write_yaml(tmp_path, raw)
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io_read"):
        typed = load_structured_config(cfg)
    messages = [r.getMessage() for r in caplog.records]
    assert any("ecnomics" in m for m in messages), messages
    assert typed["economics"]["discount_rate_pct"] == pytest.approx(7.0)


def test_yaml_absent_cycle_fade_mirrors_workbook_absent_row(tmp_path, caplog):
    """A YAML bess section without ``bess_degradation_pct_per_cycle``
    resolves to 0.0 (calendar-only fade) with an INFO log — the same
    semantics as a workbook that omits the row (io.read_workbook)."""
    raw: dict = _full_sections()
    raw["timeseries"] = _TS_INLINE
    del raw["bess"]["bess_degradation_pct_per_cycle"]
    cfg = _write_yaml(tmp_path, raw)
    with caplog.at_level(logging.INFO, logger="pvbess_opt.io_read"):
        typed = load_structured_config(cfg)
    assert typed["bess"]["bess_degradation_pct_per_cycle"] == 0.0
    assert any(
        "bess_degradation_pct_per_cycle" in r.getMessage()
        for r in caplog.records
    )
    # With the key present the supplied value is honoured unchanged.
    raw["bess"]["bess_degradation_pct_per_cycle"] = 0.008
    typed = load_structured_config(_write_yaml(tmp_path, raw))
    assert typed["bess"]["bess_degradation_pct_per_cycle"] == 0.008


# ---------------------------------------------------------------------------
# Surface 3: scenario dotted targets
# ---------------------------------------------------------------------------


def test_every_dotted_target_resolves_as_scenario_override():
    """``<sheet>.<key>`` is a valid scenario target for every schema key
    on every sheet, plus the documented aliases and bare specials."""
    for sheet, defaults in _SHEET_DEFAULTS.items():
        for key, value in defaults.items():
            scenario = {"name": "probe", sheet: {key: value}}
            validate_scenario_overrides(scenario)  # must not raise
    for alias in _PV_ALIASES:
        validate_scenario_overrides({"name": "probe", "pv": {alias: 1.0}})
    for alias in _BESS_ALIASES:
        validate_scenario_overrides({"name": "probe", "bess": {alias: 1.0}})
    validate_scenario_overrides({"name": "probe", "balancing": "on"})
    validate_scenario_overrides({"name": "probe", "capex_multiplier": 0.8})
    validate_scenario_overrides(
        {"name": "probe", "inherits": "base", "project": {"mode": "merchant"}}
    )


def test_unknown_scenario_override_key_raises():
    with pytest.raises(ValueError, match="discount_rate_pct_typo"):
        validate_scenario_overrides(
            {"name": "bad", "economics": {"discount_rate_pct_typo": 1.0}}
        )


def test_unknown_scenario_override_section_raises():
    with pytest.raises(ValueError, match="ecnomics"):
        validate_scenario_overrides(
            {"name": "bad", "ecnomics": {"discount_rate_pct": 1.0}}
        )


def test_bare_sheet_key_target_raises_with_sheet_hint():
    """A bare target that is really a sheet key (``ppa_enabled`` instead
    of ``ppa.ppa_enabled``) raises and names the owning sheet."""
    with pytest.raises(ValueError, match=r"ppa\.ppa_enabled"):
        validate_scenario_overrides({"name": "bad", "ppa_enabled": True})


def test_misplaced_scenario_key_raises_with_owning_sheet():
    with pytest.raises(ValueError, match=r"ppa\.ppa_enabled"):
        validate_scenario_overrides(
            {"name": "bad", "project": {"ppa_enabled": True}}
        )


# ---------------------------------------------------------------------------
# The shipped examples parse and validate on BOTH scenario surfaces
# ---------------------------------------------------------------------------


def test_shipped_workbook_scenarios_sheet_validates():
    frame = pd.read_excel(WORKBOOK, sheet_name="scenarios")
    enabled, scenarios = _parse_scenarios_sheet(frame)
    assert enabled is False, "the shipped example ships disabled"
    names = [s["name"] for s in scenarios]
    assert "Merchant hybrid + PPA" in names
    for scenario in resolve_inheritance(scenarios):
        validate_scenario_overrides(scenario)


def test_examples_scenarios_yaml_validates():
    scenarios = read_scenarios_file(ROOT / "examples" / "scenarios.yaml")
    names = [s["name"] for s in scenarios]
    assert "Merchant hybrid + PPA" in names
    for scenario in resolve_inheritance(scenarios):
        validate_scenario_overrides(scenario)
