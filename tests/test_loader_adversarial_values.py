"""Adversarial-VALUES loader guards.

Weird-but-plausibly-typed workbook values a client can produce in Excel
must either normalise correctly or fail fast naming the key — never be
silently accepted into a wrong number.  Locks the round-10 loader fixes:
row-order validation, boolean fail-fast, the non-negative wear cost, the
unavailability range, empty-required-column detection, non-finite
numerics, integer truncation, the discount-rate bound, the Monte-Carlo
knob ranges, negative energy columns and the max-injection profile
cells.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.io import (
    _parse_bool,
    _parse_max_injection_profile_sheet,
    _parse_value,
    read_inputs,
    read_workbook,
    validate_workbook_params,
    write_workbook,
)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def base_typed() -> dict:
    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)
    return typed


def _write(tmp_path: Path, typed: dict, name: str = "case.xlsx") -> Path:
    out = tmp_path / name
    write_workbook(typed, out)
    return out


# --- row order (the MILP chains SOC over row order) ------------------------


def test_unsorted_timeseries_rows_are_rejected(tmp_path, base_typed):
    import copy

    typed = copy.deepcopy(base_typed)
    ts = typed["ts"]
    ts.iloc[[10, 11]] = ts.iloc[[11, 10]].values  # swap two rows
    with pytest.raises(ValueError, match="chronological order"):
        read_inputs(_write(tmp_path, typed))


def test_reversed_timeseries_rows_are_rejected(tmp_path, base_typed):
    import copy

    typed = copy.deepcopy(base_typed)
    typed["ts"] = typed["ts"].iloc[::-1].reset_index(drop=True)
    with pytest.raises(ValueError, match="chronological order"):
        read_inputs(_write(tmp_path, typed))


# --- booleans: unknown tokens fail fast, friendly tokens normalise ---------


def test_bool_unknown_token_raises_naming_the_key():
    with pytest.raises(ValueError, match="terminal_soc_equal"):
        _parse_value("terminal_soc_equal", "x", True)
    with pytest.raises(ValueError, match="sensitivity_enabled"):
        _parse_value("sensitivity_enabled", "oui", True)


def test_bool_onoff_enabled_disabled_normalise():
    assert _parse_value("terminal_soc_equal", "off", True) is False
    assert _parse_value("terminal_soc_equal", "on", False) is True
    assert _parse_value("sensitivity_enabled", "disabled", True) is False
    assert _parse_value("sensitivity_enabled", "enabled", False) is True


def test_bool_without_key_context_stays_lenient():
    # Free-form flag cells outside the workbook schema keep the old
    # default-on-unknown behaviour.
    assert _parse_bool("mystery", True) is True
    assert _parse_bool("mystery", False) is False


# --- numeric guards --------------------------------------------------------


def test_negative_wear_cost_is_rejected(base_typed):
    import copy

    typed = copy.deepcopy(base_typed)
    typed["bess"]["bess_wear_cost_eur_per_mwh"] = -10.0
    with pytest.raises(ValueError, match="bess_wear_cost_eur_per_mwh"):
        validate_workbook_params(typed)


@pytest.mark.parametrize("bad", [150.0, -5.0])
def test_unavailability_pct_out_of_range_is_rejected(base_typed, bad):
    import copy

    typed = copy.deepcopy(base_typed)
    typed["project"]["unavailability_pct"] = bad
    with pytest.raises(ValueError, match="unavailability_pct"):
        validate_workbook_params(typed)


@pytest.mark.parametrize("token", ["nan", "inf", "-inf"])
def test_nonfinite_numeric_strings_are_rejected(token):
    with pytest.raises(ValueError, match="finite"):
        _parse_value("capex_bess_eur_per_kwh", token, 100.0)


def test_lifecycle_below_one_and_fractional_are_rejected(base_typed):
    import copy

    typed = copy.deepcopy(base_typed)
    typed["project"]["project_lifecycle_years"] = 0
    with pytest.raises(ValueError, match="project_lifecycle_years"):
        validate_workbook_params(typed)
    # fractional int-key cells truncate silently without the parser guard
    with pytest.raises(ValueError, match="whole number"):
        _parse_value("project_lifecycle_years", 20.7, 20)
    assert _parse_value("project_lifecycle_years", 20.0, 20) == 20


def test_discount_rate_at_or_below_minus_100_is_rejected(base_typed):
    import copy

    typed = copy.deepcopy(base_typed)
    typed["economics"]["discount_rate_pct"] = -100.0
    with pytest.raises(ValueError, match="discount_rate_pct"):
        validate_workbook_params(typed)
    # Moderately negative rates are legitimate.
    typed["economics"]["discount_rate_pct"] = -7.0
    validate_workbook_params(typed)


def test_mc_knobs_are_range_checked(base_typed):
    import copy

    typed = copy.deepcopy(base_typed)
    typed["simulation"]["uncertainty_sigma_dam"] = -0.2
    with pytest.raises(ValueError, match="uncertainty_sigma_dam"):
        validate_workbook_params(typed)

    typed = copy.deepcopy(base_typed)
    typed["simulation"]["uncertainty_n_seeds"] = 0
    with pytest.raises(ValueError, match="uncertainty_n_seeds"):
        validate_workbook_params(typed)

    typed = copy.deepcopy(base_typed)
    typed.setdefault("balancing", {})["bm_random_seed"] = -1
    with pytest.raises(ValueError, match="bm_random_seed"):
        validate_workbook_params(typed)


# --- timeseries columns ----------------------------------------------------


def test_all_nan_dam_column_in_merchant_warns_like_absent(
    tmp_path, base_typed, caplog,
):
    """A present-but-empty DAM column and an ABSENT one are semantically
    identical inputs; both get the same loud zero-revenue warning (a raise
    would kill deliberately price-free balancing-only merchant decks and
    documented price_source bypass workflows), and a non-'file'
    price_source keeps the empty column QUIET (the fetch fills it)."""
    import copy
    import logging

    typed = copy.deepcopy(base_typed)
    typed["project"]["mode"] = "merchant"
    typed["ts"]["dam_price_eur_per_mwh"] = np.nan
    with caplog.at_level(logging.WARNING):
        read_inputs(_write(tmp_path, typed))
    assert any(
        "priced 0" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]

    # With the market bypass armed the resolver fills the column, so the
    # empty column is the documented intent — no zero-revenue warning.
    caplog.clear()
    typed2 = copy.deepcopy(typed)
    typed2.setdefault("market_data", {})["price_source"] = "entsoe"
    from pvbess_opt.io import _normalise_timeseries

    with caplog.at_level(logging.WARNING):
        _normalise_timeseries(
            typed2["ts"].copy(), mode="merchant",
            market_sources=typed2["market_data"],
        )
    assert not any(
        "priced 0" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_all_nan_load_column_in_self_consumption_is_rejected(
    tmp_path, base_typed,
):
    """load_kwh in self-consumption has no later filler (no market source,
    no resolver), so an entirely empty column stays a hard error."""
    import copy

    typed = copy.deepcopy(base_typed)
    typed["ts"]["load_kwh"] = np.nan
    with pytest.raises(ValueError, match="load_kwh"):
        read_inputs(_write(tmp_path, typed))


def test_negative_pv_kwh_is_rejected(tmp_path, base_typed):
    import copy

    typed = copy.deepcopy(base_typed)
    typed["ts"].loc[12, "pv_kwh"] = -500.0
    with pytest.raises(ValueError, match="pv_kwh"):
        read_inputs(_write(tmp_path, typed))


# --- max-injection profile cells -------------------------------------------


def _profile_frame(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "hour_of_day": list(range(24)),
        "max_injection_pct": values,
    })


def test_max_injection_profile_blank_cell_is_rejected():
    vals = [100.0] * 24
    vals[12] = np.nan
    with pytest.raises(ValueError, match="hour 12"):
        _parse_max_injection_profile_sheet(_profile_frame(vals))


@pytest.mark.parametrize("bad", [-50.0, 8000.0])
def test_max_injection_profile_out_of_range_is_rejected(bad):
    vals = [100.0] * 24
    vals[3] = bad
    with pytest.raises(ValueError, match=r"\[0, 100\]"):
        _parse_max_injection_profile_sheet(_profile_frame(vals))


# --- the valid shipped workbook still loads unchanged ----------------------


def test_shipped_workbook_passes_all_new_guards():
    _params, ts = read_inputs(ROOT / "inputs" / "input.xlsx")
    assert len(ts) == 35040
    assert pd.to_datetime(ts["timestamp"]).is_monotonic_increasing


# --- round-11 guard refinements --------------------------------------------


def test_blank_timestamp_cell_named_precisely(tmp_path, base_typed):
    """A blank/unparseable timestamp cell must be named as such (with its
    row), not mis-reported as an out-of-order sheet the sort remedy cannot
    fix."""
    import copy

    typed = copy.deepcopy(base_typed)
    typed["ts"].loc[50, "timestamp"] = pd.NaT
    with pytest.raises(ValueError, match=r"blank/unparseable timestamp.*50"):
        read_inputs(_write(tmp_path, typed))


def test_native_infinity_on_int_key_raises_cleanly():
    """A native inf (YAML .inf / JSON Infinity) on an integer key must get
    the finite-number ValueError, not an uncaught OverflowError; NaN stays
    the blank-cell sentinel resolving to the default."""
    with pytest.raises(ValueError, match="project_lifecycle_years"):
        _parse_value("project_lifecycle_years", float("inf"), 20)
    assert _parse_value("project_lifecycle_years", float("nan"), 20) == 20


def test_weather_year_fractional_is_rejected():
    from pvbess_opt.io import _parse_pv_weather_year

    with pytest.raises(ValueError, match="whole calendar year"):
        _parse_pv_weather_year(2019.5, 2019)
    assert _parse_pv_weather_year(2019.0, 2019) == 2019


# --- round-12 guard refinements --------------------------------------------


def test_empty_imbalance_column_behaves_like_absent(tmp_path, base_typed, caplog):
    """A present-but-all-NaN imbalance price column must behave EXACTLY like
    an absent one.  It previously passed the imbalance_pricing='single'
    presence gate and reached settlement as NaN — NaN cashflows with no
    error, while an absent column raised cleanly at load."""
    import copy
    import logging

    typed = copy.deepcopy(base_typed)
    typed.setdefault("simulation", {})["uncertainty_enabled"] = True
    typed["simulation"]["imbalance_enabled"] = True
    typed["simulation"]["imbalance_pricing"] = "single"
    typed["ts"]["imbalance_price_eur_per_mwh"] = np.nan
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValueError, match="imbalance_price_eur_per_mwh"):
            read_inputs(_write(tmp_path, typed))
    assert any(
        "exactly like an absent column" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]

    # Dual pricing: the dropped empty column re-enables the documented DAM
    # proxy instead of propagating NaN into settlement.
    from pvbess_opt.io import _normalise_timeseries
    from pvbess_opt.rolling_horizon import resolve_imbalance_prices

    idx = pd.date_range("2026-01-01", periods=4, freq="h")
    ts = pd.DataFrame({
        "timestamp": idx, "pv_kwh": [1.0] * 4, "load_kwh": [2.0] * 4,
        "dam_price_eur_per_mwh": [50.0] * 4,
        "imbalance_price_eur_per_mwh": [np.nan] * 4,
    })
    out = _normalise_timeseries(ts, mode="self_consumption")
    assert "imbalance_price_eur_per_mwh" not in out.columns
    short, long = resolve_imbalance_prices(
        out, np.array([50.0, 60.0, 40.0, 55.0]),
        pricing="dual", mult_short=1.15, mult_long=0.85,
    )
    assert np.isfinite(short).all() and np.isfinite(long).all()


def test_empty_deck_variant_column_is_dropped_and_deck_fails_fast(caplog):
    """An all-NaN deck variant column previously survived under a factually
    wrong 'filled via ffill/bfill' warning and silently priced the deck's
    scenario 0 everywhere; it is now dropped like any empty column, so a
    scenario requesting the deck hits the loud 'matches no variant column'
    error instead."""
    import logging

    from pvbess_opt.io import _normalise_timeseries
    from pvbess_opt.scenarios import _apply_price_deck

    idx = pd.date_range("2026-01-01", periods=4, freq="h")
    ts = pd.DataFrame({
        "timestamp": idx, "pv_kwh": [1.0] * 4, "load_kwh": [2.0] * 4,
        "dam_price_eur_per_mwh": [50.0] * 4,
        "dam_price_eur_per_mwh__low": [np.nan] * 4,
    })
    with caplog.at_level(logging.WARNING):
        out = _normalise_timeseries(ts, mode="merchant")
    assert "dam_price_eur_per_mwh__low" not in out.columns
    assert any(
        "'low' deck will not exist" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]
    assert not any(
        "filled via ffill/bfill" in r.getMessage() for r in caplog.records
    )
    with pytest.raises(ValueError, match=r"matches no .*variant column"):
        _apply_price_deck(out, "low", scenario_name="s", keep_deck=None)


def test_unparseable_timestamp_string_named_by_row():
    """A garbage timestamp STRING must get the branded blank/unparseable
    message with the row named, not pandas' raw parse error."""
    from pvbess_opt.io import _normalise_timeseries

    idx = pd.date_range("2026-01-01", periods=4, freq="h")
    ts = pd.DataFrame({
        "timestamp": idx.astype(object), "pv_kwh": [1.0] * 4,
        "load_kwh": [2.0] * 4, "dam_price_eur_per_mwh": [50.0] * 4,
    })
    ts.loc[2, "timestamp"] = "banana"
    with pytest.raises(ValueError, match=r"blank/unparseable timestamp.*2"):
        _normalise_timeseries(ts, mode="merchant")


