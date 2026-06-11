"""Compact EUR formatter shared by every financial plot in :mod:`pvbess_opt.plotting.financial`.

Replaces matplotlib's default scientific notation (``1e6``, ``1e7``)
with a magnitude-aware string (``€12.3M``, ``€45k``, ``€850``).
The same helper drives the tornado-plot annotations so axes and labels
speak the same language.

Three modes:

* ``auto``    — pick ``B`` / ``M`` / ``k`` / no-suffix automatically.
* ``millions``— always render in millions (``€0.5M``, ``€12.3M``).
* ``raw``     — full digits with thousands separators (``€12,345,678``).

Tick rendering is **adaptive**: the axis formatter inspects the tick
spacing and escalates the decimal precision when the default would
collapse neighbouring ticks into identical strings — e.g. a
Monte-Carlo profit axis spanning a few hundred EUR around €1.18M used
to render every tick as ``€1.2M``; it now renders ``€1.1818M`` /
``€1.1820M`` / ... so the axis stays readable.  Wide axes keep the
historical ``€12.3M`` style untouched.
"""

from __future__ import annotations

import math
from itertools import pairwise
from typing import Any

from matplotlib.ticker import Formatter

EUR = "€"  # € — single Unicode point so the formatter is portable.

# Never escalate more than this many decimals past a branch's default —
# beyond that the labels stop being readable and the caller should use
# ``raw`` mode instead.
_MAX_EXTRA_DECIMALS = 6


def resolve_currency_format(econ: dict[str, Any] | None) -> str:
    """Return a validated ``currency_format`` ('auto' | 'millions' | 'raw')."""
    if econ is None:
        return "auto"
    raw = str(econ.get("currency_format", "auto") or "auto").strip().lower()
    if raw not in ("auto", "millions", "raw"):
        return "auto"
    return raw


def format_eur(
    value: float,
    format_mode: str = "auto",
    *,
    decimals: int = 1,
    small_decimals: int = 0,
) -> str:
    """Render ``value`` (EUR) as a compact string.

    ``decimals`` controls the ``B`` / ``M`` suffix precision;
    ``small_decimals`` the ``k`` / unsuffixed / ``raw`` precision (both
    default to the historical fixed precisions, so plain calls are
    byte-identical to before).

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
        # Thousands separator; matches Excel default at 0 decimals.
        return f"{sign}{EUR}{abs_v:,.{small_decimals}f}"

    # auto
    if abs_v >= 1e9:
        return f"{sign}{EUR}{abs_v / 1e9:.{decimals}f}B"
    if abs_v >= 1e6:
        return f"{sign}{EUR}{abs_v / 1e6:.{decimals}f}M"
    if abs_v >= 1e3:
        return f"{sign}{EUR}{abs_v / 1e3:.{small_decimals}f}k"
    return f"{sign}{EUR}{abs_v:.{small_decimals}f}"


def format_eur_adaptive(
    value: float,
    *,
    resolution: float,
    format_mode: str = "auto",
) -> str:
    """Render ``value`` like :func:`format_eur`, escalating precision so
    values ``resolution`` apart produce distinct strings.

    The legend analogue of :class:`_EuroTickFormatter`: a Monte-Carlo
    legend quoting P10 / P50 / P90 a few hundred EUR apart around
    ``€1.18M`` must not collapse all three to ``€1.2M``.  Pass the
    smallest pairwise difference between the quoted values as
    ``resolution``; a non-positive resolution falls back to the fixed
    default precision.
    """
    if value is None or (isinstance(value, float) and value != value):  # NaN
        return ""
    scale = _branch_scale(abs(float(value)), format_mode)
    default_dec = 1 if (scale >= 1e6 and format_mode != "raw") else 0
    dec = default_dec
    if resolution is not None and resolution > 0.0:
        needed = math.ceil(-math.log10(resolution / scale) - 1e-9)
        dec = min(max(default_dec, needed), default_dec + _MAX_EXTRA_DECIMALS)
    if scale >= 1e6 and format_mode != "raw":
        return format_eur(float(value), format_mode, decimals=dec)
    return format_eur(
        float(value), format_mode, decimals=dec, small_decimals=dec,
    )


def _branch_scale(abs_v: float, format_mode: str) -> float:
    """Suffix scale :func:`format_eur` will use for ``abs_v`` in ``format_mode``."""
    if format_mode == "millions":
        return 1e6
    if format_mode == "raw":
        return 1.0
    if abs_v >= 1e9:
        return 1e9
    if abs_v >= 1e6:
        return 1e6
    if abs_v >= 1e3:
        return 1e3
    return 1.0


class _EuroTickFormatter(Formatter):
    """Tick formatter wrapping :func:`format_eur` with adaptive precision.

    When attached to an axis it reads the major tick spacing and raises
    the decimal count just enough that neighbouring ticks render as
    distinct strings.  Detached (``self.axis is None``) or degenerate
    axes fall back to the fixed default precision.
    """

    def __init__(self, format_mode: str = "auto", *, decimals: int = 1) -> None:
        self._mode = format_mode
        self._decimals = int(decimals)

    def _tick_spacing(self) -> float | None:
        axis = getattr(self, "axis", None)
        if axis is None:
            return None
        try:
            locs = sorted({float(loc) for loc in axis.get_majorticklocs()})
        except (TypeError, ValueError):
            return None
        diffs = [b - a for a, b in pairwise(locs) if b > a]
        if diffs:
            return min(diffs)
        try:
            vmin, vmax = axis.get_view_interval()
        except (TypeError, ValueError):
            return None
        span = abs(float(vmax) - float(vmin))
        return span / 6.0 if span > 0.0 else None

    def __call__(self, x: float, pos: int | None = None) -> str:
        _ = pos  # matplotlib Formatter signature; unused
        value = float(x)
        scale = _branch_scale(abs(value), self._mode)
        default_dec = (
            self._decimals if scale >= 1e6 and self._mode != "raw" else 0
        )
        dec = default_dec
        spacing = self._tick_spacing()
        if spacing is not None and spacing > 0.0:
            # Distinct neighbours need the rendered resolution
            # (scale * 10^-dec) to be at most the tick spacing.
            needed = math.ceil(-math.log10(spacing / scale) - 1e-9)
            dec = min(max(default_dec, needed), default_dec + _MAX_EXTRA_DECIMALS)
        if scale >= 1e6 and self._mode != "raw":
            return format_eur(value, self._mode, decimals=dec)
        return format_eur(
            value, self._mode, decimals=self._decimals, small_decimals=dec,
        )


def euro_axis_formatter(
    format_mode: str = "auto", *, decimals: int = 1,
) -> Formatter:
    """Return a matplotlib ``Formatter`` rendering ticks via :func:`format_eur`.

    Apply via::

        ax.yaxis.set_major_formatter(euro_axis_formatter("auto"))

    ``format_mode`` locks a plot into ``millions`` or ``raw`` while the
    rest of the run uses ``auto``; ``decimals`` sets the default
    magnitude-suffix precision (1, preserving the historical ``€12.3M``
    style).  Precision escalates automatically on axes whose tick
    spacing is too narrow for the default — see
    :class:`_EuroTickFormatter`.
    """
    return _EuroTickFormatter(format_mode, decimals=decimals)
