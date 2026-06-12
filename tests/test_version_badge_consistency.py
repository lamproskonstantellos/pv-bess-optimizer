"""Version consistency: README badge, citation block, CITATION.cff
all agree with ``pvbess_opt.__version__``."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

import pvbess_opt

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
CITATION_CFF = ROOT / "CITATION.cff"

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


def test_readme_citation_versions_match_package_version():
    """The Citing section (plain citation + BibTeX) carries the live
    version string."""
    text = README.read_text(encoding="utf-8")
    version = pvbess_opt.__version__
    plain = re.search(r"PV & BESS Optimizer \(v([0-9.]+)\)", text)
    assert plain is not None, "plain citation line not found in README"
    assert plain.group(1) == version
    bibtex = re.search(r"version\s*=\s*\{([0-9.]+)\}", text)
    assert bibtex is not None, "BibTeX version field not found in README"
    assert bibtex.group(1) == version


def test_citation_cff_matches_package_version():
    """CITATION.cff parses as YAML, carries the required CFF 1.2.0
    fields, and its version matches ``pvbess_opt.__version__``."""
    assert CITATION_CFF.exists(), "CITATION.cff missing at the repo root"
    data = yaml.safe_load(CITATION_CFF.read_text(encoding="utf-8"))
    assert data["cff-version"] == "1.2.0"
    for field in ("message", "title", "authors", "version",
                  "date-released", "repository-code"):
        assert field in data, f"CITATION.cff missing {field!r}"
    assert data["version"] == pvbess_opt.__version__
    assert data["authors"][0]["family-names"] == "Konstantellos"
    # date-released must be a real ISO date (yaml parses it to date).
    import datetime

    released = data["date-released"]
    if isinstance(released, str):
        released = datetime.date.fromisoformat(released)
    assert isinstance(released, datetime.date)
