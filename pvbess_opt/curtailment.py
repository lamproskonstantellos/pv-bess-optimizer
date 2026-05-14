"""Hour-of-day curtailment-cap profile helpers.

The workbook ships with a ``curtailment_profile`` sheet that
specifies the regulatory grid-export curtailment cap as a percentage
per hour-of-day, optionally per calendar month.

Two supported shapes (auto-detected by the loader in
:mod:`pvbess_opt.io`):

* **(24,)** — single ``curtailment_pct`` column applied to every day
  of the year.
* **(24, 12)** — twelve monthly columns
  (``curtailment_pct_jan`` … ``curtailment_pct_dec``).

The :func:`build_per_step_curtailment_frac` helper expands the profile
to a per-timestep fraction array aligned with the timeseries.  When the
profile is constant 27 % at every hour (the default fixture)
the resulting per-step series is a flat 0.27, reproducing the scalar
scalar baseline exactly.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _normalise_profile(profile: np.ndarray | None) -> np.ndarray:
    """Return a (24,) or (24, 12) float array of *percentages*."""
    if profile is None:
        return np.full(24, 27.0, dtype=float)
    arr = np.asarray(profile, dtype=float)
    if arr.ndim == 1:
        if arr.shape[0] != 24:
            raise ValueError(
                "curtailment profile must have 24 hourly rows "
                f"(got shape {arr.shape})."
            )
        return arr
    if arr.ndim == 2:
        if arr.shape != (24, 12):
            raise ValueError(
                "curtailment profile (2-D) must be shape (24, 12) "
                f"(got {arr.shape})."
            )
        return arr
    raise ValueError(
        "curtailment profile must be 1-D (24,) or 2-D (24, 12); "
        f"got shape {arr.shape}."
    )


def build_per_step_curtailment_frac(
    timestamps: pd.Series | pd.DatetimeIndex | np.ndarray,
    profile: np.ndarray | None,
) -> np.ndarray:
    """Map each timestep to its curtailment fraction (0..1).

    Parameters
    ----------
    timestamps
        Datetime-like index or column with one entry per MILP step.
    profile
        Either a (24,) hourly cap profile applied to every day, or a
        (24, 12) cap profile indexed by ``(hour_of_day, month - 1)``.
        Values are interpreted as percentages (e.g. 27 ⇒ 0.27).
        ``None`` falls back to a flat 27 % default.
    """
    arr = _normalise_profile(profile)
    ts = pd.to_datetime(pd.Index(timestamps))
    hours = ts.hour.to_numpy(dtype=int)
    if arr.ndim == 1:
        per_step_pct = arr[hours]
    else:
        months = ts.month.to_numpy(dtype=int) - 1
        per_step_pct = arr[hours, months]
    return np.clip(per_step_pct.astype(float) / 100.0, 0.0, 1.0)
