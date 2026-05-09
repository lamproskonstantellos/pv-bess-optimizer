"""Shared deterministic PV helpers for short-fixture timeseries.

The canonical 15-min reference shape lives at
``data/pv_shape_15min.csv`` (real-world 8 MW site, 35 040 rows, sums
to 12 568 961.7517 kWh).  This module exposes a tiny helper that
loads, downsamples and slices that shape without any randomness — used
by every short-fixture timeseries in the test suite so the v0.7
noise-bleed-at-night bug cannot creep back into a unit test.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CANONICAL_PV_CSV = ROOT / "data" / "pv_shape_15min.csv"
CANONICAL_PV_REFERENCE_KWP: float = 8000.0
# 1 June 00:00 in a non-leap year starting at Jan 1 = day index 151 ⇒
# hour index 151 * 24 = 3624.
JUNE_1_HOUR_INDEX: int = 151 * 24


def hourly_canonical_pv_window(
    n_hours: int, pv_nameplate_kwp: float,
    *,
    start_hour: int = JUNE_1_HOUR_INDEX,
) -> np.ndarray:
    """Deterministic hourly PV slice from the canonical 8 MW reference shape.

    Loads ``data/pv_shape_15min.csv`` (35 040 rows @ 15-min cadence),
    sums consecutive quadruplets to produce 8 760 hourly values (still
    in 8 MW scale, kWh per hour), slices ``n_hours`` from ``start_hour``
    (default June 1 00:00), then scales by
    ``pv_nameplate_kwp / 8000`` to match the test's nameplate.

    Same inputs ⇒ identical output.  No noise, no random, no smoothing.
    """
    shape_15min = pd.read_csv(CANONICAL_PV_CSV)[
        "pv_kwh_8mw_reference"
    ].to_numpy(dtype=float)
    hourly = shape_15min.reshape(-1, 4).sum(axis=1)
    window = hourly[start_hour: start_hour + n_hours]
    return window * (float(pv_nameplate_kwp) / CANONICAL_PV_REFERENCE_KWP)
