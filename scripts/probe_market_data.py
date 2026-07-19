"""Probe the market-data endpoints of the planned ingestion layer.

One-shot reconnaissance for the ``pvbess_opt.marketdata`` providers:
confirms which datasets each endpoint actually serves before any
provider code is written, and records raw responses as candidate test
fixtures.  Three endpoint families are probed:

* **ENTSO-E Transparency API** (``https://web-api.tp.entsoe.eu/api``) —
  day-ahead prices (A44, 12.1.D), contracted balancing capacity
  (A81, 17.1.B&C), activated balancing energy prices (A84, 17.1.F) and
  imbalance prices (A85, 17.1.G) for a bidding-zone list (default:
  GR plus a DE_LU control zone that is known to publish everything, so
  an empty GR answer can be attributed to the zone rather than to the
  query).  The A44 probe fires two windows — one before and one after
  the 2025-10-01 SDAC 15-min MTU go-live — so the PT60M/PT15M split is
  observed directly.
* **ADMIE / IPTO file API** (``https://www.admie.gr/getOperationMarketFile``)
  — the Greek balancing/imbalance results are published as daily xlsx
  files per ``FileCategory``; the exact category names are not
  documented, so a candidate list is probed and every category that
  answers with a non-empty file list is reported (with the first file
  URL, downloadable via ``--save-dir`` for column-map pinning).
* **HEnEx** daily DAM results workbooks
  (``.../YYYYMMDD_EL-DAM_ResultsSummary_EN_vNN.xlsx``) — the
  cross-check source for the GR A44 series; version suffixes are
  probed in order.

The ENTSO-E token is resolved exactly like the future ``market_data``
sheet contract: the ``entsoe_token`` key of the workbook's
``market_data`` sheet (when the sheet exists), then the environment
variable named by ``entsoe_token_env`` (default ``ENTSOE_API_TOKEN``).
The token is never printed — log lines mask it to its first 8
characters.

Usage::

    python scripts/probe_market_data.py                     # full probe
    python scripts/probe_market_data.py --save-dir probes/  # keep bodies
    python scripts/probe_market_data.py --skip admie henex  # ENTSO-E only
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import zipfile
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ENTSOE_URL = "https://web-api.tp.entsoe.eu/api"
ADMIE_URL = "https://www.admie.gr/getOperationMarketFile"
HENEX_URL_TEMPLATE = (
    "https://www.enexgroup.gr/documents/20126/366820/"
    "{yyyymmdd}_EL-DAM_ResultsSummary_EN_v{version:02d}.xlsx"
)

# EIC codes of the zone enum the marketdata layer will ship (§ design).
ZONE_EIC: dict[str, str] = {
    "GR": "10YGR-HTSO-----Y",
    "DE_LU": "10Y1001A1001A82H",
    "FR": "10YFR-RTE------C",
    "IT_NORD": "10Y1001A1001A73I",
    "ES": "10YES-REE------0",
    "BG": "10YCA-BULGARIA-R",
    "RO": "10YRO-TEL------P",
}

# Candidate ADMIE FileCategory names around the four dataset families
# named in the design notes.  Deliberately over-inclusive: the endpoint
# answers an empty JSON list for an unknown category, so a wrong guess
# costs one request and no error.
ADMIE_CATEGORIES: tuple[str, ...] = (
    "ISP1ISPResults",
    "ISP2ISPResults",
    "ISP3IntraDayISPResults",
    "ISPResults",
    "ISP1DayAheadLoadForecast",
    "BalancingEnergyProduct",
    "BalancingEnergyPerProduct",
    "ActivatedBalancingEnergyPrices",
    "ActivatedBalancingEnergyAndSettlementPrices",
    "IMBABE",
    "ImbalancePrice",
    "RealTimeSCADARES",
    "DayAheadSchedulingUnitAvailabilities",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe ENTSO-E / ADMIE / HEnEx market-data endpoints "
                    "and report which datasets each one serves.",
    )
    parser.add_argument(
        "--workbook", type=Path, default=REPO_ROOT / "inputs" / "input.xlsx",
        help="Workbook whose market_data sheet (if present) carries the "
             "ENTSO-E token (default: inputs/input.xlsx).",
    )
    parser.add_argument(
        "--zones", nargs="+", default=["GR", "DE_LU"],
        choices=sorted(ZONE_EIC),
        help="Bidding zones to probe on ENTSO-E (default: GR DE_LU).",
    )
    parser.add_argument(
        "--save-dir", type=Path, default=None,
        help="Directory to save raw response bodies into (candidate test "
             "fixtures); omit to probe without saving.",
    )
    parser.add_argument(
        "--skip", nargs="*", default=[],
        choices=("entsoe", "admie", "henex"),
        help="Endpoint families to skip.",
    )
    parser.add_argument(
        "--timeout", type=float, default=60.0,
        help="Per-request timeout in seconds (default 60).",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Token resolution (mirrors the future market_data sheet contract)
# ---------------------------------------------------------------------------


def _mask(token: str) -> str:
    """Return the loggable form of a token: first 8 chars + ellipsis."""
    return token[:8] + "…" if len(token) > 8 else "…"


def resolve_entsoe_token(workbook: Path) -> str | None:
    """Workbook ``market_data`` sheet first, then the environment."""
    token = ""
    env_name = "ENTSOE_API_TOKEN"
    if workbook.exists():
        try:
            import pandas as pd

            sheet = pd.read_excel(workbook, sheet_name="market_data")
            kv = dict(
                zip(
                    sheet.iloc[:, 0].astype(str).str.strip(),
                    sheet.iloc[:, 1],
                    strict=False,
                )
            )
            raw = kv.get("entsoe_token")
            if raw is not None and str(raw).strip().lower() not in ("", "nan"):
                token = str(raw).strip()
            raw_env = kv.get("entsoe_token_env")
            if raw_env is not None and str(raw_env).strip():
                env_name = str(raw_env).strip()
        except ValueError:
            pass  # no market_data sheet yet — the pre-P1 state
    if not token:
        token = os.environ.get(env_name, "").strip()
    if token:
        print(f"ENTSO-E token resolved ({_mask(token)}).")
        return token
    print(
        "No ENTSO-E token found: set the entsoe_token key on the workbook's "
        f"market_data sheet or export {env_name}. Skipping ENTSO-E probes."
    )
    return None


# ---------------------------------------------------------------------------
# Response dissection helpers
# ---------------------------------------------------------------------------


def _local_name(tag: str) -> str:
    """Strip the XML namespace from a tag name."""
    return tag.rsplit("}", 1)[-1]


def _dissect_xml(body: bytes) -> str:
    """One-line summary of a CIM XML body (or the reason it is not one)."""
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        return f"unparseable XML ({exc})"
    name = _local_name(root.tag)
    series = [el for el in root.iter() if _local_name(el.tag) == "TimeSeries"]
    resolutions = sorted(
        {
            (el.text or "").strip()
            for el in root.iter()
            if _local_name(el.tag) == "resolution"
        }
    )
    if name == "Acknowledgement_MarketDocument":
        reasons = [
            (el.text or "").strip()
            for el in root.iter()
            if _local_name(el.tag) == "text"
        ]
        return f"Acknowledgement (no data): {'; '.join(r for r in reasons if r)}"
    points = sum(1 for el in root.iter() if _local_name(el.tag) == "Point")
    return (
        f"{name}: {len(series)} TimeSeries, {points} Points, "
        f"resolutions={resolutions or ['-']}"
    )


def _dissect_body(body: bytes) -> str:
    """Summarise an ENTSO-E body: plain CIM XML or a ZIP of XMLs."""
    if body[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = zf.namelist()
            inner = _dissect_xml(zf.read(names[0])) if names else "empty ZIP"
        return f"ZIP of {len(names)} file(s); first: {inner}"
    return _dissect_xml(body)


def _save(save_dir: Path | None, name: str, body: bytes) -> None:
    if save_dir is None:
        return
    save_dir.mkdir(parents=True, exist_ok=True)
    (save_dir / name).write_bytes(body)


# ---------------------------------------------------------------------------
# ENTSO-E probes
# ---------------------------------------------------------------------------


def _entsoe_get(
    token: str, params: dict[str, str], timeout: float,
) -> tuple[int, bytes]:
    query = {"securityToken": token, **params}
    resp = requests.get(ENTSOE_URL, params=query, timeout=timeout)
    return resp.status_code, resp.content


def probe_entsoe(
    token: str, zones: Iterable[str], save_dir: Path | None, timeout: float,
) -> None:
    print("\n=== ENTSO-E Transparency API ===")
    # (label, window, extra query params).  The two A44 windows bracket
    # the 2025-10-01 SDAC 15-min MTU go-live; balancing/imbalance use a
    # single short 2025 window (their resolution is TSO-fixed).
    a44_windows = [
        ("A44 day-ahead (pre 15-min MTU)", "202503150000", "202503170000"),
        ("A44 day-ahead (post 15-min MTU)", "202511150000", "202511170000"),
    ]
    for zone in zones:
        eic = ZONE_EIC[zone]
        print(f"\n--- zone {zone} ({eic}) ---")
        for label, start, end in a44_windows:
            status, body = _entsoe_get(
                token,
                {
                    "documentType": "A44",
                    "in_Domain": eic,
                    "out_Domain": eic,
                    "contract_MarketAgreement.type": "A01",
                    "periodStart": start,
                    "periodEnd": end,
                },
                timeout,
            )
            print(f"{label}: HTTP {status} — {_dissect_body(body)}")
            _save(save_dir, f"entsoe_{zone}_A44_{start}.xml", body)
        balancing_probes = [
            ("A81 FCR capacity (17.1.B&C)", {"processType": "A52"}),
            ("A81 aFRR capacity (17.1.B&C)", {"processType": "A51"}),
            ("A81 mFRR capacity (17.1.B&C)", {"processType": "A47"}),
        ]
        for label, extra in balancing_probes:
            status, body = _entsoe_get(
                token,
                {
                    "documentType": "A81",
                    "businessType": "B95",
                    "controlArea_Domain": eic,
                    "periodStart": "202503150000",
                    "periodEnd": "202503170000",
                    **extra,
                },
                timeout,
            )
            print(f"{label}: HTTP {status} — {_dissect_body(body)}")
            _save(
                save_dir,
                f"entsoe_{zone}_A81_{extra['processType']}.xml",
                body,
            )
        for label, doc, extra in [
            ("A84 activated balancing prices (17.1.F)", "A84",
             {"processType": "A16"}),
            ("A85 imbalance prices (17.1.G)", "A85", {}),
        ]:
            status, body = _entsoe_get(
                token,
                {
                    "documentType": doc,
                    "controlArea_Domain": eic,
                    "periodStart": "202503150000",
                    "periodEnd": "202503160000",
                    **extra,
                },
                timeout,
            )
            print(f"{label}: HTTP {status} — {_dissect_body(body)}")
            _save(save_dir, f"entsoe_{zone}_{doc}.xml", body)


# ---------------------------------------------------------------------------
# ADMIE probes
# ---------------------------------------------------------------------------


def probe_admie(save_dir: Path | None, timeout: float) -> None:
    print("\n=== ADMIE / IPTO file API ===")
    # A short, closed window well in the past so every daily file exists.
    window = {"dateStart": "2025-06-02", "dateEnd": "2025-06-03"}
    for category in ADMIE_CATEGORIES:
        try:
            resp = requests.get(
                ADMIE_URL,
                params={**window, "FileCategory": category},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            print(f"{category}: request failed ({exc})")
            continue
        if resp.status_code != 200:
            print(f"{category}: HTTP {resp.status_code}")
            continue
        try:
            listing = resp.json()
        except json.JSONDecodeError:
            print(f"{category}: HTTP 200 but non-JSON body "
                  f"({resp.content[:60]!r})")
            continue
        if not listing:
            print(f"{category}: empty list (category unknown or no files)")
            continue
        first = listing[0]
        url = first.get("file_path") or first.get("file_url") or str(first)
        print(f"{category}: {len(listing)} file(s); first: {url}")
        if save_dir is not None and isinstance(url, str) and url.startswith("http"):
            try:
                file_resp = requests.get(url, timeout=timeout)
                if file_resp.status_code == 200:
                    _save(save_dir, f"admie_{category}_{Path(url).name}",
                          file_resp.content)
                    print(f"    saved {Path(url).name} "
                          f"({len(file_resp.content)} bytes)")
            except requests.RequestException as exc:
                print(f"    download failed ({exc})")


# ---------------------------------------------------------------------------
# HEnEx probes
# ---------------------------------------------------------------------------


def probe_henex(save_dir: Path | None, timeout: float) -> None:
    print("\n=== HEnEx daily DAM workbooks ===")
    probe_day = date.today() - timedelta(days=7)
    yyyymmdd = probe_day.strftime("%Y%m%d")
    for version in range(1, 6):
        url = HENEX_URL_TEMPLATE.format(yyyymmdd=yyyymmdd, version=version)
        try:
            resp = requests.get(url, timeout=timeout)
        except requests.RequestException as exc:
            print(f"v{version:02d}: request failed ({exc})")
            return
        print(f"{probe_day} v{version:02d}: HTTP {resp.status_code} "
              f"({len(resp.content)} bytes)")
        if resp.status_code == 200:
            _save(save_dir, f"henex_{yyyymmdd}_v{version:02d}.xlsx",
                  resp.content)
            return
    print("No version suffix answered 200 for that date; the workbook may "
          "publish later or under a different path.")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if "entsoe" not in args.skip:
        token = resolve_entsoe_token(args.workbook)
        if token is not None:
            try:
                probe_entsoe(token, args.zones, args.save_dir, args.timeout)
            except requests.RequestException as exc:
                print(f"ENTSO-E probe aborted: {exc}")
    if "admie" not in args.skip:
        probe_admie(args.save_dir, args.timeout)
    if "henex" not in args.skip:
        probe_henex(args.save_dir, args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
