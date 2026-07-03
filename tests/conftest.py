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


from tests._pv_helpers import hourly_canonical_pv_window  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: tests that exercise the real-scale workbook (~minutes wall-clock)",
    )


def _make_short_ts(n_hours: int = 48, *, with_load: bool = True, seed: int = 0) -> pd.DataFrame:
    """Synthetic short timeseries for unit tests.

    PV is **deterministic** — a downsampled hourly slice of the
    case-study workbook's ``timeseries::pv_kwh`` column scaled to
    4500 kWp.  No randomness in the PV column whatsoever.  Load and
    DAM remain seeded synthetic curves (their tests don't require
    data-driven realism).
    """
    rng = np.random.default_rng(seed)
    timestamps = pd.date_range("2026-06-01 00:00", periods=n_hours, freq="h")
    h = np.arange(n_hours).astype(float) % 24
    pv = hourly_canonical_pv_window(n_hours, pv_nameplate_kwp=4500.0)
    dam = 100.0 - 50.0 * np.sin(np.pi * (h - 6) / 12.0) + rng.normal(0, 5, n_hours)
    df = {"timestamp": timestamps, "pv_kwh": pv, "dam_price_eur_per_mwh": dam}
    if with_load:
        load = 3000.0 + 1500.0 * np.exp(-((h - 9) ** 2) / 8.0) + rng.normal(0, 50, n_hours)
        df["load_kwh"] = np.maximum(load, 800.0)
    return pd.DataFrame(df)


def _short_params(mode: str = "self_consumption") -> dict:
    """Minimal valid param dict for a 48-hour test."""
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
        "retail_tariff_eur_per_mwh": 120.0,
        "mode": mode,
        "allow_bess_grid_charging": False,
        "show_titles": False,
    }


def _make_short_ts_15min(n_steps: int = 96 * 7, *, seed: int = 0) -> pd.DataFrame:
    """Synthetic 15-min cadence timeseries — one week by default.

    Mirrors :func:`_make_short_ts` but at the production workbook's
    15-minute step so tests that depend on the real-hours semantic of
    ``window_hours`` / ``commit_hours`` can exercise the cadence-aware
    code path.
    """
    rng = np.random.default_rng(seed)
    timestamps = pd.date_range("2026-06-01 00:00", periods=n_steps, freq="15min")
    # Hour-of-day repeated across the week (each hour has 4 quarter-hour rows).
    h = (np.arange(n_steps) / 4.0) % 24.0
    pv_hourly = hourly_canonical_pv_window(
        n_steps // 4 + 1, pv_nameplate_kwp=4500.0,
    )
    # Repeat each hourly PV value 4 times to fill the quarter-hour steps and
    # rescale to a per-step kWh (canonical_pv is kWh/h).
    pv = np.repeat(pv_hourly, 4)[:n_steps] / 4.0
    dam = 100.0 - 50.0 * np.sin(np.pi * (h - 6) / 12.0) + rng.normal(0, 5, n_steps)
    load = 3000.0 + 1500.0 * np.exp(-((h - 9) ** 2) / 8.0) + rng.normal(0, 50, n_steps)
    # 15-min cadence ⇒ per-step kWh = kW * 0.25 h.  Existing _short_params
    # are sized for hourly cadence, so scale load down to match.
    return pd.DataFrame({
        "timestamp": timestamps,
        "pv_kwh": pv,
        "dam_price_eur_per_mwh": dam,
        "load_kwh": np.maximum(load, 800.0) / 4.0,
    })


@pytest.fixture(scope="module")
def short_ts() -> pd.DataFrame:
    return _make_short_ts(48)


@pytest.fixture(scope="module")
def short_ts_15min() -> pd.DataFrame:
    """One week of synthetic 15-min cadence data (672 steps)."""
    return _make_short_ts_15min(96 * 7)


@pytest.fixture(scope="module")
def short_params_15min() -> dict:
    """Hourly :func:`_short_params` with ``dt_minutes`` flipped to 15."""
    p = _short_params("self_consumption")
    p["dt_minutes"] = 15
    return p


@pytest.fixture(scope="module")
def short_params() -> dict:
    return _short_params("self_consumption")


@pytest.fixture(scope="module")
def short_params_merchant() -> dict:
    return _short_params("merchant")


@pytest.fixture
def repo_input_xlsx() -> Path:
    return ROOT / "inputs" / "input.xlsx"
