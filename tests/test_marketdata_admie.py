"""ADMIE provider: xlsx parsing, DST day assembly, fetch/cache, registry.

No live network — the ADMIE HTTP calls are mocked with synthetic daily
workbooks built to the provider's declared (provisional) header-pattern
contract; pin against real files via ``scripts/probe_market_data.py
--save-dir`` on a network-enabled machine.
"""

from __future__ import annotations

import io
import json
from datetime import date, timedelta

import numpy as np
import pytest
from openpyxl import Workbook

from pvbess_opt.marketdata import (
    ZONES,
    MarketDataCache,
    MarketDataError,
    MarketDataUnavailableError,
    resolve_dataset_source,
)
from pvbess_opt.marketdata import admie as admie_mod
from pvbess_opt.marketdata.admie import (
    BALANCING_HEADER_PATTERNS,
    IMBALANCE_HEADER_PATTERNS,
    assemble_year_local,
    fetch_gr_balancing_year,
    list_daily_files,
    parse_daily_workbook,
    series_columns,
)

GR = ZONES["gr"]
DE_LU = ZONES["de_lu"]

# Human-looking headers that satisfy the provider's regex contract.
_BALANCING_HEADERS: dict[str, str] = {
    "afrr_up_capacity_price_eur_per_mwh": "aFRR Up Capacity Price (EUR/MWh)",
    "afrr_dn_capacity_price_eur_per_mwh": "aFRR Down Capacity Price (EUR/MWh)",
    "mfrr_up_capacity_price_eur_per_mwh": "mFRR Up Capacity Price (EUR/MWh)",
    "mfrr_dn_capacity_price_eur_per_mwh": "mFRR Down Capacity Price (EUR/MWh)",
    "afrr_up_activation_price_eur_per_mwh":
        "aFRR Up Activation Price (EUR/MWh)",
    "afrr_dn_activation_price_eur_per_mwh":
        "aFRR Down Activation Price (EUR/MWh)",
    "mfrr_up_activation_price_eur_per_mwh":
        "mFRR Up Activation Price (EUR/MWh)",
    "mfrr_dn_activation_price_eur_per_mwh":
        "mFRR Down Activation Price (EUR/MWh)",
}


def _daily_xlsx(
    n_periods: int, *, value: float = 10.0,
    headers: dict[str, str] | None = None,
) -> bytes:
    """One synthetic ADMIE daily workbook (period column + price columns)."""
    columns = headers if headers is not None else _BALANCING_HEADERS
    wb = Workbook()
    ws = wb.active
    ws.append(["Delivery Period", *columns.values()])
    for period in range(1, n_periods + 1):
        ws.append([period, *[value] * len(columns)])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Daily workbook parsing
# ---------------------------------------------------------------------------


def test_parses_all_declared_balancing_columns():
    body = _daily_xlsx(48, value=25.0)
    parsed = parse_daily_workbook(
        body, BALANCING_HEADER_PATTERNS, source_name="test.xlsx",
    )
    assert set(parsed) == set(BALANCING_HEADER_PATTERNS)
    for values in parsed.values():
        assert len(values) == 48
        assert (values == 25.0).all()


def test_no_matching_header_is_an_error():
    body = _daily_xlsx(48, headers={"x": "Something Unrelated"})
    with pytest.raises(MarketDataError, match="re-pinning"):
        parse_daily_workbook(
            body, BALANCING_HEADER_PATTERNS, source_name="test.xlsx",
        )


def test_disagreeing_period_counts_rejected():
    wb = Workbook()
    ws = wb.active
    ws.append(["Period", "aFRR Up Capacity Price", "aFRR Down Capacity Price"])
    for period in range(1, 49):
        # The second column stops half-way: unequal period counts.
        row = [period, 10.0] + ([20.0] if period <= 24 else [None])
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    with pytest.raises(MarketDataError, match="disagree on the period"):
        parse_daily_workbook(
            buffer.getvalue(),
            {
                "afrr_up_capacity_price_eur_per_mwh":
                    r"afrr.*up.*capacity.*price",
                "afrr_dn_capacity_price_eur_per_mwh":
                    r"afrr.*down.*capacity.*price",
            },
            source_name="test.xlsx",
        )


def test_imbalance_pattern_matches_single_column():
    body = _daily_xlsx(
        96,
        value=42.0,
        headers={"imb": "Imbalance Price (EUR/MWh)"},
    )
    parsed = parse_daily_workbook(
        body, IMBALANCE_HEADER_PATTERNS, source_name="imb.xlsx",
    )
    assert list(parsed) == ["imbalance_price_eur_per_mwh"]
    assert len(parsed["imbalance_price_eur_per_mwh"]) == 96


