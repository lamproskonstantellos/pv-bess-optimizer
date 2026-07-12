"""Conformance tests: lock docs/* against pvbess_opt.

The two canonical design docs at ``docs/self_consumption_design.md``
and ``docs/balancing_market_design.md`` describe the MILP contract in
prose. This test parses the symbol names out of those files and asserts
each one is attached to a freshly built Pyomo model, so adding a
constraint in code without documenting it (or vice versa) breaks CI.

Three checks:

* Self-consumption constraints listed under "Hard constraints"
  (``### NAME(t)``) exist on a ``mode = self_consumption`` model.
* Audit invariants under "Ten audit invariants" appear in
  :func:`pvbess_opt.optimization.verify_dispatch_invariants` output
  and stay within :data:`pvbess_opt.kpis.ENERGY_TOLERANCE`.
* Symbols claimed PASS in the verification appendix of the balancing
  design doc (``BM_POWER_DN``, ``BM_POWER_UP``, ``BM_SOC_UP``,
  ``BM_SOC_DN``, ``r_balancing``) exist on a balancing-enabled model.
"""

from __future__ import annotations

import re
from pathlib import Path

from pvbess_opt.kpis import ENERGY_TOLERANCE
from pvbess_opt.optimization import (
    build_model,
    run_scenario,
    verify_dispatch_invariants,
)
from tests._balancing_helpers import _balancing_on

ROOT = Path(__file__).resolve().parent.parent
SELF_CONSUMPTION_SPEC = ROOT / "docs" / "self_consumption_design.md"
BALANCING_VERIFICATION = ROOT / "docs" / "balancing_market_design.md"

# Sections in the self-consumption spec are organised under H2 anchors;
# the constraint names live as H3s ("### NAME(t)" or "### NAME, ...") and
# the invariant names live as H3s under the "Ten audit invariants"
# section.
_CONSTRAINT_SECTION_HEADING = "Hard constraints: formal statements"
_INVARIANT_SECTION_HEADING = "Ten audit invariants"

_BALANCING_SYMBOLS = (
    "BM_POWER_DN", "BM_POWER_UP", "BM_SOC_UP", "BM_SOC_DN", "r_balancing",
)


