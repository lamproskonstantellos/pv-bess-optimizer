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
_SECTIONS = (
    "project", "pv", "bess", "economics", "simulation", "balancing", "ppa",
    "intraday", "market_data", "scenario_engine",
)


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


def test_json_schema_declares_every_top_level_extra():
    """Every non-sheet top-level config key the loader accepts must be a
    declared property of the emitted JSON Schema, so the introspectable
    schema surface matches the load surface (regression: sizing /
    bm_merit_order / inline timeseries were accepted but undeclared)."""
    from pvbess_opt.io_read import _TOP_LEVEL_EXTRAS

    props = set(config_json_schema()["properties"])
    missing = sorted(k for k in _TOP_LEVEL_EXTRAS if k not in props)
    assert missing == [], f"config schema omits accepted keys: {missing}"


def test_json_schema_trajectories_description_lists_every_stream():
    """The config-schema trajectories description must advertise ALL
    stream names, including the Eq. E60/E61 split-stream taxonomy — it
    is the introspectable surface a user reads to discover them.
    """
    from pvbess_opt.io import TRAJECTORY_STREAMS

    desc = config_json_schema()["properties"]["trajectories"]["description"]
    for stream in TRAJECTORY_STREAMS:
        assert stream in desc, f"trajectory stream {stream!r} missing from schema"


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


def test_yaml_config_can_express_bm_merit_order_curve(tmp_path):
    """A config that enables the merit-order activation curve must be able to
    supply it: without the bm_merit_order surface, materialising the config
    to a workbook fails read_workbook with 'add the bm_merit_order sheet',
    guidance a config user cannot follow (there is no sheet to add)."""
    import yaml

    from pvbess_opt.io_read import materialize_to_xlsx

    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    cfg = tmp_path / "merit.yaml"
    dump_structured_config(typed, cfg)
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    raw["balancing"]["balancing_enabled"] = True
    raw["balancing"]["bm_merit_order_enabled"] = True
    raw["bm_merit_order"] = [
        {"product": "afrr_up", "price_eur_per_mwh": 50,
         "activation_probability_pct": 90},
        {"product": "afrr_up", "price_eur_per_mwh": 100,
         "activation_probability_pct": 40},
    ]
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8")

    loaded = load_structured_config(cfg)
    assert "bm_merit_order" in loaded
    # Materialise + re-read: the sheet is written and validated, no raise.
    wb = materialize_to_xlsx(cfg, tmp_path)
    reread = read_workbook(wb)
    assert reread["balancing"]["bm_merit_order_enabled"] is True
    assert reread["balancing"]["bm_merit_order_curve"] == {
        "afrr_up": [(50.0, 90.0), (100.0, 40.0)],
    }


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


def test_validate_config_is_never_stricter_than_the_loader():
    """The external-validation surface must accept every form the loader
    accepts: integral floats on integer keys (YAML/openpyxl deliver 20.0),
    0/1 and TRUTHY/FALSY tokens on booleans, case-insensitive enum tokens.
    Relaxation-only — the invalid forms must still be flagged."""
    from pvbess_opt.io_read import validate_config

    assert validate_config(
        {"project": {"project_lifecycle_years": 20.0}}
    ) == []
    assert validate_config({"ppa": {"ppa_enabled": 1}}) == []
    assert validate_config(
        {"simulation": {"uncertainty_enabled": "true"}}
    ) == []
    assert validate_config({"project": {"mode": "Merchant"}}) == []
    assert validate_config(
        {"simulation": {"uncertainty_n_seeds": 40.0}}
    ) == []
    # Still-invalid forms stay flagged.
    assert validate_config({"project": {"project_lifecycle_years": 20.7}})
    assert validate_config({"ppa": {"ppa_enabled": "banana"}})
    assert validate_config({"project": {"mode": "sideways"}})


def test_yaml_profile_list_error_names_the_key(tmp_path):
    """A non-numeric entry in the YAML max_injection_profile list must name
    the config key, not surface numpy's bare conversion error."""
    import numpy as np

    from pvbess_opt.io_read import load_structured_config

    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=48, freq="h"),
        "pv_kwh": np.full(48, 5.0),
        "load_kwh": np.full(48, 3.0),
        "dam_price_eur_per_mwh": np.full(48, 60.0),
    })
    ts.to_csv(tmp_path / "ts.csv", index=False)
    cfg = tmp_path / "bad_profile.yaml"
    cfg.write_text(
        "timeseries_path: ts.csv\n"
        "max_injection_profile: ["
        + ", ".join(["100.0"] * 23)
        + ", high]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="max_injection_profile"):
        load_structured_config(cfg)


def test_pv_timeseries_path_as_frame_source_stays_quiet(tmp_path, caplog):
    """A YAML config whose pv.timeseries_path IS the frame source (no
    top-level timeseries_path) must not fire the column-vs-file conflict
    warning — the frame's pv_kwh came from that very file.  The path must
    also not propagate into the materialised pv sheet."""
    import numpy as np

    from pvbess_opt.io_read import load_structured_config

    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=48, freq="h"),
        "pv_kwh": np.full(48, 5.0),
        "load_kwh": np.full(48, 3.0),
        "dam_price_eur_per_mwh": np.full(48, 60.0),
    })
    ts.to_csv(tmp_path / "ts.csv", index=False)
    cfg = tmp_path / "pvpath.yaml"
    cfg.write_text(
        "pv:\n"
        "  pv_nameplate_kwp: 1000\n"
        "  timeseries_path: ts.csv\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        typed = load_structured_config(cfg)
    assert not any(
        "IGNORED" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]
    assert not typed["pv"].get("timeseries_path")
    assert float(typed["ts"]["pv_kwh"].iloc[0]) == pytest.approx(5.0)
