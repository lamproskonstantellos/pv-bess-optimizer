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


# --- round-12: the never-stricter contract, closed over the full matrix ----


def test_validate_config_accepts_all_loader_accepted_value_classes():
    """Round 11 closed integral floats / bool tokens / enum case; the
    round-12 sweep found five more loader-accepted classes the validator
    still flagged.  Each pair below LOADS successfully, so validate_config
    must not flag it."""
    from pvbess_opt.io_read import validate_config

    cases = {
        # numeric strings on number / integer keys
        "economics": {"discount_rate_pct": "7.5"},
        "project": {"project_lifecycle_years": "20"},
        # non-0/1 numeric on a boolean key; blank-string sentinel on enum
        "ppa": {"ppa_enabled": 1.0},
        # numbers on free-form string keys (a year-named scenario)
    }
    for section, kv in cases.items():
        errs = [e for e in validate_config({section: kv})
                if any(k in e for k in kv)]
        assert errs == [], (section, kv, errs)
    # blank-string / NaN "use the default" sentinels
    assert [e for e in validate_config({"project": {"mode": ""}})
            if "mode" in e] == []
    assert [e for e in validate_config(
        {"ppa": {"ppa_enabled": float("nan")}}) if "ppa_enabled" in e] == []
    # a bare section header (YAML ``simulation:`` -> None)
    assert [e for e in validate_config({"simulation": None})
            if "simulation" in e] == []
    # numbers on string keys
    # Schema-present free-form string keys (a year-named deck/scenario):
    # probe keys in their OWN sections — an unknown key is skipped before
    # any type check and would make this lock vacuous.
    errs = validate_config({"economics": {"debt_sizing_deck": 2030}})
    assert [e for e in errs if "debt_sizing_deck" in e] == [], errs
    errs = validate_config(
        {"scenario_engine": {"debt_sizing_scenario": 2030}}
    )
    assert [e for e in errs if "debt_sizing_scenario" in e] == [], errs


def test_validate_config_still_flags_loader_rejected_values():
    """The relaxation must not overshoot: values the LOADER rejects stay
    flagged (non-finite numerics; a list on a scalar-or-null key; genuine
    garbage)."""
    from pvbess_opt.io_read import validate_config

    errs = validate_config({"economics": {"discount_rate_pct": float("inf")}})
    assert any("discount_rate_pct" in e for e in errs), errs
    errs = validate_config({"project": {"p_grid_export_max_kw": [1, 2]}})
    assert any("p_grid_export_max_kw" in e for e in errs), errs
    errs = validate_config({"project": {"mode": "sideways"}})
    assert any("mode" in e for e in errs), errs
    errs = validate_config({"ppa": {"ppa_enabled": "banana"}})
    assert any("ppa_enabled" in e for e in errs), errs


def test_blank_top_level_timeseries_path_falls_back_to_pv_section(tmp_path):
    """``timeseries_path: ''`` (a blank cell exported to YAML) is the same
    "unset" intent as an absent key: the pv-section file must still win the
    frame instead of a 'Config provides no time-series' failure."""
    import numpy as np

    from pvbess_opt.io_read import load_structured_config

    pv_csv = tmp_path / "pv.csv"
    idx = pd.date_range("2026-01-01", periods=24, freq="h")
    pd.DataFrame({
        "timestamp": idx,
        "pv_kwh": np.linspace(0.0, 23.0, 24),
        "load_kwh": [5.0] * 24,
        "dam_price_eur_per_mwh": [60.0] * 24,
    }).to_csv(pv_csv, index=False)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "timeseries_path: ''\n"
        "project:\n  mode: merchant\n"
        f"pv:\n  timeseries_path: {pv_csv.name}\n"
        "  pv_nameplate_kwp: 10\n"
        "bess:\n  bess_power_kw: 10\n  bess_capacity_kwh: 20\n",
        encoding="utf-8",
    )
    typed = load_structured_config(cfg)
    assert len(typed["ts"]) == 24
    assert float(typed["ts"]["pv_kwh"].iloc[-1]) == 23.0
    # The frame-source flag must agree with the fallback: the pv path fed
    # the FRAME, so it must not leak into the typed pv sheet (a surviving
    # path misfires the loud column-vs-file conflict warning on every
    # load of the materialised workbook).
    assert not typed["pv"].get("timeseries_path")


