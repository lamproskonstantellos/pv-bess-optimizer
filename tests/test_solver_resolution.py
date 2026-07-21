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


# ---------------------------------------------------------------------------
# Achieved optimality gap capture (requested vs proven)
# ---------------------------------------------------------------------------


class _FakeProblem:
    def __init__(self, lower, upper):
        self.lower_bound = lower
        self.upper_bound = upper


class _FakeResult:
    def __init__(self, lower, upper):
        self.problem = [_FakeProblem(lower, upper)]


def test_achieved_gap_matches_solver_definition():
    from pvbess_opt.optimization import _achieved_gap_from_result

    # Maximise: lower_bound is the incumbent, upper_bound the bound;
    # gap = |bound - incumbent| / |incumbent| = 500 / 1_000_000 = 5e-4,
    # exactly the solver's own printed relative gap.
    gap = _achieved_gap_from_result(_FakeResult(1_000_000.0, 1_000_500.0))
    assert gap == pytest.approx(5e-4, rel=1e-9)


def test_achieved_gap_none_when_bounds_missing():
    from pvbess_opt.optimization import _achieved_gap_from_result

    assert _achieved_gap_from_result(_FakeResult(None, 1.0)) is None
    assert _achieved_gap_from_result(_FakeResult(1.0, None)) is None
    # An object with no ``problem`` at all must not raise.
    assert _achieved_gap_from_result(object()) is None


def test_achieved_gap_none_when_incumbent_near_zero():
    """A ~0 objective has no meaningful relative gap: report None rather
    than a huge ratio floored by a tiny denominator."""
    from pvbess_opt.optimization import _achieved_gap_from_result

    assert _achieved_gap_from_result(_FakeResult(0.0, 1e-6)) is None
    assert _achieved_gap_from_result(_FakeResult(0.5, 0.5000001)) is None
    # Just above the |incumbent| >= 1 floor: a real (tiny) gap is kept.
    gap = _achieved_gap_from_result(_FakeResult(1000.0, 1000.5))
    assert gap == pytest.approx(5e-4, rel=1e-9)


def test_achieved_gap_none_on_non_finite():
    from pvbess_opt.optimization import _achieved_gap_from_result

    assert _achieved_gap_from_result(_FakeResult(1.0, float("inf"))) is None


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_run_scenario_stashes_achieved_gap_on_frame():
    """The proven gap rides on the returned frame's .attrs so the public
    tuple signature stays unchanged; a real solve populates it."""
    import numpy as np
    import pandas as pd

    from pvbess_opt.optimization import run_scenario

    params = {
        "mode": "self_consumption", "bess_power_kw": 500.0,
        "bess_capacity_kwh": 1000.0, "efficiency_charge": 0.95,
        "efficiency_discharge": 0.95, "soc_min_frac": 0.2,
        "soc_max_frac": 0.95, "initial_soc_frac": 0.5,
        "max_cycles_per_day": 1.5, "p_grid_export_max_kw": 500.0,
        "retail_tariff_eur_per_mwh": 120.0,
        "allow_bess_grid_charging": True, "unavailability_pct": 0.0,
        "dt_minutes": 60,
    }
    n = 48
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "pv_kwh": np.maximum(np.sin(np.arange(n) * np.pi / 12) * 400, 0),
        "load_kwh": np.full(n, 200.0),
        "dam_price_eur_per_mwh": 80 + 30 * np.sin(np.arange(n) * np.pi / 12),
    })
    res, _solver = run_scenario(params, ts, "highs", mip_gap=1e-4,
                                time_limit_seconds=60)
    gap = res.attrs.get("solver_gap_achieved")
    assert gap is not None
    assert 0.0 <= gap < 1e-2  # near the requested 1e-4


def _appsi_highs_available() -> bool:
    try:
        from pyomo.contrib.appsi.solvers.highs import Highs
        return bool(Highs().available())
    except Exception:
        return False


@pytest.mark.skipif(
    not _appsi_highs_available(), reason="appsi_highs solver not available",
)
def test_appsi_highs_solves_self_consumption_without_slack_collision():
    """Regression: a decision Var literally named ``slack`` collides with
    pyomo's APPSI reserved import Suffix (the loader calls
    ``.import_enabled()`` on it), crashing every self_consumption solve
    under ``--solver appsi_highs``.  The Var is named ``export_slack``.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from conftest import _make_short_ts, _short_params

    from pvbess_opt.optimization import build_model, run_scenario

    params = _short_params("self_consumption")
    ts = _make_short_ts(24, with_load=True)
    model = build_model(params, ts)
    assert not hasattr(model, "slack")   # reserved APPSI suffix name
    assert hasattr(model, "export_slack")
    res, resolved = run_scenario(params, ts, solver_name="appsi_highs")
    assert resolved == "appsi_highs"
    assert len(res) == 24
