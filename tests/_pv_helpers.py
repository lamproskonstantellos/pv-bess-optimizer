"""Shared deterministic PV helpers for short-fixture timeseries.

The case-study workbook ``inputs/input.xlsx`` is the single source of
truth for the PV shape.  Its ``timeseries`` sheet ships with 35 040
15-minute rows scaled to 1 MW x 1500 kWh/kWp/yr (1 500 000 kWh annual).
This module exposes a tiny helper that loads, downsamples and slices
the workbook PV column without any randomness — used by every short
fixture in the test suite so the noise-bleed-at-night bug cannot creep
back into a unit test.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
WORKBOOK_PV_REFERENCE_KWP: float = 1000.0   # default workbook nameplate
# 1 June 00:00 in a non-leap year starting at Jan 1 = day index 151 ⇒
# hour index 151 * 24 = 3624.
JUNE_1_HOUR_INDEX: int = 151 * 24


def hourly_canonical_pv_window(
    n_hours: int, pv_nameplate_kwp: float,
    *,
    start_hour: int = JUNE_1_HOUR_INDEX,
) -> np.ndarray:
    """Deterministic hourly PV slice sourced from inputs/input.xlsx.

    Reads the workbook's 15-minute ``pv_kwh`` column (35 040 rows,
    1 MW default scaling), sums consecutive quadruplets to produce
    8 760 hourly values, slices ``n_hours`` from ``start_hour``
    (default June 1 00:00), then rescales linearly by
    ``pv_nameplate_kwp / WORKBOOK_PV_REFERENCE_KWP``.

    Same inputs ⇒ identical output.  No noise, no random, no smoothing.
    """
    ts = pd.read_excel(
        ROOT / "inputs" / "input.xlsx", sheet_name="timeseries",
    )
    shape_15min = ts["pv_kwh"].to_numpy(dtype=float)
    hourly = shape_15min.reshape(-1, 4).sum(axis=1)
    window = hourly[start_hour: start_hour + n_hours]
    return window * (float(pv_nameplate_kwp) / WORKBOOK_PV_REFERENCE_KWP)
