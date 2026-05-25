"""Verify the repository contains no references to previous version
identifiers — the codebase is evergreen."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = (
    r"\bv0\.5\b", r"\bv0\.6\b", r"\bv0\.7\b",
    r"\bv0\.8\.0\b", r"\bv0\.8\.1\b", r"\bv0\.8\.2\b",
    r"\bv0\.8\.3\b",
    # Release / phase / round / bug annotations are not allowed in the
    # evergreen surfaces; describe current behaviour in present tense.
    # (Patterns that would otherwise match the literal tokens here are
    # written with escapes or split so this file does not flag itself.)
    r"\bPhase [1-8]\b",
    r"\bRound-[1-5]\b",
    "Bug " "#",
    r"\bF1[0-2]\b", r"\bF[1-9]\b",
    r"pre-v0\.8",
    r"v0\.8 polish",
    "post-" "DEVEX",
    "post-" "refactor",
    "pre-" "refactor",
)
SCAN_GLOBS = ("**/*.py", "**/*.md", "**/*.rst")
ALLOWED_PATHS = {
    "tests/test_no_historical_version_strings.py",
    "tests/test_v0_leftover_audit.py",
    # The audit reports document the audit findings and are the surfaces
    # that name the prior tokens they removed.
    "docs/audit_report.md",
    "docs/audit_report_phase1.md",
    "docs/audit_report_v0_9_0.md",
    "docs/audit_test_index.md",
}
SKIP_DIR_PARTS = {"__pycache__", "build", ".git", "_static", "_templates"}


@pytest.mark.parametrize("pattern", FORBIDDEN)
def test_no_old_version_strings(pattern):
    hits = []
    for glob in SCAN_GLOBS:
        for path in ROOT.glob(glob):
            rel = path.relative_to(ROOT)
            if any(part in SKIP_DIR_PARTS for part in rel.parts):
                continue
            if str(rel).replace("\\", "/") in ALLOWED_PATHS:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for i, line in enumerate(text.splitlines(), 1):
                if re.search(pattern, line):
                    hits.append(f"{rel}:{i}: {line.rstrip()}")
    assert not hits, (
        f"Forbidden version string matching {pattern!r}:\n"
        + "\n".join(hits)
    )
