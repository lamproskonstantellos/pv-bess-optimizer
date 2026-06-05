"""Resample an hourly per-kWp profile onto the model's sub-hourly grid.

Mirrors the energy-conservation rule of ``scripts/resample_timeseries.py``
(a *flow* split equally across the finer sub-intervals), specialised to the
exact whole-hour upsample the model grid needs: 8760 hourly values become
``8760 * steps_per_hour`` (e.g. 35 040 at 15-minute cadence), with the
annual total preserved.
"""

from __future__ import annotations

import numpy as np


def upsample_hourly_to_grid(hourly_kwh: object, steps_per_hour: int) -> np.ndarray:
    """Split each hourly kWh equally across ``steps_per_hour`` sub-steps.

    Energy-conserving: ``sum(out) == sum(hourly_kwh)``.  Returns an array of
    length ``len(hourly_kwh) * steps_per_hour``.
    """
    if steps_per_hour <= 0:
        raise ValueError(f"steps_per_hour must be positive, got {steps_per_hour}")
    arr = np.asarray(hourly_kwh, dtype=float)
    return np.repeat(arr / float(steps_per_hour), steps_per_hour)
