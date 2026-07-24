"""The single PV-source resolution rule (auto | file | pvgis).

One presence-aware rule, shared by the Excel reader and the structured
config loader, decides where the PV profile comes from.  These tests pin
every row of the resolution table (PVGIS HTTP is mocked — no live
network), the Excel/YAML parity, the additive template, and the
location-field range checks.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.io import (
    BALANCING_SHEET_DEFAULTS,
    BESS_SHEET_DEFAULTS,
    ECONOMICS_SHEET_DEFAULTS,
    PROJECT_SHEET_DEFAULTS,
    PV_SHEET_DEFAULTS,
    SIMULATION_SHEET_DEFAULTS,
    read_workbook,
    validate_pv_location_fields,
    write_workbook,
)
from pvbess_opt.io_read import load_structured_config

ROOT = Path(__file__).resolve().parent.parent
REPO_INPUT_XLSX = ROOT / "inputs" / "input.xlsx"

HOURS = 8760


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _typed(
    *,
    n: int = HOURS,
    freq: str = "h",
    pv_kwh: str = "filled",
    pv_overrides: dict | None = None,
) -> dict:
    """A minimal merchant-mode typed dict for the resolver to chew on."""
    ts_data: dict = {
        "timestamp": pd.date_range("2019-01-01", periods=n, freq=freq),
        "dam_price_eur_per_mwh": np.full(n, 50.0),
    }
    if pv_kwh == "filled":
        ts_data["pv_kwh"] = np.full(n, 100.0)
    else:  # "empty" — column present, all blank
        ts_data["pv_kwh"] = np.full(n, np.nan)
    pv = dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=1000.0)
    if pv_overrides:
        pv.update(pv_overrides)
    return {
        "ts": pd.DataFrame(ts_data),
        "project": dict(PROJECT_SHEET_DEFAULTS, mode="merchant"),
        "pv": pv,
        "bess": dict(BESS_SHEET_DEFAULTS),
        "economics": dict(ECONOMICS_SHEET_DEFAULTS),
        "simulation": dict(SIMULATION_SHEET_DEFAULTS),
        "balancing": dict(BALANCING_SHEET_DEFAULTS),
    }


def _write(tmp_path: Path, typed: dict, name: str = "wb.xlsx") -> Path:
    out = tmp_path / name
    write_workbook(typed, out)
    return out


def _mock_pvgis(monkeypatch, *, per_kwp_value: float = 0.2) -> dict:
    """Patch the resource-layer fetch; return a dict capturing the call."""
    import pvbess_opt.resource as resource_pkg
    from pvbess_opt.resource.base import ProfileResult

    captured: dict = {}

    def fake_fetch(lat, lon, **opts):
        captured["lat"] = lat
        captured["lon"] = lon
        captured["opts"] = opts
        return ProfileResult(
            per_kwp_kwh=np.full(HOURS, per_kwp_value),
            metadata={"source": "pvgis"},
        )

    monkeypatch.setattr(resource_pkg, "fetch_pv_profile", fake_fetch)
    return captured


# ---------------------------------------------------------------------------
# Template integrity
# ---------------------------------------------------------------------------


def test_template_pv_sheet_exposes_source_and_location():
    pv = pd.read_excel(REPO_INPUT_XLSX, sheet_name="pv")
    keys = set(pv["key"].dropna())
    for key in (
        "pv_source", "latitude", "longitude", "tilt", "azimuth",
        "losses_pct", "weather_year", "timeseries_path",
    ):
        assert key in keys, f"missing pv-sheet row {key!r}"


def test_template_timeseries_has_single_pv_column():
    ts = pd.read_excel(REPO_INPUT_XLSX, sheet_name="timeseries", nrows=1)
    assert "pv_kwh" in ts.columns
    assert "pv_kwh_override" not in ts.columns


def test_raddatabase_is_excel_reachable_and_reaches_pvgis(tmp_path, monkeypatch):
    """The PVGIS radiation-database selector is a first-class pv-sheet input.

    It defaults to None, round-trips through a workbook write/read, the
    shipped template carries the row, and a value set on the pv sheet
    actually reaches the PVGIS fetch — closing the Excel/YAML asymmetry
    where ``raddatabase`` was consumable but not settable from Excel.
    """
    # Default is None (absent), and the shipped workbook carries the row.
    assert PV_SHEET_DEFAULTS["raddatabase"] is None
    shipped_keys = set(pd.read_excel(REPO_INPUT_XLSX, sheet_name="pv")["key"].dropna())
    assert "raddatabase" in shipped_keys

    # A value set on the pv sheet round-trips; a blank cell resolves to None.
    typed = _typed(pv_overrides={"raddatabase": "PVGIS-ERA5"})
    assert read_workbook(_write(tmp_path, typed))["pv"]["raddatabase"] == "PVGIS-ERA5"
    assert read_workbook(_write(tmp_path, _typed(), "blank.xlsx"))["pv"][
        "raddatabase"
    ] is None

    # An Excel-set value flows through resolve_pv_source into the PVGIS fetch.
    captured = _mock_pvgis(monkeypatch)
    typed_pvgis = _typed(
        pv_kwh="empty",
        pv_overrides={
            "pv_source": "pvgis", "latitude": 37.98, "longitude": 23.73,
            "raddatabase": "PVGIS-SARAH3",
        },
    )
    read_workbook(_write(tmp_path, typed_pvgis, "pvgis.xlsx"))
    assert captured["opts"].get("raddatabase") == "PVGIS-SARAH3"


# ---------------------------------------------------------------------------
# Resolution table — one test per row (§2.3)
# ---------------------------------------------------------------------------


def test_auto_filled_pv_kwh_uses_file(tmp_path):
    """auto + pv_kwh data + no location → file (used verbatim)."""
    out = _write(tmp_path, _typed(pv_kwh="filled"))
    loaded = read_workbook(out)
    assert float(loaded["ts"]["pv_kwh"].sum()) == pytest.approx(
        100.0 * HOURS, rel=1e-9,
    )


def test_auto_empty_pv_kwh_with_location_fetches_pvgis(tmp_path, monkeypatch):
    """auto + empty pv_kwh + location → PVGIS fetch."""
    cap = _mock_pvgis(monkeypatch, per_kwp_value=0.2)
    typed = _typed(
        pv_kwh="empty", pv_overrides={"latitude": 37.98, "longitude": 23.73},
    )
    loaded = read_workbook(_write(tmp_path, typed))
    assert cap["lat"] == pytest.approx(37.98)
    assert float(loaded["ts"]["pv_kwh"].sum()) == pytest.approx(
        0.2 * 1000.0 * HOURS,
    )


def test_auto_empty_pv_kwh_no_location_raises(tmp_path):
    """auto + empty pv_kwh + no location → a clear, actionable error."""
    out = _write(tmp_path, _typed(pv_kwh="empty"))
    with pytest.raises(ValueError, match="latitude"):
        read_workbook(out)


def test_auto_filled_pv_kwh_with_location_warns_and_keeps_file(
    tmp_path, monkeypatch, caplog,
):
    """auto + pv_kwh data + location → file wins, with a warning."""
    cap = _mock_pvgis(monkeypatch)
    typed = _typed(
        pv_kwh="filled", pv_overrides={"latitude": 37.98, "longitude": 23.73},
    )
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io_read"):
        loaded = read_workbook(_write(tmp_path, typed))
    assert "lat" not in cap, "PVGIS must not be fetched when the column wins"
    assert float(loaded["ts"]["pv_kwh"].sum()) == pytest.approx(
        100.0 * HOURS, rel=1e-9,
    )
    assert any("location is ignored" in r.getMessage() for r in caplog.records)


def test_file_source_empty_column_raises(tmp_path):
    """pv_source=file + empty column → error."""
    out = _write(tmp_path, _typed(pv_kwh="empty", pv_overrides={"pv_source": "file"}))
    with pytest.raises(ValueError, match="pv_source=file"):
        read_workbook(out)


def test_pvgis_source_no_location_raises(tmp_path):
    """pv_source=pvgis + no location → error."""
    out = _write(
        tmp_path, _typed(pv_kwh="filled", pv_overrides={"pv_source": "pvgis"}),
    )
    with pytest.raises(ValueError, match="latitude/longitude"):
        read_workbook(out)


def test_pvgis_source_ignores_filled_column_with_warning(
    tmp_path, monkeypatch, caplog,
):
    """pv_source=pvgis + location → PVGIS even when the column has data."""
    cap = _mock_pvgis(monkeypatch, per_kwp_value=0.2)
    typed = _typed(
        pv_kwh="filled",
        pv_overrides={
            "pv_source": "pvgis", "latitude": 37.98, "longitude": 23.73,
        },
    )
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io_read"):
        loaded = read_workbook(_write(tmp_path, typed))
    assert cap["lat"] == pytest.approx(37.98)
    assert float(loaded["ts"]["pv_kwh"].sum()) == pytest.approx(
        0.2 * 1000.0 * HOURS,
    )
    assert any(
        "that PV data is ignored" in r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# External timeseries_path (file sub-mode)
# ---------------------------------------------------------------------------


def test_excel_timeseries_path_sources_pv(tmp_path):
    n = 96
    external = pd.DataFrame({
        "timestamp": pd.date_range("2019-06-01", periods=n, freq="15min"),
        "pv_kwh": np.full(n, 7.0),
    })
    external.to_csv(tmp_path / "ext.csv", index=False)
    typed = _typed(
        n=n, freq="15min", pv_kwh="empty",
        pv_overrides={"timeseries_path": "ext.csv"},
    )
    loaded = read_workbook(_write(tmp_path, typed))
    # The external column is consumed verbatim (no rescale).
    assert float(loaded["ts"]["pv_kwh"].sum()) == pytest.approx(
        7.0 * n, rel=1e-9,
    )


def test_pv_column_wins_over_timeseries_path_with_loud_warning(
    tmp_path, caplog,
):
    """Both surfaces populated: the column wins by documented priority, but
    the ignored file must be named in a WARNING — silently running the wrong
    plant's profile is the failure mode (the workbook note reads 'instead of
    the pv_kwh column', so a client filling the path expects it to win)."""
    import logging

    n = 96
    external = pd.DataFrame({
        "timestamp": pd.date_range("2019-06-01", periods=n, freq="15min"),
        "pv_kwh": np.full(n, 7.0),
    })
    external.to_csv(tmp_path / "ext.csv", index=False)
    typed = _typed(
        n=n, freq="15min",  # pv_kwh column populated by the fixture
        pv_overrides={"timeseries_path": "ext.csv"},
    )
    with caplog.at_level(logging.WARNING):
        loaded = read_workbook(_write(tmp_path, typed))
    # The column value (not 7.0) is used ...
    assert float(loaded["ts"]["pv_kwh"].iloc[0]) != 7.0
    # ... and the ignored file is called out loudly.
    assert any(
        "IGNORED" in r.getMessage() and "ext.csv" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_external_pv_nan_gaps_are_filled_like_the_column(tmp_path, caplog):
    """NaN gaps in a timeseries_path CSV must get the same ffill/bfill + gap
    warning the Excel column path receives — the normalisation runs before
    the resolver injects the external series, so without the fill the NaNs
    reach the MILP and crash at model build with no pointer to the file."""
    import logging

    n = 96
    vals = np.full(n, 7.0)
    vals[10:20] = np.nan
    external = pd.DataFrame({
        "timestamp": pd.date_range("2019-06-01", periods=n, freq="15min"),
        "pv_kwh": vals,
    })
    external.to_csv(tmp_path / "gaps.csv", index=False)
    typed = _typed(
        n=n, freq="15min", pv_kwh="empty",
        pv_overrides={"timeseries_path": "gaps.csv"},
    )
    with caplog.at_level(logging.WARNING):
        loaded = read_workbook(_write(tmp_path, typed))
    assert not loaded["ts"]["pv_kwh"].isna().any()
    assert float(loaded["ts"]["pv_kwh"].iloc[12]) == pytest.approx(7.0)
    assert any(
        "NaN" in r.getMessage() and "gaps.csv" in r.getMessage()
        for r in caplog.records
    )
    # An ENTIRELY empty external column is an input mistake, not a gap.
    empty = pd.DataFrame({
        "timestamp": pd.date_range("2019-06-01", periods=n, freq="15min"),
        "pv_kwh": np.full(n, np.nan),
    })
    empty.to_csv(tmp_path / "empty.csv", index=False)
    typed2 = _typed(
        n=n, freq="15min", pv_kwh="empty",
        pv_overrides={"timeseries_path": "empty.csv"},
    )
    with pytest.raises(ValueError, match="entirely empty"):
        read_workbook(_write(tmp_path, typed2, name="empty_case.xlsx"))


# ---------------------------------------------------------------------------
# Excel / YAML parity for PVGIS
# ---------------------------------------------------------------------------


def test_excel_pvgis_matches_yaml_pvgis(tmp_path, monkeypatch):
    _mock_pvgis(monkeypatch, per_kwp_value=0.3)

    prices = pd.DataFrame({
        "timestamp": pd.date_range("2019-01-01", periods=HOURS, freq="h"),
        "dam_price_eur_per_mwh": np.full(HOURS, 50.0),
    })
    prices.to_csv(tmp_path / "ts.csv", index=False)
    cfg = tmp_path / "run.yaml"
    cfg.write_text(
        "pv:\n"
        "  pv_source: pvgis\n"
        "  pv_nameplate_kwp: 1000\n"
        "  latitude: 37.98\n"
        "  longitude: 23.73\n"
        "project:\n"
        "  mode: merchant\n"
        "timeseries_path: ts.csv\n",
        encoding="utf-8",
    )
    yaml_pv = load_structured_config(cfg)["ts"]["pv_kwh"].to_numpy(float)

    typed = _typed(
        pv_kwh="empty",
        pv_overrides={
            "pv_source": "pvgis", "latitude": 37.98, "longitude": 23.73,
        },
    )
    excel_pv = read_workbook(_write(tmp_path, typed))["ts"]["pv_kwh"].to_numpy(float)

    np.testing.assert_allclose(excel_pv, yaml_pv)


# ---------------------------------------------------------------------------
# Location-field range checks
# ---------------------------------------------------------------------------


def test_validate_pv_location_fields_accepts_defaults():
    validate_pv_location_fields(dict(PV_SHEET_DEFAULTS))
    validate_pv_location_fields({
        "latitude": 37.98, "longitude": 23.73, "tilt": "optimal",
        "azimuth": 180.0, "losses_pct": 14.0, "weather_year": 2019,
    })
    # 'tmy' is rejected end-to-end (the PVGIS provider does not support
    # it), so the validator must not advertise it either.
    with pytest.raises(ValueError, match="weather_year"):
        validate_pv_location_fields({"tilt": 30.0, "weather_year": "tmy"})


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("latitude", 200.0, "latitude"),
        ("longitude", -400.0, "longitude"),
        ("tilt", 120.0, "tilt"),
        ("azimuth", 720.0, "azimuth"),
        ("losses_pct", 150.0, "losses_pct"),
        ("weather_year", 1850, "weather_year"),
        ("weather_year", "garbage", "weather_year"),
    ],
)
def test_validate_pv_location_fields_rejects_out_of_range(field, value, fragment):
    with pytest.raises(ValueError, match=fragment):
        validate_pv_location_fields({field: value})


