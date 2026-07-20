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
    parser: Callable[[bytes], list[Any]] | None = None,
) -> list[Any]:
    """Fetch one window, bisecting when the platform reports too much data.

    ``parser`` turns one XML document into result items (default: the
    A44 publication parser producing :class:`PriceSegment` items; the
    balancing datasets pass their tagged parser).
    """
    parse = parser if parser is not None else parse_publication_document
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
    items: list[Any] = []
    for document in _bodies_from_response(body):
        try:
            items.extend(parse(document))
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
                    parser=parser,
                ) + _fetch_window(
                    base_params, mid, end,
                    token=token, timeout=timeout, depth=depth + 1,
                    parser=parser,
                )
            # A genuinely empty sub-window inside a ZIP is tolerable;
            # overall emptiness is checked by the caller.
            logger.info("[marketdata] ENTSO-E no-data window: %s", exc)
    return items


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


#: Intraday-auction selector → ``classificationSequence_AttributeInstanceComponent.Position``.
#: PROVISIONAL until pinned by ``scripts/probe_market_data.py``: the
#: pan-European SIDC intraday auctions (IDA1/IDA2/IDA3, live since
#: June 2024) publish on the Transparency Platform through the same
#: A44 Price Document endpoint with ``contract_MarketAgreement.type``
#: A07 (intraday) and the auction sequence as the classification
#: position — the parameter pair the API guide documents for the
#: intraday auction prices view.  Continuous SIDC trade prices are
#: exchange-proprietary and deliberately NOT fetchable here.
INTRADAY_AUCTIONS: dict[str, int] = {"ida1": 1, "ida2": 2, "ida3": 3}


def fetch_intraday_auction_year(
    zone: Zone,
    year: int,
    auction: str,
    *,
    token_resolver: Callable[[], str],
    cache: MarketDataCache,
    fetch_mode: str = "cache_first",
    timeout: float = _REQUEST_TIMEOUT_S,
) -> MarketSeries:
    """Fetch one calendar year of intraday-AUCTION clearing prices.

    The A44 sibling of :func:`fetch_day_ahead_year` with the intraday
    contract type and the ``auction`` selector (``ida1`` / ``ida2`` /
    ``ida3``) mapped to the classification-sequence position.  Note
    the auctions only exist from mid-2024 on — a reference year before
    that returns no data (a loud error, not an empty series).
    """
    auction = str(auction).strip().lower()
    if auction not in INTRADAY_AUCTIONS:
        raise MarketDataError(
            f"intraday_auction {auction!r} is not one of "
            f"{', '.join(sorted(INTRADAY_AUCTIONS))}."
        )
    cache_key = MarketDataCache.key(
        "ida-a44", zone.code, year, auction=auction,
    )
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
        "[marketdata] ENTSO-E A44/A07 fetch: zone %s (%s), auction %s, "
        "local year %d (%s → %s UTC), token %s.",
        zone.code, zone.eic, auction.upper(), year,
        _fmt_period(start - timedelta(hours=1)),
        _fmt_period(end + timedelta(hours=1)),
        mask_token(token),
    )
    segments = _fetch_window(
        {
            "documentType": "A44",
            "in_Domain": zone.eic,
            "out_Domain": zone.eic,
            "contract_MarketAgreement.type": "A07",
            "classificationSequence_AttributeInstanceComponent.Position":
                str(INTRADAY_AUCTIONS[auction]),
        },
        start - timedelta(hours=1),
        end + timedelta(hours=1),
        token=token,
        timeout=timeout,
    )
    if not segments:
        raise MarketDataError(
            f"ENTSO-E returned no {auction.upper()} intraday-auction "
            f"data for zone {zone.code} in {year}; the SIDC IDAs only "
            "publish from mid-2024 on — check the zone/year combination "
            "on https://transparency.entsoe.eu."
        )
    series = MarketSeries(
        segments=segments,
        metadata={
            "source": "entsoe",
            "dataset": "ida-a44",
            "zone": zone.code,
            "eic": zone.eic,
            "year": int(year),
            "auction": auction,
            "fetched_at": utcnow_isoformat(),
            "cache_key": cache_key,
            "cache_state": "live fetch" if mode != "refresh" else "refresh",
        },
    )
    cache.save(cache_key, series)
    return series


