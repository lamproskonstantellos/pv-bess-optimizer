"""PVGIS resource ingestion: parse, scale, cache, resample, tz, integration.

No live network — the PVGIS HTTP call is mocked throughout.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.resource import PVGISProvider, fetch_pv_profile
from pvbess_opt.resource.pvgis import PVGIS_SERIESCALC_URL
from pvbess_opt.resource.resample import upsample_hourly_to_grid
from pvbess_opt.timeutils import apply_fixed_utc_offset

HOURS = 8760


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _install_fake_get(monkeypatch, p_watts, counter=None):
    import requests

    payload = {"outputs": {"hourly": [{"P": float(p)} for p in p_watts]}}

    def fake_get(url, params=None, timeout=None):
        assert url == PVGIS_SERIESCALC_URL
        assert params is not None and "lat" in params and params["peakpower"] == 1
        assert timeout is not None
        if counter is not None:
            counter["n"] += 1
        return _FakeResponse(payload)

    monkeypatch.setattr(requests, "get", fake_get)


def test_provider_parses_per_kwp_and_scales(monkeypatch, tmp_path):
    _install_fake_get(monkeypatch, np.full(HOURS, 1000.0))  # 1000 W -> 1 kWh/kWp/h
    prov = PVGISProvider(cache_dir=tmp_path / "cache")
    result = prov.pv_profile(37.98, 23.73, weather_year=2019)
    assert result.per_kwp_kwh.shape == (HOURS,)
    assert result.per_kwp_kwh[0] == pytest.approx(1.0)
    assert result.per_kwp_kwh.sum() == pytest.approx(float(HOURS))
    # Linear scaling by nameplate.
    assert (result.per_kwp_kwh * 10000.0).sum() == pytest.approx(HOURS * 10000.0)


def test_cache_hit_avoids_second_fetch(monkeypatch, tmp_path):
    counter = {"n": 0}
    _install_fake_get(monkeypatch, np.full(HOURS, 500.0), counter)
    prov = PVGISProvider(cache_dir=tmp_path / "cache")
    r1 = prov.pv_profile(37.98, 23.73, weather_year=2019)
    r2 = prov.pv_profile(37.98, 23.73, weather_year=2019)
    assert counter["n"] == 1  # second call served from disk cache
    np.testing.assert_allclose(r1.per_kwp_kwh, r2.per_kwp_kwh)


def test_leap_year_length_is_rejected(monkeypatch, tmp_path):
    _install_fake_get(monkeypatch, np.full(8784, 100.0))  # leap-year length
    prov = PVGISProvider(cache_dir=tmp_path / "cache")
    with pytest.raises(ValueError, match="8760"):
        prov.pv_profile(37.98, 23.73, weather_year=2020)


def test_tmy_is_rejected(tmp_path):
    prov = PVGISProvider(cache_dir=tmp_path / "cache")
    with pytest.raises(ValueError, match="tmy"):
        prov.pv_profile(37.98, 23.73, weather_year="tmy")


def test_upsample_conserves_energy_and_count():
    hourly = np.arange(HOURS, dtype=float)
    grid = upsample_hourly_to_grid(hourly, 4)
    assert grid.shape == (HOURS * 4,)
    assert grid.sum() == pytest.approx(hourly.sum())
    np.testing.assert_allclose(grid[:4], np.full(4, hourly[0] / 4.0))


def test_apply_fixed_utc_offset_rolls_forward():
    arr = np.arange(8, dtype=float)
    shifted = apply_fixed_utc_offset(arr, offset_hours=2, steps_per_hour=1)
    np.testing.assert_allclose(shifted, np.roll(arr, 2))
    assert shifted[2] == arr[0]


def test_fetch_pv_profile_wrapper(monkeypatch, tmp_path):
    _install_fake_get(monkeypatch, np.full(HOURS, 250.0))
    result = fetch_pv_profile(
        37.98, 23.73, cache_dir=tmp_path / "c", weather_year=2019,
    )
    assert result.per_kwp_kwh[0] == pytest.approx(0.25)
    assert result.metadata["source"] == "pvgis"


def test_load_structured_config_pvgis_builds_pv_kwh(monkeypatch, tmp_path):
    """`pv_source: pvgis` fetches, scales, resamples onto the 35 040 grid."""
    import pvbess_opt.resource as resource_pkg
    from pvbess_opt.io_read import load_structured_config
    from pvbess_opt.resource.base import ProfileResult

    per_kwp = np.full(HOURS, 0.5)  # 0.5 kWh/kWp/h
    captured: dict = {}

    def fake_fetch(lat, lon, **opts):
        captured["lat"] = lat
        captured["lon"] = lon
        captured["opts"] = opts
        return ProfileResult(per_kwp_kwh=per_kwp.copy(), metadata={"source": "pvgis"})

    monkeypatch.setattr(resource_pkg, "fetch_pv_profile", fake_fetch)

    ts = pd.DataFrame({
        "timestamp": pd.date_range("2019-01-01", periods=35040, freq="15min"),
        "dam_price_eur_per_mwh": np.full(35040, 50.0),
    })
    ts.to_csv(tmp_path / "ts.csv", index=False)

    cfg = tmp_path / "run.yaml"
    cfg.write_text(
        "pv:\n"
        "  pv_source: pvgis\n"
        "  pv_nameplate_kwp: 10000\n"
        "  latitude: 37.98\n"
        "  longitude: 23.73\n"
        "  tilt: optimal\n"
        "project:\n"
        "  mode: merchant\n"
        "timeseries_path: ts.csv\n",
        encoding="utf-8",
    )

    typed = load_structured_config(cfg)
    assert captured["lat"] == pytest.approx(37.98)
    assert captured["opts"]["tilt"] == "optimal"
    assert len(typed["ts"]) == 35040
    assert "pv_kwh" in typed["ts"].columns
    assert typed["pv"]["pv_source"] == "file"  # converted after resolution
    assert float(typed["ts"]["pv_kwh"].sum()) == pytest.approx(
        0.5 * 10000.0 * HOURS
    )