def test_duplicate_timestamp_named_by_row():
    """A duplicated row passes a non-strict monotonic check and previously
    died in timestep detection with a resample hint that cannot fix
    duplication; it is now named directly."""
    from pvbess_opt.io import _normalise_timeseries

    idx = list(pd.date_range("2026-01-01", periods=4, freq="h"))
    idx[2] = idx[1]
    ts = pd.DataFrame({
        "timestamp": idx, "pv_kwh": [1.0] * 4, "load_kwh": [2.0] * 4,
        "dam_price_eur_per_mwh": [50.0] * 4,
    })
    with pytest.raises(ValueError, match=r"duplicate timestamp at row 2"):
        _normalise_timeseries(ts, mode="merchant")


def test_negative_pv_blamed_on_original_row_not_bfilled():
    """With a leading blank cell bfilled from a negative value, the error
    must blame the file's own negative cell, not the blank row 0."""
    from pvbess_opt.io import _normalise_timeseries

    idx = pd.date_range("2026-01-01", periods=4, freq="h")
    ts = pd.DataFrame({
        "timestamp": idx, "pv_kwh": [np.nan, -1.0, 2.0, 3.0],
        "load_kwh": [2.0] * 4, "dam_price_eur_per_mwh": [50.0] * 4,
    })
    with pytest.raises(ValueError, match=r"01:00"):
        _normalise_timeseries(ts, mode="merchant")