# ---------------------------------------------------------------------------
# Year assembly (DST + leap + completeness)
# ---------------------------------------------------------------------------


def _year_daily(
    year: int, *, per_day_value=None, n_periods: int = 48,
) -> list[tuple[date, dict[str, np.ndarray]]]:
    """Synthetic per-day parses for a full local year (30-min cadence)."""
    from calendar import isleap

    spring, fall = admie_mod._dst_transition_dates(year)
    steps_per_hour = n_periods // 24
    days = 366 if isleap(year) else 365
    out: list[tuple[date, dict[str, np.ndarray]]] = []
    for n in range(days):
        day = date(year, 1, 1) + timedelta(days=n)
        count = n_periods
        if day == spring:
            count = n_periods - steps_per_hour   # 23-hour local day
        elif day == fall:
            count = n_periods + steps_per_hour   # 25-hour local day
        value = float(per_day_value(day)) if per_day_value else 10.0
        out.append((
            day,
            {"afrr_up_capacity_price_eur_per_mwh":
                np.full(count, value)},
        ))
    return out


def test_assemble_full_year_normalises_dst_days():
    daily = _year_daily(2025, per_day_value=lambda d: d.timetuple().tm_yday)
    assembled = assemble_year_local(daily, 2025, source_name="test")
    native_minutes, values = assembled[
        "afrr_up_capacity_price_eur_per_mwh"
    ]
    assert native_minutes == 30
    assert len(values) == 365 * 48
    # Every day contributes exactly its nominal 48 half-hours, DST or
    # not — day N's block is the constant N.
    for day_index in (0, 88, 298, 364):  # incl. the 2025 DST days
        block = values[day_index * 48:(day_index + 1) * 48]
        assert (block == float(day_index + 1)).all()


def test_assemble_leap_year_drops_feb_29():
    daily = _year_daily(2024)
    assembled = assemble_year_local(daily, 2024, source_name="test")
    _minutes, values = assembled["afrr_up_capacity_price_eur_per_mwh"]
    assert len(values) == 365 * 48


def test_assemble_missing_day_is_an_error():
    daily = _year_daily(2025)[:-1]
    with pytest.raises(MarketDataError, match="missing"):
        assemble_year_local(daily, 2025, source_name="test")


def test_assemble_duplicate_day_is_an_error():
    daily = _year_daily(2025)
    daily.append(daily[0])
    with pytest.raises(MarketDataError, match="duplicate"):
        assemble_year_local(daily, 2025, source_name="test")


def test_assemble_column_absent_one_day_is_an_error():
    daily = _year_daily(2025)
    daily[100] = (daily[100][0], {})
    daily[100][1]["some_other_column"] = np.full(48, 1.0)
    with pytest.raises(MarketDataError, match="refusing a partial year"):
        assemble_year_local(daily, 2025, source_name="test")


def test_unexpected_day_length_is_an_error():
    daily = _year_daily(2025)
    day, _cols = daily[10]
    daily[10] = (day, {
        "afrr_up_capacity_price_eur_per_mwh": np.full(50, 1.0),
    })
    with pytest.raises(MarketDataError, match="DST variants"):
        assemble_year_local(daily, 2025, source_name="test")


# ---------------------------------------------------------------------------
# File listing + fetch/cache
# ---------------------------------------------------------------------------


def test_list_daily_files_parses_json_listing(monkeypatch):
    listing = [
        {"file_path": "https://x/20250601_f.xlsx"},
        {"file_url": "https://x/20250602_f.xlsx"},
        "https://x/20250603_f.xlsx",
    ]

    def fake_get(url, params, timeout):
        assert params["FileCategory"] == "SomeCategory"
        return 200, json.dumps(listing).encode()

    monkeypatch.setattr(admie_mod, "_http_get", fake_get)
    urls = list_daily_files(
        "SomeCategory", date(2025, 6, 1), date(2025, 6, 3),
    )
    assert len(urls) == 3


def test_list_daily_files_empty_listing_names_the_category(monkeypatch):
    monkeypatch.setattr(
        admie_mod, "_http_get", lambda _url, _params, _timeout: (200, b"[]"),
    )
    with pytest.raises(MarketDataError, match="re-pinning"):
        list_daily_files("Nope", date(2025, 6, 1), date(2025, 6, 2))


