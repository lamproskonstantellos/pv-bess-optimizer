"""Market-data ingestion layer (historical prices by API; opt-in).

Selecting an API source on the ``market_data`` sheet replaces the
matching workbook price columns with fetched historical series — see
:mod:`pvbess_opt.marketdata.base` for the calendar contract,
:mod:`pvbess_opt.marketdata.entsoe` for the ENTSO-E providers
(day-ahead A44, balancing A81/A84, imbalance A85),
:mod:`pvbess_opt.marketdata.admie` for the GR balancing/imbalance file
API, and :mod:`pvbess_opt.marketdata.henex` for the GR day-ahead
cross-check.  With every source at its ``file`` default the layer is
inert.
"""

from .base import (
    FETCH_MODES,
    ZONES,
    MarketDataCache,
    MarketDataError,
    MarketDataUnavailableError,
    MarketSeries,
    PriceSegment,
    Zone,
    blank_entsoe_token,
    mask_token,
    materialize_bypassed_workbook,
    resample_intensive,
    resolve_dataset_source,
    resolve_entsoe_token,
    resolve_market_data,
    sample_local_year,
    stitch_segments_utc,
    validate_model_year_grid,
    zone_from_token,
)

__all__ = [
    "FETCH_MODES",
    "ZONES",
    "MarketDataCache",
    "MarketDataError",
    "MarketDataUnavailableError",
    "MarketSeries",
    "PriceSegment",
    "Zone",
    "blank_entsoe_token",
    "mask_token",
    "materialize_bypassed_workbook",
    "resample_intensive",
    "resolve_dataset_source",
    "resolve_entsoe_token",
    "resolve_market_data",
    "sample_local_year",
    "stitch_segments_utc",
    "validate_model_year_grid",
    "zone_from_token",
]
