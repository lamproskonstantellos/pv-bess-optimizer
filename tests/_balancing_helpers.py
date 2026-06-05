"""Shared balancing-market test helpers.

The canonical ``_balancing_on`` used across the balancing test modules.
The ``**overrides`` form is the superset of the per-module copies it
replaces; called with no overrides it reproduces the no-override variant
exactly.
"""

from __future__ import annotations

from pvbess_opt.io import BALANCING_SHEET_DEFAULTS


def _balancing_on(params: dict, **overrides) -> dict:
    out = dict(params)
    bm = dict(BALANCING_SHEET_DEFAULTS, balancing_enabled=True)
    bm["bm_settlement_minutes"] = int(out.get("dt_minutes", 60))
    bm.update(overrides)
    out["balancing"] = bm
    return out
