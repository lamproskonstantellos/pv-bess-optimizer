"""Colour-registry universality tests.

Every plot label must resolve to a colour through ``config.COLORS`` or
``config.MERCHANT_COLORS`` — no plotting module is allowed to bake an
inline hex literal.  Within each registry every label must map to a
distinct hex value, so two physically different flows never end up
sharing a colour (the bug that caused PV→Grid and BESS→Grid revenue
bars to render in the same hue on the daily merchant revenue plot).
"""

from __future__ import annotations

import re
from pathlib import Path

from pvbess_opt.config import COLORS, MERCHANT_COLORS


def _assert_unique(name: str, registry: dict[str, str]) -> None:
    seen: dict[str, str] = {}
    for label, color in registry.items():
        if color in seen.values():
            other = next(k for k, v in seen.items() if v == color)
            raise AssertionError(
                f"{name}: colour {color!r} used by both "
                f"{other!r} and {label!r}"
            )
        seen[label] = color


def test_no_two_labels_share_a_color():
    """Every distinct label in COLORS must resolve to a distinct hex
    value.  Catches the merchant-revenue color clash bug."""
    _assert_unique("COLORS", COLORS)


def test_no_two_merchant_labels_share_a_color():
    """The merchant revenue labels each have a unique colour — the
    PV→Grid and BESS→Grid revenue bars must not collide."""
    _assert_unique("MERCHANT_COLORS", MERCHANT_COLORS)


def test_merchant_colors_match_their_energy_counterparts():
    """Each (revenue) / (cost) label shares the energy colour of the
    physical flow it represents, so the financial view of a flow is
    visually consistent with its energy view."""
    assert MERCHANT_COLORS["PV→Grid (revenue)"] == COLORS["PV→Grid (export)"]
    assert MERCHANT_COLORS["BESS→Grid (revenue)"] == COLORS["BESS→Grid (export)"]
    assert MERCHANT_COLORS["Import→BESS (cost)"] == COLORS["Import→BESS (charge)"]


def test_no_inline_colors_in_plotting_modules():
    """No plotting module may hardcode hex colours.  All colours come
    from config.COLORS / config.MERCHANT_COLORS via helpers."""
    hex_re = re.compile(r'["\']#[0-9A-Fa-f]{6}["\']')
    plotting_dir = Path(__file__).resolve().parent.parent / "pvbess_opt" / "plotting"
    offenders: list[str] = []
    for py_file in plotting_dir.rglob("*.py"):
        if py_file.name == "__init__.py":
            continue
        text = py_file.read_text()
        for match in hex_re.finditer(text):
            offenders.append(f"{py_file.name}: {match.group()}")
    assert not offenders, (
        "Inline hex colours found in plotting modules:\n"
        + "\n".join(offenders)
    )