# ---------------------------------------------------------------------------
# Balancing datasets (17.1.B&C / 17.1.F / 17.1.G) — non-GR zones
# ---------------------------------------------------------------------------
# The GR balancing domain on the platform is effectively empty (co-
# optimised ISP; results publish nationally via ADMIE), so these
# fetchers serve the OTHER zones of the registry; the per-zone source
# registry in base.py routes GR to the ADMIE provider.

#: Point-level price fields, probed in order (each dataset family uses
#: its own tag; ``price.amount`` is the last-resort generic field).
_PRICE_FIELDS: tuple[str, ...] = (
    "procurement_Price.amount",
    "activation_Price.amount",
    "imbalance_Price.amount",
    "price.amount",
)

#: Units accepted on balancing documents.  Capacity prices commonly
#: publish per MW of reserved capacity per period ("MAW"); those are
#: normalised to the model's EUR/MWh reservation basis by dividing by
#: the period length in hours.  PROVISIONAL pending recorded live
#: documents (see docs/notes/market_data_spike.md): the normalisation
#: is driven by the declared unit, never applied blindly.
_MW_UNITS: frozenset[str] = frozenset({"MAW", "MW"})
_MWH_UNITS: frozenset[str] = frozenset({"MWH"})


def parse_balancing_document(
    body: bytes,
) -> list[tuple[dict[str, str], PriceSegment]]:
    """Parse a ``Balancing_MarketDocument`` into tagged price segments.

    Each ``TimeSeries``/``Period`` becomes one segment tagged with the
    TimeSeries' ``businessType``, ``flowDirection.direction`` and
    ``imbalance_Price.category`` (empty string when absent), so the
    dataset assemblers can map segments onto model columns.  MW-based
    capacity prices are normalised to the per-MWh reservation basis
    here, where the period resolution is in scope.
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
    if root_name != "Balancing_MarketDocument":
        raise MarketDataError(
            f"unexpected ENTSO-E document {root_name!r} (expected "
            "Balancing_MarketDocument)."
        )
    out: list[tuple[dict[str, str], PriceSegment]] = []
    for ts_el in (
        el for el in root.iter() if _local_name(el.tag) == "TimeSeries"
    ):
        currency = _child_text(ts_el, "currency_Unit.name") or "EUR"
        if currency.upper() != "EUR":
            raise MarketDataError(
                f"ENTSO-E balancing TimeSeries priced in {currency}; "
                "only EUR is supported."
            )
        unit = (
            _child_text(ts_el, "price_Measure_Unit.name") or "MWH"
        ).upper()
        if unit not in (_MW_UNITS | _MWH_UNITS):
            raise MarketDataError(
                f"ENTSO-E balancing TimeSeries with unsupported price "
                f"unit {unit!r} (supported: MWH, MAW/MW)."
            )
        tags = {
            "businessType": _child_text(ts_el, "businessType") or "",
            "direction": _child_text(ts_el, "flowDirection.direction") or "",
            "category": _child_text(ts_el, "imbalance_Price.category") or "",
        }
        for period in (
            el for el in ts_el.iter() if _local_name(el.tag) == "Period"
        ):
            start_text = _child_text(period, "start")
            end_text = _child_text(period, "end")
            res_text = _child_text(period, "resolution") or ""
            minutes = _RESOLUTION_MINUTES.get(res_text)
            if start_text is None or end_text is None or minutes is None:
                raise MarketDataError(
                    "ENTSO-E balancing Period without a valid "
                    "timeInterval/resolution."
                )
            start = _parse_utc(start_text)
            end = _parse_utc(end_text)
            n_steps = int((end - start).total_seconds() // 60) // minutes
            if n_steps <= 0:
                raise MarketDataError(
                    f"ENTSO-E balancing Period with a non-positive span "
                    f"({start_text} → {end_text})."
                )
            by_position: dict[int, float] = {}
            for point in (
                el for el in period.iter() if _local_name(el.tag) == "Point"
            ):
                pos_text = _child_text(point, "position")
                price_text = next(
                    (
                        text for field in _PRICE_FIELDS
                        if (text := _child_text(point, field)) is not None
                    ),
                    None,
                )
                if pos_text is None or price_text is None:
                    raise MarketDataError(
                        "ENTSO-E balancing Point without position or a "
                        "recognised price field "
                        f"({', '.join(_PRICE_FIELDS)})."
                    )
                by_position[int(pos_text)] = float(price_text)
            if 1 not in by_position:
                raise MarketDataError(
                    f"ENTSO-E balancing Period starting {start_text} has "
                    "no position-1 point; cannot expand the curve."
                )
            values: list[float] = []
            last = by_position[1]
            for pos in range(1, n_steps + 1):
                last = by_position.get(pos, last)
                values.append(last)
            if unit in _MW_UNITS:
                # EUR/MW per period → EUR/MW/h == the model's EUR/MWh
                # reservation basis.
                hours = minutes / 60.0
                values = [v / hours for v in values]
            out.append((
                dict(tags),
                PriceSegment(
                    start_utc=start,
                    resolution_minutes=minutes,
                    values=values,
                ),
            ))
    if not out:
        raise EntsoeNoDataError("document carried no TimeSeries periods")
    return out


#: flowDirection.direction → model column direction token.
_DIRECTION_TOKENS: dict[str, str] = {"A01": "up", "A02": "dn"}

#: A84 businessType → product token (A96 aFRR, A97 mFRR).
_ACTIVATION_BUSINESS_TYPES: dict[str, str] = {"A96": "afrr", "A97": "mfrr"}

#: A85 imbalance_Price.category → model column (A04 excess balance →
#: long, A05 insufficient balance → short; uncategorised → the single
#: imbalance price column).  PROVISIONAL pending recorded documents.
_IMBALANCE_CATEGORY_COLUMNS: dict[str, str] = {
    "A04": "imbalance_price_long_eur_per_mwh",
    "A05": "imbalance_price_short_eur_per_mwh",
    "": "imbalance_price_eur_per_mwh",
}

#: The 9 balancing price columns (5 capacity + 4 activation; FCR has no
#: activation by design) the capacity/activation fetch serves.
ENTSOE_BALANCING_COLUMNS: tuple[str, ...] = (
    "fcr_capacity_price_eur_per_mwh",
    "afrr_up_capacity_price_eur_per_mwh",
    "afrr_dn_capacity_price_eur_per_mwh",
    "mfrr_up_capacity_price_eur_per_mwh",
    "mfrr_dn_capacity_price_eur_per_mwh",
    "afrr_up_activation_price_eur_per_mwh",
    "afrr_dn_activation_price_eur_per_mwh",
    "mfrr_up_activation_price_eur_per_mwh",
    "mfrr_dn_activation_price_eur_per_mwh",
)


def _series_from_column_segments(
    columns: dict[str, list[PriceSegment]], metadata: dict[str, Any],
) -> MarketSeries:
    """Pack a per-column segment map into one cacheable MarketSeries.

    The flat segment list plus a ``columns_map`` (column → segment
    indices) in the metadata JSON-round-trips through the shared cache
    without touching the payload schema.
    """
    flat: list[PriceSegment] = []
    index_map: dict[str, list[int]] = {}
    for column, segments in sorted(columns.items()):
        index_map[column] = list(
            range(len(flat), len(flat) + len(segments)),
        )
        flat.extend(segments)
    metadata["columns_map"] = index_map
    return MarketSeries(segments=flat, metadata=metadata)


def column_segments(series: MarketSeries) -> dict[str, list[PriceSegment]]:
    """Rebuild the per-column segment map from a packed MarketSeries."""
    raw_map = series.metadata.get("columns_map")
    if not isinstance(raw_map, dict):
        raise MarketDataError(
            "packed balancing cache payload has no columns_map; delete "
            f"the cache entry {series.metadata.get('cache_key', '?')} "
            "and re-fetch."
        )
    return {
        str(column): [series.segments[int(i)] for i in indices]
        for column, indices in raw_map.items()
    }


def _fetch_packed_dataset(
    zone: Zone,
    year: int,
    *,
    dataset: str,
    requests_spec: list[tuple[dict[str, str], dict[str, str]]],
    column_for: Callable[[dict[str, str]], str | None],
    token_resolver: Callable[[], str],
    cache: MarketDataCache,
    fetch_mode: str,
    timeout: float,
) -> MarketSeries:
    """Shared cache/fetch skeleton for the tagged balancing datasets.

    ``requests_spec`` lists (extra query params, static tags) — one API
    request each; ``column_for`` maps the merged tags of a returned
    segment to a model column (None drops the segment).
    """
    cache_key = MarketDataCache.key(dataset, zone.code, year)
    mode = str(fetch_mode).strip().lower()
    if mode in ("cache_first", "offline"):
        cached = cache.load(cache_key)
        if cached is not None:
            cached.metadata["cache_state"] = "cache hit"
            cached.metadata.setdefault("cache_key", cache_key)
            return cached
        if mode == "offline":
            raise MarketDataError(
                "market_fetch_mode='offline' but the required cache "
                f"entry is missing: {cache.path(cache_key)} (cache key "
                f"{cache_key}). Run once with network access "
                "(cache_first) or copy the cache file in."
            )

    token = token_resolver()
    tz = ZoneInfo(zone.tz)
    start = datetime(year, 1, 1, tzinfo=tz).astimezone(UTC)
    end = datetime(year + 1, 1, 1, tzinfo=tz).astimezone(UTC)
    columns: dict[str, list[PriceSegment]] = {}
    for extra_params, static_tags in requests_spec:
        items = _fetch_window(
            {"controlArea_Domain": zone.eic, **extra_params},
            start - timedelta(hours=1),
            end + timedelta(hours=1),
            token=token,
            timeout=timeout,
            parser=parse_balancing_document,
        )
        for tags, segment in items:
            column = column_for({**tags, **static_tags})
            if column is not None:
                columns.setdefault(column, []).append(segment)
    if not columns:
        raise MarketDataError(
            f"ENTSO-E returned no {dataset} data for zone {zone.code} "
            f"in {year}; the zone may not publish this dataset — use "
            "the 'file' source for it."
        )
    series = _series_from_column_segments(
        columns,
        {
            "source": "entsoe",
            "dataset": dataset,
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


def fetch_balancing_prices_year(
    zone: Zone,
    year: int,
    *,
    token_resolver: Callable[[], str],
    cache: MarketDataCache,
    fetch_mode: str = "cache_first",
    timeout: float = _REQUEST_TIMEOUT_S,
) -> MarketSeries:
    """Contracted capacity prices (A81) + activated energy prices (A84).

    Three A81 requests (FCR A52 / aFRR A51 / mFRR A47, businessType
    B95) plus one A84 request (processType A16, businessType A96/A97
    filtered in assembly) — together the nine model balancing columns.
    """
    def column_for(tags: dict[str, str]) -> str | None:
        product = tags.get("product") or ""
        direction = _DIRECTION_TOKENS.get(tags.get("direction", ""))
        if tags.get("kind") == "capacity":
            if product == "fcr":
                # FCR is symmetric: no direction split on the platform.
                return "fcr_capacity_price_eur_per_mwh"
            if direction is None:
                return None
            return f"{product}_{direction}_capacity_price_eur_per_mwh"
        product = _ACTIVATION_BUSINESS_TYPES.get(
            tags.get("businessType", ""), "",
        )
        if not product or direction is None:
            return None
        return f"{product}_{direction}_activation_price_eur_per_mwh"

    return _fetch_packed_dataset(
        zone, year,
        dataset="bal-prices",
        requests_spec=[
            (
                {
                    "documentType": "A81", "businessType": "B95",
                    "processType": process,
                },
                {"kind": "capacity", "product": product},
            )
            for process, product in (
                ("A52", "fcr"), ("A51", "afrr"), ("A47", "mfrr"),
            )
        ] + [
            (
                {"documentType": "A84", "processType": "A16"},
                {"kind": "activation"},
            ),
        ],
        column_for=column_for,
        token_resolver=token_resolver,
        cache=cache,
        fetch_mode=fetch_mode,
        timeout=timeout,
    )


def fetch_imbalance_prices_year(
    zone: Zone,
    year: int,
    *,
    token_resolver: Callable[[], str],
    cache: MarketDataCache,
    fetch_mode: str = "cache_first",
    timeout: float = _REQUEST_TIMEOUT_S,
) -> MarketSeries:
    """Imbalance prices (A85, 17.1.G; 15-min ISPs EU-wide since 2025).

    Categorised series land on the dual short/long columns, an
    uncategorised series on the single imbalance price column.
    """
    def column_for(tags: dict[str, str]) -> str | None:
        return _IMBALANCE_CATEGORY_COLUMNS.get(tags.get("category", ""))

    return _fetch_packed_dataset(
        zone, year,
        dataset="imbalance",
        requests_spec=[({"documentType": "A85"}, {})],
        column_for=column_for,
        token_resolver=token_resolver,
        cache=cache,
        fetch_mode=fetch_mode,
        timeout=timeout,
    )
