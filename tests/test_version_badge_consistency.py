"""README version badge consistency with ``pvbess_opt.__version__``."""

from __future__ import annotations

import re
from pathlib import Path

import pvbess_opt


ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

# Match a shields.io version badge of the form
# https://img.shields.io/badge/version-X.Y.Z-...
_BADGE_RE = re.compile(
    r"https://img\.shields\.io/badge/version-([0-9]+\.[0-9]+\.[0-9]+)"
)


def test_version_badge_matches_package_version():
    assert README.exists(), "README.md missing"
    text = README.read_text(encoding="utf-8")
    match = _BADGE_RE.search(text)
    assert match is not None, "version badge not found in README.md"
    badge_version = match.group(1)
    assert badge_version == pvbess_opt.__version__, (
        f"README badge claims version {badge_version!r}, but "
        f"pvbess_opt.__version__ == {pvbess_opt.__version__!r}"
    )
