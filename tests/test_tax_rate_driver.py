"""TaxRate tornado driver + opt-in post-tax cumulative line.

The driver is active only while the tax layer is on; taxes are
nonlinear, so each leg is a full cashflow + tax-layer rebuild, and the
driver reports POST-TAX deltas in dedicated columns while its pre-tax
metric columns stay NaN (the pre-tax tornado layouts skip it).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import build_yearly_cashflow, compute_financial_kpis
from pvbess_opt.sensitivity import (
    run_sensitivity_analysis,
    variables_for_npv_sensitivity,
)


def _econ(**overrides) -> dict:
    econ = {
        "project_lifecycle_years": 6,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "capex_pv_eur_per_kw": 400.0,
        "capex_bess_eur_per_kwh": 100.0,
        "devex_pv_eur_per_kw": 0.0,
        "devex_bess_eur_per_kw": 0.0,
        "opex_pv_eur_per_kwp": 5.0,
        "opex_bess_eur_per_kw": 5.0,
        "pv_degradation_year1_pct": 2.0,
        "pv_degradation_annual_pct": 1.0,
        "bess_degradation_annual_pct": 1.5,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 0.0,
        "aggregator_fee_pct_revenue": 0.0,
    }
    econ.update(overrides)
    return econ


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}


def _kpis() -> dict:
    return {
        "profit_load_from_pv_eur": 80_000.0,
        "profit_load_from_bess_eur": 20_000.0,
        "profit_export_from_pv_eur": 10_000.0,
        "profit_export_from_bess_eur": 30_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 135_000.0,
        "pv_export_mwh": 800.0,
        "bess_export_mwh": 300.0,
        "bess_total_discharge_mwh": 500.0,
    }


def test_driver_absent_while_tax_layer_off():
    econ = _econ()
    names = [v["name"] for v in variables_for_npv_sensitivity(econ)]
    assert "TaxRate" not in names
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    fin = compute_financial_kpis(cf, econ)
    sens = run_sensitivity_analysis(_kpis(), econ, _caps(), fin)
    assert "TaxRate" not in set(sens["variable"])
    # No post-tax delta columns leak into a tax-off frame.
    assert "npv_post_tax_eur" not in sens.columns


def test_post_tax_deltas_match_full_rebuild():
    econ = _econ(
        corporate_tax_rate_pct=22.0,
        sensitivity_tax_rate_delta_pp=5.0,
    )
    kpis = _kpis()
    cf = build_yearly_cashflow(kpis, econ, _caps())
    fin = compute_financial_kpis(cf, econ)
    sens = run_sensitivity_analysis(kpis, econ, _caps(), fin)
    rows = sens[sens["variable"] == "TaxRate"].set_index("scenario")
    assert set(rows.index) == {"base", "low", "high"}
    # Pre-tax metric columns stay NaN — the pre-tax tornado skips it.
    assert np.isnan(float(rows.loc["high", "npv_eur"]))
    assert np.isnan(float(rows.loc["low", "irr_pct"]))
    # Driver values are the absolute rates in percentage points.
    assert float(rows.loc["low", "value"]) == pytest.approx(17.0)
    assert float(rows.loc["high", "value"]) == pytest.approx(27.0)
    # The post-tax legs equal an explicit full rebuild.
    for scen, rate in (("low", 17.0), ("high", 27.0)):
        econ_r = {**econ, "corporate_tax_rate_pct": rate}
        manual = compute_financial_kpis(
            build_yearly_cashflow(kpis, econ_r, _caps()), econ_r,
        )
        assert float(rows.loc[scen, "npv_post_tax_eur"]) == pytest.approx(
            manual["npv_post_tax_eur"], abs=0.5,
        ), scen
        assert float(
            rows.loc[scen, "delta_npv_post_tax_eur"]
        ) == pytest.approx(
            manual["npv_post_tax_eur"] - fin["npv_post_tax_eur"], abs=0.5,
        ), scen
    # A higher rate cannot raise the post-tax NPV.
    assert float(rows.loc["high", "delta_npv_post_tax_eur"]) <= 1e-9
    assert float(rows.loc["low", "delta_npv_post_tax_eur"]) >= -1e-9


def test_cumulative_figure_gains_post_tax_line_only_when_on(
    tmp_path, monkeypatch,
):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    import pvbess_opt.plotting.financial as fin_plots

    captured: dict[str, list[str]] = {}

    real_save = fin_plots.save_figure

    def _capture_save(out_path):
        ax = plt.gcf().axes[0]
        captured[str(out_path)] = [
            line.get_label() for line in ax.get_lines()
        ]
        return real_save(out_path)

    monkeypatch.setattr(fin_plots, "save_figure", _capture_save)
    kpis = _kpis()
    for rate, expect_line in ((0.0, False), (22.0, True)):
        econ = _econ(corporate_tax_rate_pct=rate)
        cf = build_yearly_cashflow(kpis, econ, _caps())
        # The frame ALWAYS carries the post-tax column (zero rate =
        # value-identical copy); the RATE is the rendering gate.
        assert "cumulative_dcf_post_tax_eur" in cf.columns
        out = fin_plots.plot_cumulative_cashflow(
            cf, tmp_path / f"cum_{int(rate)}.pdf", econ=econ,
        )
        assert out.exists()
        labels = captured[str(tmp_path / f"cum_{int(rate)}.pdf")]
        assert (
            "Cumulative discounted cash-flow (post-tax)" in labels
        ) is expect_line, rate


def test_theme_registration():
    from pvbess_opt.theme import (
        FINANCIAL_LABELS,
        FINANCIAL_LEGEND_ORDER,
        financial_color,
    )
    assert "Cumulative discounted cash-flow (post-tax)" in FINANCIAL_LABELS
    assert (
        "Cumulative discounted cash-flow (post-tax)"
        in FINANCIAL_LEGEND_ORDER
    )
    assert financial_color(
        "Cumulative discounted cash-flow (post-tax)",
    ) == "#5C6BC0"


def test_workbook_key_round_trip(tmp_path):
    from pvbess_opt.io import (
        BALANCING_SHEET_DEFAULTS,
        BESS_SHEET_DEFAULTS,
        ECONOMICS_SHEET_DEFAULTS,
        PROJECT_SHEET_DEFAULTS,
        PV_SHEET_DEFAULTS,
        SIMULATION_SHEET_DEFAULTS,
        read_workbook,
        write_workbook,
    )
    n = 24
    typed = {
        "ts": pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
            "pv_kwh": np.full(n, 100.0),
            "dam_price_eur_per_mwh": np.full(n, 60.0),
        }),
        "project": dict(PROJECT_SHEET_DEFAULTS, mode="merchant"),
        "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=1000.0),
        "bess": dict(BESS_SHEET_DEFAULTS),
        "economics": dict(
            ECONOMICS_SHEET_DEFAULTS,
            corporate_tax_rate_pct=22.0,
            sensitivity_tax_rate_delta_pp=3.0,
        ),
        "simulation": dict(SIMULATION_SHEET_DEFAULTS),
        "balancing": dict(BALANCING_SHEET_DEFAULTS),
    }
    path = tmp_path / "wb.xlsx"
    write_workbook(typed, path)
    back = read_workbook(path)
    assert back["economics"]["sensitivity_tax_rate_delta_pp"] == 3.0