# --- round-13 guard refinements --------------------------------------------


def test_validate_config_union_numerics_route_numerically():
    """A numeric value must satisfy a type union via its NUMERIC member —
    falling through to a "string" member let 20.5 / inf pass the
    ["integer", "string"] key whose loader demands a whole year."""
    from pvbess_opt.io_read import validate_config

    for bad in (20.5, float("inf"), float("-inf")):
        errs = validate_config({"bess": {"bess_replacement_year": bad}})
        assert any("bess_replacement_year" in e for e in errs), (bad, errs)
    for ok in (20, 20.0, "3", None):
        errs = [e for e in validate_config(
            {"bess": {"bess_replacement_year": ok}})
            if "bess_replacement_year" in e]
        assert errs == [], (ok, errs)
    # Nullable grid caps: the loader parses non-finite numerics AS the
    # documented uncapped sentinel (inf), so the validator must accept
    # them there (the shipped workbook's typed dict carries inf).
    for ok in (float("inf"), 5000.0, None, "none"):
        errs = [e for e in validate_config(
            {"project": {"p_grid_import_max_kw": ok}})
            if "p_grid_import_max_kw" in e]
        assert errs == [], (ok, errs)


def test_materialize_to_xlsx_drops_resolved_pv_path(tmp_path, caplog):
    """The materialised temp workbook carries the resolved pv_kwh column
    verbatim, so a surviving pv.timeseries_path would point at a path
    relative to the ORIGINAL config dir and misfire the loud column-vs-file
    conflict warning on every read of the materialised workbook."""
    import logging

    import numpy as np

    from pvbess_opt.io import read_inputs
    from pvbess_opt.io_read import materialize_to_xlsx

    idx = pd.date_range("2026-01-01", periods=24, freq="h")
    (tmp_path / "pv_only.csv").write_text(
        "timestamp,pv_kwh\n" + "\n".join(
            f"{t},{v}"
            for t, v in zip(idx, np.linspace(0, 23, 24), strict=True)
        ), encoding="utf-8",
    )
    pd.DataFrame({
        "timestamp": idx, "load_kwh": [5.0] * 24,
        "dam_price_eur_per_mwh": [60.0] * 24,
    }).to_csv(tmp_path / "frame.csv", index=False)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "timeseries_path: frame.csv\n"
        "project:\n  mode: merchant\n"
        "pv:\n  timeseries_path: pv_only.csv\n"
        "  pv_nameplate_kwp: 10\n"
        "bess:\n  bess_power_kw: 10\n  bess_capacity_kwh: 20\n",
        encoding="utf-8",
    )
    dst = materialize_to_xlsx(cfg, tmp_path / "out")
    pv_sheet = pd.read_excel(dst, sheet_name="pv")
    row = pv_sheet[pv_sheet["key"] == "timeseries_path"]
    assert row["value"].isna().all(), "materialised path cell must be blank"
    with caplog.at_level(logging.WARNING):
        _params, ts = read_inputs(dst)
    assert not any(
        "IGNORED" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]
    assert float(ts["pv_kwh"].iloc[-1]) == 23.0


# ---------------------------------------------------------------------------
# Config-surface parity guards (silent bool coercions, .inf, lists, blanks).
# The workbook sheet layer rejects booleans in numeric fields one layer
# above _parse_value, so the YAML/JSON path — which calls the key parsers
# directly — needs the same guards inside the special-routed parsers.
# ---------------------------------------------------------------------------


