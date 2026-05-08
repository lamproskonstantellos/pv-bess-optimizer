"""Shared pytest fixtures and path setup.

Adds the repository root to ``sys.path`` so tests can ``import pvbess_opt``
without having to ``pip install -e .`` first.  Keeps the test suite usable
in a fresh checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _make_short_ts(n_hours: int = 48, *, with_load: bool = True, seed: int = 0) -> pd.DataFrame:
    """Synthetic short timeseries for unit tests.

    PV is zero outside the 06:00-18:00 daylight window.  Multiplicative
    noise is applied **only to daylight steps** so night PV stays
    exactly zero (no Gaussian-bleed bug).
    """
    rng = np.random.default_rng(seed)
    timestamps = pd.date_range("2026-06-01 00:00", periods=n_hours, freq="h")
    h = np.arange(n_hours).astype(float) % 24
    daylight = (h >= 6) & (h <= 18)
    pv_clean = 4000.0 * np.where(
        daylight, np.sin(np.pi * (h - 6) / 12.0), 0.0,
    )
    pv_noise = np.where(daylight, rng.normal(1.0, 0.05, n_hours), 1.0)
    pv = np.maximum(pv_clean * pv_noise, 0.0)
    pv = np.where(daylight, pv, 0.0)
    dam = 100.0 - 50.0 * np.sin(np.pi * (h - 6) / 12.0) + rng.normal(0, 5, n_hours)
    df = {"timestamp": timestamps, "pv_kwh": pv, "dam_price_eur_per_mwh": dam}
    if with_load:
        load = 3000.0 + 1500.0 * np.exp(-((h - 9) ** 2) / 8.0) + rng.normal(0, 50, n_hours)
        df["load_kwh"] = np.maximum(load, 800.0)
    return pd.DataFrame(df)


def _short_params(mode: str = "vnb") -> dict:
    """Minimal valid param dict for a 48-hour test (v0.8 schema)."""
    return {
        "dt_minutes": 60,
        "efficiency_charge": 0.97,
        "efficiency_discharge": 0.97,
        "soc_min_frac": 0.20,
        "soc_max_frac": 0.95,
        "initial_soc_frac": 0.50,
        "terminal_soc_equal": True,
        "max_cycles_per_day": 1.0,
        "p_grid_export_max_kw": 5000.0,
        "pv_nameplate_kwp": 4500.0,
        "bess_power_kw": 5000.0,
        "bess_capacity_kwh": 20000.0,
        "curtailment_frac": 0.27,
        "retail_tariff_eur_per_mwh": 132.0,
        "settlement_minutes": 15,
        "mode": mode,
        "allow_bess_grid_charging": False,
        "show_titles": False,
    }


@pytest.fixture(scope="module")
def short_ts() -> pd.DataFrame:
    return _make_short_ts(48)


@pytest.fixture(scope="module")
def short_params() -> dict:
    return _short_params("vnb")


@pytest.fixture(scope="module")
def short_params_merchant() -> dict:
    return _short_params("merchant")


@pytest.fixture
def repo_input_xlsx() -> Path:
    return ROOT / "inputs" / "input.xlsx"
