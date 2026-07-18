"""Weighted price-scenario ensemble: stats, shared debt, reuse rules."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from pvbess_opt.pricedata import PriceDataError
from pvbess_opt.pricedata.ensemble import (
    run_price_scenario_ensemble,
    weighted_percentile,
)

HOURS = 8760


# ---------------------------------------------------------------------------
# Weighted percentile convention
# ---------------------------------------------------------------------------


def test_weighted_percentile_discrete_cdf():
    values = [100.0, 200.0, 300.0]
    weights = [20.0, 30.0, 50.0]
    # CDF: 100 -> 20 %, 200 -> 50 %, 300 -> 100 %.
    assert weighted_percentile(values, weights, 10.0) == 100.0
    assert weighted_percentile(values, weights, 20.0) == 100.0
    assert weighted_percentile(values, weights, 50.0) == 200.0
    assert weighted_percentile(values, weights, 90.0) == 300.0


def test_weighted_percentile_orders_by_value():
    # Unsorted input: ordering happens on the VALUE axis.
    assert weighted_percentile(
        [300.0, 100.0, 200.0], [50.0, 20.0, 30.0], 50.0,
    ) == 200.0


def test_weighted_percentile_empty_is_nan():
    assert np.isnan(weighted_percentile([], [], 50.0))


# ---------------------------------------------------------------------------
# Ensemble evaluation (economics faked for closed-form control)
# ---------------------------------------------------------------------------


_NPV_BY_SCENARIO = {"Central": 300.0, "Downside": 100.0, "Upside": 500.0}


def _fake_economics(monkeypatch, captured: list[dict]):
    def fake_build(kpis, econ, capacities):
        captured.append(econ)
        return pd.DataFrame({"project_year": [0, 1]})

    def fake_fin(cashflow, econ, **_kwargs):
        # NPV keyed on which scenario's trajectories this econ carries.
        marker = econ["trajectories"]["revenue_dam_pv"]["values"][1]
        name = {0.9: "Central", 0.5: "Downside", 1.1: "Upside"}[
            round(marker, 6)
        ]
        return {
            "npv_eur": _NPV_BY_SCENARIO[name],
            "irr_pct": 10.0,
            "simple_payback_years": 8.0,
            "min_dscr": 1.5,
        }

    monkeypatch.setattr(
        "pvbess_opt.economics.build_yearly_cashflow", fake_build,
    )
    monkeypatch.setattr(
        "pvbess_opt.economics.compute_financial_kpis", fake_fin,
    )


def _parametric_store(tmp_path: Path, name: str, level_pct: float) -> None:
    store = tmp_path / name
    store.mkdir()
    (store / "meta.yaml").write_text(
        yaml.safe_dump({
            "provider": "parametric",
            "parametric": {"dam_level_pct_per_yr": level_pct},
        }),
        encoding="utf-8",
    )


def _econ_base(tmp_path: Path) -> dict:
    _parametric_store(tmp_path, "central", -10.0)
    _parametric_store(tmp_path, "down", -50.0)
    _parametric_store(tmp_path, "up", 10.0)
    return {
        "price_scenarios_enabled": True,
        "scenario_projection_mode": "reprice",
        "price_basis": "nominal",
        "price_base_year": 0,
        "cpi_pct": 0.0,
        "project_lifecycle_years": 2,
        "project_start_year": 2026,
        "_sized_debt_eur": 123456.0,
        "_gearing_sized_pct": 70.0,
        "trajectories": {
            "opex_pv": {"mode": "overlay", "values": [1.0, 1.05]},
        },
        "price_scenarios": [
            {"name": "Central", "provider": "parametric", "vintage": "v",
             "weight_pct": 30.0, "store_path": "central", "notes": ""},
            {"name": "Downside", "provider": "parametric", "vintage": "v",
             "weight_pct": 20.0, "store_path": "down", "notes": ""},
            {"name": "Upside", "provider": "parametric", "vintage": "v",
             "weight_pct": 50.0, "store_path": "up", "notes": ""},
        ],
    }


def _ts() -> pd.DataFrame:
    return pd.DataFrame({
        "dam_price_eur_per_mwh": np.full(HOURS, 100.0),
        "pv_kwh": np.zeros(HOURS),
    })


def _res() -> pd.DataFrame:
    return pd.DataFrame({"pv_to_grid_kwh": np.full(HOURS, 1.0)})


def test_ensemble_weighted_stats_and_shared_debt(monkeypatch, tmp_path):
    captured: list[dict] = []
    _fake_economics(monkeypatch, captured)
    result = run_price_scenario_ensemble(
        _econ_base(tmp_path), {"profit_total_eur": 1.0},
        {"pv_kwp": 1000.0}, _ts(), _res(),
        base_dir=tmp_path,
    )
    assert result is not None
    table = result.table.set_index("scenario")
    assert set(table.index) == {"Central", "Downside", "Upside"}
    # E[NPV] = 0.3x300 + 0.2x100 + 0.5x500 = 360.
    assert result.stats["expected_npv_eur"] == pytest.approx(360.0)
    # Weighted CDF: 100 -> 20 %, 300 -> 50 %, 500 -> 100 %.
    assert result.stats["npv_p10_eur"] == 100.0
    assert result.stats["npv_p50_eur"] == 300.0
    assert result.stats["npv_p90_eur"] == 500.0
    assert result.stats["expected_irr_pct"] == pytest.approx(10.0)
    # Shared-debt invariant: every member inherited the frozen keys.
    assert len(captured) == 3
    for econ_s in captured:
        assert econ_s["_sized_debt_eur"] == 123456.0
        assert econ_s["_gearing_sized_pct"] == 70.0
        # The user's non-price trajectory rides along untouched.
        assert econ_s["trajectories"]["opex_pv"]["values"] == [1.0, 1.05]
    assert any("E[NPV]" in line for line in result.summary_lines)


def test_ensemble_reuses_applied_trajectories(monkeypatch, tmp_path):
    captured: list[dict] = []
    _fake_economics(monkeypatch, captured)
    applied = {
        "revenue_dam_pv": {"mode": "replace", "values": [1.0, 0.5]},
        "revenue_dam_bess_export": {
            "mode": "replace", "values": [1.0, 0.5],
        },
        "expense_dam_bess_charge": {
            "mode": "replace", "values": [1.0, 0.5],
        },
    }
    econ = _econ_base(tmp_path)
    econ["trajectories"] = {**econ["trajectories"], **applied}
    result = run_price_scenario_ensemble(
        econ, {"profit_total_eur": 1.0}, {"pv_kwp": 1000.0},
        _ts(), _res(),
        base_dir=tmp_path,
        applied_trajectories=applied,
        applied_name="Downside",
    )
    assert result is not None
    row = result.table.set_index("scenario").loc["Downside"]
    assert bool(row["applied"]) is True
    # The Downside member reused the given block verbatim (its derived
    # parametric factors would be identical here by construction, but
    # under resolve mode they differ — the reuse is the contract).
    downside_econ = [
        e for e in captured
        if e["trajectories"]["revenue_dam_pv"]["values"] == [1.0, 0.5]
    ]
    assert len(downside_econ) == 1


def test_ensemble_disarmed_returns_none(tmp_path):
    econ = {"price_scenarios_enabled": False}
    assert run_price_scenario_ensemble(
        econ, {}, {}, _ts(), _res(), base_dir=tmp_path,
    ) is None


def test_ensemble_rejects_broken_weights(monkeypatch, tmp_path):
    captured: list[dict] = []
    _fake_economics(monkeypatch, captured)
    econ = _econ_base(tmp_path)
    econ["price_scenarios"][0]["weight_pct"] = 90.0  # sum now 160
    with pytest.raises(PriceDataError, match="sum to 100"):
        run_price_scenario_ensemble(
            econ, {"profit_total_eur": 1.0}, {"pv_kwp": 1000.0},
            _ts(), _res(), base_dir=tmp_path,
        )


def test_ensemble_frame_appends_stat_rows(monkeypatch, tmp_path):
    """The results-workbook frame: scenario rows first, then the
    labelled weighted-stat rows (self-contained sheet)."""
    from pvbess_opt.pipeline import _ensemble_frame

    captured: list[dict] = []
    _fake_economics(monkeypatch, captured)
    result = run_price_scenario_ensemble(
        _econ_base(tmp_path), {"profit_total_eur": 1.0},
        {"pv_kwp": 1000.0}, _ts(), _res(),
        base_dir=tmp_path,
    )
    frame = _ensemble_frame(result)
    assert frame is not None
    labels = list(frame["scenario"])
    assert labels[:3] == ["Central", "Downside", "Upside"]
    assert labels[3:] == ["E[NPV]", "P10", "P50", "P90", "E[IRR]"]
    stats = frame.set_index("scenario")
    assert stats.loc["E[NPV]", "npv_eur"] == pytest.approx(360.0)
    assert stats.loc["P50", "npv_eur"] == 300.0
    assert stats.loc["E[IRR]", "irr_pct"] == pytest.approx(10.0)
    assert _ensemble_frame(None) is None
