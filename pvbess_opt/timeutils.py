"""Single source of truth for time-axis conversions.

The MILP, KPI helpers, rolling-horizon engine, balancing module and
I/O loader all need to convert ``params['dt_minutes']`` (an integer
minutes per timestep) into a per-step duration expressed in hours.
Spreading the literal expression
``float(params['dt_minutes']) / 60.0`` across modules makes any future
change to the convention (e.g. negative-time guards, sub-minute
cadences) tricky to roll out consistently.

This module exposes one helper, :func:`dt_hours_from`, which every
call site uses.  Behaviour is unchanged from the previous inline
expression; this is a pure refactor.
"""

from __future__ import annotations

from typing import Any

import numpy as np

__all__ = ["apply_fixed_utc_offset", "dt_hours_from"]


def dt_hours_from(params: dict[str, Any]) -> float:
    """Return ``params['dt_minutes'] / 60.0`` as a non-negative float.

    Treats a missing or zero / negative ``dt_minutes`` as 0.0 hours so
    every legacy call site that previously wrote
    ``float(params.get('dt_minutes', 0) or 0) / 60.0`` continues to
    receive the same value (callers downstream interpret 0.0 as "no
    balancing block fired" or "no timestep duration"; preserve that
    semantics).  Build call sites that require a positive timestep --
    :func:`pvbess_opt.optimization.build_model` and
    :func:`pvbess_opt.optimization.model_to_dataframe` -- guard the
    returned value explicitly (``if dt_h <= 0: raise ValueError``)
    rather than relying on a ``KeyError`` from this helper, which never
    raises.
    """
    raw = params.get("dt_minutes", 0) or 0
    minutes = float(raw)
    if minutes < 0.0:
        minutes = 0.0
    return minutes / 60.0


def apply_fixed_utc_offset(
    profile: object, offset_hours: int, steps_per_hour: int,
) -> np.ndarray:
    """Shift a per-step profile by a fixed whole-hour UTC offset (no DST).

    A profile indexed in UTC is rolled forward by ``offset_hours`` so each
    step carries the value that occurred ``offset_hours`` earlier in UTC —
    e.g. ``+2`` maps UTC midnight to 02:00 local for Europe/Athens.

    A **fixed** offset (rather than a DST-aware ``zoneinfo`` conversion) is
    deliberate: the model runs on a uniform 35 040-step grid (15-min x a
    non-leap year), and a true UTC->Europe/Athens conversion would produce
    23h/25h transition days that break that assumption.  Callers that need
    wall-clock DST alignment must re-grid the transition days first.
    """
    arr = np.asarray(profile, dtype=float)
    return np.roll(arr, int(offset_hours) * int(steps_per_hour))
