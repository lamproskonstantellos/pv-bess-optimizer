"""Compact EUR formatter shared by every financial plot in :mod:`pvbess_opt.plotting.financial`.

Replaces matplotlib's default scientific notation (``1e6``, ``1e7``)
with a magnitude-aware string (``€12.3M``, ``€45k``, ``€850``).
The same helper drives the tornado-plot annotations so axes and labels
speak the same language.

Three modes:

* ``auto``    — pick ``B`` / ``M`` / ``k`` / no-suffix automatically.
* ``millions``— always render in millions (``€0.5M``, ``€12.3M``).
* ``raw``     — full digits with thousands separators (``€12,345,678``).
"""

from __future__ import annotations

from typing import Any

from matplotlib.ticker import FuncFormatter

EUR = "€"  # € — single Unicode point so the formatter is portable.


def resolve_currency_format(econ: dict[str, Any] | None) -> str:
    """Return a validated ``currency_format`` ('auto' | 'millions' | 'raw')."""
    if econ is None:
        return "auto"
    raw = str(econ.get("currency_format", "auto") or "auto").strip().lower()
    if raw not in ("auto", "millions", "raw"):
        return "auto"
    return raw


def format_eur(
    value: float, format_mode: str = "auto", *, decimals: int = 1,
) -> str:
    """Render ``value`` (EUR) as a compact string.

    Examples (auto mode)::

        12_345_678  -> "€12.3M"
        45_000      -> "€45k"
        850         -> "€850"
        -3_200_000  -> "-€3.2M"
    """
    if value is None or (isinstance(value, float) and value != value):  # NaN
        return ""

    sign = "-" if value < 0 else ""
    abs_v = abs(float(value))

    if format_mode == "millions":
        return f"{sign}{EUR}{abs_v / 1e6:.{decimals}f}M"

    if format_mode == "raw":
        # Thousands separator with no decimals; matches Excel default.
        return f"{sign}{EUR}{abs_v:,.0f}"

    # auto
    if abs_v >= 1e9:
        return f"{sign}{EUR}{abs_v / 1e9:.{decimals}f}B"
    if abs_v >= 1e6:
        return f"{sign}{EUR}{abs_v / 1e6:.{decimals}f}M"
    if abs_v >= 1e3:
        return f"{sign}{EUR}{abs_v / 1e3:.0f}k"
    return f"{sign}{EUR}{abs_v:.0f}"


def euro_axis_formatter(format_mode: str = "auto") -> FuncFormatter:
    """Return a matplotlib ``FuncFormatter`` rendering ticks via :func:`format_eur`.

    Apply via::

        ax.yaxis.set_major_formatter(euro_axis_formatter("auto"))

    The closure captures ``format_mode`` so callers can lock a plot
    into ``millions`` or ``raw`` while the rest of the run uses
    ``auto``.
    """
    def _fmt(x: float, _pos: int) -> str:
        return format_eur(float(x), format_mode, decimals=1)
    return FuncFormatter(_fmt)