def test_bad_latitude_rejected_through_loader(tmp_path):
    typed = _typed(
        pv_kwh="filled", pv_overrides={"latitude": 200.0, "longitude": 23.0},
    )
    out = _write(tmp_path, typed)
    with pytest.raises(ValueError, match="latitude"):
        read_workbook(out)


# ---------------------------------------------------------------------------
# PVGIS field-wiring audit: every pv-sheet geometry field reaches the
# fetch verbatim, explicit zeros survive, blanks fall back to defaults,
# and PVGIS ALWAYS wins over workbook PV data (column and external
# timeseries_path file alike).
# ---------------------------------------------------------------------------


def test_pvgis_all_geometry_fields_reach_fetch_from_excel(
    tmp_path, monkeypatch,
):
    captured = _mock_pvgis(monkeypatch)
    typed = _typed(
        pv_kwh="empty",
        pv_overrides={
            "pv_source": "pvgis",
            "latitude": 37.98, "longitude": 23.73,
            "tilt": 25.0, "azimuth": 15.0, "losses_pct": 3.5,
            "weather_year": 2020, "raddatabase": "PVGIS-SARAH3",
        },
    )
    read_workbook(_write(tmp_path, typed, "fields.xlsx"))
    assert captured["lat"] == pytest.approx(37.98)
    assert captured["lon"] == pytest.approx(23.73)
    opts = captured["opts"]
    assert opts["tilt"] == pytest.approx(25.0)
    assert opts["azimuth"] == pytest.approx(15.0)
    assert opts["losses_pct"] == pytest.approx(3.5)
    assert opts["weather_year"] == 2020
    assert opts["raddatabase"] == "PVGIS-SARAH3"


