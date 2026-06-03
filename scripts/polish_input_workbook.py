"""Polish ``inputs/input.xlsx`` to the canonical pre-launch styling.

The script is idempotent: re-running it produces a byte-identical
workbook (modulo openpyxl's metadata timestamp).  Five operations are
applied in order:

1. Sweep every sheet for the prior amber bootstrap fill (``FFF2CC``)
   and reset it to *no fill*.
2. Rebuild the ``notes`` column of every parameter sheet (``project``,
   ``pv``, ``bess``, ``economics``, ``simulation``, ``balancing``) from
   the canonical templates in :mod:`pvbess_opt.io` so wording changes
   in the typed dict actually reach the workbook.
3. AutoFit each column to ``min(80, max(10, max_cell_width + 2))``.
   The ``timeseries`` sheet is large (~35 040 rows of uniform floats);
   we sample the first 200 data rows plus the header to keep this
   bounded.
4. Apply the minimal global header accent on row 1 of every sheet:
   navy fill ``#1F3864``, white bold font, thin ``#BFBFBF`` bottom
   border.  No banded rows, no per-cell fills below row 1.
5. Wrap the ``notes`` column on every parameter sheet for readable
   long descriptions under the column-width cap, then freeze row 1
   (``ws.freeze_panes = "A2"``).
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from pvbess_opt.io import _SHEET_ROW_TEMPLATES

AMBER_FILL_HEXES: frozenset[str] = frozenset({
    "FFF2CC", "00FFF2CC",
})

# Minimal navy header style. Excel "Dark Blue 1, Lighter 25%" — chosen
# deliberately to read well as a single global accent against an
# otherwise unstyled body.
HEADER_FILL_HEX: str = "1F3864"
HEADER_FONT_HEX: str = "FFFFFF"
HEADER_BORDER_HEX: str = "BFBFBF"

# AutoFit clamps. The lower bound keeps narrow integer columns readable;
# the upper bound prevents a single long "notes" cell from inflating
# the column past usable width (the notes column gets wrap-text instead).
MIN_COL_WIDTH: float = 10.0
MAX_COL_WIDTH: float = 80.0
COL_WIDTH_PADDING: float = 2.0

# How many data rows to sample when computing the AutoFit width of the
# ``timeseries`` sheet. The columns there are uniform 15-min floats so
# a small head sample matches a full scan.
_TIMESERIES_AUTOFIT_SAMPLE_ROWS: int = 200

_PARAMETER_SHEETS: tuple[str, ...] = (
    "project", "pv", "bess", "economics", "simulation", "balancing", "ppa",
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


def _column_text_width(value: object) -> int:
    """Return the printed character width of ``value`` (longest line)."""
    if value is None:
        return 0
    text = str(value)
    if not text:
        return 0
    return max(len(line) for line in text.splitlines())


def _autofit_columns(ws: Worksheet, *, sample_rows: int | None = None) -> None:
    """Set every column's width from the max text width in that column.

    ``sample_rows`` caps the body scan — used on the ``timeseries``
    sheet to avoid scanning 35 040 uniform rows. The header is always
    measured regardless of the cap.
    """
    max_col = ws.max_column
    max_row = ws.max_row
    if max_col < 1 or max_row < 1:
        return

    # Header (row 1) always measured.
    widths = [
        _column_text_width(ws.cell(row=1, column=c).value)
        for c in range(1, max_col + 1)
    ]

    if sample_rows is None:
        scan_last_row = max_row
    else:
        scan_last_row = min(max_row, 1 + int(sample_rows))

    if scan_last_row >= 2:
        for r in range(2, scan_last_row + 1):
            for c in range(1, max_col + 1):
                width = _column_text_width(ws.cell(row=r, column=c).value)
                if width > widths[c - 1]:
                    widths[c - 1] = width

    for idx, raw in enumerate(widths, start=1):
        width = max(
            MIN_COL_WIDTH,
            min(MAX_COL_WIDTH, float(raw) + COL_WIDTH_PADDING),
        )
        ws.column_dimensions[get_column_letter(idx)].width = width


def _apply_header_style(ws: Worksheet) -> None:
    fill = PatternFill(
        start_color=HEADER_FILL_HEX,
        end_color=HEADER_FILL_HEX,
        fill_type="solid",
    )
    font = Font(bold=True, color=HEADER_FONT_HEX)
    border = Border(bottom=Side(border_style="thin", color=HEADER_BORDER_HEX))
    if ws.max_row < 1:
        return
    for cell in ws[1]:
        cell.font = font
        cell.fill = fill
        cell.border = border


def _wrap_notes_column(ws: Worksheet) -> None:
    """Wrap text on the ``notes`` column of a parameter sheet."""
    header = [(cell.value, cell.column) for cell in ws[1]]
    notes_col = next(
        (c for v, c in header if isinstance(v, str) and v.strip().lower() == "notes"),
        None,
    )
    if notes_col is None:
        return
    alignment = Alignment(wrap_text=True, vertical="top")
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        cell = row[notes_col - 1]
        cell.alignment = alignment


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

        sample_rows = (
            _TIMESERIES_AUTOFIT_SAMPLE_ROWS
            if sheet_name == "timeseries" else None
        )
        _autofit_columns(ws, sample_rows=sample_rows)
        _apply_header_style(ws)

        if sheet_name in _PARAMETER_SHEETS:
            _wrap_notes_column(ws)

        ws.freeze_panes = "A2"
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
