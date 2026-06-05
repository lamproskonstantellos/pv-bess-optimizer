"""Pluggable PV-resource providers.

A resource provider turns a location (and array geometry) into a per-kWp
PV energy profile.  PVGIS is implemented here; the
:class:`~pvbess_opt.resource.base.ResourceProvider` protocol keeps the
door open for CAMS / NREL PVWatts / Open-Meteo without changing callers.
"""

from .base import ProfileResult, ResourceProvider
from .pvgis import PVGISProvider, fetch_pv_profile

__all__ = [
    "PVGISProvider",
    "ProfileResult",
    "ResourceProvider",
    "fetch_pv_profile",
]