def test_pvgis_explicit_zero_losses_and_azimuth_survive(
    tmp_path, monkeypatch,
):
    """An explicit 0 is a value, not a blank: losses_pct = 0 must reach
    the fetch as 0 (a falsy-`or` would silently restore the 14 %
    default), and azimuth = 0 (due south) likewise."""
    captured = _mock_pvgis(monkeypatch)
    typed = _typed(
        pv_kwh="empty",
        pv_overrides={
            "pv_source": "pvgis",
            "latitude": 37.98, "longitude": 23.73,
            "losses_pct": 0.0, "azimuth": 0.0,
        },
    )
    read_workbook(_write(tmp_path, typed, "zeros.xlsx"))
    assert captured["opts"]["losses_pct"] == 0.0
    assert captured["opts"]["azimuth"] == 0.0


def test_pvgis_null_geometry_falls_back_to_defaults(monkeypatch):
    """Hand-built / YAML-null geometry fields fall back to the PVGIS
    defaults instead of crashing on float(None)/int(None)."""
    from pvbess_opt.io_read import resolve_pv_source

    captured = _mock_pvgis(monkeypatch)
    typed = _typed(pv_kwh="empty", pv_overrides={
        "pv_source": "pvgis",
        "latitude": 37.98, "longitude": 23.73,
        "tilt": None, "azimuth": None, "losses_pct": None,
        "weather_year": None, "raddatabase": None,
    })
    resolve_pv_source(typed)
    opts = captured["opts"]
    assert opts["tilt"] == "optimal"
    assert opts["azimuth"] == 0.0
    assert opts["losses_pct"] == 14.0
    assert opts["weather_year"] == 2019
    assert opts["raddatabase"] is None


