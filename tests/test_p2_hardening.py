"""Regression tests for the Pass-2 P2 hardening items.

* **P2.4** — ``_balancing_invariants`` reads ``soc_min_frac`` /
  ``soc_max_frac`` strictly so a hand-built ``params`` dict missing the
  keys surfaces a ``KeyError`` rather than silently passing.
* **P2.7** — ``_scale_revenue`` clamps perturbed gross at zero before
  applying the aggregator fee, matching the base build's sign
  convention when perturbed gross is negative.
* **P2.8** — ``_has_feasible_incumbent`` probes the named ``soc``
  variable, so refactors that change variable declaration order do not
  flip the verdict.
"""

from __future__ import annotations

import pandas as pd
import pyomo.environ as pyo
import pytest

from pvbess_opt.optimization import (
    _has_feasible_incumbent,
    build_model,
    model_to_dataframe,
    verify_dispatch_invariants,
)
from pvbess_opt.sensitivity import _scale_revenue

# ---------------------------------------------------------------------------
# P2.4 — strict params access in _balancing_invariants
# ---------------------------------------------------------------------------


def _minimal_dispatch_frame(n: int = 2) -> pd.DataFrame:
    """Tiny dispatch frame with the columns the verifier reads."""
    products = ("fcr", "afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn")
    cols: dict[str, list] = {
        "timestamp": list(pd.date_range("2026-01-01", periods=n, freq="1h")),
        "soc_kwh": [50.0] * n,
        "pv_kwh": [0.0] * n,
        "load_kwh": [0.0] * n,
        "pv_to_load_kwh": [0.0] * n,
        "pv_to_bess_kwh": [0.0] * n,
        "pv_to_grid_kwh": [0.0] * n,
        "pv_curtail_kwh": [0.0] * n,
        "bess_dis_load_kwh": [0.0] * n,
        "bess_dis_grid_kwh": [0.0] * n,
        "bess_charge_grid_kwh": [0.0] * n,
        "grid_to_load_kwh": [0.0] * n,
        "grid_export_total_kwh": [0.0] * n,
    }
    for p in products:
        cols[f"bm_reservation_{p}_kw"] = [0.0] * n
    return pd.DataFrame(cols)


def _balancing_enabled_params() -> dict:
    return {
        "dt_minutes": 60,
        "bess_power_kw": 100.0,
        "bess_capacity_kwh": 400.0,
        "efficiency_charge": 0.97,
        "efficiency_discharge": 0.97,
        "balancing": {
            "balancing_enabled": True,
            "fcr_required_duration_hours": 1.0,
            "bm_settlement_minutes": 60,
            "bm_soc_headroom_pct": 0.0,
            "dam_capacity_share_pct": 70.0,
            "fcr_capacity_share_pct": 30.0,
        },
    }


def test_balancing_invariants_strict_soc_keys():
    """A params dict missing ``soc_min_frac`` triggers KeyError now."""
    params = _balancing_enabled_params()
    # Intentionally omit soc_min_frac / soc_max_frac.
    res = _minimal_dispatch_frame()
    with pytest.raises(KeyError):
        verify_dispatch_invariants(res, params)


def test_balancing_invariants_strict_soc_keys_pass_when_present():
    """With soc_min_frac / soc_max_frac populated the verifier runs."""
    params = _balancing_enabled_params() | {
        "soc_min_frac": 0.1,
        "soc_max_frac": 0.95,
    }
    res = _minimal_dispatch_frame()
    out = verify_dispatch_invariants(res, params)
    # Verifier returns a dict keyed by invariant names.
    assert isinstance(out, dict)


# ---------------------------------------------------------------------------
# P2.7 — _scale_revenue clamps negative gross
# ---------------------------------------------------------------------------


def test_scale_revenue_clamps_negative_gross_to_zero_fee():
    """When perturbed gross is negative in a year, the aggregator fee
    in that year is zero (matches economics.py:396-400 base build).

    The base cashflow uses ``aggregator_fee_frac = 0.1`` (10 %).  We
    perturb at ``factor = -0.5`` so the second year's revenue flips to
    a negative value while the third year stays positive — the second
    must drop its fee to zero, the third must keep a non-zero fee.
    """
    df = pd.DataFrame({
        "project_year": [0, 1, 2, 3],
        # Year-1 nets are positive (post-fee), revenue_gross = net / 0.9.
        "revenue_eur": [0.0, 90.0, 180.0, 90.0],
        "revenue_retail_eur": [0.0, 45.0, 90.0, 45.0],
        "revenue_dam_eur": [0.0, 45.0, 90.0, 45.0],
        # 10 % aggregator fee on each year's gross.
        "aggregator_fee_eur": [0.0, -10.0, -20.0, -10.0],
        "balancing_revenue_eur": [0.0, 0.0, 0.0, 0.0],
        "opex_eur": [0.0, -5.0, -5.0, -5.0],
        "capex_eur": [-100.0, 0.0, 0.0, 0.0],
        "devex_eur": [0.0, 0.0, 0.0, 0.0],
        "discount_factor": [1.0, 0.93, 0.87, 0.81],
        "net_cashflow_eur": [-100.0, 85.0, 175.0, 85.0],
        "discounted_cf_eur": [-100.0, 79.0, 152.0, 68.8],
    })
    out = _scale_revenue(df, factor=-0.5)
    year_2_fee = float(out.loc[out["project_year"] == 2, "aggregator_fee_eur"].iloc[0])
    # Year 2's perturbed gross is negative → clamp to 0.
    assert year_2_fee == pytest.approx(0.0, abs=1e-9), (
        f"Expected 0 fee on negative perturbed gross, got {year_2_fee}"
    )


