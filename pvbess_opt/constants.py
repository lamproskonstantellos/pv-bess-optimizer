"""Shared numeric constants for the model.

Single source of truth for values consumed by :mod:`pvbess_opt.io`,
:mod:`pvbess_opt.optimization`, :mod:`pvbess_opt.max_injection`,
:mod:`pvbess_opt.economics`, :mod:`pvbess_opt.sensitivity` and
:mod:`pvbess_opt.plotting.lifecycle`.
"""

from __future__ import annotations

__all__ = [
    "BENCHMARK_LCOE_HIGH_EUR_PER_MWH",
    "BENCHMARK_LCOE_LOW_EUR_PER_MWH",
    "BENCHMARK_LCOS_HIGH_EUR_PER_MWH",
    "BENCHMARK_LCOS_LOW_EUR_PER_MWH",
    "DEFAULT_MAX_INJECTION_PCT_HOURLY",
    "DEFAULT_SENSITIVITY_DELTA_PCT",
    "DEFAULT_SENSITIVITY_DISCOUNT_RATE_DELTA_PP",
    "DEFAULT_SENSITIVITY_TAX_RATE_DELTA_PP",
]

# Lazard 2024 utility-scale benchmark bands (EUR/MWh, EUR-equivalent at
# ~1.08 EUR/USD).  Workbook ``benchmark_lco{e,s}_*`` keys override these
# per project.
BENCHMARK_LCOE_LOW_EUR_PER_MWH: float = 30.0
BENCHMARK_LCOE_HIGH_EUR_PER_MWH: float = 85.0
BENCHMARK_LCOS_LOW_EUR_PER_MWH: float = 157.0
BENCHMARK_LCOS_HIGH_EUR_PER_MWH: float = 274.0

# Default one-at-a-time sensitivity deltas.  CAPEX / OPEX / revenue are
# relative (percent); the discount rate is an absolute shift (percentage
# points).
DEFAULT_SENSITIVITY_DELTA_PCT: float = 10.0
DEFAULT_SENSITIVITY_DISCOUNT_RATE_DELTA_PP: float = 2.0
#: TaxRate tornado driver +/- in percentage points (statutory-rate
#: changes move in whole points; 5 pp spans a typical reform).
DEFAULT_SENSITIVITY_TAX_RATE_DELTA_PP: float = 5.0

# Default share of ``p_grid_export_max_kw`` that is available for export,
# in percent (per hour-of-day).  Applied when the workbook omits the
# ``max_injection_profile`` sheet.  100.0 means "no curtailment" — the
# constraint binds only on the regulatory grid-connection nameplate.
# Users opt in to curtailment by supplying a profile below 100.
DEFAULT_MAX_INJECTION_PCT_HOURLY: float = 100.0