def test_pvgis_blank_cells_resolve_to_defaults_via_excel(
    tmp_path, monkeypatch,
):
    """Blank workbook cells parse to the sheet defaults and the fetch
    receives exactly those."""
    captured = _mock_pvgis(monkeypatch)
    typed = _typed(
        pv_kwh="empty",
        pv_overrides={
            "pv_source": "pvgis",
            "latitude": 37.98, "longitude": 23.73,
            "tilt": None, "losses_pct": None, "weather_year": None,
        },
    )
    read_workbook(_write(tmp_path, typed, "blanks.xlsx"))
    assert captured["opts"]["tilt"] == "optimal"
    assert captured["opts"]["losses_pct"] == 14.0
    assert captured["opts"]["weather_year"] == 2019


def test_pvgis_replaces_pv_and_scales_by_nameplate(tmp_path, monkeypatch):
    """With pv_source = pvgis the resolved pv_kwh IS the fetched profile
    scaled by the nameplate — the filled timeseries column is ignored."""
    captured = _mock_pvgis(monkeypatch, per_kwp_value=0.25)
    typed = _typed(
        pv_kwh="filled",  # 100 kWh in every step — must NOT survive
        pv_overrides={
            "pv_source": "pvgis", "latitude": 37.98, "longitude": 23.73,
        },
    )
    out = read_workbook(_write(tmp_path, typed, "replace.xlsx"))
    pv_kwh = out["ts"]["pv_kwh"].to_numpy(dtype=float)
    assert captured["lat"] == pytest.approx(37.98)
    # 0.25 kWh/kWp x 1000 kWp = 250 kWh per hour step.
    assert pv_kwh.sum() == pytest.approx(0.25 * 1000.0 * HOURS)
    assert not np.any(pv_kwh == 100.0)


