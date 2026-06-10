"""The metric/value sheets in 03_results.xlsx must stay scalar.

Sequence-valued KPI entries (``lifetime_bm_revenue_eur_per_year`` — the
per-year balancing revenue list kept on the dict for API consumers) were
previously written as a Python ``repr`` string crammed into one Excel
cell.  The same numbers already live as the proper
``cashflow_yearly['balancing_revenue_eur']`` column, so the writer now
drops sequence values from the ``kpis_year1`` and ``financial_kpis``
sheets.
"""

from __future__ import annotations

import numpy as np
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


def test_metric_value_sheets_drop_sequence_values(tmp_path):
    kpis = {
        "profit_total_eur": 123.45,
        "mode": "merchant",
        "a_list_diagnostic": [1.0, 2.0, 3.0],
        "bess_utilization_diagnostics": {"bess_capacity_mwh": 2.0},
    }
    fin = {
        "npv_eur": 1000.0,
        "lifetime_bm_revenue_eur_per_year": [0.0] * 20,
        "irr_pct": 7.5,
    }
    out = write_results_workbook(
        tmp_path / "03_results.xlsx",
        res_year1=_minimal_res(),
        kpis_year1=kpis,
        kpis_monthly_year1=None,
        financial_kpis=fin,
    )

    k1 = pd.read_excel(out, sheet_name="kpis_year1")
    assert "a_list_diagnostic" not in set(k1["metric"])
    assert "profit_total_eur" in set(k1["metric"])
    # nested dicts are still flattened to scalar rows
    assert "bess_util_capacity_mwh" in set(k1["metric"])

    f = pd.read_excel(out, sheet_name="financial_kpis")
    assert "lifetime_bm_revenue_eur_per_year" not in set(f["metric"])
    assert {"npv_eur", "irr_pct"} <= set(f["metric"])
    # no repr-of-list strings anywhere in either sheet
    for frame in (k1, f):
        assert not frame["value"].astype(str).str.startswith("[").any()
