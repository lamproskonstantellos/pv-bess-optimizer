"""Polish ``inputs/input.xlsx`` to the canonical pre-launch styling.

The script is idempotent: re-running it produces a byte-identical
workbook (modulo openpyxl's metadata timestamp).  Three operations are
applied in order:

1. Sweep every sheet for the prior amber bootstrap fill (``FFF2CC``)
   and reset it to *no fill*.
2. Rebuild the ``notes`` column of every parameter sheet (``project``,
   ``pv``, ``bess``, ``economics``, ``simulation``, ``balancing``) from
   the canonical templates in :mod:`pvbess_opt.io` so wording changes
   in the typed dict actually reach the workbook.
3. Apply the shared house style via
   :func:`pvbess_opt.io_style.style_worksheet`: navy ``#1F3864`` frozen
   header (white bold font, thin ``#BFBFBF`` bottom border), AutoFit
   column widths, and wrap-text on the ``notes`` column.  The same styler
   runs on every output workbook, so input and output look identical.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import PatternFill
from openpyxl.worksheet.worksheet import Worksheet

from pvbess_opt.io import _SHEET_ROW_TEMPLATES
from pvbess_opt.io_style import style_worksheet

AMBER_FILL_HEXES: frozenset[str] = frozenset({
    "FFF2CC", "00FFF2CC",
})

_PARAMETER_SHEETS: tuple[str, ...] = (
    "project", "pv", "bess", "economics", "simulation", "balancing",
)

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


def _clear_amber_fills(ws: Worksheet) -> int:
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


def _rebuild_parameter_notes(ws: Worksheet, sheet_name: str) -> int:
    """Overwrite the ``notes`` column from :data:`_SHEET_ROW_TEMPLATES`.

    The header row (row 1) carries the column names ``key | value | unit
    | notes`` written by :func:`pvbess_opt.io.write_workbook`. We locate
    the ``notes`` column from the header, build a ``key -> notes`` map
    from the row template, and rewrite every matching body cell. Keys
    not present in the template are left untouched; unknown sheets are
    a no-op.

    Returns the number of cells rewritten so the caller can log it.
    """
    template = _SHEET_ROW_TEMPLATES.get(sheet_name)
    if template is None:
        return 0
    notes_by_key = {key: notes for key, _default, _unit, notes in template}

    header = [(cell.value, cell.column) for cell in ws[1]]
    key_col = next(
        (c for v, c in header if isinstance(v, str) and v.strip().lower() == "key"),
        None,
    )
    notes_col = next(
        (c for v, c in header if isinstance(v, str) and v.strip().lower() == "notes"),
        None,
    )
    if key_col is None or notes_col is None:
        return 0

    rewritten = 0
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        key_cell = row[key_col - 1]
        if not isinstance(key_cell.value, str):
            continue
        key = key_cell.value.strip()
        new_notes = notes_by_key.get(key)
        if new_notes is None:
            continue
        notes_cell = row[notes_col - 1]
        if notes_cell.value != new_notes:
            notes_cell.value = new_notes
        rewritten += 1
    return rewritten


def polish_workbook(path: Path) -> dict[str, int]:
    """Polish ``path`` in place and return per-sheet diagnostics.

    Returned dict maps sheet name to the number of amber-fill cells
    cleared on that sheet (kept for backward compatibility with the
    earlier polish script's logging).
    """
    wb = load_workbook(path)
    cleared_by_sheet: dict[str, int] = {}
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        cleared_by_sheet[sheet_name] = _clear_amber_fills(ws)
        if sheet_name in _PARAMETER_SHEETS:
            _rebuild_parameter_notes(ws, sheet_name)
        style_worksheet(ws)
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
        logger.info(
            "%s: polished (cleared %d amber-highlighted cells, "
            "AutoFit applied, header styled, frozen at A2).",
            sheet, n,
        )


if __name__ == "__main__":
    _main()
