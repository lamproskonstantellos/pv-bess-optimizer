"""Polish ``inputs/input.xlsx`` to the canonical pre-launch styling.

Idempotent: re-running produces the same workbook (modulo the
non-deterministic timestamp that openpyxl writes into the file
metadata).

Two operations:

1. Sweep every cell in every sheet for the prior amber highlight
   (``FFF2CC``) used by the balancing-feature bootstrap script and
   reset its fill to *no fill*.
2. Apply a single global header accent to row 1 of every sheet:
   bold font, ``#F2F2F2`` fill, and a thin ``#BFBFBF`` bottom border.
   No other styling is applied anywhere else in the workbook.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Border, Font, PatternFill, Side

AMBER_FILL_HEXES: frozenset[str] = frozenset({
    "FFF2CC", "00FFF2CC",
})
HEADER_FILL_HEX: str = "F2F2F2"
HEADER_BORDER_HEX: str = "BFBFBF"

logger = logging.getLogger(__name__)


def _is_amber_fill(fill: PatternFill) -> bool:
    for attr in ("fgColor", "start_color"):
        colour = getattr(fill, attr, None)
        if colour is None:
            continue
        rgb = getattr(colour, "rgb", None)
        if not isinstance(rgb, str):
            continue
        if rgb.upper() in AMBER_FILL_HEXES:
            return True
    return False


def _clear_amber_fills(ws) -> int:
    cleared = 0
    blank = PatternFill(fill_type=None)
    for row in ws.iter_rows():
        for cell in row:
            fill = cell.fill
            if fill is None or fill.fill_type is None:
                continue
            if _is_amber_fill(fill):
                cell.fill = blank
                cleared += 1
    return cleared


def _apply_header_style(ws) -> None:
    fill = PatternFill(start_color=HEADER_FILL_HEX,
                       end_color=HEADER_FILL_HEX, fill_type="solid")
    border = Border(bottom=Side(border_style="thin", color=HEADER_BORDER_HEX))
    if ws.max_row < 1:
        return
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = fill
        cell.border = border


def polish_workbook(path: Path) -> dict[str, int]:
    """Polish ``path`` in place and return a per-sheet diagnostics dict."""
    wb = load_workbook(path)
    cleared_by_sheet: dict[str, int] = {}
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        cleared_by_sheet[sheet_name] = _clear_amber_fills(ws)
        _apply_header_style(ws)
    wb.save(path)
    return cleared_by_sheet


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path", nargs="?", default="inputs/input.xlsx", type=Path,
        help="Workbook to polish (default: inputs/input.xlsx).",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cleared = polish_workbook(args.path)
    for sheet, n in cleared.items():
        logger.info("%s: cleared %d amber-highlighted cells.", sheet, n)


if __name__ == "__main__":
    _main()
