"""Schema-integrity guards across the sheet defaults / row templates.

These invariants are load-bearing:

* ``read_economic_params`` flattens every parameter sheet into ONE dict,
  so a key name reused on two sheets would silently shadow a value.
* ``write_workbook`` renders sheets from ``_SHEET_ROW_TEMPLATES`` while
  the loader falls back to ``_SHEET_DEFAULTS`` — a default that drifts
  between the two would make a freshly generated workbook disagree with
  a workbook that omits the key.
* The per-key parsing sets (``_BOOL_KEYS`` / ``_INT_KEYS`` /
  ``_STR_KEYS`` / ``_ALLOWED_VALUES``) must reference real schema keys,
  otherwise a typo silently disables typed parsing for that key.
"""

from __future__ import annotations

from pvbess_opt.io import (
    _ALLOWED_VALUES,
    _BOOL_KEYS,
    _INT_KEYS,
    _SHEET_DEFAULTS,
    _SHEET_ROW_TEMPLATES,
    _STR_KEYS,
)


def test_keys_unique_across_sheets():
    seen: dict[str, str] = {}
    for sheet, defaults in _SHEET_DEFAULTS.items():
        for key in defaults:
            assert key not in seen, (
                f"key {key!r} declared on both {seen[key]!r} and {sheet!r}; "
                "read_economic_params flattens all sheets into one dict, so "
                "duplicate names silently shadow each other"
            )
            seen[key] = sheet


def test_row_templates_match_defaults_keys_and_values():
    for sheet, rows in _SHEET_ROW_TEMPLATES.items():
        defaults = _SHEET_DEFAULTS[sheet]
        row_keys = [r[0] for r in rows]
        assert sorted(row_keys) == sorted(defaults), (
            f"{sheet}: row template and defaults dict disagree on keys"
        )
        assert len(row_keys) == len(set(row_keys)), f"{sheet}: duplicate row key"
        for key, value, _unit, _notes in rows:
            default = defaults[key]
            if default is None or value is None:
                assert default == value, f"{sheet}.{key}"
                continue
            try:
                assert float(default) == float(value), f"{sheet}.{key}"
            except (TypeError, ValueError):
                assert default == value, f"{sheet}.{key}"


def test_typed_parsing_sets_reference_real_keys():
    all_keys = {k for d in _SHEET_DEFAULTS.values() for k in d}
    for name, group in (
        ("_BOOL_KEYS", _BOOL_KEYS),
        ("_INT_KEYS", _INT_KEYS),
        ("_STR_KEYS", _STR_KEYS),
        ("_ALLOWED_VALUES", set(_ALLOWED_VALUES)),
    ):
        unknown = set(group) - all_keys
        assert not unknown, f"{name} references unknown keys: {sorted(unknown)}"
