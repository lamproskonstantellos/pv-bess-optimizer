"""Repository-hygiene audit.

Combines two evergreen contracts into one file:

* **Version-string scan** — no forbidden version / phase / round / bug
  annotations in any ``.py`` / ``.md`` / ``.rst`` surface (regex scan).
* **Leftover-token audit** — no forbidden legacy identifiers or version
  literals in the source tree (literal-token scan), required tokens and
  files are present, ``inputs/input.xlsx`` loads through the typed
  loader without legacy-schema warnings and exposes the documented
  schema, and the package version equals the README badge.

The grep is implemented in pure Python so it works offline and on
Windows without external tools.  This file necessarily contains the
forbidden patterns and tokens as data, so it allow-lists itself in both
scans (and assembles the release-annotation tokens at runtime so the
literal strings never appear in the source).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Version-string regex scan
# ---------------------------------------------------------------------------

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
    "tests/test_repo_hygiene.py",
    # The production-readiness report is a process/audit artifact (not an
    # evergreen user surface); it necessarily records phase and
    # finding-number annotations as data, so — like this file — it
    # allow-lists itself out of the version/phase regex scan.
    "docs/production_readiness_report.md",
}
SKIP_DIR_PARTS = {
    "__pycache__", "build", ".git", "_static", "_templates",
    # Virtual-environment / tooling caches: never project source.  Without
    # these, a developer's in-tree ``.venv`` makes this whole-tree glob scan
    # the installed third-party packages and spuriously match the forbidden
    # patterns inside them (e.g. ``\bF[1-9]\b`` in matplotlib/pyomo).
    ".venv", "venv", "env", "site-packages", ".tox", ".eggs",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    # Gitignored developer workspaces / run outputs: not project source.
    "scratch", "results",
}


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


# ---------------------------------------------------------------------------
# Leftover-token audit
# ---------------------------------------------------------------------------

# Paths that are scanned by the forbidden-tokens grep.
SCAN_DIRS: tuple[Path, ...] = (
    ROOT / "pvbess_opt",
    ROOT / "scripts",
    ROOT / "tests",
    ROOT / "docs",
)
SCAN_FILES: tuple[Path, ...] = (
    ROOT / "README.md",
    ROOT / "main.py",
)

# Files whose legacy-key mentions are intentional (warning path /
# legacy-test machinery / this audit file).  Paths are stored relative to
# ROOT for portability.
FORBIDDEN_ALLOWED: frozenset[Path] = frozenset(
    Path(p) for p in (
        "tests/test_plot_scopes.py",
        "tests/test_repo_hygiene.py",
        # Audit artifact: records phase / finding-number tokens as data.
        "docs/production_readiness_report.md",
    )
)

# Release / phase / round / bug annotations are banned from every
# evergreen surface.  The tokens are assembled at runtime so this source
# file does not contain (and therefore does not flag) the literal
# strings.  Finding-number tags are covered by the word-boundary regexes
# above; bare letter-plus-digit substrings would clash with hex colour
# literals such as ``#F57C00``.
_V08 = "v0.8"
_RELEASE_ANNOTATION_TOKENS: tuple[str, ...] = (
    *(f"Phase {n}" for n in range(1, 9)),
    *(f"Round-{n}" for n in range(1, 6)),
    "Bug #",
    f"pre-{_V08}",
    f"{_V08} polish",
    "post-" "DEVEX",
    "post-" "refactor",
    "pre-" "refactor",
)

FORBIDDEN_TOKENS: tuple[str, ...] = (
    "weight_curtail_tiebreak",
    "weight_cycles_term",
    "_OPTIMIZATION_DEFAULTS",
    "_PROJECT_OPTIMIZATION_ROWS",
    "_SYSTEM_DEFAULTS",
    "plot_daily_year1",
    "HOMER convention",
    "HOMER / Gridcog",
    '"0.5.0"',
    '"0.6.0"',
    '"0.7.0"',
    '"0.8.0"',
    '"0.8.1"',
    '"0.8.2"',
    '"0.8.3"',
    "capex_licenses_eur_per_kw",
    "battery_hours",
    "p_charge_max_kw",
    "p_dis_max_kw",
    *_RELEASE_ANNOTATION_TOKENS,
)

REQUIRED_TOKENS: tuple[str, ...] = (
    "pv_present",
    "bess_present",
    "pv_kwh_override",
    "capex_year",
    "plot_daily_scope",
    "uncertainty_compare_sources",
    "lcoe_eur_per_mwh",
    "lcos_eur_per_mwh",
    "pv_capacity_factor",
    "bess_lifetime_cycles",
    "unavailability_pct",
    "aggregator_fee_pct_revenue",
    "devex_pv_eur_per_kw",
    "devex_bess_eur_per_kw",
    "site_capex_eur",
    "site_devex_eur",
    "max_injection_profile",
    "DEFAULT_MAX_INJECTION_PCT_HOURLY",
    "retail_inflation_pct",
    "dam_inflation_pct",
    "net_revenue_line",
    "FINANCIAL_LABELS",
    "FINANCIAL_LEGEND_ORDER",
    "financial_color",
    "apply_financial_legend",
    "apply_universal_margins",
)

REQUIRED_FILES: tuple[str, ...] = (
    "docs/source/technical.documentation/uncertainty_modelling.rst",
    "docs/source/technical.documentation/asset_modes.rst",
    "docs/CHANGELOG.md",
    "pvbess_opt/plotting/lifecycle.py",
    "tests/test_io_v08_schema.py",
    "tests/test_year0_convention.py",
    "tests/test_asset_modes.py",
    "tests/test_uncertainty_config.py",
    "tests/test_plot_scopes.py",
    "tests/test_merchant_plots.py",
    "tests/test_financial_kpis.py",
)


def _iter_text_files() -> list[Path]:
    """Yield every text file under SCAN_DIRS plus the SCAN_FILES."""
    skip_dirs = {"__pycache__", "build", ".git", "_static", "_templates"}
    skip_suffixes = {
        ".pyc", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".xlsx",
        ".ico", ".so", ".whl",
    }
    out: list[Path] = []
    for d in SCAN_DIRS:
        if not d.exists():
            continue
        for path in d.rglob("*"):
            if not path.is_file():
                continue
            if any(part in skip_dirs for part in path.parts):
                continue
            if path.suffix.lower() in skip_suffixes:
                continue
            out.append(path)
    for f in SCAN_FILES:
        if f.exists() and f.is_file():
            out.append(f)
    return out


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""


@pytest.mark.parametrize("token", FORBIDDEN_TOKENS)
def test_forbidden_token_returns_zero_hits(token: str) -> None:
    hits: list[str] = []
    for path in _iter_text_files():
        rel = path.relative_to(ROOT)
        if rel in FORBIDDEN_ALLOWED:
            continue
        text = _read_text(path)
        if token in text:
            for i, line in enumerate(text.splitlines(), start=1):
                if token in line:
                    hits.append(f"{rel}:{i}: {line.rstrip()}")
    assert not hits, (
        f"Forbidden token {token!r} found outside allowed paths:\n"
        + "\n".join(hits)
    )


@pytest.mark.parametrize("token", REQUIRED_TOKENS)
def test_required_token_appears_at_least_once(token: str) -> None:
    found = False
    for path in _iter_text_files():
        if token in _read_text(path):
            found = True
            break
    assert found, f"Required token {token!r} not found anywhere."


@pytest.mark.parametrize("relpath", REQUIRED_FILES)
def test_required_file_exists(relpath: str) -> None:
    target = ROOT / relpath
    assert target.exists() and target.is_file(), (
        f"Required file missing: {relpath}"
    )


# ---------------------------------------------------------------------------
# Inputs / fixtures audit
# ---------------------------------------------------------------------------


def test_repo_input_xlsx_loads_through_loader_cleanly(caplog):
    """inputs/input.xlsx must load through read_workbook with no
    legacy-key warnings."""
    import logging

    from pvbess_opt.io import read_workbook

    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io"):
        read_workbook(ROOT / "inputs" / "input.xlsx")
    legacy_warnings = [
        rec for rec in caplog.records
        if rec.levelno >= logging.WARNING
        and rec.name.startswith("pvbess_opt.io")
        and (
            "no longer supported" in rec.getMessage()
            or "legacy name of" in rec.getMessage()
        )
    ]
    assert not legacy_warnings, (
        "inputs/input.xlsx still emits legacy-schema warnings: "
        + " | ".join(rec.getMessage() for rec in legacy_warnings)
    )


def test_inputs_xlsx_uses_documented_sheet_schema():
    """inputs/input.xlsx must expose the documented typed dict."""
    from pvbess_opt.io import read_workbook
    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    for section in ("project", "pv", "bess", "economics", "simulation"):
        assert section in typed
    assert "uncertainty_enabled" in typed["simulation"]
    assert "plot_daily_scope" in typed["simulation"]
    # No legacy keys leak through.
    for section in ("project", "pv", "bess", "economics", "simulation"):
        for legacy in (
            "plot_daily_year1",
            "weight_curtail_tiebreak", "weight_cycles_term",
            "solver_mip_gap", "solver_time_limit_seconds",
            "capex_licenses_eur_per_kw",
            "battery_hours", "p_charge_max_kw", "p_dis_max_kw",
        ):
            assert legacy not in typed[section]


# ---------------------------------------------------------------------------
# Sanity: pvbess_opt.__version__ matches the README badge
# ---------------------------------------------------------------------------


def test_pvbess_version_string_matches_init_version():
    """The version exposed by the package equals the README badge."""
    import re

    import pvbess_opt

    version = pvbess_opt.__version__
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(
        r"img\.shields\.io/badge/version-([^-\s]+)-blue", readme,
    )
    assert match, "version badge not found in README.md"
    assert match.group(1) == version, (
        f"README badge {match.group(1)!r} != __version__ {version!r}"
    )