# ---------------------------------------------------------------------------
# Markdown parsing
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section_body(text: str, h2_title: str) -> str:
    """Return the body of the H2 section whose title matches ``h2_title``.

    Match is fuzzy: the doc may prefix the H2 with a numeric anchor
    (``## 3. Hard constraints``) so the regex skips an optional
    ``<number>.`` prefix.
    """
    pattern = re.compile(
        rf"^##\s+(?:\d+\.\s+)?{re.escape(h2_title)}\s*$(?P<body>.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if match is None:
        raise ValueError(
            f"section ## {h2_title!r} not found in spec; "
            "did the heading get renamed?"
        )
    return match.group("body")


_H3_CONSTRAINT_TOKEN_RE = re.compile(r"[A-Z][A-Z0-9_]{2,}")


def _h3_constraint_names(body: str) -> list[str]:
    """Pull constraint names from H3 headings.

    Each H3 in the spec is one of:

    * ``### NAME(t)`` — single constraint.
    * ``### NAME(t), OTHER(t), THIRD(t)`` — several constraints sharing a
      section (e.g. ``CH_LIM(t), DIS_LIM(t), MODE_LINK(t)``); every
      comma-separated name is asserted present.
    * ``### NAME, NAME_ALT / NAME_MIN / NAME_MAX`` — the part after the
      first ``/`` is a mutually-exclusive alternation (e.g.
      ``SOC_TERM / SOC_TERM_MIN / SOC_TERM_MAX`` — only one branch is on a
      built model, gated by ``terminal_soc_equal``).  Names before the
      slash are unconditional and asserted; the alternation branches are
      intentionally not (only the first, ``SOC_TERM``, before the slash is
      kept).

    Harvesting every UPPER_SNAKE token in the pre-slash heading (rather
    than stopping at the first ``(``) makes the conformance check cover
    the trailing names of multi-constraint sections — previously
    ``DIS_LIM``, ``MODE_LINK`` and ``NO_SIM_GRID_EXPORT`` were silently
    skipped.
    """
    out: list[str] = []
    for line in body.splitlines():
        if not line.startswith("### "):
            continue
        heading = line[4:].split("/", 1)[0]
        out.extend(_H3_CONSTRAINT_TOKEN_RE.findall(heading))
    return out


_INVARIANT_H3_RE = re.compile(
    r"^###\s+(invariant_\d+_[a-z0-9_]+)",
    re.MULTILINE,
)


def _h3_invariant_names(body: str) -> list[str]:
    return _INVARIANT_H3_RE.findall(body)


# ---------------------------------------------------------------------------
# Fixture builders (mirrors the conftest short_* fixtures so this test
# stays self-contained and can be run in isolation).
# ---------------------------------------------------------------------------


def _self_consumption_params() -> dict:
    return {
        "dt_minutes": 60,
        "efficiency_charge": 0.97,
        "efficiency_discharge": 0.97,
        "soc_min_frac": 0.20,
        "soc_max_frac": 0.95,
        "initial_soc_frac": 0.50,
        "terminal_soc_equal": True,
        "max_cycles_per_day": 1.0,
        "p_grid_export_max_kw": 5000.0,
        "pv_nameplate_kwp": 4500.0,
        "bess_power_kw": 5000.0,
        "bess_capacity_kwh": 20000.0,
        "retail_tariff_eur_per_mwh": 120.0,
        "mode": "self_consumption",
        # Conformance fixture enables grid charging so the optional
        # GRID_CHARGE_GATE / GRID_CHG_PV_GATE constraints documented in
        # §3 of the spec are attached to the built model; the regex
        # parses every documented name unconditionally.
        "allow_bess_grid_charging": True,
        # Same opt-in for IMPORT_CAP (Eq. S35): a generous finite cap
        # attaches the constraint without ever binding.
        "p_grid_import_max_kw": 100_000.0,
        "show_titles": False,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_self_consumption_spec_constraints_present(short_ts):
    """Every ### symbol under §3 maps to a Pyomo attribute."""
    body = _section_body(_read(SELF_CONSUMPTION_SPEC), _CONSTRAINT_SECTION_HEADING)
    expected = _h3_constraint_names(body)

    # Sanity: the spec must list a non-trivial number of constraints —
    # if the markdown parsing collapses to zero, the regex went stale.
    assert len(expected) >= 12, (
        f"parsed only {len(expected)} constraint names from the spec; "
        "expected at least 12 (regex likely stale)."
    )

    params = _self_consumption_params()
    model = build_model(params, short_ts)
    missing = [
        name for name in expected
        if not hasattr(model, name)
    ]
    assert not missing, (
        "self-consumption MILP is missing constraints documented in the spec: "
        f"{missing}. Either add the constraint to the model or remove the "
        "H3 from docs/self_consumption_design.md."
    )


def test_self_consumption_invariants_within_tolerance(short_ts):
    """Every documented audit invariant is reported within tolerance."""
    body = _section_body(_read(SELF_CONSUMPTION_SPEC), _INVARIANT_SECTION_HEADING)
    expected = _h3_invariant_names(body)
    assert len(expected) == 10, (
        f"parsed {len(expected)} invariant names from the spec; "
        "expected exactly 10."
    )

    params = _self_consumption_params()
    _res, _solver, res_full = run_scenario(
        params, short_ts, solver_name="highs", return_unrounded=True,
    )
    invariants = verify_dispatch_invariants(res_full, params, mode="self_consumption")

    for name in expected:
        assert name in invariants, (
            f"invariant {name!r} listed in the spec but missing from "
            "verify_dispatch_invariants output."
        )
        # invariant_6 is a count, others are residual kWh; both should
        # be ≤ ENERGY_TOLERANCE for a clean solve.
        assert invariants[name] <= ENERGY_TOLERANCE, (
            f"{name} = {invariants[name]:.4g} exceeds tolerance "
            f"{ENERGY_TOLERANCE}."
        )


def test_balancing_verification_symbols_present(short_ts):
    """The PASS symbols in the balancing appendix exist on a model."""
    text = _read(BALANCING_VERIFICATION)
    # Cheap sanity: every documented symbol must actually appear in the
    # verification doc (catches a rename that bypassed the doc).
    for symbol in _BALANCING_SYMBOLS:
        assert symbol in text, (
            f"symbol {symbol!r} is asserted by the conformance test but "
            "is not mentioned in docs/balancing_market_design.md."
        )

    params = _balancing_on(_self_consumption_params())
    model = build_model(params, short_ts)
    missing = [s for s in _BALANCING_SYMBOLS if not hasattr(model, s)]
    assert not missing, (
        "balancing-enabled MILP is missing symbols claimed PASS in "
        f"docs/balancing_market_design.md: {missing}."
    )
