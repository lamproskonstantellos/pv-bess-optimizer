"""Tier-2 support-year re-solves: grid, interpolation, factors, delta.

The MILP is faked throughout (the machinery is exercised end-to-end in
the slow pipeline test); these tests pin the closed-form contracts:
support-year parsing, the resolve-grid resampling rules, the
degradation-normalised factor construction, log-linear interpolation
with its linear fallback, and the Tier-2 − Tier-1 delta table.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.pricedata import PriceDataError, ScenarioDeck
from pvbess_opt.pricedata.resolve import (
    build_resolve_delta,
    build_resolve_grid,
    derive_resolve_trajectories,
    interpolate_support_factors,
    parse_support_years,
)

HOURS = 8760


# ---------------------------------------------------------------------------
# Support-year parsing
# ---------------------------------------------------------------------------


def test_parse_support_years_forces_year_one_and_dedups():
    assert parse_support_years("5, 10,5", 20) == [1, 5, 10]
    assert parse_support_years("", 20) == [1]


def test_parse_support_years_rejects_out_of_range():
    with pytest.raises(PriceDataError, match="outside"):
        parse_support_years("1,25", 20)


def test_parse_support_years_rejects_non_numeric():
    with pytest.raises(PriceDataError, match="not a year"):
        parse_support_years("1,abc", 20)


# ---------------------------------------------------------------------------
# Resolve-grid resampling
# ---------------------------------------------------------------------------


def _ts_15min(n_hours: int = 4) -> pd.DataFrame:
    n = n_hours * 4
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="15min"),
        "pv_kwh": np.full(n, 25.0),
        "load_kwh": np.full(n, 10.0),
        "dam_price_eur_per_mwh": np.tile([10.0, 20.0, 30.0, 40.0], n_hours),
        "fcr_capacity_price_eur_per_mwh": np.full(n, 5.0),
    })


def test_grid_sums_energy_and_averages_prices():
    grid = build_resolve_grid(_ts_15min(), 15, 60)
    assert len(grid) == 4
    assert grid["pv_kwh"].iloc[0] == pytest.approx(100.0)     # 4 x 25 kWh
    assert grid["dam_price_eur_per_mwh"].iloc[0] == pytest.approx(25.0)
    # Balancing columns are dropped: the re-solve runs day-ahead only.
    assert "fcr_capacity_price_eur_per_mwh" not in grid.columns


def test_grid_rejects_finer_resolution():
    with pytest.raises(PriceDataError, match="finer"):
        build_resolve_grid(_ts_15min(), 60, 15)


def test_grid_rejects_non_multiple_resolution():
    with pytest.raises(PriceDataError, match="multiple"):
        build_resolve_grid(_ts_15min(), 15, 40)


# ---------------------------------------------------------------------------
# Interpolation
# ---------------------------------------------------------------------------


def test_loglinear_is_geometric_between_supports():
    values = interpolate_support_factors(
        {1: 1.0, 5: 0.64}, 7, interp="loglinear", stream="s",
    )
    # log-linear: each yearly step multiplies by 0.64^(1/4).
    ratio = 0.64 ** 0.25
    for year in range(2, 6):
        assert values[year - 1] == pytest.approx(ratio ** (year - 1))
    # Beyond the last support year the factor holds.
    assert values[5] == pytest.approx(0.64)
    assert values[6] == pytest.approx(0.64)
    # Monotone decline between the supports (the tail then holds flat).
    from itertools import pairwise

    assert all(b < a for a, b in pairwise(values[:5]))


def test_loglinear_falls_back_to_linear_on_nonpositive(caplog):
    with caplog.at_level(logging.WARNING):
        values = interpolate_support_factors(
            {1: 1.0, 3: 0.0}, 3, interp="loglinear", stream="s",
        )
    assert values == pytest.approx([1.0, 0.5, 0.0])
    assert any("falling" in r.message for r in caplog.records)


def test_interpolation_anchors_year_one():
    values = interpolate_support_factors(
        {1: 1.0, 2: 2.0}, 2, interp="loglinear", stream="s",
    )
    assert values[0] == 1.0 and values[1] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Factor construction with a faked MILP
# ---------------------------------------------------------------------------


def _fake_run_scenario(params, ts, **_kwargs):
    """Dispatch mirror: exports = pv, discharge 1 kWh + charge 1 kWh."""
    n = len(ts)
    res = pd.DataFrame({
        "timestamp": ts["timestamp"],
        "pv_to_grid_kwh": ts["pv_kwh"].to_numpy(dtype=float),
        "bess_dis_grid_kwh": np.full(n, 1.0),
        "bess_charge_grid_kwh": np.full(n, 1.0),
        "soc_kwh": np.full(n, 0.0),
    })
    return res, "fake", res


def _hourly_year_ts() -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=HOURS, freq="h"),
        "pv_kwh": np.full(HOURS, 2.0),
        "dam_price_eur_per_mwh": np.full(HOURS, 100.0),
    })


def _deck(year2_level: float) -> ScenarioDeck:
    return ScenarioDeck(
        name="R", provider="file", vintage="v", weight_pct=100.0,
        dam={
            1: np.full(HOURS, 100.0),
            2: np.full(HOURS, year2_level),
        },
    )


def _econ(**overrides) -> dict:
    econ = {
        "pv_degradation_year1_pct": 0.0,
        "pv_degradation_annual_pct": 0.0,
        "bess_degradation_annual_pct": 0.0,
        "bess_replacement_year_effective": 0,
    }
    econ.update(overrides)
    return econ


def _params() -> dict:
    return {
        "dt_minutes": 60,
        "bess_capacity_kwh": 1000.0,
        "balancing": {"balancing_enabled": True},
        "intraday": {"id_enabled": True},
    }


def test_resolve_factors_track_price_ratio_without_degradation(
    monkeypatch, caplog,
):
    import pvbess_opt.optimization as optimization_mod

    monkeypatch.setattr(
        optimization_mod, "run_scenario", _fake_run_scenario,
    )
    monkeypatch.setattr(
        "pvbess_opt.kpis.compute_kpis",
        lambda _res, _params, **_kw: {},
    )
    with caplog.at_level(logging.INFO):
        trajectories, table = derive_resolve_trajectories(
            _deck(50.0), _params(), _hourly_year_ts(), _econ(),
            n_years=3, support_years=[1, 2],
            resolution_minutes=60,
        )
    # Prices halve in year 2; the mirrored dispatch keeps the same
    # volumes, so every DAM factor is exactly the price ratio.
    for stream in ("revenue_dam_pv", "revenue_dam_bess_export",
                   "expense_dam_bess_charge"):
        assert trajectories[stream]["values"][:2] == pytest.approx(
            [1.0, 0.5],
        )
    # Runtime is logged per solve.
    assert any("took" in r.message for r in caplog.records)
    assert set(table["project_year"]) == {1, 2}
    assert (table["solve_seconds"] >= 0.0).all()


def test_resolve_normalises_out_analytic_degradation(monkeypatch):
    import pvbess_opt.optimization as optimization_mod

    monkeypatch.setattr(
        optimization_mod, "run_scenario", _fake_run_scenario,
    )
    monkeypatch.setattr(
        "pvbess_opt.kpis.compute_kpis",
        lambda _res, _params, **_kw: {},
    )
    # 10 %/yr PV degradation: the year-2 re-solve sees 0.9 x pv volume,
    # so its raw revenue ratio is 0.9 x the price ratio — the factor
    # divides the analytic pv_factor back out (no double counting).
    trajectories, _ = derive_resolve_trajectories(
        _deck(50.0), _params(), _hourly_year_ts(),
        _econ(pv_degradation_annual_pct=10.0),
        n_years=2, support_years=[1, 2],
        resolution_minutes=60,
    )
    assert trajectories["revenue_dam_pv"]["values"] == pytest.approx(
        [1.0, 0.5],
    )
    # BESS streams carry no PV fade: still the pure price ratio.
    assert trajectories["revenue_dam_bess_export"]["values"] == (
        pytest.approx([1.0, 0.5])
    )


def test_resolve_disables_other_market_blocks(monkeypatch):
    seen: list[dict] = []

    def spy_run(params, ts, **kwargs):
        seen.append(params)
        return _fake_run_scenario(params, ts, **kwargs)

    import pvbess_opt.optimization as optimization_mod

    monkeypatch.setattr(optimization_mod, "run_scenario", spy_run)
    monkeypatch.setattr(
        "pvbess_opt.kpis.compute_kpis",
        lambda _res, _params, **_kw: {},
    )
    derive_resolve_trajectories(
        _deck(80.0), _params(), _hourly_year_ts(), _econ(),
        n_years=2, support_years=[1, 2], resolution_minutes=60,
    )
    for params_y in seen:
        assert params_y["balancing"]["balancing_enabled"] is False
        assert params_y["intraday"]["id_enabled"] is False
        assert params_y["dt_minutes"] == 60


# ---------------------------------------------------------------------------
# Delta diagnostic
# ---------------------------------------------------------------------------


def test_delta_table_reports_tier_gap():
    tier1 = {
        "revenue_dam_pv": {"mode": "replace", "values": [1.0, 0.8, 0.7]},
    }
    tier2 = {
        "revenue_dam_pv": {"mode": "replace", "values": [1.0, 0.9, 0.75]},
    }
    delta = build_resolve_delta(tier1, tier2, [1, 3])
    pv_rows = delta[delta["stream"] == "revenue_dam_pv"]
    year3 = pv_rows[pv_rows["project_year"] == 3].iloc[0]
    assert year3["g_tier1_reprice"] == pytest.approx(0.7)
    assert year3["g_tier2_resolve"] == pytest.approx(0.75)
    assert year3["delta"] == pytest.approx(0.05)
