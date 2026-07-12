"""Intraday (IDA) input surface — sheet registry, loader gates, overrides.

Covers the data plumbing of the intraday venue (Eq. I1): the optional
``intraday`` workbook sheet, the ``ida_price_eur_per_mwh`` timeseries
column requirement, the zero-default bit-identity contract, and the
scenario / YAML override propagation.  The two-stage re-dispatch itself
is covered by the dispatch and economics suites.
"""

from __future__ import annotations

import logging

import openpyxl
import pandas as pd
import pytest

from pvbess_opt import scenarios as scn_mod
from pvbess_opt.io import (
    INTRADAY_SHEET_DEFAULTS,
    _typed_to_flat,
    read_workbook,
    validate_workbook_params,
    write_workbook,
)
from pvbess_opt.io_read import dump_structured_config, load_structured_config
from pvbess_opt.rolling_horizon import PRICE_COLUMNS

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_typed(n: int = 24, *, with_ida_column: bool = False) -> dict:
    from pvbess_opt.io import (
        BESS_SHEET_DEFAULTS,
        ECONOMICS_SHEET_DEFAULTS,
        PROJECT_SHEET_DEFAULTS,
        PV_SHEET_DEFAULTS,
        SIMULATION_SHEET_DEFAULTS,
    )

    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "pv_kwh": [100.0] * n,
        "load_kwh": [50.0] * n,
        "dam_price_eur_per_mwh": [80.0] * n,
    })
    if with_ida_column:
        ts["ida_price_eur_per_mwh"] = [85.0] * n
    return {
        "ts": ts,
        "project": dict(PROJECT_SHEET_DEFAULTS),
        "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=1000.0),
        "bess": dict(
            BESS_SHEET_DEFAULTS, bess_power_kw=500.0,
            bess_capacity_kwh=2000.0,
        ),
        "economics": dict(ECONOMICS_SHEET_DEFAULTS),
        "simulation": dict(SIMULATION_SHEET_DEFAULTS),
    }


def _write_workbook_without_intraday_sheet(typed: dict, dst) -> None:
    write_workbook(typed, dst)
    wb = openpyxl.load_workbook(dst)
    del wb["intraday"]
    wb.save(dst)


# ---------------------------------------------------------------------------
# Sheet registry + zero-default bit-identity
# ---------------------------------------------------------------------------


def test_intraday_sheet_absent_falls_back_to_defaults(tmp_path):
    dst = tmp_path / "no_intraday.xlsx"
    _write_workbook_without_intraday_sheet(_minimal_typed(), dst)
    typed = read_workbook(dst)
    assert typed["intraday"] == dict(INTRADAY_SHEET_DEFAULTS)


def test_sheet_absent_vs_defaults_bit_identical(tmp_path):
    """A workbook without the sheet and one carrying it at defaults
    produce identical typed sections and identical flat ``(params, ts)``.
    """
    dst_with = tmp_path / "with_sheet.xlsx"
    dst_without = tmp_path / "without_sheet.xlsx"
    write_workbook(_minimal_typed(), dst_with)
    _write_workbook_without_intraday_sheet(_minimal_typed(), dst_without)

    typed_with = read_workbook(dst_with)
    typed_without = read_workbook(dst_without)
    for section in (
        "project", "pv", "bess", "economics", "simulation",
        "balancing", "ppa", "intraday",
    ):
        assert typed_with[section] == typed_without[section], section

    params_with, ts_with = _typed_to_flat(typed_with)
    params_without, ts_without = _typed_to_flat(typed_without)
    assert params_with["intraday"] == params_without["intraday"]
    assert params_with["intraday"] == dict(INTRADAY_SHEET_DEFAULTS)
    pd.testing.assert_frame_equal(ts_with, ts_without)


def test_params_carry_intraday_section(tmp_path):
    dst = tmp_path / "wb.xlsx"
    write_workbook(_minimal_typed(), dst)
    params, _ts = _typed_to_flat(read_workbook(dst))
    assert params["intraday"]["id_enabled"] is False
    assert params["intraday"]["id_max_deviation_frac_of_cap"] == 0.25
    assert params["intraday"]["id_allow_purchases"] is True


