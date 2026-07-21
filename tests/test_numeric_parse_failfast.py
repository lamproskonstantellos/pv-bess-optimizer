"""Fail-fast contract for numeric workbook cells.

A blank / NaN / empty cell resolves to the key default (unchanged
behaviour, and the basis for the opt-in bit-identity guarantee); a
NON-blank cell that cannot be parsed as a number MUST raise a loud,
key-naming error rather than silently substituting the default and
producing a confident-wrong financial/physical result.

Regression for the round-3 audit finding: ``_parse_value`` /
``_parse_grid_export_max`` / ``validate_pv_location_fields`` previously
warned-and-defaulted on unparseable numeric input.
"""

from __future__ import annotations

import numpy as np
import pytest

from pvbess_opt.io import (
    _parse_grid_export_max,
    _parse_value,
    validate_pv_location_fields,
)

# --- generic numeric keys --------------------------------------------------


def test_unparseable_float_key_raises_naming_the_key():
    # e.g. a Greek/European decimal comma: '7,5' meant 7.5.
    with pytest.raises(ValueError, match="discount_rate_pct"):
        _parse_value("discount_rate_pct", "7,5", 7.0)


def test_unparseable_int_key_raises_naming_the_key():
    with pytest.raises(ValueError, match="project_lifecycle_years"):
        _parse_value("project_lifecycle_years", "twenty", 20)


@pytest.mark.parametrize("blank", [None, float("nan"), "", "   "])
def test_blank_numeric_cell_still_resolves_to_default(blank):
    # The bit-identity basis: a blank cell must keep using the default.
    assert _parse_value("discount_rate_pct", blank, 7.0) == 7.0
    assert _parse_value("project_lifecycle_years", blank, 20) == 20


def test_valid_numeric_cell_unchanged():
    assert _parse_value("discount_rate_pct", "7.5", 7.0) == 7.5
    assert _parse_value("discount_rate_pct", 7.5, 7.0) == 7.5


# --- grid-cap keys ---------------------------------------------------------


def test_grid_cap_unit_suffix_raises():
    # '10 MW' (unit typo) must not silently cap at the default kW value.
    with pytest.raises(ValueError, match="p_grid_export_max_kw"):
        _parse_grid_export_max("10 MW", "p_grid_export_max_kw")


@pytest.mark.parametrize("token", ["", "inf", "unlimited", "disabled", None])
def test_grid_cap_unlimited_tokens_still_parse_to_inf(token):
    assert np.isinf(_parse_grid_export_max(token, "p_grid_import_max_kw"))


def test_grid_cap_valid_number_unchanged():
    assert _parse_grid_export_max("5000", "p_grid_export_max_kw") == 5000.0


# --- PV location geometry --------------------------------------------------


@pytest.mark.parametrize(
    "field, bad",
    [
        ("latitude", "45N"),
        ("longitude", "23E"),
        ("azimuth", "south"),
        ("losses_pct", "14%"),
    ],
)
def test_present_but_unparseable_location_field_raises(field, bad):
    # Must raise a targeted message (not silently drop to None and later
    # misreport the field as *missing*).
    with pytest.raises(ValueError, match=field):
        validate_pv_location_fields({field: bad})


@pytest.mark.parametrize("field", ["latitude", "longitude", "azimuth", "losses_pct"])
def test_blank_location_field_passes(field):
    # Blank / absent is optional and must not raise.
    validate_pv_location_fields({field: ""})
    validate_pv_location_fields({field: None})