def test_pvgis_ignores_external_timeseries_path_pv(
    tmp_path, monkeypatch, caplog,
):
    """pv_source = pvgis + a pv-sheet timeseries_path file: the location
    wins — the file PV is ignored (with a warning) while the ts sheet's
    price columns are untouched."""
    captured = _mock_pvgis(monkeypatch, per_kwp_value=0.25)
    external = tmp_path / "pv_profile.csv"
    pd.DataFrame({
        "timestamp": pd.date_range("2019-01-01", periods=HOURS, freq="h"),
        "pv_kwh": np.full(HOURS, 999.0),
    }).to_csv(external, index=False)
    typed = _typed(
        pv_kwh="empty",
        pv_overrides={
            "pv_source": "pvgis", "latitude": 37.98, "longitude": 23.73,
            "timeseries_path": str(external),
        },
    )
    from pvbess_opt.io_read import resolve_pv_source

    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io_read"):
        resolve_pv_source(typed, base_dir=tmp_path)
    pv_kwh = typed["ts"]["pv_kwh"].to_numpy(dtype=float)
    assert pv_kwh.sum() == pytest.approx(0.25 * 1000.0 * HOURS)
    assert not np.any(pv_kwh == 999.0)
    assert captured["opts"] is not None
    # Prices from the ts sheet survive untouched.
    assert (
        typed["ts"]["dam_price_eur_per_mwh"].to_numpy() == 50.0
    ).all()
    assert any(
        "that PV data is ignored" in r.getMessage() for r in caplog.records
    )


def test_file_mode_never_calls_pvgis(tmp_path, monkeypatch):
    """pv_source = file with a location set: the file wins and the
    PVGIS fetch is NEVER called."""
    captured = _mock_pvgis(monkeypatch)
    typed = _typed(
        pv_kwh="filled",
        pv_overrides={
            "pv_source": "file",
            "latitude": 37.98, "longitude": 23.73,
        },
    )
    out = read_workbook(_write(tmp_path, typed, "filemode.xlsx"))
    assert "lat" not in captured  # fetch never invoked
    assert (out["ts"]["pv_kwh"].to_numpy() == 100.0).all()