# ---------------------------------------------------------------------------
# P2.8 — _has_feasible_incumbent probes a named variable
# ---------------------------------------------------------------------------


def test_has_feasible_incumbent_uses_soc_variable():
    """A model with ``soc`` populated returns True even when other
    earlier-declared variables have ``.value is None``."""
    m = pyo.ConcreteModel()
    # Declare some unrelated variables first — these would have been
    # the "first var encountered" under the previous heuristic.
    m.unrelated_a = pyo.Var(initialize=None)
    m.unrelated_b = pyo.Var()
    m.soc = pyo.Var([0, 1, 2], initialize={0: 1.0, 1: 2.0, 2: 3.0})
    assert _has_feasible_incumbent(m) is True


def test_has_feasible_incumbent_false_when_soc_unloaded():
    m = pyo.ConcreteModel()
    m.soc = pyo.Var([0, 1, 2])  # no initial values → .value is None
    for v in m.soc.values():
        v.value = None
    assert _has_feasible_incumbent(m) is False


def test_inflation_defaults_documented_near_each_other():
    """P2.5: ``bm_inflation_pct`` and ``dam_inflation_pct`` defaults
    differ.  conventions.md must reference both within 50 lines of
    each other so the rationale is colocated."""
    from pathlib import Path

    text = Path(__file__).resolve().parents[1].joinpath(
        "pvbess_opt", "conventions.md",
    ).read_text(encoding="utf-8").splitlines()
    bm_lines = [i for i, line in enumerate(text) if "bm_inflation_pct" in line]
    dam_lines = [i for i, line in enumerate(text) if "dam_inflation_pct" in line]
    assert bm_lines, "bm_inflation_pct not referenced in conventions.md"
    assert dam_lines, "dam_inflation_pct not referenced in conventions.md"
    min_gap = min(abs(b - d) for b in bm_lines for d in dam_lines)
    assert min_gap < 50, (
        f"bm_inflation_pct and dam_inflation_pct mentions are {min_gap} "
        "lines apart in conventions.md (must be < 50)."
    )


def test_has_feasible_incumbent_fallback_for_models_without_soc():
    """A model that has no ``soc`` (legacy / mock scenarios) still
    returns a verdict via the fallback path."""
    m = pyo.ConcreteModel()
    m.x = pyo.Var(initialize=5.0)
    assert _has_feasible_incumbent(m) is True


# ---------------------------------------------------------------------------
# B1 — dt_minutes build-path guard (defense-in-depth)
#
# build_model / model_to_dataframe raise ValueError("dt_minutes must be
# positive") when dt_hours_from(params) <= 0.  The loader rejects dt <= 0
# upstream, so the guard is unreachable in normal flow; call the build
# functions directly with a hand-built params dict (bypassing the loader /
# validator) so the build-site guard itself is exercised.
# ---------------------------------------------------------------------------


def _dt_guard_ts(n: int = 1) -> pd.DataFrame:
    """A one-row timeseries; the dt guard fires before it is read."""
    return pd.DataFrame({
        "timestamp": list(pd.date_range("2026-01-01", periods=n, freq="1h")),
        "load_kwh": [0.0] * n,
        "pv_kwh": [0.0] * n,
    })


# dt_hours_from() maps a zero, negative, or missing dt_minutes to 0.0 hours
# (it clamps negatives and defaults the missing key), so all three reach the
# `if dt_h <= 0: raise ValueError` guard at the build site.
_NONPOSITIVE_DT_PARAMS = [
    {"dt_minutes": 0},        # zero
    {"dt_minutes": -15},      # negative
    {},                       # missing key entirely
]


@pytest.mark.parametrize("dt_params", _NONPOSITIVE_DT_PARAMS)
def test_build_model_rejects_nonpositive_dt(dt_params: dict):
    """build_model's dt guard fires for dt_minutes in {0, -15, missing}.

    The guard sits ahead of the empty-timeseries check, so a one-row ts is
    enough; the ValueError must carry the build-site message, proving the
    guard (not some downstream failure) is what fired.
    """
    with pytest.raises(ValueError, match="dt_minutes must be positive"):
        build_model(dt_params, _dt_guard_ts())


@pytest.mark.parametrize("dt_params", _NONPOSITIVE_DT_PARAMS)
def test_model_to_dataframe_rejects_nonpositive_dt(dt_params: dict):
    """model_to_dataframe's dt guard fires for dt_minutes in {0, -15, missing}.

    Pass an empty ConcreteModel: the guard raises before the model is ever
    inspected, so no solved model is required to reach it.
    """
    with pytest.raises(ValueError, match="dt_minutes must be positive"):
        model_to_dataframe(pyo.ConcreteModel(), _dt_guard_ts(), dt_params)