def _config_from_shipped(tmp_path, mutate):
    """Dump the shipped workbook to YAML, mutate the raw mapping, save."""
    import yaml

    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    typed["ts"] = typed["ts"].iloc[:24].reset_index(drop=True)
    cfg = tmp_path / "cfg.yaml"
    dump_structured_config(typed, cfg)
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    mutate(raw)
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return cfg


def test_config_bool_grid_cap_rejected_named(tmp_path):
    """`p_grid_export_max_kw: true` previously loaded as a silent 1.0-kW
    cap that strangled the plant; both cap keys now reject booleans."""
    cfg = _config_from_shipped(
        tmp_path, lambda raw: raw["project"].update(
            p_grid_export_max_kw=True,
        ),
    )
    with pytest.raises(ValueError, match=r"p_grid_export_max_kw.*boolean"):
        load_structured_config(cfg)


def test_config_bool_replacement_year_rejected_named(tmp_path):
    """`bess_replacement_year: true` previously loaded as a silent year-1
    replacement (float(True) == 1)."""
    cfg = _config_from_shipped(
        tmp_path, lambda raw: raw["bess"].update(bess_replacement_year=True),
    )
    with pytest.raises(ValueError, match=r"bess_replacement_year.*boolean"):
        load_structured_config(cfg)


def test_config_bool_tilt_and_weather_year_rejected_named():
    from pvbess_opt.io import _parse_value

    with pytest.raises(ValueError, match=r"tilt.*boolean"):
        _parse_value("tilt", True, "optimal")
    with pytest.raises(ValueError, match=r"weather_year.*boolean"):
        _parse_value("weather_year", True, 2019)


def test_config_inf_augmentation_years_named(tmp_path):
    """`bess_augmentation_years: .inf` previously died in int(inf) with a
    bare OverflowError before the round-14 named guard could fire."""
    import yaml

    cfg = _config_from_shipped(
        tmp_path,
        lambda raw: raw["bess"].update(bess_augmentation_years=None),
    )
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    raw["bess"]["bess_augmentation_years"] = float("inf")
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8")
    # The load keeps the token; the run path (materialise -> read ->
    # validate) names the key instead of dying in int(inf) with a bare
    # OverflowError.
    from pvbess_opt.io_read import materialize_to_xlsx

    loaded = load_structured_config(cfg)
    assert loaded["bess"]["bess_augmentation_years"] == "inf"
    wb = materialize_to_xlsx(cfg, tmp_path)
    with pytest.raises(ValueError, match="bess_augmentation_years"):
        read_workbook(wb)


def test_config_augmentation_list_form_loads(tmp_path):
    """A native YAML list — the natural config form — normalises to the
    canonical CSV instead of stringifying to '[8, 15]' garbage."""
    cfg = _config_from_shipped(
        tmp_path,
        lambda raw: raw["bess"].update(bess_augmentation_years=[8, 15]),
    )
    loaded = load_structured_config(cfg)
    assert loaded["bess"]["bess_augmentation_years"] == "8,15"
    assert validate_config(
        {"bess": {"bess_augmentation_years": [8, 15]}},
    ) == []


def test_config_augmentation_bool_and_date_rejected_named():
    import datetime

    from pvbess_opt.io import _parse_value

    with pytest.raises(ValueError, match=r"bess_augmentation_years.*boolean"):
        _parse_value("bess_augmentation_years", True, None)
    with pytest.raises(ValueError, match=r"bess_augmentation_years.*date"):
        _parse_value(
            "bess_augmentation_years", datetime.datetime(2026, 1, 1), None,
        )


