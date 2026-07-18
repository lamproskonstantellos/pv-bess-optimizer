"""ENTSO-E Transparency Platform provider (day-ahead prices, 12.1.D/A44).

Thin ``requests``-based client for the REST endpoint
(``https://web-api.tp.entsoe.eu/api``).  A deliberate design decision
over wrapping the ``entsoe-py`` PyPI client: the layer needs exact
control of pagination, the ZIP-of-XML envelope, the
``Acknowledgement_MarketDocument`` no-data marker and token masking, it
must be fully testable offline against recorded/synthetic documents, and
the repo's dependency policy keeps the base requirements minimal
(``requests`` is already a base dependency; ``entsoe-py`` would add
pandas-version coupling for functionality — the 15-minute MTU stitch —
that the calendar engine in :mod:`pvbess_opt.marketdata.base` owns
anyway.)

API facts encoded here (verified 07/2026):

* query parameter ``securityToken``; ~400 requests/min per token, HTTP
  429 plus a 10-minute ban beyond;
* 1-year maximum range per request; multi-document answers arrive as a
  ZIP of XMLs; an ``Acknowledgement_MarketDocument`` means "no data" —
  or "too much data" (reason 999 with an *amount of requested data*
  text), which this client handles by bisecting the window;
* timestamps are UTC in ``yyyyMMddHHmm`` form;
* day-ahead prices (``documentType=A44`` +
  ``contract_MarketAgreement.type=A01``) publish PT60M market time
  units for delivery days before 2025-10-01 and PT15M after (the SDAC
  15-minute MTU go-live; Ireland stays PT30M) — one response may mix
  resolutions, which the calendar engine stitches.
"""

from __future__ import annotations

import io
import logging
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from .base import (
    MarketDataCache,
    MarketDataError,
    MarketSeries,
    PriceSegment,
    Zone,
    mask_token,
    utcnow_isoformat,
)

logger = logging.getLogger(__name__)

ENTSOE_API_URL = "https://web-api.tp.entsoe.eu/api"

#: ISO-8601 durations the day-ahead documents actually use.
_RESOLUTION_MINUTES: dict[str, int] = {
    "PT15M": 15,
    "PT30M": 30,
    "PT60M": 60,
    "P1D": 24 * 60,
}

_REQUEST_TIMEOUT_S = 60.0


class EntsoeNoDataError(MarketDataError):
    """The platform acknowledged the query with a no-data reason."""


def _http_get(params: dict[str, str], timeout: float) -> tuple[int, bytes]:
    """One GET against the API; tests monkeypatch this symbol."""
    import requests

    resp = requests.get(ENTSOE_API_URL, params=params, timeout=timeout)
    return resp.status_code, resp.content


