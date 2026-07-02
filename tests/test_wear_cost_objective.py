"""Solver-free wear-cost invariants on the MILP objective.

The battery wear cost ``bess_wear_cost_eur_per_mwh`` is a dispatch
shadow price: it enters the optimization objective ONLY, as
``-wear x (bess_dis_load + bess_dis_grid) / 1000`` summed over steps.
It never appears in the output frame, the KPI layer, the finance layer
or the lifetime projection, so it can never be double-counted against
the replacement CAPEX charged in the cashflow.

These tests build the Pyomo model without solving it, so they run on
any environment (no HiGHS needed) and complement the end-to-end guard
``tests/test_degradation.py::
test_wear_cost_suppresses_cycles_and_is_not_double_counted``.
"""

from __future__ import annotations

import pandas as pd
import pyomo.environ as pyo
import pytest

from pvbess_opt.optimization import build_model, model_to_dataframe

WEAR = 12.5  # EUR/MWh, arbitrary non-round value


def _params(wear: float) -> dict:
    return {
        "dt_minutes": 60,
        "efficiency_charge": 0.95,
        "efficiency_discharge": 0.95,
        "soc_min_frac": 0.10,
        "soc_max_frac": 0.95,
        "initial_soc_frac": 0.50,
        "terminal_soc_equal": False,
        "max_cycles_per_day": 10.0,
        "p_grid_export_max_kw": 1_000.0,
        "pv_nameplate_kwp": 100.0,
        "bess_power_kw": 50.0,
        "bess_capacity_kwh": 200.0,
        "retail_tariff_eur_per_mwh": 200.0,
        "mode": "self_consumption",
        "allow_bess_grid_charging": False,
        "bess_wear_cost_eur_per_mwh": wear,
    }


def _ts(n: int = 6) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=n, freq="h"),
        "pv_kwh": [80.0] * n,
        "load_kwh": [20.0] * n,
        "dam_price_eur_per_mwh": [60.0] * n,
    })


def _zero_all_variables(model: pyo.ConcreteModel) -> None:
    for var in model.component_data_objects(pyo.Var, descend_into=True):
        var.set_value(0.0, skip_validation=True)


def test_objective_contains_wear_term_with_correct_coefficient():
    """d(OBJ)/d(discharge) carries exactly -wear/1000 per kWh discharged.

    Evaluate the objective expression at a fixed variable assignment
    with and without discharge: the difference isolates the wear
    contribution (all other terms are held fixed), which must equal
    -wear x MWh for each discharge route.
    """
    ts = _ts()
    model = build_model(_params(WEAR), ts)
    _zero_all_variables(model)
    base = pyo.value(model.OBJ)

    # Discharge 10 kWh to load and 30 kWh to grid at step 2.
    model.bess_dis_load[2].set_value(10.0, skip_validation=True)
    model.bess_dis_grid[2].set_value(30.0, skip_validation=True)
    with_dis = pyo.value(model.OBJ)

    retail = 200.0
    dam = 60.0
    revenue = retail * 10.0 / 1000.0 + dam * 30.0 / 1000.0
    wear_penalty = WEAR * (10.0 + 30.0) / 1000.0
    assert with_dis - base == pytest.approx(revenue - wear_penalty, abs=1e-9)


def test_zero_wear_objective_matches_no_wear_delta():
    """With wear = 0 the same assignment yields revenue only (term off)."""
    ts = _ts()
    model = build_model(_params(0.0), ts)
    _zero_all_variables(model)
    base = pyo.value(model.OBJ)
    model.bess_dis_load[2].set_value(10.0, skip_validation=True)
    model.bess_dis_grid[2].set_value(30.0, skip_validation=True)
    with_dis = pyo.value(model.OBJ)
    revenue = 200.0 * 10.0 / 1000.0 + 60.0 * 30.0 / 1000.0
    assert with_dis - base == pytest.approx(revenue, abs=1e-9)


def test_output_frame_has_no_wear_column():
    """model_to_dataframe emits no wear column of any spelling."""
    ts = _ts()
    model = build_model(_params(WEAR), ts)
    _zero_all_variables(model)
    res = model_to_dataframe(model, ts, _params(WEAR))
    wear_cols = [c for c in res.columns if "wear" in c.lower()]
    assert wear_cols == []


def test_wear_never_reaches_kpi_or_finance_layers():
    """No module outside the objective consumes the wear parameter.

    profit_total_eur is a pure price-times-flow sum (kpis.py), the
    cashflow charges replacement CAPEX only (economics.py), and the
    lifetime projection scales flows (lifetime.py); none of them read
    bess_wear_cost_eur_per_mwh.
    """
    import inspect

    from pvbess_opt import economics, kpis, lifetime

    for mod in (kpis, economics, lifetime):
        source = inspect.getsource(mod)
        assert "wear" not in source.lower(), (
            f"{mod.__name__} must not reference the wear cost"
        )
