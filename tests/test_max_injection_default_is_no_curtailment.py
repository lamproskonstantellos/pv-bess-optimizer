"""The default ``max_injection_profile`` represents *no curtailment*.

A flat 100 % profile (the project default) expands to a per-step
fraction of 1.0, meaning the per-step export cap binds only on the
regulatory grid-connection nameplate ``p_grid_export_max_kw`` and not on
the curtailment profile.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from pvbess_opt.constants import DEFAULT_MAX_INJECTION_PCT_HOURLY
from pvbess_opt.max_injection import build_per_step_max_injection_frac


def test_default_constant_is_one_hundred_percent():
    """The project default is 100 % (no curtailment)."""
    assert DEFAULT_MAX_INJECTION_PCT_HOURLY == 100.0


def test_default_profile_produces_flat_unity_fraction():
    """``profile=None`` yields a per-step fraction series of 1.0."""
    timestamps = pd.date_range("2026-01-01", periods=96, freq="15min")
    frac = build_per_step_max_injection_frac(timestamps, profile=None)
    assert frac.shape == (96,)
    assert np.allclose(frac, 1.0)


def test_canonical_workbook_ships_no_curtailment_profile():
    """The shipped reference workbook is no-curtailment at every hour."""
    profile = pd.read_excel(
        "inputs/input.xlsx", sheet_name="max_injection_profile",
    )["max_injection_pct"]
    assert (profile == 100.0).all()