def _fmt_period(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%d%H%M")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    for el in element.iter():
        if _local_name(el.tag) == name:
            return (el.text or "").strip()
    return None


def _parse_utc(stamp: str) -> datetime:
    """Parse a CIM interval instant (``2025-09-30T22:00Z`` family)."""
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(
        UTC,
    )


def _ack_reason(root: ElementTree.Element) -> str:
    texts = [
        (el.text or "").strip()
        for el in root.iter()
        if _local_name(el.tag) == "text"
    ]
    return "; ".join(t for t in texts if t)


def parse_publication_document(body: bytes) -> list[PriceSegment]:
    """Parse one ``Publication_MarketDocument`` into price segments.

    Namespace-agnostic on purpose: the platform has shipped several CIM
    namespace versions over the years and the structure is stable.  Each
    ``Period`` becomes one segment; ``curveType`` A03 point gaps (a
    position is omitted when its price repeats the previous one) are
    expanded by forward-fill within the period.
    """
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise MarketDataError(
            f"unparseable ENTSO-E response ({exc}); first bytes: "
            f"{body[:80]!r}."
        ) from exc
    root_name = _local_name(root.tag)
    if root_name == "Acknowledgement_MarketDocument":
        raise EntsoeNoDataError(
            _ack_reason(root) or "acknowledged with no reason text"
        )
    if root_name != "Publication_MarketDocument":
        raise MarketDataError(
            f"unexpected ENTSO-E document {root_name!r} (expected "
            "Publication_MarketDocument)."
        )
    segments: list[PriceSegment] = []
    for ts_el in (el for el in root.iter() if _local_name(el.tag) == "TimeSeries"):
        currency = _child_text(ts_el, "currency_Unit.name") or "EUR"
        unit = _child_text(ts_el, "price_Measure_Unit.name") or "MWH"
        if currency.upper() != "EUR" or unit.upper() != "MWH":
            raise MarketDataError(
                f"ENTSO-E TimeSeries priced in {currency}/{unit}; only "
                "EUR/MWH day-ahead series are supported."
            )
        for period in (
            el for el in ts_el.iter() if _local_name(el.tag) == "Period"
        ):
            start_text = _child_text(period, "start")
            end_text = _child_text(period, "end")
            res_text = _child_text(period, "resolution") or ""
            if start_text is None or end_text is None:
                raise MarketDataError(
                    "ENTSO-E Period without a timeInterval start/end."
                )
            minutes = _RESOLUTION_MINUTES.get(res_text)
            if minutes is None:
                raise MarketDataError(
                    f"unsupported ENTSO-E resolution {res_text!r} "
                    f"(supported: {', '.join(_RESOLUTION_MINUTES)})."
                )
            start = _parse_utc(start_text)
            end = _parse_utc(end_text)
            n_steps = int((end - start).total_seconds() // 60) // minutes
            if n_steps <= 0:
                raise MarketDataError(
                    f"ENTSO-E Period with a non-positive span "
                    f"({start_text} → {end_text})."
                )
            by_position: dict[int, float] = {}
            for point in (
                el for el in period.iter() if _local_name(el.tag) == "Point"
            ):
                pos_text = _child_text(point, "position")
                price_text = _child_text(point, "price.amount")
                if pos_text is None or price_text is None:
                    raise MarketDataError(
                        "ENTSO-E Point without position/price.amount."
                    )
                by_position[int(pos_text)] = float(price_text)
            if 1 not in by_position:
                raise MarketDataError(
                    f"ENTSO-E Period starting {start_text} has no "
                    "position-1 point; cannot expand the A03 curve."
                )
            values: list[float] = []
            last = by_position[1]
            for pos in range(1, n_steps + 1):
                last = by_position.get(pos, last)
                values.append(last)
            segments.append(
                PriceSegment(
                    start_utc=start,
                    resolution_minutes=minutes,
                    values=values,
                )
            )
    if not segments:
        raise EntsoeNoDataError("document carried no TimeSeries periods")
    return segments


def _bodies_from_response(body: bytes) -> list[bytes]:
    """A response is one XML document or a ZIP of XML documents."""
    if body[:2] != b"PK":
        return [body]
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        return [zf.read(name) for name in zf.namelist()]


def _is_too_much_data(reason: str) -> bool:
    return "amount of requested data" in reason.lower()


def _fetch_window(
    base_params: dict[str, str],
    start: datetime,
    end: datetime,
    *,
    token: str,
    timeout: float,
    depth: int = 0,
) -> list[PriceSegment]:
    """Fetch one window, bisecting when the platform reports too much data."""
    status, body = _http_get(
        {
            "securityToken": token,
            **base_params,
            "periodStart": _fmt_period(start),
            "periodEnd": _fmt_period(end),
        },
        timeout,
    )
    if status in (401, 403):
        raise MarketDataError(
            f"ENTSO-E rejected the API token ({mask_token(token)}, HTTP "
            f"{status}); check the token or request a new one at "
            "https://transparency.entsoe.eu."
        )
    if status == 429:
        raise MarketDataError(
            "ENTSO-E rate limit hit (HTTP 429; ~400 requests/min per "
            "token, exceeding it triggers a 10-minute ban). Retry later "
            "or reuse the on-disk cache."
        )
    if status != 200:
        raise MarketDataError(
            f"ENTSO-E request failed with HTTP {status}; response starts "
            f"{body[:120]!r}."
        )
    segments: list[PriceSegment] = []
    for document in _bodies_from_response(body):
        try:
            segments.extend(parse_publication_document(document))
        except EntsoeNoDataError as exc:
            if _is_too_much_data(str(exc)):
                if depth >= 6:
                    raise MarketDataError(
                        "ENTSO-E keeps reporting too much data below a "
                        f"{(end - start).days}-day window; giving up."
                    ) from exc
                mid = start + (end - start) / 2
                logger.info(
                    "[marketdata] ENTSO-E window %s → %s exceeds the "
                    "response limit; bisecting.",
                    _fmt_period(start), _fmt_period(end),
                )
                return _fetch_window(
                    base_params, start, mid,
                    token=token, timeout=timeout, depth=depth + 1,
                ) + _fetch_window(
                    base_params, mid, end,
                    token=token, timeout=timeout, depth=depth + 1,
                )
            # A genuinely empty sub-window inside a ZIP is tolerable;
            # overall emptiness is checked by the caller.
            logger.info("[marketdata] ENTSO-E no-data window: %s", exc)
    return segments


def fetch_day_ahead_year(
    zone: Zone,
    year: int,
    *,
    token_resolver: Callable[[], str],
    cache: MarketDataCache,
    fetch_mode: str = "cache_first",
    timeout: float = _REQUEST_TIMEOUT_S,
) -> MarketSeries:
    """Fetch (or load from cache) one calendar year of day-ahead prices.

    ``token_resolver`` is called only when a live request is actually
    needed — a cache hit (and the whole ``offline`` mode) works without
    any token configured.  The fetch window is the zone-local calendar
    year expressed in UTC with an hour of margin on each side, so the
    calendar engine can sample every local instant including the DST
    transition days; surplus steps outside the local year are simply
    never sampled.
    """
    cache_key = MarketDataCache.key("dam-a44", zone.code, year)
    cache_path = cache.path(cache_key)
    mode = str(fetch_mode).strip().lower()

    if mode in ("cache_first", "offline"):
        cached = cache.load(cache_key)
        if cached is not None:
            cached.metadata["cache_state"] = "cache hit"
            cached.metadata.setdefault("cache_key", cache_key)
            return cached
        if mode == "offline":
            raise MarketDataError(
                "market_fetch_mode='offline' but the required cache entry "
                f"is missing: {cache_path} (cache key {cache_key}). Run "
                "once with network access (cache_first) or copy the cache "
                "file in."
            )

    token = token_resolver()
    tz = ZoneInfo(zone.tz)
    start = datetime(year, 1, 1, tzinfo=tz).astimezone(UTC)
    end = datetime(year + 1, 1, 1, tzinfo=tz).astimezone(UTC)
    logger.info(
        "[marketdata] ENTSO-E A44 fetch: zone %s (%s), local year %d "
        "(%s → %s UTC), token %s.",
        zone.code, zone.eic, year,
        _fmt_period(start - timedelta(hours=1)),
        _fmt_period(end + timedelta(hours=1)),
        mask_token(token),
    )
    segments = _fetch_window(
        {
            "documentType": "A44",
            "in_Domain": zone.eic,
            "out_Domain": zone.eic,
            "contract_MarketAgreement.type": "A01",
        },
        start - timedelta(hours=1),
        end + timedelta(hours=1),
        token=token,
        timeout=timeout,
    )
    if not segments:
        raise MarketDataError(
            f"ENTSO-E returned no day-ahead data for zone {zone.code} in "
            f"{year}; check the zone/year combination on "
            "https://transparency.entsoe.eu."
        )
    series = MarketSeries(
        segments=segments,
        metadata={
            "source": "entsoe",
            "dataset": "dam-a44",
            "zone": zone.code,
            "eic": zone.eic,
            "year": int(year),
            "fetched_at": utcnow_isoformat(),
            "cache_key": cache_key,
            "cache_state": "live fetch" if mode != "refresh" else "refresh",
        },
    )
    cache.save(cache_key, series)
    return series


def build_query_params(**overrides: Any) -> dict[str, str]:
    """Assemble raw query parameters (probe/diagnostic helper)."""
    return {str(k): str(v) for k, v in overrides.items()}
