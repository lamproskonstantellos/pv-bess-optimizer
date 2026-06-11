"""Structured (YAML/JSON) config: parse-identity, schema, validators.

A YAML config and the equivalent Excel workbook must parse to the same
typed dict and produce the same results.  The JSON Schema validates a
sample config; the PV consistency validator warns on a mismatched
nameplate.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest

from pvbess_opt.io import read_workbook
from pvbess_opt.io_read import (
    config_json_schema,
    dump_structured_config,
    load_structured_config,
    validate_config,
    validate_pv_consistency,
)

ROOT = Path(__file__).resolve().parent.parent
_SECTIONS = ("project", "pv", "bess", "economics", "simulation", "balancing", "ppa")


def _highs_available() -> bool:
    try:
        import highspy
    except ImportError:
        return False
    return bool(highspy)


def test_shipped_workbook_pv_source_is_auto():
    """The shipped workbook ships pv_source='auto' (it resolves to file mode
    because pv_kwh is filled and no location is set)."""
    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    assert typed["pv"]["pv_source"] == "auto"


def test_json_schema_validates_a_sample_config():
    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    raw = {sec: dict(typed[sec]) for sec in _SECTIONS}
    raw["timeseries_path"] = "ts.csv"
    schema = config_json_schema()
    assert schema["type"] == "object"
    assert validate_config(raw, schema) == []


def test_json_schema_rejects_bad_values():
    raw = {
        "pv": {"pv_source": "solar", "pv_nameplate_kwp": "lots"},
        "project": {"mode": "island"},
    }
    errors = validate_config(raw)
    assert any("pv_source" in e for e in errors)
    assert any("pv_nameplate_kwp" in e for e in errors)
    assert any("mode" in e for e in errors)


def test_yaml_round_trip_parses_identically(tmp_path):
    """Dump the workbook's typed dict to YAML+CSV and load it back; the
    parsed sections and time-series must be identical."""
    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    cfg = tmp_path / "run.yaml"
    dump_structured_config(typed, cfg)
    loaded = load_structured_config(cfg)
    for sec in _SECTIONS:
        assert loaded[sec] == typed[sec], f"section {sec!r} differs"
    pd.testing.assert_frame_equal(
        loaded["ts"].reset_index(drop=True),
        typed["ts"].reset_index(drop=True),
        check_dtype=False,
    )


def test_validate_pv_consistency_warns_on_divergence(caplog):
    pv = [0.5] * 8760  # ~4380 kWh/yr => implied ~3.65 kWp at 1200 kWh/kWp
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io_read"):
        implied = validate_pv_consistency(pv, nameplate_kwp=100.0)
    assert implied is not None and implied < 10.0
    assert any("PV consistency" in r.getMessage() for r in caplog.records)


def test_validate_pv_consistency_quiet_when_consistent(caplog):
    pv = [120000.0 / 8760] * 8760  # 120 MWh/yr => 100 kWp at 1200 kWh/kWp
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io_read"):
        implied = validate_pv_consistency(pv, nameplate_kwp=100.0)
    assert implied == pytest.approx(100.0, rel=1e-6)
    assert not [r for r in caplog.records if "PV consistency" in r.getMessage()]


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_yaml_config_runs_identically_to_excel(tmp_path):
    """`run` on a YAML config matches `run` on the equivalent workbook."""
    from pvbess_opt import RunConfig, run
    from pvbess_opt.io import write_workbook

    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)  # 1 day @ 15 min

    short_xlsx = tmp_path / "short.xlsx"
    write_workbook(typed, short_xlsx)
    cfg = tmp_path / "short.yaml"
    dump_structured_config(typed, cfg)

    common = dict(solver="highs", mip_gap=0.05, time_limit=180)
    res_xlsx = run(RunConfig(excel=short_xlsx, outdir=tmp_path / "x", **common))
    res_yaml = run(RunConfig(excel=cfg, outdir=tmp_path / "y", **common))

    assert set(res_xlsx.kpis) == set(res_yaml.kpis)
    for key, val in res_xlsx.kpis.items():
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        assert res_yaml.kpis[key] == pytest.approx(val, rel=1e-9, abs=1e-6), key
