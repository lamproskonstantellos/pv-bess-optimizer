"""Price-scenario store: schema validation, resample, basis bridge, adapters."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from pvbess_opt.pricedata import (
    PriceDataError,
    build_parametric_deck,
    build_tyndp_deck,
    load_scenario_store,
    stub_provider_error,
)

HOURS = 8760


def _write_store(
    tmp_path: Path,
    *,
    meta: dict | None = None,
    dam_years: dict[int, np.ndarray] | None = None,
    ida_years: dict[int, np.ndarray] | None = None,
    balancing_rows: list[dict] | None = None,
) -> Path:
    store = tmp_path / "scenario_store"
    store.mkdir(exist_ok=True)
    (store / "meta.yaml").write_text(
        yaml.safe_dump(meta if meta is not None else {
            "provider": "file", "vintage": "2026-07", "zone": "GR",
            "currency": "EUR", "basis": "nominal",
        }),
        encoding="utf-8",
    )
    if dam_years is None:
        dam_years = {1: np.full(HOURS, 50.0), 2: np.full(HOURS, 45.0)}
    frames = []
    for year, values in dam_years.items():
        frame = pd.DataFrame({
            "year": year,
            "step": np.arange(1, len(values) + 1),
            "dam_price_eur_per_mwh": values,
        })
        if ida_years is not None and year in ida_years:
            frame["ida_price_eur_per_mwh"] = ida_years[year]
        frames.append(frame)
    pd.concat(frames).to_csv(store / "dam.csv", index=False)
    if balancing_rows is not None:
        pd.DataFrame(balancing_rows).to_csv(
            store / "balancing_annual.csv", index=False,
        )
    return store


def _load(store: Path, **overrides):
    kwargs = dict(
        name="s", provider="file", vintage="v", weight_pct=100.0,
        n_steps=HOURS, dt_minutes=60, n_years=20, start_year=2026,
    )
    kwargs.update(overrides)
    return load_scenario_store(store, **kwargs)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_loads_minimal_store_with_hold_last(tmp_path, caplog):
    import logging

    with caplog.at_level(logging.INFO):
        deck = _load(_write_store(tmp_path))
    # The hold_last tail is materialised at load (every operating year
    # carries its own curve so the basis bridge can stamp per-year
    # deflators); years past the declared ones repeat the last curve.
    assert sorted(deck.dam) == list(range(1, 21))
    assert (deck.dam_curve(2) == 45.0).all()
    # Year 20 holds the last declared curve (documented hold_last).
    assert (deck.dam_curve(20) == 45.0).all()
    assert any("hold_last" in r.message for r in caplog.records)


def test_missing_meta_is_precise(tmp_path):
    store = _write_store(tmp_path)
    (store / "meta.yaml").unlink()
    with pytest.raises(PriceDataError, match=r"meta\.yaml"):
        _load(store)


def test_non_eur_currency_rejected(tmp_path):
    store = _write_store(tmp_path, meta={"currency": "USD"})
    with pytest.raises(PriceDataError, match="USD"):
        _load(store)


def test_real_basis_requires_base_year(tmp_path):
    store = _write_store(tmp_path, meta={"basis": "real"})
    with pytest.raises(PriceDataError, match="base_year"):
        _load(store)


def test_missing_dam_file_is_precise(tmp_path):
    store = _write_store(tmp_path)
    (store / "dam.csv").unlink()
    with pytest.raises(PriceDataError, match=r"dam\.csv"):
        _load(store)


def test_year_gap_is_a_hard_error(tmp_path):
    store = _write_store(tmp_path, dam_years={
        1: np.full(HOURS, 50.0), 3: np.full(HOURS, 40.0),
    })
    with pytest.raises(PriceDataError, match="missing"):
        _load(store)


def test_step_gap_is_a_hard_error(tmp_path):
    values = np.full(HOURS, 50.0)
    store = _write_store(tmp_path, dam_years={1: values})
    df = pd.read_csv(store / "dam.csv")
    df = df[df["step"] != 100]
    df.to_csv(store / "dam.csv", index=False)
    with pytest.raises(PriceDataError, match="contiguous"):
        _load(store)


def test_nan_prices_rejected(tmp_path):
    values = np.full(HOURS, 50.0)
    values[7] = np.nan
    store = _write_store(tmp_path, dam_years={1: values})
    with pytest.raises(PriceDataError, match="NaN"):
        _load(store)


def test_partial_year_curve_rejected(tmp_path):
    store = _write_store(tmp_path, dam_years={1: np.full(5000, 50.0)})
    with pytest.raises(PriceDataError, match="whole non-leap year"):
        _load(store)


def test_unknown_balancing_product_rejected(tmp_path):
    store = _write_store(tmp_path, balancing_rows=[{
        "year": 1, "product": "ffr",
        "capacity_price_eur_per_mwh": 5.0,
        "activation_price_eur_per_mwh": 0.0,
    }])
    with pytest.raises(PriceDataError, match="ffr"):
        _load(store)


def test_fcr_activation_price_rejected(tmp_path):
    store = _write_store(tmp_path, balancing_rows=[{
        "year": 1, "product": "fcr",
        "capacity_price_eur_per_mwh": 5.0,
        "activation_price_eur_per_mwh": 9.0,
    }])
    with pytest.raises(PriceDataError, match="no activation"):
        _load(store)


def test_duplicate_year_product_rejected(tmp_path):
    row = {
        "year": 1, "product": "afrr_up",
        "capacity_price_eur_per_mwh": 5.0,
        "activation_price_eur_per_mwh": 1.0,
    }
    store = _write_store(tmp_path, balancing_rows=[row, dict(row)])
    with pytest.raises(PriceDataError, match="duplicate"):
        _load(store)


def test_ida_column_loads_when_present(tmp_path):
    store = _write_store(
        tmp_path,
        dam_years={1: np.full(HOURS, 50.0)},
        ida_years={1: np.full(HOURS, 55.0)},
    )
    deck = _load(store)
    assert deck.ida is not None
    assert (deck.ida[1] == 55.0).all()


# ---------------------------------------------------------------------------
# Calendar / resample rules
# ---------------------------------------------------------------------------


def test_hourly_store_step_holds_onto_15min_grid(tmp_path):
    deck = _load(
        _write_store(tmp_path, dam_years={1: np.arange(HOURS, dtype=float)}),
        n_steps=HOURS * 4, dt_minutes=15,
    )
    curve = deck.dam[1]
    assert len(curve) == HOURS * 4
    np.testing.assert_array_equal(curve[:8], [0, 0, 0, 0, 1, 1, 1, 1])


def test_15min_store_averages_onto_hourly_grid(tmp_path):
    quarters = np.tile([10.0, 20.0, 30.0, 40.0], HOURS)
    deck = _load(
        _write_store(tmp_path, dam_years={1: quarters}),
        n_steps=HOURS, dt_minutes=60,
    )
    assert deck.dam[1][0] == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# Basis bridge (real ↔ nominal)
# ---------------------------------------------------------------------------


def test_real_store_inflates_to_nominal(tmp_path):
    store = _write_store(
        tmp_path,
        meta={"basis": "real", "base_year": 2025},
        dam_years={1: np.full(HOURS, 100.0), 2: np.full(HOURS, 100.0)},
    )
    deck = _load(store, cpi_pct=2.0, start_year=2026)
    # Year 1 (calendar 2026): 100 × 1.02^(2026-2025); year 2: ×1.02².
    assert deck.dam[1][0] == pytest.approx(102.0)
    assert deck.dam[2][0] == pytest.approx(104.04)


def test_nominal_store_deflates_to_real_engine(tmp_path):
    store = _write_store(
        tmp_path, dam_years={1: np.full(HOURS, 100.0)},
    )
    deck = _load(
        store, engine_basis="real", engine_base_year=2026,
        cpi_pct=2.0, start_year=2026,
    )
    # Calendar 2026 == the engine base year: factor 1.
    assert deck.dam[1][0] == pytest.approx(100.0)


def test_real_store_rebases_to_real_engine(tmp_path):
    store = _write_store(
        tmp_path,
        meta={"basis": "real", "base_year": 2024},
        dam_years={1: np.full(HOURS, 100.0)},
    )
    deck = _load(
        store, engine_basis="real", engine_base_year=2026,
        cpi_pct=2.0, start_year=2026,
    )
    # Constant rebase 1.02^(2026-2024) — independent of the year: a
    # 2024-real price expressed in 2026 euros is numerically HIGHER,
    # matching the composition real→nominal→real of the two
    # cross-basis branches.
    assert deck.dam[1][0] == pytest.approx(100.0 * 1.02**2)


def test_zero_cpi_bridge_is_inert(tmp_path):
    store = _write_store(
        tmp_path,
        meta={"basis": "real", "base_year": 2020},
        dam_years={1: np.full(HOURS, 100.0)},
    )
    deck = _load(store, cpi_pct=0.0)
    assert deck.dam[1][0] == pytest.approx(100.0)


def test_bridge_scales_balancing_table_too(tmp_path):
    store = _write_store(
        tmp_path,
        meta={"basis": "real", "base_year": 2025},
        dam_years={1: np.full(HOURS, 100.0)},
        balancing_rows=[{
            "year": 1, "product": "afrr_up",
            "capacity_price_eur_per_mwh": 10.0,
            "activation_price_eur_per_mwh": 20.0,
        }],
    )
    deck = _load(store, cpi_pct=2.0, start_year=2026)
    assert deck.balancing is not None
    row = deck.balancing.iloc[0]
    assert row["capacity_price_eur_per_mwh"] == pytest.approx(10.2)
    assert row["activation_price_eur_per_mwh"] == pytest.approx(20.4)


# ---------------------------------------------------------------------------
# Parametric adapter
# ---------------------------------------------------------------------------


def _parametric_store(tmp_path: Path, block: dict) -> Path:
    store = tmp_path / "parametric_store"
    store.mkdir(exist_ok=True)
    (store / "meta.yaml").write_text(
        yaml.safe_dump({
            "provider": "parametric", "vintage": "v1",
            "parametric": block,
        }),
        encoding="utf-8",
    )
    return store


def test_parametric_level_drift_alone(tmp_path):
    store = _parametric_store(tmp_path, {"dam_level_pct_per_yr": -10.0})
    deck = build_parametric_deck(
        store, name="p", vintage="v", weight_pct=100.0,
        year1_dam=np.full(HOURS, 100.0), pv_kwh=None,
        year1_balancing=None, n_years=3,
    )
    assert deck.dam[1][0] == pytest.approx(100.0)
    assert deck.dam[2][0] == pytest.approx(90.0)
    assert deck.dam[3][0] == pytest.approx(81.0)


def test_parametric_capture_decline_hits_pv_hours_only(tmp_path):
    store = _parametric_store(
        tmp_path, {"pv_capture_decline_pct_per_yr": 10.0},
    )
    pv = np.zeros(HOURS)
    pv[12::24] = 5.0  # noon steps carry full PV weight
    deck = build_parametric_deck(
        store, name="p", vintage="v", weight_pct=100.0,
        year1_dam=np.full(HOURS, 100.0), pv_kwh=pv,
        year1_balancing=None, n_years=2,
    )
    # Year 2, noon (w = 1): 100 × (1 − (1 − 0.9^1)) = 90; night: 100.
    assert deck.dam[2][12] == pytest.approx(90.0)
    assert deck.dam[2][0] == pytest.approx(100.0)


def test_parametric_spread_scales_deviations_not_level(tmp_path):
    store = _parametric_store(
        tmp_path, {"spread_evolution_pct_per_yr": 50.0},
    )
    day = np.full(24, 100.0)
    day[12] = 140.0  # +40 above the daily mean core
    base = np.tile(day, 365)
    deck = build_parametric_deck(
        store, name="p", vintage="v", weight_pct=100.0,
        year1_dam=base, pv_kwh=None, year1_balancing=None, n_years=2,
    )
    daily_mean = day.mean()
    dev_noon = 140.0 - daily_mean
    dev_night = 100.0 - daily_mean
    assert deck.dam[1][12] == pytest.approx(140.0)
    assert deck.dam[2][12] == pytest.approx(daily_mean + dev_noon * 1.5)
    assert deck.dam[2][0] == pytest.approx(daily_mean + dev_night * 1.5)
    # The daily MEAN is untouched by a pure spread move.
    assert deck.dam[2][:24].mean() == pytest.approx(daily_mean)


def test_parametric_balancing_paths(tmp_path):
    store = _parametric_store(tmp_path, {
        "balancing": {
            "afrr_up": {
                "capacity_pct_per_yr": -10.0,
                "activation_pct_per_yr": -5.0,
            },
            "fcr": {"capacity_pct_per_yr": -20.0},
        },
    })
    deck = build_parametric_deck(
        store, name="p", vintage="v", weight_pct=100.0,
        year1_dam=np.full(HOURS, 100.0), pv_kwh=None,
        year1_balancing={"afrr_up": (10.0, 40.0), "fcr": (8.0, 0.0)},
        n_years=2,
    )
    assert deck.balancing is not None
    table = deck.balancing.set_index(["year", "product"])
    assert table.loc[(2, "afrr_up"),
                     "capacity_price_eur_per_mwh"] == pytest.approx(9.0)
    assert table.loc[(2, "afrr_up"),
                     "activation_price_eur_per_mwh"] == pytest.approx(38.0)
    assert table.loc[(2, "fcr"),
                     "capacity_price_eur_per_mwh"] == pytest.approx(6.4)
    assert table.loc[(2, "fcr"),
                     "activation_price_eur_per_mwh"] == pytest.approx(0.0)


def test_parametric_capture_without_pv_profile_is_an_error(tmp_path):
    store = _parametric_store(
        tmp_path, {"pv_capture_decline_pct_per_yr": 5.0},
    )
    with pytest.raises(PriceDataError, match="pv_kwh"):
        build_parametric_deck(
            store, name="p", vintage="v", weight_pct=100.0,
            year1_dam=np.full(HOURS, 100.0), pv_kwh=None,
            year1_balancing=None, n_years=2,
        )


def test_parametric_missing_block_is_an_error(tmp_path):
    store = tmp_path / "s"
    store.mkdir()
    (store / "meta.yaml").write_text(
        yaml.safe_dump({"provider": "parametric"}), encoding="utf-8",
    )
    with pytest.raises(PriceDataError, match="parametric"):
        build_parametric_deck(
            store, name="p", vintage="v", weight_pct=100.0,
            year1_dam=np.full(HOURS, 100.0), pv_kwh=None,
            year1_balancing=None, n_years=2,
        )


# ---------------------------------------------------------------------------
# TYNDP adapter
# ---------------------------------------------------------------------------


def _tyndp_store(tmp_path: Path, milestones: dict[int, float]) -> Path:
    store = tmp_path / "tyndp_store"
    store.mkdir(exist_ok=True)
    files: dict[int, str] = {}
    for year, level in milestones.items():
        fname = f"tyndp_{year}.csv"
        pd.DataFrame({
            "dam_price_eur_per_mwh": np.full(HOURS, level),
        }).to_csv(store / fname, index=False)
        files[year] = fname
    (store / "meta.yaml").write_text(
        yaml.safe_dump({
            "provider": "tyndp", "vintage": "TYNDP-2026",
            "license": "CC-BY 4.0",
            "tyndp": {"files": files},
        }),
        encoding="utf-8",
    )
    return store


def test_tyndp_interpolates_between_milestones(tmp_path):
    store = _tyndp_store(tmp_path, {2030: 50.0, 2040: 70.0})
    deck = build_tyndp_deck(
        store, name="t", vintage="v", weight_pct=100.0,
        n_steps=HOURS, dt_minutes=60, n_years=25, start_year=2026,
    )
    assert deck.dam[1][0] == pytest.approx(50.0)    # 2026 → hold first
    assert deck.dam[5][0] == pytest.approx(50.0)    # 2030 exactly
    assert deck.dam[10][0] == pytest.approx(60.0)   # 2035 → midpoint
    assert deck.dam[15][0] == pytest.approx(70.0)   # 2040 exactly
    assert deck.dam[25][0] == pytest.approx(70.0)   # 2050 → hold last


def test_tyndp_missing_file_is_an_error(tmp_path):
    store = _tyndp_store(tmp_path, {2030: 50.0})
    (store / "tyndp_2030.csv").unlink()
    with pytest.raises(PriceDataError, match="not found"):
        build_tyndp_deck(
            store, name="t", vintage="v", weight_pct=100.0,
            n_steps=HOURS, dt_minutes=60, n_years=5, start_year=2026,
        )


def test_tyndp_without_files_block_is_an_error(tmp_path):
    store = tmp_path / "s"
    store.mkdir()
    (store / "meta.yaml").write_text(
        yaml.safe_dump({"provider": "tyndp"}), encoding="utf-8",
    )
    with pytest.raises(PriceDataError, match=r"tyndp\.files"):
        build_tyndp_deck(
            store, name="t", vintage="v", weight_pct=100.0,
            n_steps=HOURS, dt_minutes=60, n_years=5, start_year=2026,
        )


# ---------------------------------------------------------------------------
# Vendor stubs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["retwin", "ffe", "maon", "afry"])
def test_vendor_stub_error_names_the_alternatives(provider):
    err = stub_provider_error(provider)
    assert provider in str(err)
    assert "parametric" in str(err)


# ---------------------------------------------------------------------------
# Audit regressions: basis bridge on the held tail, balancing table
# validation, adapter basis handling, negative-price cannibalization
# ---------------------------------------------------------------------------


def test_real_store_held_tail_keeps_inflating(tmp_path):
    """hold_last is materialised BEFORE the bridge: a real-basis store
    keeps inflating at CPI through the held tail instead of
    flatlining at the last declared year's nominal level."""
    store = _write_store(
        tmp_path,
        meta={"basis": "real", "base_year": 2026},
        dam_years={1: np.full(HOURS, 100.0), 2: np.full(HOURS, 100.0)},
    )
    deck = _load(store, n_years=4, cpi_pct=2.0, start_year=2026)
    assert deck.dam_curve(1).mean() == pytest.approx(100.0)
    assert deck.dam_curve(2).mean() == pytest.approx(102.0)
    assert deck.dam_curve(3).mean() == pytest.approx(104.04)
    assert deck.dam_curve(4).mean() == pytest.approx(106.1208)