# ---------------------------------------------------------------------------
# Loader gates
# ---------------------------------------------------------------------------


def test_id_enabled_requires_ida_price_column(tmp_path):
    typed = _minimal_typed()
    typed["project"] = dict(typed["project"])
    typed["project"]["mode"] = "merchant"
    typed["intraday"] = dict(INTRADAY_SHEET_DEFAULTS, id_enabled=True)
    dst = tmp_path / "missing_col.xlsx"
    write_workbook(typed, dst)
    with pytest.raises(ValueError, match="ida_price_eur_per_mwh"):
        read_workbook(dst)


def test_id_enabled_with_column_loads_and_logs_cadence(tmp_path, caplog):
    typed = _minimal_typed(with_ida_column=True)
    typed["project"] = dict(typed["project"])
    typed["project"]["mode"] = "merchant"
    typed["intraday"] = dict(INTRADAY_SHEET_DEFAULTS, id_enabled=True)
    dst = tmp_path / "hourly.xlsx"
    write_workbook(typed, dst)
    with caplog.at_level(logging.INFO, logger="pvbess_opt.io"):
        out = read_workbook(dst)
    assert out["intraday"]["id_enabled"] is True
    assert any(
        "period-averaged" in rec.message for rec in caplog.records
    ), "hourly cadence must log the period-averaging note"


@pytest.mark.parametrize("bad_value", [-0.1, 1.5])
def test_deviation_fraction_out_of_range_rejected(bad_value):
    typed = _minimal_typed()
    typed["intraday"] = dict(
        INTRADAY_SHEET_DEFAULTS,
        id_max_deviation_frac_of_cap=bad_value,
    )
    with pytest.raises(ValueError, match="id_max_deviation_frac_of_cap"):
        validate_workbook_params(typed, dt_minutes=60)


def test_negative_fee_rejected():
    typed = _minimal_typed()
    typed["intraday"] = dict(INTRADAY_SHEET_DEFAULTS, id_fee_eur_per_mwh=-1.0)
    with pytest.raises(ValueError, match="id_fee_eur_per_mwh"):
        validate_workbook_params(typed, dt_minutes=60)


def _enabled_typed(**project_overrides) -> dict:
    """Merchant typed dict with the venue armed (v1-gate fixtures)."""
    typed = _minimal_typed(with_ida_column=True)
    typed["project"] = dict(typed["project"])
    typed["project"]["mode"] = "merchant"
    typed["project"].update(project_overrides)
    typed["intraday"] = dict(INTRADAY_SHEET_DEFAULTS, id_enabled=True)
    return typed


def test_id_enabled_requires_merchant_mode():
    typed = _enabled_typed(mode="self_consumption")
    with pytest.raises(ValueError, match="mode = 'merchant'"):
        validate_workbook_params(typed, dt_minutes=60)


def test_id_enabled_requires_finite_export_cap():
    typed = _enabled_typed(p_grid_export_max_kw=float("inf"))
    with pytest.raises(ValueError, match="finite positive"):
        validate_workbook_params(typed, dt_minutes=60)


def test_id_enabled_excludes_balancing():
    typed = _enabled_typed()
    from pvbess_opt.io import BALANCING_SHEET_DEFAULTS

    typed["balancing"] = dict(
        BALANCING_SHEET_DEFAULTS, balancing_enabled=True,
    )
    with pytest.raises(ValueError, match="balancing_enabled"):
        validate_workbook_params(typed, dt_minutes=60)


def test_id_enabled_excludes_ppa_and_support():
    from pvbess_opt.io import PPA_SHEET_DEFAULTS

    typed = _enabled_typed()
    typed["ppa"] = dict(PPA_SHEET_DEFAULTS, ppa_enabled=True)
    with pytest.raises(ValueError, match="ppa_enabled"):
        validate_workbook_params(typed, dt_minutes=60)

    typed = _enabled_typed()
    typed["ppa"] = dict(
        PPA_SHEET_DEFAULTS,
        support_scheme="sliding_fip",
        support_strike_eur_per_mwh=60.0,
    )
    with pytest.raises(ValueError, match="support_scheme"):
        validate_workbook_params(typed, dt_minutes=60)


