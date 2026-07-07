"""Solver resolution is provenance-safe: no silent substitution.

The solver identity is part of the results' provenance (run log,
SUMMARY.md, any solver statement in a publication), so requesting a
solver that is not available must be a hard error listing the installed
alternatives — never a quiet fallback to a different solver.
"""

from __future__ import annotations

import pytest

from pvbess_opt.optimization import choose_solver


def _highs_available() -> bool:
    try:
        import highspy  # noqa: F401
        return True
    except Exception:
        return False


def _gurobi_available() -> bool:
    try:
        import gurobipy  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_requested_available_solver_is_returned_verbatim():
    solver, resolved = choose_solver("highs")
    assert resolved == "highs"
    assert solver is not None


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_default_is_highs():
    _solver, resolved = choose_solver(None)
    assert resolved == "highs"


def test_unknown_solver_raises_instead_of_falling_back():
    with pytest.raises(RuntimeError) as excinfo:
        choose_solver("no_such_solver")
    msg = str(excinfo.value)
    assert "no_such_solver" in msg
    assert "not available" in msg
    # The error is actionable: it names what IS installed (or says
    # nothing else is), and explains the no-substitution rule.
    assert "installed" in msg
    assert "never" in msg and "substituted" in msg


@pytest.mark.skipif(
    _gurobi_available(), reason="gurobipy installed - fallback not exercised",
)
def test_unavailable_gurobi_raises_instead_of_silently_using_highs():
    """The exact trap this contract prevents: --solver gurobi on a
    machine without gurobipy must stop the run, not quietly produce
    HiGHS results."""
    with pytest.raises(RuntimeError) as excinfo:
        choose_solver("gurobi")
    msg = str(excinfo.value)
    assert "gurobi" in msg
    assert "gurobipy" in msg  # the install hint


def test_gurobi_gets_memory_safety_defaults():
    """Gurobi solves carry NodefileStart/NodefileDir so a huge
    branch-and-bound tree spills to disk instead of being OOM-killed.
    Node files are transparent to the search (identical decisions,
    incumbents and bounds), so this is a pure survival net."""
    from pvbess_opt.optimization import configure_solver_options

    class _FakeSolver:
        def __init__(self):
            self.options = {}

    solver = _FakeSolver()
    configure_solver_options(solver, "gurobi", mip_gap=1e-4,
                             time_limit_seconds=600)
    assert solver.options["NodefileStart"] == 8
    assert solver.options["NodefileDir"]
    assert solver.options["MIPGap"] == 1e-4

    highs = _FakeSolver()
    configure_solver_options(highs, "highs", mip_gap=1e-4,
                             time_limit_seconds=600)
    assert "NodefileStart" not in highs.options
