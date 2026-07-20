"""Tier-1 reprice engine: closed-form factors, capture KPIs, conflicts."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from pvbess_opt.pricedata import PriceDataError, ScenarioDeck
from pvbess_opt.pricedata.engine import (
    build_scenario_deck,
    deck_year1_level_check,
    derive_reprice_trajectories,
    merge_auto_trajectories,
)

HOURS = 8760


def _deck(dam: dict[int, np.ndarray], balancing=None) -> ScenarioDeck:
    return ScenarioDeck(
        name="test", provider="file", vintage="v", weight_pct=100.0,
        dam=dam, balancing=balancing,
    )


def _res(
    pv_export: np.ndarray | float = 0.0,
    bess_export: np.ndarray | float = 0.0,
    bess_charge: np.ndarray | float = 0.0,
) -> pd.DataFrame:
    def col(value):
        return (
            np.full(HOURS, float(value))
            if np.isscalar(value) else np.asarray(value, dtype=float)
        )

    return pd.DataFrame({
        "pv_to_grid_kwh": col(pv_export),
        "bess_dis_grid_kwh": col(bess_export),
        "bess_charge_grid_kwh": col(bess_charge),
    })


def _noon_mask() -> np.ndarray:
    mask = np.zeros(HOURS, dtype=bool)
    mask[12::24] = True
    return mask


# ---------------------------------------------------------------------------
# Closed-form factor micro-cases
# ---------------------------------------------------------------------------


def test_constant_prices_give_unit_factors():
    deck = _deck({
        1: np.full(HOURS, 80.0), 2: np.full(HOURS, 80.0),
    })
    trajectories, paths = derive_reprice_trajectories(
        deck, _res(pv_export=1.0, bess_export=0.5, bess_charge=0.2),
        n_years=3,
    )
    for stream in ("revenue_dam_pv", "revenue_dam_bess_export",
                   "expense_dam_bess_charge"):
        assert trajectories[stream]["mode"] == "replace"
        assert trajectories[stream]["values"] == [1.0, 1.0, 1.0]
    assert list(paths["project_year"]) == [1, 2, 3]


def test_uniform_halving_gives_half_factor():
    deck = _deck({
        1: np.full(HOURS, 80.0), 2: np.full(HOURS, 40.0),
    })
    trajectories, _ = derive_reprice_trajectories(
        deck, _res(pv_export=1.0, bess_export=1.0, bess_charge=1.0),
        n_years=2,
    )
    for stream in ("revenue_dam_pv", "revenue_dam_bess_export",
                   "expense_dam_bess_charge"):
        assert trajectories[stream]["values"] == pytest.approx([1.0, 0.5])


def test_cannibalization_hits_pv_hours_only():
    """Noon prices halve; PV exports at noon, BESS at night: the PV
    factor falls while the BESS export factor holds — the asymmetry the
    single aggregate g_dam cannot express."""
    noon = _noon_mask()
    price1 = np.full(HOURS, 100.0)
    price2 = np.where(noon, 50.0, 100.0)
    deck = _deck({1: price1, 2: price2})
    pv = np.where(noon, 4.0, 0.0)
    bess = np.where(noon, 0.0, 2.0)
    trajectories, paths = derive_reprice_trajectories(
        deck, _res(pv_export=pv, bess_export=bess), n_years=2,
    )
    assert trajectories["revenue_dam_pv"]["values"] == pytest.approx(
        [1.0, 0.5],
    )
    assert trajectories["revenue_dam_bess_export"]["values"] == (
        pytest.approx([1.0, 1.0])
    )
    # Capture KPIs: year-2 PV capture price is 50 while the mean only
    # dips by the noon share (1/24 of the day).
    year2 = paths[paths["project_year"] == 2].iloc[0]
    assert year2["pv_capture_price_eur_per_mwh"] == pytest.approx(50.0)
    expected_mean = (50.0 + 23 * 100.0) / 24.0
    assert year2["dam_mean_price_eur_per_mwh"] == pytest.approx(
        expected_mean,
    )
    assert year2["pv_capture_rate"] == pytest.approx(50.0 / expected_mean)


def test_charge_leg_reprices_on_its_own_hours():
    """Night prices double: only the (night-charging) expense leg moves."""
    noon = _noon_mask()
    price1 = np.full(HOURS, 50.0)
    price2 = np.where(noon, 50.0, 100.0)
    deck = _deck({1: price1, 2: price2})
    trajectories, _ = derive_reprice_trajectories(
        deck,
        _res(
            pv_export=np.where(noon, 1.0, 0.0),
            bess_charge=np.where(noon, 0.0, 1.0),
        ),
        n_years=2,
    )
    assert trajectories["expense_dam_bess_charge"]["values"] == (
        pytest.approx([1.0, 2.0])
    )
    assert trajectories["revenue_dam_pv"]["values"] == pytest.approx(
        [1.0, 1.0],
    )


def test_realized_spread_is_discharge_minus_charge_price():
    noon = _noon_mask()
    price = np.where(noon, 120.0, 40.0)
    deck = _deck({1: price})
    _t, paths = derive_reprice_trajectories(
        deck,
        _res(
            bess_export=np.where(noon, 2.0, 0.0),
            bess_charge=np.where(noon, 0.0, 1.0),
        ),
        n_years=1,
    )
    row = paths.iloc[0]
    assert row["bess_discharge_price_eur_per_mwh"] == pytest.approx(120.0)
    assert row["bess_charge_price_eur_per_mwh"] == pytest.approx(40.0)
    assert row["bess_realized_spread_eur_per_mwh"] == pytest.approx(80.0)


def test_zero_volume_stream_stays_flat():
    deck = _deck({1: np.full(HOURS, 80.0), 2: np.full(HOURS, 20.0)})
    trajectories, _ = derive_reprice_trajectories(
        deck, _res(pv_export=1.0), n_years=2,
    )
    # No BESS at all: both BESS streams stay inert at 1.0.
    assert trajectories["revenue_dam_bess_export"]["values"] == [1.0, 1.0]
    assert trajectories["expense_dam_bess_charge"]["values"] == [1.0, 1.0]


def test_hold_last_extends_the_final_curve():
    deck = _deck({1: np.full(HOURS, 100.0), 2: np.full(HOURS, 60.0)})
    trajectories, _ = derive_reprice_trajectories(
        deck, _res(pv_export=1.0), n_years=4,
    )
    assert trajectories["revenue_dam_pv"]["values"] == pytest.approx(
        [1.0, 0.6, 0.6, 0.6],
    )


def test_dispatch_grid_mismatch_is_an_error():
    deck = _deck({1: np.full(100, 80.0)})
    with pytest.raises(PriceDataError, match="steps"):
        derive_reprice_trajectories(deck, _res(pv_export=1.0), n_years=1)


def test_balancing_annual_table_becomes_product_streams():
    balancing = pd.DataFrame([
        {"year": 1, "product": "afrr_up",
         "capacity_price_eur_per_mwh": 10.0,
         "activation_price_eur_per_mwh": 40.0},
        {"year": 2, "product": "afrr_up",
         "capacity_price_eur_per_mwh": 5.0,
         "activation_price_eur_per_mwh": 30.0},
        {"year": 1, "product": "fcr",
         "capacity_price_eur_per_mwh": 8.0,
         "activation_price_eur_per_mwh": 0.0},
        {"year": 2, "product": "fcr",
         "capacity_price_eur_per_mwh": 2.0,
         "activation_price_eur_per_mwh": 0.0},
    ])
    deck = _deck(
        {1: np.full(HOURS, 80.0), 2: np.full(HOURS, 80.0)},
        balancing=balancing,
    )
    trajectories, paths = derive_reprice_trajectories(
        deck, _res(pv_export=1.0), n_years=3,
    )
    assert trajectories["balancing_capacity_afrr_up"]["values"] == (
        pytest.approx([1.0, 0.5, 0.5])  # hold_last past year 2
    )
    assert trajectories["balancing_activation_afrr_up"]["values"] == (
        pytest.approx([1.0, 0.75, 0.75])
    )
    assert trajectories["balancing_capacity_fcr"]["values"] == (
        pytest.approx([1.0, 0.25, 0.25])
    )
    assert "balancing_activation_fcr" not in trajectories
    # Products missing from the table produce no stream at all.
    assert "balancing_capacity_mfrr_up" not in trajectories
    assert "afrr_up_capacity_price_eur_per_mwh" in paths.columns


# ---------------------------------------------------------------------------
# Merging + guards
# ---------------------------------------------------------------------------


def test_merge_conflict_with_user_price_stream_raises():
    generated = {"revenue_dam_pv": {"mode": "replace", "values": [1.0]}}
    with pytest.raises(PriceDataError, match="revenue_dam"):
        merge_auto_trajectories(
            {"revenue_dam": {"mode": "replace", "values": [1.0]}},
            generated, scenario="s",
        )


def test_merge_passes_non_price_streams_through():
    user = {"opex_pv": {"mode": "overlay", "values": [1.0, 1.1]}}
    generated = {"revenue_dam_pv": {"mode": "replace", "values": [1.0, 0.9]}}
    merged = merge_auto_trajectories(user, generated, scenario="s")
    assert set(merged) == {"opex_pv", "revenue_dam_pv"}


def test_year1_level_drift_warns(caplog):
    import logging

    deck = _deck({1: np.full(HOURS, 200.0)})
    ts = pd.DataFrame({"dam_price_eur_per_mwh": np.full(HOURS, 100.0)})
    with caplog.at_level(logging.WARNING):
        deck_year1_level_check(deck, ts)
    assert any("departs" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Deck construction dispatch
# ---------------------------------------------------------------------------


def _entry(provider: str, store: Path) -> dict:
    return {
        "name": "S", "provider": provider, "vintage": "v",
        "weight_pct": 100.0, "store_path": str(store), "notes": "",
    }


def _deck_kwargs() -> dict:
    return dict(
        n_steps=HOURS, dt_minutes=60, n_years=2, start_year=2026,
        engine_basis="nominal", engine_base_year=0, cpi_pct=0.0,
    )


def test_build_deck_dispatches_parametric(tmp_path):
    store = tmp_path / "p"
    store.mkdir()
    (store / "meta.yaml").write_text(
        yaml.safe_dump({
            "provider": "parametric",
            "parametric": {"dam_level_pct_per_yr": -5.0},
        }),
        encoding="utf-8",
    )
    ts = pd.DataFrame({
        "dam_price_eur_per_mwh": np.full(HOURS, 100.0),
        "pv_kwh": np.zeros(HOURS),
    })
    deck = build_scenario_deck(
        _entry("parametric", store), base_dir=tmp_path, ts=ts,
        **_deck_kwargs(),
    )
    assert deck.provider == "parametric"
    assert deck.dam[2][0] == pytest.approx(95.0)


def test_build_deck_stub_provider_raises(tmp_path):
    ts = pd.DataFrame({"dam_price_eur_per_mwh": np.full(HOURS, 100.0)})
    with pytest.raises(PriceDataError, match="documented stub"):
        build_scenario_deck(
            _entry("maon", tmp_path / "m"), base_dir=tmp_path, ts=ts,
            **_deck_kwargs(),
        )


def test_build_deck_parametric_needs_dam_column(tmp_path):
    store = tmp_path / "p"
    store.mkdir()
    (store / "meta.yaml").write_text(
        yaml.safe_dump({"parametric": {}}), encoding="utf-8",
    )
    with pytest.raises(PriceDataError, match="dam_price_eur_per_mwh"):
        build_scenario_deck(
            _entry("parametric", store), base_dir=tmp_path,
            ts=pd.DataFrame({"pv_kwh": np.zeros(HOURS)}),
            **_deck_kwargs(),
        )


# ---------------------------------------------------------------------------
# The single-run arming entry point
# ---------------------------------------------------------------------------

from pvbess_opt.pricedata.engine import apply_price_scenarios  # noqa: E402


def _armed_econ(tmp_path: Path, **overrides) -> dict:
    store = tmp_path / "central"
    store.mkdir(exist_ok=True)
    (store / "meta.yaml").write_text(
        yaml.safe_dump({
            "provider": "parametric",
            "parametric": {"dam_level_pct_per_yr": -10.0},
        }),
        encoding="utf-8",
    )
    econ = {
        "price_scenarios_enabled": True,
        "scenario_projection_mode": "reprice",
        "price_basis": "nominal",
        "price_base_year": 0,
        "cpi_pct": 0.0,
        "debt_sizing_scenario": "",
        "project_lifecycle_years": 3,
        "project_start_year": 2026,
        "price_scenarios": [{
            "name": "Central", "provider": "parametric", "vintage": "v",
            "weight_pct": 100.0, "store_path": "central", "notes": "",
        }],
    }
    econ.update(overrides)
    return econ


def _armed_ts() -> pd.DataFrame:
    return pd.DataFrame({
        "dam_price_eur_per_mwh": np.full(HOURS, 100.0),
        "pv_kwh": np.zeros(HOURS),
    })


def test_apply_disarmed_returns_none(tmp_path):
    econ = _armed_econ(tmp_path, price_scenarios_enabled=False)
    before = dict(econ)
    assert apply_price_scenarios(
        econ, _armed_ts(), _res(pv_export=1.0), base_dir=tmp_path,
    ) is None
    assert econ == before


def test_apply_reprice_merges_auto_trajectories(tmp_path):
    econ = _armed_econ(tmp_path)
    application = apply_price_scenarios(
        econ, _armed_ts(), _res(pv_export=1.0, bess_export=0.5),
        base_dir=tmp_path,
    )
    assert application is not None
    assert application.applied == "Central"
    assert application.weights == {"Central": 100.0}
    block = econ["trajectories"]
    assert block["revenue_dam_pv"]["values"] == pytest.approx(
        [1.0, 0.9, 0.81],
    )
    assert block["revenue_dam_bess_export"]["values"] == pytest.approx(
        [1.0, 0.9, 0.81],
    )
    assert "Central" in application.fan
    assert any("capture" in line for line in application.summary_lines)
    # The armed marker and the applied auto-block (the ensemble's
    # verbatim-reuse input) are exposed on the application.
    assert econ["_price_scenario_applied"] == "Central"
    assert application.applied_trajectories is not None
    assert {
        "revenue_dam_pv", "revenue_dam_bess_export",
        "expense_dam_bess_charge",
    } <= set(application.applied_trajectories)


def test_apply_selects_debt_sizing_scenario(tmp_path):
    second = tmp_path / "down"
    second.mkdir()
    (second / "meta.yaml").write_text(
        yaml.safe_dump({
            "provider": "parametric",
            "parametric": {"dam_level_pct_per_yr": -50.0},
        }),
        encoding="utf-8",
    )
    econ = _armed_econ(tmp_path, debt_sizing_scenario="Downside")
    econ["price_scenarios"] = [
        econ["price_scenarios"][0] | {"weight_pct": 60.0},
        {"name": "Downside", "provider": "parametric", "vintage": "v",
         "weight_pct": 40.0, "store_path": "down", "notes": ""},
    ]
    application = apply_price_scenarios(
        econ, _armed_ts(), _res(pv_export=1.0), base_dir=tmp_path,
    )
    assert application is not None and application.applied == "Downside"
    assert econ["trajectories"]["revenue_dam_pv"]["values"] == (
        pytest.approx([1.0, 0.5, 0.25])
    )
    assert set(application.fan) == {"Central", "Downside"}


def test_apply_unknown_debt_sizing_name_errors(tmp_path):
    econ = _armed_econ(tmp_path, debt_sizing_scenario="Nope")
    with pytest.raises(PriceDataError, match="Nope"):
        apply_price_scenarios(
            econ, _armed_ts(), _res(pv_export=1.0), base_dir=tmp_path,
        )


def test_apply_resolve_mode_needs_params(tmp_path):
    econ = _armed_econ(tmp_path, scenario_projection_mode="resolve")
    with pytest.raises(PriceDataError, match="params"):
        apply_price_scenarios(
            econ, _armed_ts(), _res(pv_export=1.0), base_dir=tmp_path,
        )


def test_apply_resolve_overrides_dam_streams(monkeypatch, tmp_path):
    """The wiring contract of 'resolve': Tier-2 factors override the
    three DAM streams in econ['trajectories'] while the fan/paths
    tables stay Tier-1, and the delta table reports the gap."""
    import pvbess_opt.optimization as optimization_mod

    calls = {"n": 0}

    def fake_run(_params, ts_y, **_kwargs):
        # Each successive support-year solve exports twice as much as
        # the previous one — a dispatch adaptation the frozen Year-1
        # dispatch cannot see, so Tier-2 departs from Tier-1.
        calls["n"] += 1
        n = len(ts_y)
        frame = pd.DataFrame({
            "timestamp": ts_y["timestamp"],
            "pv_to_grid_kwh": np.full(n, float(calls["n"])),
            "bess_dis_grid_kwh": np.full(n, float(calls["n"])),
            "bess_charge_grid_kwh": np.full(n, float(calls["n"])),
            "soc_kwh": np.zeros(n),
        })
        return frame, "fake", frame

    monkeypatch.setattr(optimization_mod, "run_scenario", fake_run)
    monkeypatch.setattr(
        "pvbess_opt.kpis.compute_kpis",
        lambda _res, _params, **_kw: {},
    )
    econ = _armed_econ(
        tmp_path,
        scenario_projection_mode="resolve",
        scenario_resolve_years="2",
        scenario_resolve_resolution=60,
        scenario_interp="loglinear",
        bess_replacement_year_effective=0,
    )
    ts = _armed_ts()
    ts.insert(
        0, "timestamp",
        pd.date_range("2026-01-01", periods=HOURS, freq="h"),
    )
    application = apply_price_scenarios(
        econ, ts, _res(pv_export=1.0, bess_export=0.5, bess_charge=0.2),
        base_dir=tmp_path,
        params={
            "dt_minutes": 60, "bess_capacity_kwh": 1000.0,
            "balancing": {"balancing_enabled": True},
            "intraday": {"id_enabled": True},
        },
        kpis={"bess_total_discharge_mwh": 500.0},
    )
    assert application is not None and application.mode == "resolve"
    # Year-2 re-solve doubled every volume while prices fell to 0.9x:
    # Tier-2 factor 2 x 0.9 = 1.8, held flat beyond the last support
    # year — versus the Tier-1 reprice factors 0.9 / 0.81.
    for stream in ("revenue_dam_pv", "revenue_dam_bess_export",
                   "expense_dam_bess_charge"):
        assert econ["trajectories"][stream]["values"] == pytest.approx(
            [1.0, 1.8, 1.8],
        )
    # The fan/paths tables stay Tier-1 (every scenario on the same
    # frozen-dispatch footing); the gap lives in the delta table.
    paths_year2 = application.paths[
        application.paths["project_year"] == 2
    ].iloc[0]
    assert paths_year2["g_revenue_dam_pv"] == pytest.approx(0.9)
    assert application.resolve_delta is not None
    delta_pv = application.resolve_delta[
        (application.resolve_delta["stream"] == "revenue_dam_pv")
        & (application.resolve_delta["project_year"] == 2)
    ].iloc[0]
    assert delta_pv["g_tier1_reprice"] == pytest.approx(0.9)
    assert delta_pv["g_tier2_resolve"] == pytest.approx(1.8)
    assert delta_pv["delta"] == pytest.approx(0.9)
    assert application.resolve_support is not None
    assert set(application.resolve_support["project_year"]) == {1, 2}
    # The Year-1 discharge throughput reached the cycle-fade model.
    assert econ["_resolve_year1_discharge_mwh"] == 500.0
    assert any("Tier-2" in line for line in application.summary_lines)


def test_apply_trajectory_only_is_inert(tmp_path, caplog):
    import logging

    econ = _armed_econ(
        tmp_path, scenario_projection_mode="trajectory_only",
    )
    with caplog.at_level(logging.INFO):
        assert apply_price_scenarios(
            econ, _armed_ts(), _res(pv_export=1.0), base_dir=tmp_path,
        ) is None
    assert any("trajectory_only" in r.message for r in caplog.records)


def test_apply_enabled_without_rows_warns(tmp_path, caplog):
    import logging

    econ = _armed_econ(tmp_path, price_scenarios=None)
    with caplog.at_level(logging.WARNING):
        assert apply_price_scenarios(
            econ, _armed_ts(), _res(pv_export=1.0), base_dir=tmp_path,
        ) is None
    assert any("inert" in r.message for r in caplog.records)


def test_apply_conflicts_with_user_price_trajectory(tmp_path):
    econ = _armed_econ(tmp_path)
    econ["trajectories"] = {
        "revenue_dam": {"mode": "replace", "values": [1.0, 1.0, 1.0]},
    }
    with pytest.raises(PriceDataError, match="generates itself"):
        apply_price_scenarios(
            econ, _armed_ts(), _res(pv_export=1.0), base_dir=tmp_path,
        )


def test_balancing_hold_last_is_per_product():
    """A product with fewer declared years holds its OWN last year —
    the global max across products must never drop the stream."""
    balancing = pd.DataFrame([
        {"year": 1, "product": "afrr_up",
         "capacity_price_eur_per_mwh": 10.0,
         "activation_price_eur_per_mwh": 40.0},
        {"year": 2, "product": "afrr_up",
         "capacity_price_eur_per_mwh": 5.0,
         "activation_price_eur_per_mwh": 20.0},
        {"year": 1, "product": "mfrr_up",
         "capacity_price_eur_per_mwh": 4.0,
         "activation_price_eur_per_mwh": 16.0},
    ])
    deck = _deck(
        {1: np.full(HOURS, 80.0), 2: np.full(HOURS, 80.0)},
        balancing=balancing,
    )
    trajectories, _ = derive_reprice_trajectories(
        deck, _res(pv_export=1.0), n_years=3,
    )
    assert trajectories["balancing_capacity_afrr_up"]["values"] == (
        pytest.approx([1.0, 0.5, 0.5])
    )
    # mfrr_up covers year 1 only: flat at its own level, never dropped.
    assert trajectories["balancing_capacity_mfrr_up"]["values"] == (
        pytest.approx([1.0, 1.0, 1.0])
    )
    assert trajectories["balancing_activation_mfrr_up"]["values"] == (
        pytest.approx([1.0, 1.0, 1.0])
    )