def test_partial_nan_fetched_column_stays_quiet(caplog):
    """A partially-NaN price column about to be REPLACED by a fetch must
    not emit the 'check the input timeseries for gaps' warning — the gaps
    are irrelevant, the fetch overwrites the column wholesale."""
    import logging

    from pvbess_opt.io import _normalise_timeseries

    idx = pd.date_range("2026-01-01", periods=4, freq="h")
    ts = pd.DataFrame({
        "timestamp": idx, "pv_kwh": [1.0] * 4, "load_kwh": [2.0] * 4,
        "dam_price_eur_per_mwh": [50.0, np.nan, 40.0, np.nan],
    })
    with caplog.at_level(logging.WARNING):
        _normalise_timeseries(
            ts, mode="merchant", market_sources={"price_source": "entsoe"},
        )
    assert not any(
        "filled via ffill/bfill" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]

    # The imbalance trio maps to imbalance_source the same way (each entry
    # of _COLUMN_MARKET_SOURCE, not just the DAM one, must gate the checks).
    caplog.clear()
    ts2 = pd.DataFrame({
        "timestamp": idx, "pv_kwh": [1.0] * 4, "load_kwh": [2.0] * 4,
        "dam_price_eur_per_mwh": [50.0] * 4,
        "imbalance_price_eur_per_mwh": [np.nan] * 4,
        "imbalance_price_short_eur_per_mwh": [60.0, np.nan, 55.0, np.nan],
    })
    with caplog.at_level(logging.WARNING):
        out2 = _normalise_timeseries(
            ts2, mode="merchant",
            market_sources={"imbalance_source": "entsoe"},
        )
    # Empty AND partial imbalance columns both stay quiet (fetch fills
    # them); the empty one is NOT dropped — the fetch writes into it.
    assert "imbalance_price_eur_per_mwh" in out2.columns
    assert not any(
        "imbalance" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_read_workbook_threads_market_sources_to_the_normaliser(
    tmp_path, base_typed, caplog, monkeypatch,
):
    """The production read path must pass the market_data sources into the
    normaliser: an empty merchant DAM column with price_source='entsoe'
    stays quiet THROUGH read_inputs (not just when the normaliser is called
    directly), while the same workbook with price_source='file' warns."""
    import copy
    import logging

    from pvbess_opt import marketdata

    monkeypatch.setattr(
        marketdata, "resolve_market_data", lambda *_a, **_k: None,
    )
    typed = copy.deepcopy(base_typed)
    typed["project"]["mode"] = "merchant"
    typed["ts"]["dam_price_eur_per_mwh"] = np.nan
    typed.setdefault("market_data", {})["price_source"] = "entsoe"
    with caplog.at_level(logging.WARNING):
        read_inputs(_write(tmp_path, typed, "entsoe.xlsx"))
    assert not any(
        "priced 0" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]

    caplog.clear()
    typed["market_data"]["price_source"] = "file"
    with caplog.at_level(logging.WARNING):
        read_inputs(_write(tmp_path, typed, "file.xlsx"))
    assert any("priced 0" in r.getMessage() for r in caplog.records)


# --- round-13 guard refinements --------------------------------------------


def test_empty_balancing_column_behaves_like_absent(caplog):
    """The nine balancing price columns must follow the round-12 contract
    too: a present-but-empty column previously bypassed the scalar
    fallback (which fills only ABSENT columns) and settled every product
    at NaN — NaN balancing revenue and NaN NPV with no warning anywhere."""
    import logging

    from pvbess_opt.io import _BALANCING_TS_COLUMN_DEFAULTS, _normalise_timeseries

    idx = pd.date_range("2026-01-01", periods=4, freq="h")
    base = {"timestamp": idx, "pv_kwh": [1.0] * 4, "load_kwh": [2.0] * 4,
            "dam_price_eur_per_mwh": [50.0] * 4}
    for col in _BALANCING_TS_COLUMN_DEFAULTS:
        caplog.clear()
        ts = pd.DataFrame({**base, col: [np.nan] * 4})
        with caplog.at_level(logging.WARNING):
            out = _normalise_timeseries(ts, mode="self_consumption")
        assert col not in out.columns, col
        assert any(
            "exactly like an absent column" in r.getMessage()
            for r in caplog.records
        ), (col, [r.getMessage() for r in caplog.records])

    # A fetch-bypassed balancing column stays (the fetch fills it), quiet.
    caplog.clear()
    col = "mfrr_up_capacity_price_eur_per_mwh"
    ts = pd.DataFrame({**base, col: [np.nan] * 4})
    with caplog.at_level(logging.WARNING):
        out = _normalise_timeseries(
            ts, mode="self_consumption",
            market_sources={"balancing_source": "entsoe"},
        )
    assert col in out.columns
    assert not any(col in r.getMessage() for r in caplog.records)

    # Partial NaN gets the standard gap treatment (was: silent NaN gaps
    # riding into settlement).
    caplog.clear()
    ts2 = pd.DataFrame({**base, col: [12.0, np.nan, 14.0, np.nan]})
    with caplog.at_level(logging.WARNING):
        out2 = _normalise_timeseries(ts2, mode="self_consumption")
    assert list(out2[col]) == [12.0, 12.0, 14.0, 14.0]
    assert any("filled via ffill/bfill" in r.getMessage() for r in caplog.records)


def test_empty_grid_co2_column_is_dropped(caplog):
    """An all-NaN grid_co2_kg_per_mwh column flipped the per-step
    carbon-intensity presence gate and delivered NaN emissions KPIs; it now
    behaves exactly like an absent column (scalar/off path)."""
    import logging

    from pvbess_opt.io import _normalise_timeseries

    idx = pd.date_range("2026-01-01", periods=4, freq="h")
    ts = pd.DataFrame({
        "timestamp": idx, "pv_kwh": [1.0] * 4, "load_kwh": [2.0] * 4,
        "dam_price_eur_per_mwh": [50.0] * 4,
        "grid_co2_kg_per_mwh": [np.nan] * 4,
    })
    with caplog.at_level(logging.WARNING):
        out = _normalise_timeseries(ts, mode="merchant")
    assert "grid_co2_kg_per_mwh" not in out.columns
    assert any(
        "exactly like an absent column" in r.getMessage()
        for r in caplog.records
    )


def test_whitespace_cell_in_price_column_is_named():
    """A whitespace-only cell previously surfaced as a bare 'could not
    convert string to float' with no pointer to the sheet or column."""
    from pvbess_opt.io import _normalise_timeseries

    idx = pd.date_range("2026-01-01", periods=4, freq="h")
    ts = pd.DataFrame({
        "timestamp": idx, "pv_kwh": [1.0] * 4, "load_kwh": [2.0] * 4,
        "dam_price_eur_per_mwh": ["50", " ", "40", "30"],
    })
    with pytest.raises(ValueError, match="dam_price_eur_per_mwh"):
        _normalise_timeseries(ts, mode="merchant")


# --- round-14 guard refinements --------------------------------------------


def test_present_but_empty_balancing_column_takes_scalar_default_always():
    """The LAST line of defence for empty==absent: a present-but-all-NaN
    balancing column that survives to the scalar fallback (a fetch-exempted
    column the provider does not publish — e.g. FCR under the ADMIE source,
    Greece procures no standalone FCR) must take the scalar default like an
    absent column, not settle every product at NaN."""
    from pvbess_opt.io import (
        _apply_balancing_timeseries_fallback,
        _normalise_timeseries,
    )

    idx = pd.date_range("2026-01-01", periods=4, freq="h")
    # Direct fallback: present-but-all-NaN -> filled from the scalar.
    ts = pd.DataFrame({
        "timestamp": idx,
        "fcr_capacity_price_eur_per_mwh": [np.nan] * 4,
    })
    bal = {"balancing_enabled": True,
           "fcr_default_capacity_price_eur_per_mwh": 9.99}
    out, filled = _apply_balancing_timeseries_fallback(ts, bal)
    assert out["fcr_capacity_price_eur_per_mwh"].tolist() == [9.99] * 4
    assert "fcr_capacity_price_eur_per_mwh" in filled
    # End-to-end shape of the ADMIE gap: the normaliser keeps the column
    # quietly (fetch-exempted), the fetch never fills FCR, the fallback
    # must still rescue it.
    ts2 = pd.DataFrame({
        "timestamp": idx, "pv_kwh": [1.0] * 4, "load_kwh": [2.0] * 4,
        "dam_price_eur_per_mwh": [50.0] * 4,
        "fcr_capacity_price_eur_per_mwh": [np.nan] * 4,
    })
    kept = _normalise_timeseries(
        ts2, mode="self_consumption",
        market_sources={"balancing_source": "admie"},
    )
    assert kept["fcr_capacity_price_eur_per_mwh"].isna().all()
    rescued, _ = _apply_balancing_timeseries_fallback(kept, bal)
    assert rescued["fcr_capacity_price_eur_per_mwh"].tolist() == [9.99] * 4
    # A populated column stays verbatim (bit-identity depends on it).
    ts3 = pd.DataFrame({
        "timestamp": idx,
        "fcr_capacity_price_eur_per_mwh": [1.0, 2.0, 3.0, 4.0],
    })
    same, refilled = _apply_balancing_timeseries_fallback(ts3, bal)
    assert same["fcr_capacity_price_eur_per_mwh"].tolist() == [1.0, 2.0, 3.0, 4.0]
    # Real data is never marked as materialised (the other eight
    # absent columns are, and legitimately so).
    assert "fcr_capacity_price_eur_per_mwh" not in refilled


def test_whitespace_cells_named_in_energy_and_deck_columns():
    """The round-13 astype guard covered the canonical loop only; the
    negative-value pre-check (pv_kwh/load_kwh) and the deck-variant loop
    still surfaced a bare 'could not convert string to float'."""
    from pvbess_opt.io import _normalise_timeseries

    idx = pd.date_range("2026-01-01", periods=4, freq="h")
    base = {"timestamp": idx, "pv_kwh": [1.0] * 4, "load_kwh": [2.0] * 4,
            "dam_price_eur_per_mwh": [50.0] * 4}
    bad_pv = pd.DataFrame({**base, "pv_kwh": ["1", " ", "3", "4"]})
    with pytest.raises(ValueError, match="pv_kwh"):
        _normalise_timeseries(bad_pv, mode="merchant")
    bad_deck = pd.DataFrame({
        **base, "dam_price_eur_per_mwh__low": ["1", " ", "3", "4"],
    })
    with pytest.raises(ValueError, match="dam_price_eur_per_mwh__low"):
        _normalise_timeseries(bad_deck, mode="merchant")


def test_negative_grid_co2_is_rejected():
    """A negative carbon intensity is physically wrong and would silently
    flip emissions KPIs' sign; rejected naming the column and first row."""
    from pvbess_opt.io import _normalise_timeseries

    idx = pd.date_range("2026-01-01", periods=4, freq="h")
    ts = pd.DataFrame({
        "timestamp": idx, "pv_kwh": [1.0] * 4, "load_kwh": [2.0] * 4,
        "dam_price_eur_per_mwh": [50.0] * 4,
        "grid_co2_kg_per_mwh": [100.0, -250.0, 300.0, 200.0],
    })
    with pytest.raises(ValueError, match=r"grid_co2_kg_per_mwh.*01:00"):
        _normalise_timeseries(ts, mode="merchant")


def test_augmentation_years_infinite_entries_raise_cleanly():
    """`bess_augmentation_years: .inf` previously died in int() with a bare
    OverflowError naming nothing; it must raise ValueError naming the key
    like every other malformed entry."""
    from pvbess_opt.io import parse_augmentation_years

    for bad in (float("inf"), float("-inf"), "inf", "8,inf"):
        with pytest.raises(ValueError, match="bess_augmentation_years"):
            parse_augmentation_years(bad)
    assert parse_augmentation_years("8,15") == (8, 15)
    assert parse_augmentation_years(8.0) == (8,)
