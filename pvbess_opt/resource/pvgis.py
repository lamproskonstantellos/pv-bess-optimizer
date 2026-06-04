"""PVGIS PV-profile provider (per-kWp hourly series) with on-disk caching.

Hits the PVGIS v5.2 ``seriescalc`` endpoint with ``pvcalculation=1`` and
``peakpower=1`` so the result is a **per-kWp** profile; callers scale by
``nameplate_kwp``.  The profile is linear in ``peakpower`` (while inverter
clipping is not modelled), so one fetch per location serves every array
size — which is what makes the capacity sweep cheap.

No API key is required.  Results are cached on disk keyed on the request
geometry so a repeat run never re-hits the network.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

from .base import ProfileResult

logger = logging.getLogger(__name__)

PVGIS_SERIESCALC_URL = "https://re.jrc.ec.europa.eu/api/v5_2/seriescalc"
HOURS_PER_NON_LEAP_YEAR = 8760
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "pvbess" / "pvgis"


class PVGISProvider:
    """Fetch (and cache) a per-kWp hourly PV profile from PVGIS."""

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        *,
        timeout: float = 60.0,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
        self.timeout = float(timeout)

    # -- public API --------------------------------------------------------

    def pv_profile(
        self,
        lat: float,
        lon: float,
        *,
        tilt: float | str = "optimal",
        azimuth: float = 0.0,
        losses_pct: float = 14.0,
        weather_year: int | str = 2019,
        raddatabase: str | None = None,
    ) -> ProfileResult:
        """Return a per-kWp hourly profile (8760 kWh values) for the site."""
        params = self._build_params(
            lat, lon, tilt, azimuth, losses_pct, weather_year, raddatabase,
        )
        key = self._cache_key(params)
        cached = self._load_cache(key)
        if cached is not None:
            logger.info("PVGIS cache hit (%s).", key)
            per_kwp = cached
        else:
            logger.info(
                "PVGIS fetch: lat=%.4f lon=%.4f year=%s.", lat, lon, weather_year,
            )
            per_kwp = self._parse_per_kwp(self._fetch(params))
            self._save_cache(key, per_kwp, params)
        return ProfileResult(
            per_kwp_kwh=per_kwp,
            metadata={
                "source": "pvgis",
                "latitude": float(lat),
                "longitude": float(lon),
                "tilt": tilt,
                "azimuth": float(azimuth),
                "losses_pct": float(losses_pct),
                "weather_year": weather_year,
                "raddatabase": raddatabase,
            },
        )

    # -- request / response ------------------------------------------------

    @staticmethod
    def _build_params(
        lat: float,
        lon: float,
        tilt: float | str,
        azimuth: float,
        losses_pct: float,
        weather_year: int | str,
        raddatabase: str | None,
    ) -> dict[str, Any]:
        if str(weather_year).strip().lower() == "tmy":
            raise ValueError(
                "weather_year='tmy' is not supported; use a numeric non-leap "
                "year (e.g. 2019) so the 8760-hour grid stays uniform."
            )
        year = int(weather_year)
        params: dict[str, Any] = {
            "lat": float(lat),
            "lon": float(lon),
            "pvcalculation": 1,
            "peakpower": 1,
            "loss": float(losses_pct),
            "mountingplace": "free",
            "pvtechchoice": "crystSi",
            "aspect": float(azimuth),
            "startyear": year,
            "endyear": year,
            "outputformat": "json",
        }
        if isinstance(tilt, str) and tilt.strip().lower() == "optimal":
            params["optimalinclination"] = 1
        else:
            params["angle"] = float(tilt)
        if raddatabase:
            params["raddatabase"] = raddatabase
        return params

    def _fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        import requests

        resp = requests.get(
            PVGIS_SERIESCALC_URL, params=params, timeout=self.timeout,
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    @staticmethod
    def _parse_per_kwp(data: dict[str, Any]) -> np.ndarray:
        try:
            hourly = data["outputs"]["hourly"]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                "Unexpected PVGIS response: missing outputs.hourly."
            ) from exc
        # ``P`` is the AC power (W) of a 1 kWp system; hourly W == Wh, so
        # per-kWp energy is P / 1000 kWh.
        watts = np.array([float(rec["P"]) for rec in hourly], dtype=float)
        per_kwp = watts / 1000.0
        if per_kwp.size != HOURS_PER_NON_LEAP_YEAR:
            raise ValueError(
                f"PVGIS returned {per_kwp.size} hourly values; expected "
                f"{HOURS_PER_NON_LEAP_YEAR} (use a non-leap weather_year)."
            )
        return per_kwp

    # -- caching -----------------------------------------------------------

    @staticmethod
    def _cache_key(params: dict[str, Any]) -> str:
        blob = json.dumps(params, sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _load_cache(self, key: str) -> np.ndarray | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return np.asarray(payload["per_kwp_kwh"], dtype=float)
        except (json.JSONDecodeError, KeyError, OSError):
            logger.warning("Ignoring unreadable PVGIS cache file %s.", path)
            return None

    def _save_cache(
        self, key: str, per_kwp: np.ndarray, params: dict[str, Any],
    ) -> None:
        path = self._cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"params": params, "per_kwp_kwh": per_kwp.tolist()},
            ),
            encoding="utf-8",
        )


def fetch_pv_profile(
    lat: float,
    lon: float,
    *,
    cache_dir: str | Path | None = None,
    **opts: Any,
) -> ProfileResult:
    """Convenience wrapper around :class:`PVGISProvider`."""
    return PVGISProvider(cache_dir=cache_dir).pv_profile(lat, lon, **opts)
