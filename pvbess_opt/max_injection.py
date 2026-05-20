"""Hour-of-day max-injection cap profile helpers.

The workbook ships with a ``max_injection_profile`` sheet that
specifies the share of ``p_grid_export_max_kw`` available for export
as a percent per hour-of-day, optionally per calendar month.  This is
the input the user controls directly; the curtailed MWh appears in
the outputs.

Two supported shapes (auto-detected by the loader in
:mod:`pvbess_opt.io`):

* **(24,)** — single ``max_injection_pct`` column applied to every
  day of the year.
* **(24, 12)** — twelve monthly columns
  (``max_injection_pct_jan`` … ``max_injection_pct_dec``).

The :func:`build_per_step_max_injection_frac` helper expands the
profile to a per-timestep fraction array aligned with the timeseries.
When the profile is the default constant 73 % at every hour the
resulting per-step series is a flat 0.73.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .config import DEFAULT_MAX_INJECTION_PCT_HOURLY

logger = logging.getLogger(__name__)


def _normalise_max_injection_profile(
    profile: np.ndarray | None,
) -> np.ndarray:
    """Return a (24,) or (24, 12) float array of *percentages*.

    Percentages express the share of ``p_grid_export_max_kw``
    available for export in that hour (e.g. 73 ⇒ 73 % allowed).
    ``None`` falls back to the constant default.
    """
    if profile is None:
        return np.full(
            24, DEFAULT_MAX_INJECTION_PCT_HOURLY, dtype=float,
        )
    arr = np.asarray(profile, dtype=float)
    if arr.ndim == 1:
        if arr.shape[0] != 24:
            raise ValueError(
                "max-injection profile must have 24 hourly rows "
                f"(got shape {arr.shape})."
            )
        return arr
    if arr.ndim == 2:
        if arr.shape != (24, 12):
            raise ValueError(
                "max-injection profile (2-D) must be shape (24, 12) "
                f"(got {arr.shape})."
            )
        return arr
    raise ValueError(
        "max-injection profile must be 1-D (24,) or 2-D (24, 12); "
        f"got shape {arr.shape}."
    )


def build_per_step_max_injection_frac(
    timestamps: pd.Series | pd.DatetimeIndex | np.ndarray,
    profile: np.ndarray | None,
) -> np.ndarray:
    """Map each timestep to its max-injection fraction (0..1).

    Parameters
    ----------
    timestamps
        Datetime-like index or column with one entry per MILP step.
    profile
        Either a (24,) hourly cap profile applied to every day, or a
        (24, 12) cap profile indexed by ``(hour_of_day, month - 1)``.
        Values are interpreted as percentages of
        ``p_grid_export_max_kw`` available for export
        (e.g. 73 ⇒ 0.73).  ``None`` falls back to the project default.
    """
    arr = _normalise_max_injection_profile(profile)
    ts = pd.to_datetime(pd.Index(timestamps))
    hours = ts.hour.to_numpy(dtype=int)
    if arr.ndim == 1:
        per_step_pct = arr[hours]
    else:
        months = ts.month.to_numpy(dtype=int) - 1
        per_step_pct = arr[hours, months]
    return np.clip(per_step_pct.astype(float) / 100.0, 0.0, 1.0)
