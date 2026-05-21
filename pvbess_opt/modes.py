"""Canonical regulatory-mode resolution.

Single normalization for the ``mode`` param used across the optimizer,
KPIs, the CLI and the plotting layer.  Previously each call site rolled
its own ``str(params.get("mode", "vnb") ...).lower()`` variant; this is
the one strict form (strip + lower + validate).
"""

from __future__ import annotations

from typing import Any

VALID_MODES: tuple[str, ...] = ("vnb", "merchant")
DEFAULT_MODE: str = "vnb"


def resolve_mode(params: dict[str, Any]) -> str:
    """Return the normalized regulatory mode from ``params``.

    Defaults to ``vnb`` when absent/empty; raises ``ValueError`` for an
    unrecognized value.
    """
    mode = str(params.get("mode", DEFAULT_MODE) or DEFAULT_MODE).strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(
            f"Unknown mode {mode!r}; expected one of {VALID_MODES}."
        )
    return mode