def test_financing_and_grid_blocks_blank_means_absent(tmp_path, caplog):
    """A `gearing:` stub (None) or '' keeps the economics default with a
    warning instead of dying in float(None) unnamed; a non-blank value
    that will not parse is named."""
    cfg = _config_from_shipped(
        tmp_path,
        lambda raw: raw.update(
            financing={"gearing": None, "interest_rate": ""},
            grid={"co2_intensity": ""},
        ),
    )
    with caplog.at_level(logging.WARNING):
        loaded = load_structured_config(cfg)
    blanks = [r.getMessage() for r in caplog.records if "is blank" in r.getMessage()]
    assert len(blanks) == 3
    # The economics defaults survive untouched.
    base = read_workbook(ROOT / "inputs" / "input.xlsx")
    assert loaded["economics"]["gearing_pct"] == base["economics"]["gearing_pct"]

    cfg2 = _config_from_shipped(
        tmp_path, lambda raw: raw.update(financing={"gearing": "half"}),
    )
    with pytest.raises(ValueError, match=r"gearing.*'half'"):
        load_structured_config(cfg2)


def test_validate_config_accepts_np_bool_and_scalar_string_keys():
    """Parity: forms the loader accepts must pass validate_config —
    np.bool_ on boolean keys and non-string scalars on free-form string
    keys were rejected."""
    import numpy as np

    assert validate_config(
        {"bess": {"terminal_soc_equal": np.bool_(True)}},
    ) == []
    assert validate_config(
        {"scenario_engine": {"debt_sizing_scenario": 2030}},
    ) == []


def test_financing_grid_blocks_reject_bool_inf_and_truncation():
    """Round-2 regression: the convenience blocks' number parser accepted
    booleans (gearing: true -> silent 100 % debt), non-finite values and
    fractional tenors (int(15.5) == 15, silent truncation)."""
    import numpy as np

    from pvbess_opt.io import ECONOMICS_SHEET_DEFAULTS
    from pvbess_opt.io_read import _apply_financing_block, _apply_grid_block

    def probe(fin=None, grid=None):
        typed = {"economics": dict(ECONOMICS_SHEET_DEFAULTS)}
        raw = {}
        if fin is not None:
            raw["financing"] = fin
        if grid is not None:
            raw["grid"] = grid
        _apply_financing_block(raw, typed)
        _apply_grid_block(raw, typed)
        return typed["economics"]

    with pytest.raises(ValueError, match=r"'gearing'.*boolean True"):
        probe(fin={"gearing": True})
    with pytest.raises(ValueError, match=r"'interest_rate'.*finite"):
        probe(fin={"interest_rate": float("inf")})
    with pytest.raises(ValueError, match=r"'tenor_years'.*whole number"):
        probe(fin={"tenor_years": 15.5})
    with pytest.raises(ValueError, match=r"'co2_intensity'.*boolean"):
        probe(grid={"co2_intensity": True})
    with pytest.raises(ValueError, match=r"'co2_annual_decline'.*boolean"):
        probe(grid={"co2_annual_decline": np.bool_(True)})
    # NaN is the workbook blank sentinel: default kept, not NaN delivered.
    econ = probe(fin={"gearing": float("nan")})
    assert econ["gearing_pct"] == ECONOMICS_SHEET_DEFAULTS["gearing_pct"]
    # Legitimate values still map.
    econ = probe(fin={"gearing": 0.65, "tenor_years": 12.0})
    assert econ["gearing_pct"] == pytest.approx(65.0)
    assert econ["debt_tenor_years"] == 12


def test_scenario_resolve_years_accepts_native_list():
    """A native YAML/JSON list previously stringified to '[1, 5, 10]'
    garbage that validate_config accepted and parse_support_years
    rejected at run time (the bess_augmentation_years precedent)."""
    from pvbess_opt.io import _parse_value
    from pvbess_opt.pricedata.resolve import parse_support_years

    token = _parse_value("scenario_resolve_years", [1, 5, 10], None)
    assert token == "1,5,10"
    assert parse_support_years(token, 20) == [1, 5, 10]
    # CSV / scalar / blank forms unchanged.
    assert _parse_value("scenario_resolve_years", "1,5,10", None) == "1,5,10"
    assert _parse_value("scenario_resolve_years", 5.0, None) == "5"
    assert _parse_value("scenario_resolve_years", None, "1,5") == "1,5"
    with pytest.raises(ValueError, match="scenario_resolve_years.*boolean"):
        _parse_value("scenario_resolve_years", True, None)
