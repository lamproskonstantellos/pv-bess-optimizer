"""Polish / migrate ``inputs/input.xlsx`` to the canonical schema + styling.

The script is idempotent: re-running it produces a byte-identical
workbook (modulo openpyxl's metadata timestamp).  Operations are applied
in order:

1. Drop the deprecated ``pv_kwh_override`` column from the ``timeseries``
   sheet — PV now lives in the single ``pv_kwh`` column.
2. Sweep every sheet for the prior amber bootstrap fill (``FFF2CC``)
   and reset it to *no fill*.
3. Rebuild every parameter sheet (``project``, ``pv``, ``bess``,
   ``economics``, ``simulation``, ``balancing``, ``ppa``) from the
   canonical row templates in :mod:`pvbess_opt.io`: existing values are
   preserved by key; rows are rewritten in template order; keys removed
   from the schema are dropped; new schema keys are added with their
   defaults; missing parameter sheets are created.  A migrated workbook
   therefore carries the same rows in the same order as a freshly
   generated one.
4. Apply the shared house style via
   :func:`pvbess_opt.io_style.style_worksheet`: navy ``#1F3864`` frozen
   header (white bold font, thin ``#BFBFBF`` bottom border), AutoFit
   column widths, and wrap-text on the ``notes`` column.  The same styler
   runs on every output workbook, so input and output look identical.
5. Center-align the header row of the per-asset max-injection sheets
   (``max_injection_profile_pv`` / ``max_injection_profile_bess``) —
   their short numeric columns read better with centered headers.  The
   general house style deliberately leaves header alignment at the
   Excel default (see :mod:`pvbess_opt.io_style`).
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
from pvbess_opt.theme import HEADER_CENTER

AMBER_FILL_HEXES: frozenset[str] = frozenset({
    "FFF2CC", "00FFF2CC",
})

_PARAMETER_SHEETS: tuple[str, ...] = (
    "project", "pv", "bess", "economics", "simulation", "balancing", "ppa",
)

# Sheets whose header row is center-aligned on top of the house style.
_CENTERED_HEADER_SHEETS: tuple[str, ...] = (
    "max_injection_profile_pv",
    "max_injection_profile_bess",
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


def _column_index(ws: Worksheet, header_name: str) -> int | None:
    """Return the 1-based column index whose header-row cell matches."""
    for cell in ws[1]:
        if isinstance(cell.value, str) and cell.value.strip().lower() == header_name:
            return int(cell.column)
    return None


def _drop_legacy_pv_override(ws: Worksheet) -> bool:
    """Delete the deprecated ``pv_kwh_override`` column, if present."""
    col = _column_index(ws, "pv_kwh_override")
    if col is None:
        return False
    ws.delete_cols(col, 1)
    return True


def _sync_param_sheet(ws: Worksheet, sheet_name: str) -> int:
    """Rebuild a parameter sheet's rows from the canonical template.

    Existing values are preserved by key; everything else is canonical:

    * rows are rewritten in TEMPLATE ORDER, so a migrated workbook and a
      freshly generated one (:func:`pvbess_opt.io.write_workbook`) carry
      the same rows in the same order;
    * keys the template adds are written with their default value / unit
      / notes;
    * rows whose key has been removed from the schema are DROPPED (the
      loader already warns-and-ignores them; carrying them in the shipped
      workbook would advertise dead knobs);
    * the ``unit`` / ``notes`` columns are rewritten so wording changes
      in the typed dict actually reach the workbook.

    Returns the number of rows written.  Unknown sheets are a no-op.
    """
    template = _SHEET_ROW_TEMPLATES.get(sheet_name)
    if template is None:
        return 0
    key_col = _column_index(ws, "key")
    value_col = _column_index(ws, "value")
    unit_col = _column_index(ws, "unit")
    notes_col = _column_index(ws, "notes")
    if key_col is None or value_col is None or unit_col is None or notes_col is None:
        return 0

    template_keys = {key for key, _default, _unit, _notes in template}
    existing: dict[str, object] = {}
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        key_cell = row[key_col - 1]
        if not isinstance(key_cell.value, str) or not key_cell.value.strip():
            continue
        key = key_cell.value.strip()
        if key not in template_keys:
            logger.info(
                "%s: dropping removed schema key %r (row %d).",
                sheet_name, key, key_cell.row,
            )
            continue
        existing[key] = row[value_col - 1].value

    last_row = max(ws.max_row, len(template) + 1)
    for r in range(2, last_row + 1):
        for c in (key_col, value_col, unit_col, notes_col):
            ws.cell(row=r, column=c).value = None

    for idx, (key, default, unit, notes) in enumerate(template):
        if key not in existing:
            logger.info(
                "%s: appending new schema key %r with its default %r.",
                sheet_name, key, default,
            )
        r = idx + 2
        ws.cell(row=r, column=key_col).value = key
        ws.cell(row=r, column=value_col).value = existing.get(key, default)
        ws.cell(row=r, column=unit_col).value = unit
        ws.cell(row=r, column=notes_col).value = notes
    return len(template)


def _center_header_row(ws: Worksheet) -> None:
    """Center-align every populated header cell of ``ws`` (row 1)."""
    for cell in ws[1]:
        if cell.value is not None:
            cell.alignment = HEADER_CENTER


def _ensure_parameter_sheets(wb) -> None:
    """Create any canonical parameter sheet the workbook does not carry.

    The schema-migration counterpart of the drop/append logic in
    :func:`_sync_param_sheet`: a NEW sheet (e.g. ``ppa``) is
    created with the ``key | value | unit | notes`` header and its
    template rows, placed after the last existing parameter sheet so
    the workbook keeps its canonical ordering.
    """
    for sheet_name in _PARAMETER_SHEETS:
        if sheet_name in wb.sheetnames:
            continue
        template = _SHEET_ROW_TEMPLATES.get(sheet_name)
        if template is None:
            continue
        anchor = max(
            (
                wb.sheetnames.index(existing)
                for existing in _PARAMETER_SHEETS
                if existing in wb.sheetnames
            ),
            default=len(wb.sheetnames) - 1,
        )
        logger.info(
            "creating missing parameter sheet %r with %d template rows.",
            sheet_name, len(template),
        )
        ws = wb.create_sheet(sheet_name, index=anchor + 1)
        ws.append(["key", "value", "unit", "notes"])
        for key, default, unit, notes in template:
            ws.append([key, default, unit, notes])


def polish_workbook(path: Path) -> dict[str, int]:
    """Polish ``path`` in place and return per-sheet diagnostics.

    Returned dict maps sheet name to the number of amber-fill cells
    cleared on that sheet (kept for backward compatibility with the
    earlier polish script's logging).
    """
    wb = load_workbook(path)
    if "timeseries" in wb.sheetnames:
        _drop_legacy_pv_override(wb["timeseries"])
    _ensure_parameter_sheets(wb)
    cleared_by_sheet: dict[str, int] = {}
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        cleared_by_sheet[sheet_name] = _clear_amber_fills(ws)
        if sheet_name in _PARAMETER_SHEETS:
            _sync_param_sheet(ws, sheet_name)
        style_worksheet(ws)
        if sheet_name in _CENTERED_HEADER_SHEETS:
            _center_header_row(ws)
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
