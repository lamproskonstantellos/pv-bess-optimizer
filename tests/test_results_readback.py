"""Metric/value sheets must survive a pandas round-trip type-intact.

The ``kpis_year1`` / ``financial_kpis`` / ``economic_assumptions``
sheets mix flags and numerics in one ``value`` column.  A genuine
boolean cell in that column made ``pandas.read_excel`` coerce every
zero-valued numeric row of the sheet to Python ``False`` on readback
(the raw cells are written correctly as numeric XML and Excel displays
them right — the corruption is purely programmatic, but pandas is the
most likely client tool).  Flags are therefore written as
``'TRUE'``/``'FALSE'`` text.
"""

from __future__ import annotations

import numpy as np
import openpyxl
import pandas as pd

from pvbess_opt.io import write_results_workbook


def _minimal_res() -> pd.DataFrame:
    n = 4
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "pv_kwh": np.zeros(n),
        "load_kwh": np.zeros(n),
        "soc_kwh": np.zeros(n),
    })


def _value_of(frame: pd.DataFrame, key_col: str, key: str):
    rows = frame.loc[frame[key_col] == key, "value"]
    assert len(rows) == 1, f"expected exactly one {key!r} row"
    return rows.iloc[0]


def test_kpis_year1_survives_pandas_readback(tmp_path):
    kpis = {
        "allow_bess_grid_charging": False,  # the one genuine flag
        "grid_import_mwh": 0.0,             # zero numerics: the victims
        "pv_curtailed_mwh": 0,
        "profit_total_eur": 123.45,
        "mode": "self_consumption",
    }
    out = write_results_workbook(
        tmp_path / "03_results.xlsx",
        res_year1=_minimal_res(),
        kpis_year1=kpis,
        kpis_monthly_year1=None,
    )

    k1 = pd.read_excel(out, sheet_name="kpis_year1")
    for key in ("grid_import_mwh", "pv_curtailed_mwh"):
        v = _value_of(k1, "metric", key)
        assert not isinstance(v, (bool, np.bool_)), (
            f"{key} read back as {v!r} — the flag cell poisoned the column"
        )
        assert float(v) == 0.0
    assert float(_value_of(k1, "metric", "profit_total_eur")) == 123.45
    # The flag itself is delivered as text, not a boolean cell.
    assert _value_of(k1, "metric", "allow_bess_grid_charging") == "FALSE"

    wb = openpyxl.load_workbook(out, read_only=True)
    try:
        cells = {
            row[0].value: row[1]
            for row in wb["kpis_year1"].iter_rows(min_row=2)
        }
        assert cells["allow_bess_grid_charging"].data_type == "s"
        assert cells["grid_import_mwh"].data_type == "n"
    finally:
        wb.close()


def test_economic_assumptions_survives_pandas_readback(tmp_path):
    econ = {
        "azimuth": 0.0,
        "site_capex_eur": 0.0,
        "corporate_tax_rate_pct": 22.0,
        "uncertainty_enabled": False,
        "terminal_soc_equal": True,
        "sensitivity_enabled": np.bool_(True),  # numpy flags too
    }
    out = write_results_workbook(
        tmp_path / "03_results.xlsx",
        res_year1=_minimal_res(),
        kpis_year1={"profit_total_eur": 1.0},
        kpis_monthly_year1=None,
        economic_assumptions=econ,
    )

    ea = pd.read_excel(out, sheet_name="economic_assumptions")
    for key in ("azimuth", "site_capex_eur"):
        v = _value_of(ea, "key", key)
        assert not isinstance(v, (bool, np.bool_)), (
            f"{key} read back as {v!r} — flag cells poisoned the column"
        )
        assert float(v) == 0.0
    assert float(_value_of(ea, "key", "corporate_tax_rate_pct")) == 22.0
    assert _value_of(ea, "key", "uncertainty_enabled") == "FALSE"
    assert _value_of(ea, "key", "terminal_soc_equal") == "TRUE"
    assert _value_of(ea, "key", "sensitivity_enabled") == "TRUE"


def test_financial_kpis_flags_written_as_text(tmp_path):
    # No financial KPI is a flag today; the writer guard still covers the
    # sheet so a future one cannot re-introduce the coercion.
    out = write_results_workbook(
        tmp_path / "03_results.xlsx",
        res_year1=_minimal_res(),
        kpis_year1={"profit_total_eur": 1.0},
        kpis_monthly_year1=None,
        financial_kpis={"npv_eur": 0.0, "dscr_breach": True},
    )
    f = pd.read_excel(out, sheet_name="financial_kpis")
    v = _value_of(f, "metric", "npv_eur")
    assert not isinstance(v, (bool, np.bool_)) and float(v) == 0.0
    assert _value_of(f, "metric", "dscr_breach") == "TRUE"