def test_balancing_held_tail_is_per_product(tmp_path):
    """A product whose rows stop earlier holds its OWN last year."""
    store = _write_store(
        tmp_path,
        balancing_rows=[
            {"year": 1, "product": "afrr_up",
             "capacity_price_eur_per_mwh": 10.0,
             "activation_price_eur_per_mwh": 40.0},
            {"year": 2, "product": "afrr_up",
             "capacity_price_eur_per_mwh": 8.0,
             "activation_price_eur_per_mwh": 30.0},
            {"year": 1, "product": "mfrr_up",
             "capacity_price_eur_per_mwh": 5.0,
             "activation_price_eur_per_mwh": 20.0},
        ],
    )
    deck = _load(store, n_years=3)
    assert deck.balancing is not None
    table = deck.balancing.set_index(["year", "product"]).sort_index()
    # afrr_up holds its year-2 prices; mfrr_up holds its year-1 prices.
    assert table.loc[(3, "afrr_up"),
                     "capacity_price_eur_per_mwh"] == pytest.approx(8.0)
    assert table.loc[(3, "mfrr_up"),
                     "capacity_price_eur_per_mwh"] == pytest.approx(5.0)
    assert table.loc[(2, "mfrr_up"),
                     "activation_price_eur_per_mwh"] == pytest.approx(20.0)


