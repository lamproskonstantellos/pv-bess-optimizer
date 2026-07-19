"""Shared synthetic-fixture builders for the market-data io tests.

The builders follow the providers' declared (provisional) contracts:
ADMIE daily workbooks satisfy ``BALANCING_HEADER_PATTERNS`` /
``IMBALANCE_HEADER_PATTERNS``; the ENTSO-E balancing responder emits
``Balancing_MarketDocument`` XML for the A81/A84/A85 queries.
"""

from __future__ import annotations

import io
import json
from datetime import date, timedelta

from openpyxl import Workbook

from pvbess_opt.marketdata import admie as admie_mod

_BAL_NS = "urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:1"

ADMIE_BALANCING_HEADERS: dict[str, str] = {
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

ADMIE_IMBALANCE_HEADERS: dict[str, str] = {
    "imb": "Imbalance Price (EUR/MWh)",
}


def daily_xlsx(
    n_periods: int, *, value: float = 10.0,
    headers: dict[str, str] | None = None,
) -> bytes:
    """One synthetic ADMIE daily workbook (period column + price columns)."""
    columns = headers if headers is not None else ADMIE_BALANCING_HEADERS
    wb = Workbook()
    ws = wb.active
    ws.append(["Delivery Period", *columns.values()])
    for period in range(1, n_periods + 1):
        ws.append([period, *[value] * len(columns)])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def install_admie_year_fetch(
    monkeypatch, year: int = 2025, *,
    value: float = 10.0,
    headers: dict[str, str] | None = None,
    n_periods: int = 48,
):
    """Fake the ADMIE listing + every daily download for one local year."""
    from calendar import isleap

    spring, fall = admie_mod._dst_transition_dates(year)
    steps_per_hour = n_periods // 24
    days = 366 if isleap(year) else 365
    all_days = [date(year, 1, 1) + timedelta(days=n) for n in range(days)]
    listing = [
        {"file_path": f"https://x/{d.strftime('%Y%m%d')}_f.xlsx"}
        for d in all_days
    ]
    normal = daily_xlsx(n_periods, value=value, headers=headers)
    spring_body = daily_xlsx(
        n_periods - steps_per_hour, value=value, headers=headers,
    )
    fall_body = daily_xlsx(
        n_periods + steps_per_hour, value=value, headers=headers,
    )
    counter = {"n": 0}

    def fake_get(url, params, timeout):
        counter["n"] += 1
        if params is not None:
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


def balancing_xml(series: list[dict]) -> bytes:
    """Build a Balancing_MarketDocument from per-TimeSeries specs."""
    parts = [
        f'<?xml version="1.0"?><Balancing_MarketDocument xmlns="{_BAL_NS}">'
    ]
    for spec in series:
        field = spec.get("field", "procurement_Price.amount")
        points = "".join(
            f"<Point><position>{pos}</position>"
            f"<{field}>{price}</{field}></Point>"
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


def balancing_year_responder(params):
    """Serve a padded full-year answer for every A81/A84/A85 request.

    The window covers every shipped zone's padded local year (the
    earliest fetch start among the zone registry is UTC+3 summer time).
    """
    window = {"start": "2024-12-31T20:00Z", "end": "2026-01-01T02:00Z"}
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
        return 200, balancing_xml(product_series)
    if doc_type == "A84":
        return 200, balancing_xml([
            {
                "businessType": business, "direction": direction,
                **window, "prices": {1: 90.0},
                "field": "activation_Price.amount",
            }
            for business in ("A96", "A97")
            for direction in ("A01", "A02")
        ])
    if doc_type == "A85":
        return 200, balancing_xml([
            {
                "category": category, **window, "prices": {1: 30.0},
                "field": "imbalance_Price.amount",
            }
            for category in ("A04", "A05")
        ])
    raise AssertionError(f"unexpected documentType {doc_type}")