def _install_year_fetch(monkeypatch, year: int = 2025):
    """Fake the listing + every daily download for one year."""
    from calendar import isleap

    spring, fall = admie_mod._dst_transition_dates(year)
    days = 366 if isleap(year) else 365
    all_days = [date(year, 1, 1) + timedelta(days=n) for n in range(days)]
    listing = [
        {"file_path": f"https://x/{d.strftime('%Y%m%d')}_isp.xlsx"}
        for d in all_days
    ]
    normal = _daily_xlsx(48)
    spring_body = _daily_xlsx(46)
    fall_body = _daily_xlsx(50)
    counter = {"n": 0}

    def fake_get(url, params, timeout):
        counter["n"] += 1
        if params is not None:  # the listing request
            return 200, json.dumps(listing).encode()
        stamp = url.rsplit("/", 1)[-1][:8]
        day = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
        if day == spring:
            return 200, spring_body
        if day == fall:
            return 200, fall_body
        return 200, normal

    monkeypatch.setattr(admie_mod, "_http_get", fake_get)
    return counter


def test_fetch_gr_balancing_year_assembles_and_caches(monkeypatch, tmp_path):
    counter = _install_year_fetch(monkeypatch)
    cache = MarketDataCache(tmp_path / "cache")
    series = fetch_gr_balancing_year(2025, cache=cache)
    columns = series_columns(series)
    assert set(columns) == set(BALANCING_HEADER_PATTERNS)
    native_minutes, values = columns[
        "mfrr_dn_activation_price_eur_per_mwh"
    ]
    assert native_minutes == 30
    assert len(values) == 365 * 48
    first_round_trips = counter["n"]
    assert first_round_trips == 1 + 365  # one listing + every daily file

    # Second call: served from the on-disk cache, zero network.
    series2 = fetch_gr_balancing_year(2025, cache=cache)
    assert counter["n"] == first_round_trips
    assert series2.metadata["cache_state"] == "cache hit"
    assert set(series_columns(series2)) == set(BALANCING_HEADER_PATTERNS)


def test_fetch_offline_cache_miss_names_the_key(tmp_path):
    cache = MarketDataCache(tmp_path / "cache")
    with pytest.raises(MarketDataError, match="offline"):
        fetch_gr_balancing_year(2025, cache=cache, fetch_mode="offline")


# ---------------------------------------------------------------------------
# Per-(zone, dataset) source registry
# ---------------------------------------------------------------------------


def test_auto_routes_gr_to_admie_and_others_to_entsoe():
    assert resolve_dataset_source("balancing", "auto", GR) == "admie"
    assert resolve_dataset_source("imbalance", "auto", GR) == "admie"
    assert resolve_dataset_source("balancing", "auto", DE_LU) == "entsoe"
    assert resolve_dataset_source("imbalance", "auto", DE_LU) == "entsoe"


def test_file_source_resolves_to_none():
    assert resolve_dataset_source("balancing", "file", GR) is None


def test_explicit_admie_outside_greece_is_an_error():
    with pytest.raises(MarketDataUnavailableError, match="Greek TSO"):
        resolve_dataset_source("balancing", "admie", DE_LU)


def test_explicit_entsoe_for_greece_is_an_error():
    with pytest.raises(MarketDataUnavailableError, match=r"ADMIE|admie|auto"):
        resolve_dataset_source("balancing", "entsoe", GR)


def test_unknown_source_token_is_an_error():
    with pytest.raises(MarketDataError, match="not one of"):
        resolve_dataset_source("balancing", "vnb", GR)


def test_nominal_day_spring_fill_is_flat_last_value():
    """Sub-hourly spring-forward fill repeats the LAST step (flat),
    matching base.sample_local_year (rule 3) — not the whole preceding
    hour block (the two differ only at sub-hourly cadence).
    """
    sph = 2  # 30-min cadence
    nominal = 24 * sph
    # 23-hour local day, unique value per step so block-repeat and
    # flat-fill are distinguishable.
    raw = np.arange(nominal - sph, dtype=float)
    filled = admie_mod._nominal_day(raw, sph, kind="spring", source_name="t")
    assert len(filled) == nominal
    at = admie_mod._DST_HOUR * sph
    last_before = raw[at - 1]
    # The inserted hour must be a flat repeat of the single prior step.
    assert (filled[at:at + sph] == last_before).all()
    # Surrounding data untouched.
    assert (filled[:at] == raw[:at]).all()
    assert (filled[at + sph:] == raw[at:]).all()