def test_balancing_interior_gap_is_an_error(tmp_path):
    store = _write_store(
        tmp_path,
        balancing_rows=[
            {"year": 1, "product": "afrr_up",
             "capacity_price_eur_per_mwh": 10.0,
             "activation_price_eur_per_mwh": 40.0},
            {"year": 3, "product": "afrr_up",
             "capacity_price_eur_per_mwh": 8.0,
             "activation_price_eur_per_mwh": 30.0},
        ],
    )
    with pytest.raises(PriceDataError, match="contiguous"):
        _load(store, n_years=3)


def test_balancing_blank_capacity_cell_is_an_error(tmp_path):
    store = _write_store(
        tmp_path,
        balancing_rows=[
            {"year": 1, "product": "afrr_up",
             "capacity_price_eur_per_mwh": None,
             "activation_price_eur_per_mwh": 40.0},
        ],
    )
    with pytest.raises(PriceDataError, match="explicit 0"):
        _load(store, n_years=1)


def test_balancing_blank_non_fcr_activation_is_an_error(tmp_path):
    """A capacity-only product must price activation explicitly at 0 —
    a blank cell would leak NaN past the engine's zero-base guard."""
    store = _write_store(
        tmp_path,
        balancing_rows=[
            {"year": 1, "product": "mfrr_up",
             "capacity_price_eur_per_mwh": 15.0,
             "activation_price_eur_per_mwh": None},
        ],
    )
    with pytest.raises(PriceDataError, match="FCR is the only"):
        _load(store, n_years=1)


