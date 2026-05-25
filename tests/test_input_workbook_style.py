"""Style checks for the canonical input workbook.

The shipped workbook carries exactly one global accent: row 1 of every
sheet is bold + filled ``#F2F2F2`` + thin ``#BFBFBF`` bottom border.
No other cells carry per-sheet styling.  In particular, no cell
anywhere has the legacy amber bootstrap fill ``#FFF2CC``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent.parent
WORKBOOK = ROOT / "inputs" / "input.xlsx"

EXPECTED_HEADER_FILL = "F2F2F2"
EXPECTED_HEADER_BORDER = "BFBFBF"
FORBIDDEN_FILL_HEX = "FFF2CC"


def _normalise_rgb(rgb: object) -> str:
    if not isinstance(rgb, str):
        return ""
    return rgb.upper().lstrip("0").rjust(6, "0")[-6:]


@pytest.fixture(scope="module")
def workbook():
    return load_workbook(WORKBOOK)


def test_no_amber_fills_anywhere(workbook):
    """No cell anywhere carries the legacy amber bootstrap fill."""
    hits = []
    for sn in workbook.sheetnames:
        ws = workbook[sn]
        for row in ws.iter_rows():
            for c in row:
                fill = c.fill
                if fill is None or fill.fill_type is None:
                    continue
                for attr in ("fgColor", "start_color"):
                    colour = getattr(fill, attr, None)
                    rgb = _normalise_rgb(getattr(colour, "rgb", None))
                    if rgb == FORBIDDEN_FILL_HEX:
                        hits.append((sn, c.coordinate))
                        break
    assert not hits, f"Legacy amber fill remains in: {hits[:5]}..."


@pytest.mark.parametrize("sheet_name", [
    "timeseries", "project", "pv", "bess", "economics",
    "simulation", "max_injection_profile", "balancing",
])
def test_header_row_has_global_accent(workbook, sheet_name):
    """Row 1 of every sheet is bold + has ``F2F2F2`` fill."""
    ws = workbook[sheet_name]
    assert ws.max_row >= 1
    for cell in ws[1]:
        if cell.value is None:
            continue
        assert cell.font.bold is True, (
            f"{sheet_name}!{cell.coordinate}: header not bold"
        )
        rgb = _normalise_rgb(
            getattr(cell.fill.fgColor, "rgb", None)
            or getattr(cell.fill.start_color, "rgb", None)
        )
        assert rgb == EXPECTED_HEADER_FILL, (
            f"{sheet_name}!{cell.coordinate}: fill={rgb!r}, "
            f"expected {EXPECTED_HEADER_FILL!r}"
        )


@pytest.mark.parametrize("sheet_name", [
    "timeseries", "project", "pv", "bess", "economics",
    "simulation", "max_injection_profile", "balancing",
])
def test_non_header_rows_carry_no_persistent_fill(workbook, sheet_name):
    """Data rows (row >= 2) carry no per-cell solid fill."""
    ws = workbook[sheet_name]
    offending = []
    for row in ws.iter_rows(min_row=2, max_row=min(ws.max_row, 20)):
        for c in row:
            fill = c.fill
            if fill is None or fill.fill_type is None:
                continue
            for attr in ("fgColor", "start_color"):
                colour = getattr(fill, attr, None)
                rgb = _normalise_rgb(getattr(colour, "rgb", None))
                if rgb and rgb != "000000":
                    offending.append((c.coordinate, rgb))
                    break
    assert not offending, (
        f"{sheet_name}: data cells with unexpected fill: {offending[:5]}"
    )
