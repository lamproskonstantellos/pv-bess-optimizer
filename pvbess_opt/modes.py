"""Canonical regulatory-mode resolution.

Single normalization for the ``mode`` param used across the optimizer,
KPIs, the CLI and the plotting layer: strip + lower + validate.
"""

from __future__ import annotations

from typing import Any

__all__ = ["DEFAULT_MODE", "VALID_MODES", "resolve_mode"]

VALID_MODES: tuple[str, ...] = ("self_consumption", "merchant")
DEFAULT_MODE: str = "self_consumption"


def resolve_mode(params: dict[str, Any]) -> str:
    """Return the normalized regulatory mode from ``params``.

    Defaults to ``self_consumption`` when absent/empty; raises ``ValueError`` for an
    unrecognized value.
    """
    mode = str(params.get("mode", DEFAULT_MODE) or DEFAULT_MODE).strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(
            f"Unknown mode {mode!r}; expected one of {VALID_MODES}."
        )
    return mode