def test_tyndp_real_basis_bridges_to_nominal_engine(tmp_path):
    """A real-basis TYNDP store is bridged like a file store — the
    milestone trend must not be silently treated as nominal."""
    store = _tyndp_store(tmp_path, {2030: 50.0})
    meta = yaml.safe_load((store / "meta.yaml").read_text(encoding="utf-8"))
    meta["basis"] = "real"
    meta["base_year"] = 2030
    (store / "meta.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    deck = build_tyndp_deck(
        store, name="t", vintage="v", weight_pct=100.0,
        n_steps=HOURS, dt_minutes=60, n_years=2, start_year=2026,
        engine_basis="nominal", engine_base_year=0, cpi_pct=2.0,
    )
    # Year 1 (calendar 2026): 50 real-2030 EUR -> 50 x 1.02^(2026-2030).
    assert deck.dam[1][0] == pytest.approx(50.0 * 1.02 ** (2026 - 2030))
    assert deck.dam[2][0] == pytest.approx(50.0 * 1.02 ** (2027 - 2030))


def test_tyndp_bogus_steps_per_day_is_precise(tmp_path):
    store = tmp_path / "tyndp_store"
    store.mkdir()
    pd.DataFrame({
        "dam_price_eur_per_mwh": np.full(7 * 365, 50.0),  # 7 steps/day
    }).to_csv(store / "t.csv", index=False)
    (store / "meta.yaml").write_text(
        yaml.safe_dump({
            "provider": "tyndp", "tyndp": {"files": {2030: "t.csv"}},
        }),
        encoding="utf-8",
    )
    with pytest.raises(PriceDataError, match="steps/day"):
        build_tyndp_deck(
            store, name="t", vintage="v", weight_pct=100.0,
            n_steps=HOURS, dt_minutes=60, n_years=2, start_year=2026,
        )


