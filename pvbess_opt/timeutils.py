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

__all__ = ["dt_hours_from"]


def dt_hours_from(params: dict[str, Any]) -> float:
    """Return ``params['dt_minutes'] / 60.0`` as a non-negative float.

    Treats a missing or zero / negative ``dt_minutes`` as 0.0 hours so
    every legacy call site that previously wrote
    ``float(params.get('dt_minutes', 0) or 0) / 60.0`` continues to
    receive the same value (callers downstream interpret 0.0 as "no
    balancing block fired" or "no timestep duration"; preserve that
    semantics).  Callers that hard-required ``dt_minutes`` to be
    present in ``params`` keep their KeyError surface by reading it
    directly -- only the explicit ``.get`` callers route through this
    helper.
    """
    raw = params.get("dt_minutes", 0) or 0
    minutes = float(raw)
    if minutes < 0.0:
        minutes = 0.0
    return minutes / 60.0
