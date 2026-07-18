"""ENTSO-E day-ahead provider: parsing, envelopes, errors, cache, modes.

No live network — the HTTP call is mocked throughout (synthetic IEC
62325 CIM documents; refresh from a real recording via
``scripts/probe_market_data.py --save-dir``).
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime

import pytest

from pvbess_opt.marketdata import (
    ZONES,
    MarketDataCache,
    MarketDataError,
    MarketSeries,
    PriceSegment,
)
from pvbess_opt.marketdata import entsoe as entsoe_mod
from pvbess_opt.marketdata.entsoe import (
    EntsoeNoDataError,
    fetch_day_ahead_year,
    parse_publication_document,
)

GR = ZONES["gr"]
_CIM_NS = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"
_TOKEN = "1234567890abcdefTOKEN"


def _publication_xml(
    periods: list[tuple[str, str, str, dict[int, float]]],
) -> bytes:
    """Build a Publication_MarketDocument from (start, end, res, points)."""
    parts = [f'<?xml version="1.0"?><Publication_MarketDocument xmlns="{_CIM_NS}">']
    for start, end, resolution, points in periods:
        point_xml = "".join(
            f"<Point><position>{pos}</position>"
            f"<price.amount>{price}</price.amount></Point>"
            for pos, price in sorted(points.items())
        )
        parts.append(
            "<TimeSeries>"
            "<currency_Unit.name>EUR</currency_Unit.name>"
            "<price_Measure_Unit.name>MWH</price_Measure_Unit.name>"
            "<curveType>A03</curveType>"
            f"<Period><timeInterval><start>{start}</start>"
            f"<end>{end}</end></timeInterval>"
            f"<resolution>{resolution}</resolution>{point_xml}</Period>"
            "</TimeSeries>"
        )
    parts.append("</Publication_MarketDocument>")
    return "".join(parts).encode()


def _ack_xml(text: str) -> bytes:
    return (
        f'<?xml version="1.0"?><Acknowledgement_MarketDocument '
        f'xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:'
        f'8:1"><Reason><code>999</code><text>{text}</text></Reason>'
        f"</Acknowledgement_MarketDocument>"
    ).encode()


def _year_window_xml(prices_value: float = 150.0) -> bytes:
    """One PT60M document covering the padded GR 2025 fetch window."""
    return _publication_xml([(
        "2024-12-31T21:00Z", "2026-01-01T00:00Z", "PT60M",
        {1: prices_value},
    )])


def _install_fake_get(monkeypatch, responder, counter=None):
    def fake_get(params, timeout):
        assert timeout is not None
        if counter is not None:
            counter["n"] += 1
        return responder(params)

    monkeypatch.setattr(entsoe_mod, "_http_get", fake_get)


# ---------------------------------------------------------------------------
# Document parsing
# ---------------------------------------------------------------------------


def test_parses_hourly_document():
    body = _publication_xml([(
        "2025-03-15T00:00Z", "2025-03-16T00:00Z", "PT60M",
        {i: float(i * 10) for i in range(1, 25)},
    )])
    segments = parse_publication_document(body)
    assert len(segments) == 1
    seg = segments[0]
    assert seg.resolution_minutes == 60
    assert seg.start_utc == datetime(2025, 3, 15, tzinfo=UTC)
    assert seg.values[:3] == [10.0, 20.0, 30.0]
    assert len(seg.values) == 24


def test_parses_quarter_hour_document():
    body = _publication_xml([(
        "2025-11-15T00:00Z", "2025-11-15T01:00Z", "PT15M",
        {1: 5.0, 2: 6.0, 3: 7.0, 4: 8.0},
    )])
    seg = parse_publication_document(body)[0]
    assert seg.resolution_minutes == 15
    assert seg.values == [5.0, 6.0, 7.0, 8.0]


def test_a03_curve_gaps_forward_fill():
    # Positions 2 and 3 omitted: the price repeats position 1.
    body = _publication_xml([(
        "2025-03-15T00:00Z", "2025-03-15T04:00Z", "PT60M",
        {1: 42.0, 4: 99.0},
    )])
    seg = parse_publication_document(body)[0]
    assert seg.values == [42.0, 42.0, 42.0, 99.0]


def test_acknowledgement_raises_no_data():
    with pytest.raises(EntsoeNoDataError, match="No matching data"):
        parse_publication_document(_ack_xml("No matching data found"))


def test_unexpected_document_type_rejected():
    body = b'<?xml version="1.0"?><GL_MarketDocument xmlns="x"/>'
    with pytest.raises(MarketDataError, match="unexpected ENTSO-E document"):
        parse_publication_document(body)


def test_non_eur_currency_rejected():
    body = _publication_xml([(
        "2025-03-15T00:00Z", "2025-03-15T01:00Z", "PT60M", {1: 1.0},
    )]).replace(b"EUR", b"GBP")
    with pytest.raises(MarketDataError, match="GBP"):
        parse_publication_document(body)


# ---------------------------------------------------------------------------
# Fetch: envelopes, errors, bisection
# ---------------------------------------------------------------------------


def _fetch(monkeypatch, tmp_path, responder, *, mode="cache_first",
           counter=None) -> MarketSeries:
    _install_fake_get(monkeypatch, responder, counter)
    return fetch_day_ahead_year(
        GR, 2025,
        token_resolver=lambda: _TOKEN,
        cache=MarketDataCache(tmp_path / "cache"),
        fetch_mode=mode,
    )


def test_fetch_plain_xml_document(monkeypatch, tmp_path):
    series = _fetch(
        monkeypatch, tmp_path, lambda _params: (200, _year_window_xml()),
    )
    assert series.metadata["zone"] == "GR"
    assert series.metadata["cache_state"] == "live fetch"
    assert len(series.segments) == 1
    assert series.segments[0].values[0] == 150.0


def test_fetch_zip_envelope(monkeypatch, tmp_path):
    half1 = _publication_xml([(
        "2024-12-31T21:00Z", "2025-07-01T00:00Z", "PT60M", {1: 100.0},
    )])
    half2 = _publication_xml([(
        "2025-07-01T00:00Z", "2026-01-01T00:00Z", "PT60M", {1: 200.0},
    )])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("doc1.xml", half1)
        zf.writestr("doc2.xml", half2)
    series = _fetch(
        monkeypatch, tmp_path,
        lambda _params: (200, buffer.getvalue()),
    )
    assert len(series.segments) == 2


def test_fetch_sends_expected_query(monkeypatch, tmp_path):
    seen: dict[str, str] = {}

    def responder(params):
        seen.update(params)
        return 200, _year_window_xml()

    _fetch(monkeypatch, tmp_path, responder)
    assert seen["documentType"] == "A44"
    assert seen["in_Domain"] == GR.eic
    assert seen["out_Domain"] == GR.eic
    assert seen["contract_MarketAgreement.type"] == "A01"
    assert seen["securityToken"] == _TOKEN
    # Padded UTC window around the Athens local year: local 2025-01-01
    # 00:00 EET = 2024-12-31 22:00Z and local 2026-01-01 00:00 EET =
    # 2025-12-31 22:00Z, each padded by one hour.
    assert seen["periodStart"] == "202412312100"
    assert seen["periodEnd"] == "202512312300"


def test_too_much_data_bisects(monkeypatch, tmp_path):
    counter = {"n": 0}

    def responder(params):
        start = datetime.strptime(
            params["periodStart"], "%Y%m%d%H%M",
        ).replace(tzinfo=UTC)
        end = datetime.strptime(
            params["periodEnd"], "%Y%m%d%H%M",
        ).replace(tzinfo=UTC)
        if (end - start).days > 190:
            return 200, _ack_xml(
                "amount of requested data exceeds allowed limit",
            )
        hours = int((end - start).total_seconds() // 3600)
        return 200, _publication_xml([(
            start.strftime("%Y-%m-%dT%H:%MZ"),
            end.strftime("%Y-%m-%dT%H:%MZ"),
            "PT60M",
            {i: 75.0 for i in (1, hours)},
        )])

    series = _fetch(monkeypatch, tmp_path, responder, counter=counter)
    assert counter["n"] >= 3  # the oversized window plus its halves
    total_hours = sum(len(seg.values) for seg in series.segments)
    assert total_hours == 8762  # padded Athens local year


def test_invalid_token_masked_in_error(monkeypatch, tmp_path):
    with pytest.raises(MarketDataError) as err:
        _fetch(monkeypatch, tmp_path, lambda _params: (401, b""))
    message = str(err.value)
    assert _TOKEN not in message
    assert _TOKEN[:8] in message


def test_rate_limit_hint(monkeypatch, tmp_path):
    with pytest.raises(MarketDataError, match="10-minute ban"):
        _fetch(monkeypatch, tmp_path, lambda _params: (429, b""))


def test_empty_year_is_hard_error(monkeypatch, tmp_path):
    with pytest.raises(MarketDataError, match="no day-ahead data"):
        _fetch(
            monkeypatch, tmp_path,
            lambda _params: (200, _ack_xml("No matching data found")),
        )


# ---------------------------------------------------------------------------
# Cache + fetch modes
# ---------------------------------------------------------------------------


def test_cache_hit_avoids_second_fetch(monkeypatch, tmp_path):
    counter = {"n": 0}
    _fetch(
        monkeypatch, tmp_path,
        lambda _params: (200, _year_window_xml()), counter=counter,
    )
    series = _fetch(
        monkeypatch, tmp_path,
        lambda _params: (200, _year_window_xml()), counter=counter,
    )
    assert counter["n"] == 1
    assert series.metadata["cache_state"] == "cache hit"


def test_offline_serves_cache_without_token(monkeypatch, tmp_path):
    cache = MarketDataCache(tmp_path / "cache")
    key = MarketDataCache.key("dam-a44", "GR", 2025)
    cache.save(key, MarketSeries(
        segments=[PriceSegment(
            datetime(2024, 12, 31, 22, tzinfo=UTC), 60, [1.0] * 8760,
        )],
        metadata={"fetched_at": "2026-07-17T00:00:00+00:00"},
    ))

    def no_network(params, timeout):
        raise AssertionError("offline mode must never hit the network")

    monkeypatch.setattr(entsoe_mod, "_http_get", no_network)

    def no_token() -> str:
        raise AssertionError("offline cache hit must not need a token")

    series = fetch_day_ahead_year(
        GR, 2025, token_resolver=no_token, cache=cache,
        fetch_mode="offline",
    )
    assert series.metadata["cache_state"] == "cache hit"


def test_offline_cache_miss_names_the_missing_file(monkeypatch, tmp_path):
    cache = MarketDataCache(tmp_path / "cache")
    with pytest.raises(MarketDataError) as err:
        fetch_day_ahead_year(
            GR, 2025, token_resolver=lambda: _TOKEN, cache=cache,
            fetch_mode="offline",
        )
    message = str(err.value)
    assert "offline" in message
    assert MarketDataCache.key("dam-a44", "GR", 2025) in message


def test_refresh_overwrites_cache(monkeypatch, tmp_path):
    counter = {"n": 0}
    _fetch(
        monkeypatch, tmp_path,
        lambda _params: (200, _year_window_xml(100.0)), counter=counter,
    )
    series = _fetch(
        monkeypatch, tmp_path,
        lambda _params: (200, _year_window_xml(999.0)),
        mode="refresh", counter=counter,
    )
    assert counter["n"] == 2
    assert series.segments[0].values[0] == 999.0
    # And the refreshed values are what cache_first now serves.
    cached = _fetch(
        monkeypatch, tmp_path,
        lambda _params: (200, _year_window_xml(0.0)), counter=counter,
    )
    assert cached.segments[0].values[0] == 999.0


# ---------------------------------------------------------------------------
# Balancing documents (A81 / A84 / A85)
# ---------------------------------------------------------------------------

from pvbess_opt.marketdata.entsoe import (  # noqa: E402
    ENTSOE_BALANCING_COLUMNS,
    column_segments,
    fetch_balancing_prices_year,
    fetch_imbalance_prices_year,
    parse_balancing_document,
)

_BAL_NS = "urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:1"


def _balancing_xml(
    series: list[dict],
) -> bytes:
    """Build a Balancing_MarketDocument from per-TimeSeries specs.

    Each spec: {businessType, direction, category, unit, start, end,
    resolution, prices: dict pos→value, field}.
    """
    parts = [
        f'<?xml version="1.0"?><Balancing_MarketDocument xmlns="{_BAL_NS}">'
    ]
    for spec in series:
        points = "".join(
            f"<Point><position>{pos}</position>"
            f"<{spec.get('field', 'procurement_Price.amount')}>{price}"
            f"</{spec.get('field', 'procurement_Price.amount')}></Point>"
            for pos, price in sorted(spec["prices"].items())
        )
        direction = (
            f"<flowDirection.direction>{spec['direction']}"
            "</flowDirection.direction>"
            if spec.get("direction") else ""
        )
        business = (
            f"<businessType>{spec['businessType']}</businessType>"
            if spec.get("businessType") else ""
        )
        category = (
            f"<imbalance_Price.category>{spec['category']}"
            "</imbalance_Price.category>"
            if spec.get("category") else ""
        )
        parts.append(
            "<TimeSeries>"
            f"{business}{direction}{category}"
            "<currency_Unit.name>EUR</currency_Unit.name>"
            f"<price_Measure_Unit.name>{spec.get('unit', 'MWH')}"
            "</price_Measure_Unit.name>"
            f"<Period><timeInterval><start>{spec['start']}</start>"
            f"<end>{spec['end']}</end></timeInterval>"
            f"<resolution>{spec.get('resolution', 'PT60M')}</resolution>"
            f"{points}</Period></TimeSeries>"
        )
    parts.append("</Balancing_MarketDocument>")
    return "".join(parts).encode()


def test_parses_balancing_document_with_tags():
    body = _balancing_xml([{
        "businessType": "B95", "direction": "A01",
        "start": "2025-03-15T00:00Z", "end": "2025-03-15T04:00Z",
        "prices": {1: 12.0, 3: 15.0},
    }])
    [(tags, seg)] = parse_balancing_document(body)
    assert tags["businessType"] == "B95"
    assert tags["direction"] == "A01"
    assert seg.values == [12.0, 12.0, 15.0, 15.0]


def test_mw_unit_normalises_to_per_mwh_basis():
    # 4-hour blocks priced 100 EUR/MW per block -> 25 EUR/MW/h.
    body = _balancing_xml([{
        "businessType": "B95",
        "start": "2025-03-15T00:00Z", "end": "2025-03-16T00:00Z",
        "resolution": "P1D", "unit": "MAW", "prices": {1: 240.0},
    }])
    [(_tags, seg)] = parse_balancing_document(body)
    assert seg.values == [10.0]  # 240 EUR/MW per 24 h day == 10 EUR/MW/h


def test_mwh_unit_passes_through():
    body = _balancing_xml([{
        "businessType": "B95",
        "start": "2025-03-15T00:00Z", "end": "2025-03-15T01:00Z",
        "unit": "MWH", "prices": {1: 55.0},
    }])
    [(_tags, seg)] = parse_balancing_document(body)
    assert seg.values == [55.0]


def test_balancing_ack_raises_no_data():
    with pytest.raises(EntsoeNoDataError):
        parse_balancing_document(_ack_xml("No matching data found"))


def test_wrong_document_root_rejected_for_balancing():
    body = _publication_xml([(
        "2025-03-15T00:00Z", "2025-03-15T01:00Z", "PT60M", {1: 1.0},
    )])
    with pytest.raises(MarketDataError, match="Balancing_MarketDocument"):
        parse_balancing_document(body)


def _year_balancing_responder(params):
    """Serve a full padded GR-style year for every balancing request."""
    window = {
        "start": "2024-12-31T21:00Z", "end": "2026-01-01T00:00Z",
    }
    doc_type = params["documentType"]
    if doc_type == "A81":
        product_series = []
        if params["processType"] == "A52":
            product_series.append({
                "businessType": "B95", **window, "prices": {1: 5.0},
            })
        else:
            for direction in ("A01", "A02"):
                product_series.append({
                    "businessType": "B95", "direction": direction,
                    **window, "prices": {1: 7.0},
                })
        return 200, _balancing_xml(product_series)
    if doc_type == "A84":
        return 200, _balancing_xml([
            {
                "businessType": business, "direction": direction,
                **window, "prices": {1: 90.0},
                "field": "activation_Price.amount",
            }
            for business in ("A96", "A97")
            for direction in ("A01", "A02")
        ])
    if doc_type == "A85":
        return 200, _balancing_xml([
            {
                "category": category, **window, "prices": {1: 30.0},
                "field": "imbalance_Price.amount",
            }
            for category in ("A04", "A05")
        ])
    raise AssertionError(f"unexpected documentType {doc_type}")


def test_fetch_balancing_prices_year_covers_all_nine_columns(
    monkeypatch, tmp_path,
):
    counter = {"n": 0}
    _install_fake_get(monkeypatch, _year_balancing_responder, counter)
    series = fetch_balancing_prices_year(
        ZONES["de_lu"], 2025,
        token_resolver=lambda: _TOKEN,
        cache=MarketDataCache(tmp_path / "cache"),
    )
    columns = column_segments(series)
    assert set(columns) == set(ENTSOE_BALANCING_COLUMNS)
    assert counter["n"] == 4  # three A81 products + one A84 request
    # And the packed columns_map survives the cache round-trip.
    series2 = fetch_balancing_prices_year(
        ZONES["de_lu"], 2025,
        token_resolver=lambda: _TOKEN,
        cache=MarketDataCache(tmp_path / "cache"),
    )
    assert counter["n"] == 4
    assert set(column_segments(series2)) == set(ENTSOE_BALANCING_COLUMNS)


def test_fetch_imbalance_prices_year_maps_categories(monkeypatch, tmp_path):
    _install_fake_get(monkeypatch, _year_balancing_responder)
    series = fetch_imbalance_prices_year(
        ZONES["de_lu"], 2025,
        token_resolver=lambda: _TOKEN,
        cache=MarketDataCache(tmp_path / "cache"),
    )
    columns = column_segments(series)
    assert set(columns) == {
        "imbalance_price_long_eur_per_mwh",
        "imbalance_price_short_eur_per_mwh",
    }