def test_parametric_rejects_foreign_basis(tmp_path):
    store = _parametric_store(tmp_path, {"dam_level_pct_per_yr": -5.0})
    meta = yaml.safe_load((store / "meta.yaml").read_text(encoding="utf-8"))
    meta["basis"] = "real"
    meta["base_year"] = 2025
    (store / "meta.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    with pytest.raises(PriceDataError, match="engine basis"):
        build_parametric_deck(
            store, name="p", vintage="v", weight_pct=100.0,
            year1_dam=np.full(HOURS, 100.0), pv_kwh=None,
            year1_balancing=None, n_years=2,
        )


def test_parametric_capture_deepens_negative_prices(tmp_path):
    """Cannibalization must push a negative solar-hour price DOWN —
    the haircut applies to the daily-mean component, never as a
    multiplicative factor on the signed price."""
    store = _parametric_store(
        tmp_path, {"pv_capture_decline_pct_per_yr": 10.0},
    )
    day = np.full(24, 60.0)
    day[12] = -5.0  # negative midday price
    base = np.tile(day, 365)
    pv = np.zeros(HOURS)
    pv[12::24] = 5.0  # full PV weight at noon
    deck = build_parametric_deck(
        store, name="p", vintage="v", weight_pct=100.0,
        year1_dam=base, pv_kwh=pv, year1_balancing=None, n_years=2,
    )
    assert deck.dam[1][12] == pytest.approx(-5.0)
    # Year 2 noon: daily mean loses 10 % under full weight while the
    # negative deviation rides along -> the price gets MORE negative.
    daily_mean = day.mean()
    expected = daily_mean * 0.9 + (-5.0 - daily_mean)
    assert deck.dam[2][12] == pytest.approx(expected)
    assert deck.dam[2][12] < deck.dam[1][12]
