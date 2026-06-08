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
    assert any("column is ignored" in r.getMessage() for r in caplog.records)


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
