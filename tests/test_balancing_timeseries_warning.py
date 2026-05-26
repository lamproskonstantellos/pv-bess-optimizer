"""Single consolidated warning when balancing timeseries columns are missing.

The loader previously emitted one ``logger.warning`` per missing
balancing-price column.  A workbook with balancing enabled but no
per-product price columns triggered nine separate warning lines (five
capacity + four activation) — a noisy storm with no incremental
information per line.  The fix collects every missing column and emits
a single greppable warning naming all of them.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from pvbess_opt.io import (
    _BALANCING_TS_COLUMN_DEFAULTS,
    _apply_balancing_timeseries_fallback,
)


def _ts_without_balancing_columns() -> pd.DataFrame:
    n = 96  # one day at 15-minute cadence is enough.
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="15min"),
        "load_kwh": np.ones(n),
        "pv_kwh": np.ones(n),
        "dam_price_eur_per_mwh": np.full(n, 50.0),
    })


def _ts_with_all_balancing_columns(default_value: float = 5.0) -> pd.DataFrame:
    ts = _ts_without_balancing_columns()
    for col in _BALANCING_TS_COLUMN_DEFAULTS:
        ts[col] = np.full(len(ts), default_value, dtype=float)
    return ts


def _balancing_enabled_config() -> dict:
    return {
        "balancing_enabled": True,
        "fcr_default_capacity_price_eur_per_mwh": 8.0,
        "afrr_up_default_capacity_price_eur_per_mwh": 10.0,
        "afrr_dn_default_capacity_price_eur_per_mwh": 9.0,
        "mfrr_up_default_capacity_price_eur_per_mwh": 6.0,
        "mfrr_dn_default_capacity_price_eur_per_mwh": 5.0,
        "afrr_up_default_activation_price_eur_per_mwh": 200.0,
        "afrr_dn_default_activation_price_eur_per_mwh": 30.0,
        "mfrr_up_default_activation_price_eur_per_mwh": 250.0,
        "mfrr_dn_default_activation_price_eur_per_mwh": 25.0,
    }


def test_single_warning_lists_every_missing_column(caplog):
    """When all nine balancing columns are absent, exactly one
    warning is emitted and it names every missing column."""
    caplog.set_level(logging.WARNING, logger="pvbess_opt.io")
    ts = _ts_without_balancing_columns()
    _apply_balancing_timeseries_fallback(ts, _balancing_enabled_config())

    relevant = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "Balancing timeseries" in rec.getMessage()
    ]
    assert len(relevant) == 1, (
        f"Expected exactly one balancing-timeseries warning, "
        f"got {len(relevant)}: {[r.getMessage() for r in relevant]}"
    )
    msg = relevant[0].getMessage()
    for col in _BALANCING_TS_COLUMN_DEFAULTS:
        assert col in msg, f"warning message does not name {col!r}: {msg}"


def test_no_warning_when_all_columns_present(caplog):
    """A workbook that already carries every balancing-price column
    must not trigger the warning at all."""
    caplog.set_level(logging.WARNING, logger="pvbess_opt.io")
    ts = _ts_with_all_balancing_columns()
    _apply_balancing_timeseries_fallback(ts, _balancing_enabled_config())

    relevant = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "Balancing timeseries" in rec.getMessage()
    ]
    assert len(relevant) == 0, (
        f"Expected zero balancing-timeseries warnings, got "
        f"{[r.getMessage() for r in relevant]}"
    )


def test_no_warning_when_balancing_disabled(caplog):
    """Balancing-disabled workbooks bypass the fallback entirely."""
    caplog.set_level(logging.WARNING, logger="pvbess_opt.io")
    ts = _ts_without_balancing_columns()
    config = _balancing_enabled_config()
    config["balancing_enabled"] = False
    _apply_balancing_timeseries_fallback(ts, config)

    relevant = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "Balancing timeseries" in rec.getMessage()
    ]
    assert len(relevant) == 0
