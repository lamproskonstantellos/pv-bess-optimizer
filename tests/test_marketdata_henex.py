"""HEnEx cross-check: workbook parsing, version fallback, divergence stats."""

from __future__ import annotations

import io
from datetime import date

import numpy as np
import pytest
from openpyxl import Workbook

from pvbess_opt.marketdata import MarketDataError
from pvbess_opt.marketdata import henex as henex_mod
from pvbess_opt.marketdata.henex import crosscheck_gr_dam, fetch_henex_dam_day


def _results_xlsx(prices: list[float]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["Delivery Period", "Market Clearing Price (EUR/MWh)"])
    for period, price in enumerate(prices, start=1):
        ws.append([period, price])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def test_fetch_tries_version_suffixes_in_order(monkeypatch):
    seen: list[str] = []

    def fake_get(url, timeout):
        seen.append(url)
        if url.endswith("_v02.xlsx"):
            return 200, _results_xlsx([50.0] * 24)
        return 404, b""

    monkeypatch.setattr(henex_mod, "_http_get", fake_get)
    prices = fetch_henex_dam_day(date(2025, 6, 1))
    assert len(prices) == 24
    assert (prices == 50.0).all()
    assert seen[0].endswith("_v01.xlsx") and seen[1].endswith("_v02.xlsx")


def test_fetch_no_version_answers_is_an_error(monkeypatch):
    monkeypatch.setattr(
        henex_mod, "_http_get", lambda _url, _timeout: (404, b""),
    )
    with pytest.raises(MarketDataError, match="no HEnEx"):
        fetch_henex_dam_day(date(2025, 6, 1))


def test_parse_rejects_layout_without_price_header(monkeypatch):
    wb = Workbook()
    wb.active.append(["Something", "Else"])
    buffer = io.BytesIO()
    wb.save(buffer)
    monkeypatch.setattr(
        henex_mod, "_http_get",
        lambda _url, _timeout: (200, buffer.getvalue()),
    )
    with pytest.raises(MarketDataError, match="re-pin"):
        fetch_henex_dam_day(date(2025, 6, 1))


def test_crosscheck_agreement_is_silent(caplog):
    stats = crosscheck_gr_dam(
        np.full(24, 80.0), np.full(24, 80.0),
    )
    assert stats["max_abs_diff"] == 0.0
    assert not [r for r in caplog.records if "divergence" in r.message]


def test_crosscheck_divergence_warns(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        stats = crosscheck_gr_dam(
            np.full(24, 80.0), np.full(24, 81.0),
        )
    assert stats["max_abs_diff"] == pytest.approx(1.0)
    assert any("divergence" in r.message for r in caplog.records)


def test_crosscheck_step_holds_coarser_side():
    # Hourly ENTSO-E vs quarter-hourly HEnEx: the hourly side step-holds
    # (intensive rule) so identical levels compare equal.
    hourly = np.arange(24, dtype=float)
    quarters = np.repeat(hourly, 4)
    stats = crosscheck_gr_dam(hourly, quarters)
    assert stats["max_abs_diff"] == 0.0
    assert stats["n_periods"] == 96.0


def test_crosscheck_incommensurable_counts_rejected():
    with pytest.raises(MarketDataError, match="incommensurable"):
        crosscheck_gr_dam(np.full(24, 1.0), np.full(36, 1.0))
