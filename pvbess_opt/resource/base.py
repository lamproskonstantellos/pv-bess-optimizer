"""Resource-provider protocol and result type."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np


@dataclass
class ProfileResult:
    """A per-kWp PV energy profile plus provider metadata.

    ``per_kwp_kwh`` is the hourly energy (kWh) a 1 kWp array produces over
    one non-leap year (8760 values).  The profile is **linear in array
    size** while inverter clipping is not modelled, so callers scale it by
    ``nameplate_kwp`` — this is what makes a capacity sweep cheap (one
    fetch per location, not one per size).  Once an AC/DC clipping layer
    exists, clip *after* scaling.
    """

    per_kwp_kwh: np.ndarray
    metadata: dict[str, Any] = field(default_factory=dict)


class ResourceProvider(Protocol):
    """Turns a location into a per-kWp PV profile."""

    def pv_profile(self, lat: float, lon: float, **opts: Any) -> ProfileResult:
        ...