def test_id_enabled_excludes_uncertainty_and_midlife():
    typed = _enabled_typed()
    typed["simulation"] = dict(
        typed["simulation"], uncertainty_enabled=True,
    )
    with pytest.raises(ValueError, match="uncertainty_enabled"):
        validate_workbook_params(typed, dt_minutes=60)

    typed = _enabled_typed()
    typed["simulation"] = dict(typed["simulation"], midlife_resolve_year=8)
    with pytest.raises(ValueError, match="midlife_resolve_year"):
        validate_workbook_params(typed, dt_minutes=60)


def test_ida_price_deck_variant_column_accepted(tmp_path):
    """``ida_price_eur_per_mwh__<deck>`` passes the deck base-name gate."""
    typed = _minimal_typed(with_ida_column=True)
    typed["ts"]["ida_price_eur_per_mwh__low"] = 60.0
    dst = tmp_path / "deck.xlsx"
    write_workbook(typed, dst)
    out = read_workbook(dst)
    assert "ida_price_eur_per_mwh__low" in out["ts"].columns


# ---------------------------------------------------------------------------
# Overrides — scenarios (dotted targets) and YAML round-trip
# ---------------------------------------------------------------------------


def test_scenario_dotted_override_reaches_intraday_sheet(tmp_path):
    dst = tmp_path / "base.xlsx"
    write_workbook(_minimal_typed(with_ida_column=True), dst)
    typed = read_workbook(dst)

    scenario = {
        "name": "ida_on",
        "intraday": {
            "id_enabled": True,
            "id_fee_eur_per_mwh": 0.15,
        },
    }
    scn_mod.validate_scenario_overrides(scenario)
    overridden = scn_mod._apply_scenario_overrides(typed, scenario)
    assert overridden["intraday"]["id_enabled"] is True
    assert overridden["intraday"]["id_fee_eur_per_mwh"] == 0.15
    # The base dict is untouched (overrides operate on a deep copy).
    assert typed["intraday"]["id_enabled"] is False


def test_yaml_round_trip_preserves_intraday_values(tmp_path):
    dst = tmp_path / "wb.xlsx"
    typed = _minimal_typed(with_ida_column=True)
    typed["project"] = dict(typed["project"])
    typed["project"]["mode"] = "merchant"
    typed["intraday"] = dict(
        INTRADAY_SHEET_DEFAULTS,
        id_enabled=True,
        id_max_deviation_frac_of_cap=0.4,
        id_fee_eur_per_mwh=0.12,
        id_inflation_pct=1.5,
    )
    write_workbook(typed, dst)

    yaml_path = tmp_path / "config.yaml"
    dump_structured_config(read_workbook(dst), yaml_path)
    loaded = load_structured_config(yaml_path)
    assert loaded["intraday"]["id_enabled"] is True
    assert loaded["intraday"]["id_max_deviation_frac_of_cap"] == 0.4
    assert loaded["intraday"]["id_fee_eur_per_mwh"] == 0.12
    assert loaded["intraday"]["id_inflation_pct"] == 1.5


# ---------------------------------------------------------------------------
# Rolling-horizon price contract
# ---------------------------------------------------------------------------


def test_ida_price_registered_as_noisable_price():
    """The IDA price is a forecastable market price: it must live in
    PRICE_COLUMNS (noise-eligible + actuals-restored), NOT in the
    actuals-only imbalance set.
    """
    from pvbess_opt.rolling_horizon import IMBALANCE_PRICE_COLUMNS

    assert "ida_price_eur_per_mwh" in PRICE_COLUMNS
    assert "ida_price_eur_per_mwh" not in IMBALANCE_PRICE_COLUMNS
