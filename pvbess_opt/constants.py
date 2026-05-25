"""Shared numeric constants for the financial / sensitivity model.

Single source of truth for values consumed by :mod:`pvbess_opt.io`,
:mod:`pvbess_opt.economics`, :mod:`pvbess_opt.sensitivity` and
:mod:`pvbess_opt.plotting.lifecycle`.
"""

from __future__ import annotations

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
