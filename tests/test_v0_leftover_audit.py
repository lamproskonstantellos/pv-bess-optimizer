"""v0 leftover audit (consolidated v0.5 / v0.6 / v0.7 / v0.8 contract).

Codifies the forbidden-tokens / required-tokens / files-must-exist
contract.  The grep is implemented in pure Python so it works offline
and on Windows without external tools.

Allowed locations for forbidden tokens:

* The legacy-warning paths inside :func:`pvbess_opt.io._parse_kv_sheet`
  (and the module-level ``_LEGACY_OPTIMIZATION_KEYS`` /
  ``_LEGACY_V08_REMOVED`` constants + their docstrings).
* The historical ``docs/v0.6_changelog.md`` and the new
  ``docs/v0.8_changelog.md`` migration files.
* The README's "What's new" diff and the Sphinx changelog entry.
* Migration / asset-mode docs that retain a side-by-side diff.
* Test files that exercise the legacy paths and this audit file
  itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

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

# Files whose v0.5-token mentions are intentional (warning path /
# legacy-test machinery / v0.6 migration changelog / this audit
# file).  Paths are stored relative to ROOT for portability.
FORBIDDEN_ALLOWED: frozenset[Path] = frozenset(
    Path(p) for p in (
        "pvbess_opt/io.py",
        "docs/v0.6_changelog.md",
        "docs/v0.8_changelog.md",
        "docs/source/changelog.rst",
        "docs/technical.documentation/asset_modes.md",
        "README.md",
        "tests/test_io.py",
        "tests/test_io_v08_schema.py",
        "tests/test_plot_scopes.py",
        "tests/test_v0_leftover_audit.py",
        "tests/test_economics_v08.py",
        "tests/test_bess_spec.py",
        "tests/test_asset_modes.py",
    )
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
    "capex_licenses_eur_per_kw",
    "battery_hours",
    "p_charge_max_kw",
    "p_dis_max_kw",
)

REQUIRED_TOKENS: tuple[str, ...] = (
    "pv_present",
    "bess_present",
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
    "curtailment_profile",
    '"0.8.0"',
)

REQUIRED_FILES: tuple[str, ...] = (
    "docs/v0.6_changelog.md",
    "docs/technical.documentation/uncertainty_modelling.md",
    "docs/technical.documentation/asset_modes.md",
    "pvbess_opt/plotting/lifecycle.py",
    "tests/test_io_v08_schema.py",
    "tests/test_year0_convention.py",
    "tests/test_asset_modes.py",
    "tests/test_uncertainty_config.py",
    "tests/test_plot_scopes.py",
    "tests/test_merchant_plots.py",
    "tests/test_financial_kpis_v06.py",
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


# ---------------------------------------------------------------------------
# Forbidden-tokens audit
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Required-tokens audit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", REQUIRED_TOKENS)
def test_required_token_appears_at_least_once(token: str) -> None:
    found = False
    for path in _iter_text_files():
        if token in _read_text(path):
            found = True
            break
    assert found, f"Required token {token!r} not found anywhere."


# ---------------------------------------------------------------------------
# Required files exist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relpath", REQUIRED_FILES)
def test_required_file_exists(relpath: str) -> None:
    target = ROOT / relpath
    assert target.exists() and target.is_file(), (
        f"Required file missing: {relpath}"
    )


# ---------------------------------------------------------------------------
# Inputs / fixtures audit
# ---------------------------------------------------------------------------


def test_repo_input_xlsx_round_trips_through_v08_loader_cleanly(caplog):
    """Inputs/input.xlsx must load through read_workbook with no
    legacy-key warnings — i.e. it carries the v0.8 schema."""
    import logging
    from pvbess_opt.io import read_workbook

    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io"):
        read_workbook(ROOT / "inputs" / "input.xlsx")
    legacy_warnings = [
        rec for rec in caplog.records
        if rec.levelno >= logging.WARNING
        and rec.name.startswith("pvbess_opt.io")
        and any(re.search(t, rec.getMessage())
                for t in (
                    "legacy v0.5", "plot_daily_year1",
                    "v0.7 two-sheet layout", "v0.8 dropped this",
                ))
    ]
    assert not legacy_warnings, (
        "inputs/input.xlsx still emits legacy-schema warnings: "
        + " | ".join(rec.getMessage() for rec in legacy_warnings)
    )


def test_inputs_xlsx_uses_v08_schema():
    """inputs/input.xlsx must expose the v0.8 seven-sheet typed dict."""
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
# Sanity: pvbess_opt.__version__ matches the badge token
# ---------------------------------------------------------------------------


def test_pvbess_version_string_is_exactly_0_8_0():
    import pvbess_opt
    assert pvbess_opt.__version__ == "0.8.0"
