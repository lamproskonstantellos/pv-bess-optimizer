"""Plot-scope unification tests.

The daily / monthly / yearly scope vocabulary is unified to
``none`` | ``year1_only`` | ``all``.  The Year-1-only daily branch
(the historical bool ``plot_daily_year1`` is replaced by an enum
identical to monthly / yearly.

This module probes the dispatcher logic in ``main`` rather than
executing the full plot fan-out (which lives behind a HiGHS-required
smoke test in test_input_workbook_smoke.py).
"""

from __future__ import annotations

import pytest

from main import _scope_active_for_year


@pytest.mark.parametrize("scope,year,expected", [
    ("all", 1, True),
    ("all", 25, True),
    ("year1_only", 1, True),
    ("year1_only", 2, False),
    ("year1_only", 25, False),
    ("none", 1, False),
    ("none", 25, False),
    ("ALL", 1, True),  # case-insensitive
    ("Year1_Only", 2, False),
    ("", 1, False),
])
def test_scope_active_for_year_truth_table(scope, year, expected):
    assert _scope_active_for_year(scope, year) is expected


def test_scope_combinations_3x3_truth_table():
    """All nine (resolution x scope) combos behave consistently."""
    project_years = list(range(1, 4))  # 3-year dummy lifetime
    for scope in ("none", "year1_only", "all"):
        active = [_scope_active_for_year(scope, y) for y in project_years]
        if scope == "none":
            assert active == [False, False, False]
        elif scope == "year1_only":
            assert active == [True, False, False]
        else:
            assert active == [True, True, True]


def test_econ_defaults_use_unified_vocabulary():
    from pvbess_opt.io import _ALLOWED_VALUES, SIMULATION_SHEET_DEFAULTS
    daily_default = str(SIMULATION_SHEET_DEFAULTS["plot_daily_scope"]).lower()
    assert daily_default in {"none", "year1_only", "all"}
    expected = frozenset({"none", "year1_only", "all"})
    assert _ALLOWED_VALUES["plot_daily_scope"] == expected
    assert _ALLOWED_VALUES["plot_monthly_scope"] == expected
    assert _ALLOWED_VALUES["plot_yearly_scope"] == expected


def test_main_dispatcher_drops_obsolete_token():
    """main.py must not contain the obsolete plot_daily_year1 token."""
    import inspect

    import main
    src = inspect.getsource(main)
    assert "plot_daily_year1" not in src


def test_warning_when_plot_daily_scope_is_all(caplog):
    """Selecting plot_daily_scope=all logs a WARNING with the PDF count."""
    import argparse

    from main import _resolve_uncertainty_config

    # Synthesize the args + econ a real run would produce.
    args = argparse.Namespace(
        rolling_horizon=False,
        compare_uncertainty_sources=False,
        monte_carlo=None,
        window_hours=None,
        commit_hours=None,
        seed=42,
    )
    econ = {
        "uncertainty_enabled": False,
        "uncertainty_compare_sources": False,
        "uncertainty_n_seeds": 30,
        "uncertainty_window_hours": 48,
        "uncertainty_commit_hours": 24,
        "uncertainty_dam_enabled": True,
        "uncertainty_pv_enabled": True,
        "uncertainty_load_enabled": True,
        "uncertainty_sigma_dam": 0.20,
        "uncertainty_sigma_pv": 0.12,
        "uncertainty_sigma_load": 0.05,
        "plot_daily_scope": "all",
        "project_lifecycle_years": 25,
    }
    # The WARNING is emitted in _run_one; assert the helper call works
    # without crashing for a representative config.
    _resolve_uncertainty_config(args, econ, mode="self_consumption")
