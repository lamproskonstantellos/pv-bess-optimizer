"""Grep audits 1–7 from the round-3 universality addendum.

These tests treat the addendum's command-line grep audits as
regression checks.  Each test runs the same regex over every public
plotting module and fails if a forbidden pattern reappears.  Module
state is not introspected — we look directly at the source so the
audits survive even when a function is unused at runtime.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PLOTTING_DIR = Path(__file__).resolve().parents[1] / "pvbess_opt" / "plotting"
STYLE_PY = PLOTTING_DIR / "style.py"


def _plotting_sources() -> dict[Path, str]:
    """Return ``{path: source_text}`` for every plotting module other
    than ``style.py``.

    ``style.py`` is excluded because it owns ``annotate_value_safe``,
    the one helper that legitimately wraps ``ax.text(...bbox=...)``.
    """
    out: dict[Path, str] = {}
    for path in sorted(PLOTTING_DIR.glob("*.py")):
        if path.name in ("__init__.py", "style.py"):
            continue
        out[path] = path.read_text(encoding="utf-8")
    return out


def _strip_comments(src: str) -> str:
    return re.sub(r"#.*$", "", src, flags=re.MULTILINE)


# ---------------------------------------------------------------------------
# Audit 1 — inline ax.annotate(...bbox=...) with a numeric value
# Audit 2 — inline ax.text(...bbox=...) with a numeric value
# (combined; either form is forbidden outside annotate_value_safe)
# ---------------------------------------------------------------------------


def test_audit_1_2_no_inline_annotate_or_text_with_bbox():
    pattern = re.compile(
        r'\bax\d*\.(annotate|text)\([^)]*bbox\s*=', re.DOTALL,
    )
    offenders: list[str] = []
    for path, src in _plotting_sources().items():
        if pattern.search(_strip_comments(src)):
            offenders.append(path.name)
    assert not offenders, (
        f"Inline ax.text(... bbox=...) / ax.annotate(... bbox=...) "
        f"calls found in: {offenders}.  Route through "
        "annotate_value_safe in style.py."
    )


# ---------------------------------------------------------------------------
# Audit 3 — no markeredgecolor='white' anywhere in plotting code
# ---------------------------------------------------------------------------


def test_audit_3_no_white_marker_edge():
    pattern = re.compile(r'markeredgecolor\s*=\s*["\']white["\']')
    offenders: list[str] = []
    for path, src in _plotting_sources().items():
        if pattern.search(_strip_comments(src)):
            offenders.append(path.name)
    assert not offenders, (
        f"markeredgecolor='white' found in: {offenders}.  Round-3 "
        "universality rule forbids white marker-edge rings."
    )


# ---------------------------------------------------------------------------
# Audit 4 — no italic prose captions (fontstyle='italic')
# ---------------------------------------------------------------------------


def test_audit_4_no_italic_prose_captions():
    pattern = re.compile(r'fontstyle\s*=\s*["\']italic["\']')
    offenders: list[str] = []
    for path, src in _plotting_sources().items():
        if pattern.search(_strip_comments(src)):
            offenders.append(path.name)
    assert not offenders, (
        f"fontstyle='italic' found in: {offenders}.  Free-floating "
        "italic prose is not allowed in round-3 plots."
    )


# ---------------------------------------------------------------------------
# Audit 5 — date format strings match the plot resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename,allowed", [
    ("daily.py",   ('"%H:%M"', "'%H:%M'")),
    ("monthly.py", ('"%d-%m-%Y"', "'%d-%m-%Y'")),
    ("yearly.py",  ('"%m-%Y"', "'%m-%Y'")),
])
def test_audit_5_date_formats_match_resolution(filename, allowed):
    """Every DateFormatter(...) call in a resolution-specific module
    uses the format string that matches the plot's resolution."""
    src = (PLOTTING_DIR / filename).read_text(encoding="utf-8")
    src = _strip_comments(src)
    for match in re.finditer(r"DateFormatter\(([^)]+)\)", src):
        spec = match.group(1).strip()
        assert any(token in spec for token in allowed), (
            f"{filename}: DateFormatter({spec}) does not match the "
            f"resolution's allowed format strings {allowed}"
        )


# ---------------------------------------------------------------------------
# Audit 6 — no inline hex colour literals in plotting code
# ---------------------------------------------------------------------------


def test_audit_6_no_inline_hex_colour_literals():
    pattern = re.compile(r'["\']#[0-9A-Fa-f]{6}["\']')
    offenders: dict[str, list[str]] = {}
    for path, src in _plotting_sources().items():
        matches = pattern.findall(_strip_comments(src))
        if matches:
            offenders[path.name] = matches
    assert not offenders, (
        f"Inline hex colour literal(s) found: {offenders}.  Source "
        "every colour from FINANCIAL_COLORS / financial_color / "
        "COLORS in pvbess_opt.config."
    )


# ---------------------------------------------------------------------------
# Audit 7 — financial / lifecycle / uncertainty plot labels are
# canonical (informational; routed through apply_financial_legend).
# ---------------------------------------------------------------------------


def test_audit_7_financial_labels_route_through_legend_helper():
    """Each module that emits canonical financial labels also imports
    ``apply_financial_legend`` so legend ordering is governed by the
    single source of truth.  This is the soft side of Audit 7 — the
    strict label match is enforced at render time by
    :func:`pvbess_opt.config.apply_financial_legend` itself, which
    logs warnings for non-canonical entries (covered by
    tests/test_financial_label_consistency.py)."""
    for filename in ("financial.py", "lifecycle.py"):
        src = (PLOTTING_DIR / filename).read_text(encoding="utf-8")
        assert "apply_financial_legend" in src, (
            f"{filename}: must import apply_financial_legend so legend "
            "ordering follows FINANCIAL_LEGEND_ORDER."
        )


# ---------------------------------------------------------------------------
# Sanity: annotate_value_safe is the only place ax.text(...bbox=...) lives
# ---------------------------------------------------------------------------


def test_annotate_value_safe_is_the_only_bbox_text_site():
    """style.py::annotate_value_safe contains a single ax.text(... bbox=...)
    call.  Verify it's still there — the rest of the plotting code routes
    every bbox annotation through this helper.
    """
    src = STYLE_PY.read_text(encoding="utf-8")
    assert "def annotate_value_safe(" in src
    # The helper builds bbox_kwargs and passes them through ax.text.
    assert re.search(r"return ax\.text\(", src), (
        "style.py::annotate_value_safe should call ax.text(...) once"
    )
