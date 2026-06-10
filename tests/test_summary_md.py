"""00_summary/SUMMARY.md — the at-a-glance digest the layout advertises.

The output-layout docs (pipeline docstring, users.guide/running.rst)
promised ``00_summary/SUMMARY.md`` but the pipeline never wrote it;
``00_summary/`` only carried ``run_log.txt``.  ``write_summary_md`` now
fulfils the contract.
"""

from __future__ import annotations

import numpy as np

from pvbess_opt.io import write_summary_md


def _params() -> dict:
    return {
        "mode": "self_consumption",
        "pv_nameplate_kwp": 15000.0,
        "bess_power_kw": 15000.0,
        "bess_capacity_kwh": 60000.0,
        "allow_bess_grid_charging": False,
    }


def test_summary_md_written_with_headline_tables(tmp_path):
    out = write_summary_md(
        tmp_path / "00_summary" / "SUMMARY.md",
        kpis_year1={
            "profit_total_eur": 2_849_537.42,
            "pv_generation_mwh": 22_275.0,
            "bess_equivalent_cycles_per_day": 0.4391,
        },
        financial_kpis={
            "npv_eur": 8_221_621.88,
            "irr_pct": 15.3365,
            "simple_payback_years": 5.802,
            "lcoe_eur_per_mwh": 44.78,
            "discounted_payback_years": float("nan"),
        },
        params=_params(),
        solver_name="highs",
    )
    text = out.read_text(encoding="utf-8")
    assert "# Run summary" in text
    assert "`self_consumption`" in text
    assert "PV 15,000 kWp | BESS 15,000 kW / 60,000 kWh" in text
    assert "| Year-1 profit [EUR] | 2,849,537 |" in text
    assert "| NPV [EUR] | 8,221,622 |" in text
    assert "| IRR [%] | 15.34 |" in text
    # NaN renders as n/a, never as 'nan'
    assert "| Discounted payback [years] | n/a |" in text
    assert "03_results.xlsx" in text


def test_summary_md_without_financials(tmp_path):
    out = write_summary_md(
        tmp_path / "SUMMARY.md",
        kpis_year1={"profit_total_eur": 1.0},
        financial_kpis=None,
        params=_params(),
    )
    text = out.read_text(encoding="utf-8")
    assert "## Financial KPIs" not in text
    assert "## Year-1 dispatch KPIs" in text
    assert not np.any([c in text for c in ("{", "}")])
