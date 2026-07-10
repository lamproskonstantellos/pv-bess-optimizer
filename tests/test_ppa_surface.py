"""PPA workbook surface, plot reconciliation, and regression locks.

Locks the Phase-9 wiring of the PPA engine (tests/test_ppa_engine.py
covers the engine itself):

* the ``ppa`` sheet round-trips through Excel and YAML, validates its
  knobs, rejects an ENABLED baseload structure with guidance, and the
  scenarios engine accepts ``ppa.*`` dotted targets;
* ``ppa_enabled = FALSE`` leaves the dispatch frame, KPI dict, and
  cashflow numerically identical to a build without the feature;
* the revenue-stack PPA bar equals the cashflow column (which the KPI
  feed built), and the merchant revenue views render the PPA series —
  including the previously-dropped NEGATIVE stacks (the keep-filter
  tested the signed sum, so the grid-charging cost and any CfD leg
  vanished from the daily/monthly/yearly revenue views);
* the PPA-price tornado driver appears only for an enabled contract
  and moves the NPV in the right direction;
* SUMMARY.md carries the lifetime PPA row only when the stream is
  non-zero.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import build_yearly_cashflow, compute_financial_kpis
from pvbess_opt.io import (
    BALANCING_SHEET_DEFAULTS,
    BESS_SHEET_DEFAULTS,
    ECONOMICS_SHEET_DEFAULTS,
    PPA_SHEET_DEFAULTS,
    PROJECT_SHEET_DEFAULTS,
    PV_SHEET_DEFAULTS,
    SIMULATION_SHEET_DEFAULTS,
    read_inputs,
    read_workbook,
    validate_workbook_params,
    write_summary_md,
    write_workbook,
)
from pvbess_opt.io_read import dump_structured_config, load_structured_config

ROOT = Path(__file__).resolve().parent.parent


def _ts(n: int = 24) -> pd.DataFrame:
    h = np.arange(n) % 24
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=n, freq="h"),
        "pv_kwh": np.clip(4000 * np.sin(np.pi * (h - 6) / 12.0), 0, None)
        * ((h >= 6) & (h <= 18)),
        "load_kwh": np.full(n, 500.0),
        "dam_price_eur_per_mwh": np.full(n, 80.0),
    })


def _typed(ppa_overrides: dict | None = None) -> dict:
    return {
        "ts": _ts(),
        "project": dict(PROJECT_SHEET_DEFAULTS),
        "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=4500.0),
        "bess": dict(
            BESS_SHEET_DEFAULTS, bess_power_kw=1000.0,
            bess_capacity_kwh=4000.0,
        ),
        "economics": dict(ECONOMICS_SHEET_DEFAULTS),
        "simulation": dict(SIMULATION_SHEET_DEFAULTS),
        "balancing": dict(BALANCING_SHEET_DEFAULTS),
        "ppa": dict(PPA_SHEET_DEFAULTS, **(ppa_overrides or {})),
        "max_injection_profile": np.full(24, 100.0),
    }


# ---------------------------------------------------------------------------
# Workbook / YAML surface
# ---------------------------------------------------------------------------


def test_shipped_workbook_carries_disabled_ppa_sheet():
    repo_xlsx = ROOT / "inputs" / "input.xlsx"
    typed = read_workbook(repo_xlsx)
    assert typed["ppa"]["ppa_enabled"] is False
    assert typed["ppa"]["ppa_structure"] == "pay_as_produced"


def test_ppa_sheet_round_trips_workbook_and_yaml(tmp_path):
    overrides = {
        "ppa_enabled": True,
        "ppa_settlement": "cfd",
        "ppa_price_eur_per_mwh": 72.5,
        "ppa_volume_share_pct": 60.0,
        "ppa_term_years": 7,
        "ppa_inflation_pct": 1.5,
    }
    xlsx = tmp_path / "ppa.xlsx"
    write_workbook(_typed(overrides), xlsx)
    back = read_workbook(xlsx)
    for key, value in overrides.items():
        assert back["ppa"][key] == value, key

    params, _ts_loaded = read_inputs(xlsx)
    assert params["ppa"]["ppa_settlement"] == "cfd"
    assert params["ppa"]["ppa_volume_share_pct"] == 60.0

    cfg = tmp_path / "ppa.yaml"
    dump_structured_config(_typed(overrides), cfg)
    loaded = load_structured_config(cfg)
    for key, value in overrides.items():
        assert loaded["ppa"][key] == value, key


def test_enabled_baseload_requires_band_and_cfd():
    # The baseload structure is live (Eqs. P9-P11): a zero band is
    # rejected, and so is physical settlement (cfd-only in v1 - the
    # totals are identical under symmetric spot settlement, so only
    # the deferred flow attribution would differ).
    typed = _typed({"ppa_enabled": True, "ppa_structure": "baseload"})
    with pytest.raises(ValueError, match="ppa_baseload_mw"):
        validate_workbook_params(typed, dt_minutes=60)
    typed = _typed({
        "ppa_enabled": True, "ppa_structure": "baseload",
        "ppa_baseload_mw": 2.0, "ppa_settlement": "physical",
    })
    with pytest.raises(ValueError, match="cfd"):
        validate_workbook_params(typed, dt_minutes=60)
    typed = _typed({
        "ppa_enabled": True, "ppa_structure": "baseload",
        "ppa_baseload_mw": 2.0, "ppa_settlement": "cfd",
    })
    validate_workbook_params(typed, dt_minutes=60)


@pytest.mark.parametrize("key,value,match", [
    ("ppa_volume_share_pct", 150.0, "ppa_volume_share_pct"),
    ("ppa_price_eur_per_mwh", -5.0, "ppa_price_eur_per_mwh"),
    ("ppa_term_years", 0, "ppa_term_years"),
])
def test_enabled_contract_validates_knobs(key, value, match):
    typed = _typed({"ppa_enabled": True, key: value})
    with pytest.raises(ValueError, match=match):
        validate_workbook_params(typed, dt_minutes=60)


def test_disabled_contract_skips_validation():
    typed = _typed({"ppa_enabled": False, "ppa_term_years": 0})
    validate_workbook_params(typed, dt_minutes=60)  # must not raise


def test_scenarios_accept_dotted_ppa_targets():
    from pvbess_opt.scenarios import _apply_scenario_overrides

    base = _typed()
    out = _apply_scenario_overrides(
        base,
        {"name": "ppa-on", "ppa": {"ppa_enabled": True,
                                   "ppa_volume_share_pct": 80}},
    )
    assert out["ppa"]["ppa_enabled"] is True
    assert out["ppa"]["ppa_volume_share_pct"] == 80
    # The base typed dict is untouched (deep copy).
    assert base["ppa"]["ppa_enabled"] is False


def test_scenarios_sheet_parses_dotted_ppa_rows():
    from pvbess_opt.scenarios import _parse_scenarios_sheet

    df = pd.DataFrame({
        "enabled": ["TRUE", None],
        "name": ["With PPA", None],
        "inherits": [None, None],
        "target": ["ppa.ppa_enabled", "ppa.ppa_volume_share_pct"],
        "value": ["TRUE", 80],
    })
    enabled, scenarios = _parse_scenarios_sheet(df)
    assert enabled
    assert scenarios[0]["ppa"] == {
        "ppa_enabled": "TRUE", "ppa_volume_share_pct": 80,
    }


# ---------------------------------------------------------------------------
# Regression: disabled contract == pre-PPA outputs
# ---------------------------------------------------------------------------


def test_disabled_ppa_run_is_numerically_identical(tmp_path):
    """A workbook with the (disabled) ppa sheet produces the same
    dispatch frame, KPI dict, and cashflow as one without the sheet."""
    from pvbess_opt.economics import read_economic_params
    from pvbess_opt.kpis import compute_kpis
    from pvbess_opt.optimization import run_scenario

    with_sheet = tmp_path / "with.xlsx"
    write_workbook(_typed(), with_sheet)

    # Drop the ppa sheet entirely for the "pre-feature" workbook.
    import openpyxl

    without_sheet = tmp_path / "without.xlsx"
    write_workbook(_typed(), without_sheet)
    wb = openpyxl.load_workbook(without_sheet)
    del wb["ppa"]
    wb.save(without_sheet)

    solver_kw = {
        "solver_name": "highs", "mip_gap": 0.0, "time_limit_seconds": 120,
    }
    pa, tsa = read_inputs(with_sheet)
    pb, tsb = read_inputs(without_sheet)
    ra, _ = run_scenario(pa, tsa, **solver_kw)
    rb, _ = run_scenario(pb, tsb, **solver_kw)
    pd.testing.assert_frame_equal(ra, rb)

    ka = compute_kpis(ra, pa, verify_balance=False)
    kb = compute_kpis(rb, pb, verify_balance=False)
    assert ka == kb

    ea = read_economic_params(with_sheet)
    eb = read_economic_params(without_sheet)
    caps = {"pv_kwp": 4500.0, "bess_kw": 1000.0, "bess_kwh": 4000.0}
    cfa = build_yearly_cashflow(ka, ea, caps)
    cfb = build_yearly_cashflow(kb, eb, caps)
    pd.testing.assert_frame_equal(cfa, cfb)
    assert float(cfa["ppa_revenue_eur"].abs().sum()) == 0.0


# ---------------------------------------------------------------------------
# Plot reconciliation + negative-stack rendering
# ---------------------------------------------------------------------------


def _ppa_cashflow_and_kpis(settlement: str = "physical"):
    econ: dict = {}
    for defaults in (
        PROJECT_SHEET_DEFAULTS, PV_SHEET_DEFAULTS, BESS_SHEET_DEFAULTS,
        ECONOMICS_SHEET_DEFAULTS, PPA_SHEET_DEFAULTS,
    ):
        econ.update(defaults)
    econ.update({
        "project_lifecycle_years": 6,
        "project_start_year": 2026,
        "pv_degradation_year1_pct": 0.0,
        "pv_degradation_annual_pct": 0.0,
        "bess_degradation_annual_pct": 0.0,
        "bess_degradation_pct_per_cycle": 0.0,
        "bess_replacement_year": 0,
        "aggregator_fee_pct_revenue": 0.0,
        "ppa_enabled": True,
        "ppa_settlement": settlement,
        "ppa_term_years": 4,
        "ppa_inflation_pct": 0.0,
    })
    kpis = {
        "profit_total_eur": 900_000.0,
        "profit_load_from_pv_eur": 300_000.0,
        "profit_load_from_bess_eur": 100_000.0,
        "profit_export_from_pv_eur": 200_000.0,
        "profit_export_from_bess_eur": 100_000.0,
        "expense_charge_bess_grid_eur": 0.0,
        "revenue_pv_ppa_eur": 200_000.0,
        "ppa_covered_dam_value_eur": 150_000.0,
        "bess_total_discharge_mwh": 1_000.0,
        "revenue_bess_dam_eur": 100_000.0,
        "revenue_bess_fcr_eur": 0.0,
        "revenue_bess_afrr_up_eur": 0.0,
        "revenue_bess_afrr_dn_eur": 0.0,
        "revenue_bess_mfrr_up_eur": 0.0,
        "revenue_bess_mfrr_dn_eur": 0.0,
    }
    caps = {"pv_kwp": 4500.0, "bess_kw": 1000.0, "bess_kwh": 4000.0}
    cf = build_yearly_cashflow(kpis, econ, caps)
    return cf, kpis, econ


def test_revenue_stack_ppa_bar_equals_cashflow_column(tmp_path):
    from pvbess_opt.plotting import lifecycle as lifecycle_mod
    from pvbess_opt.plotting.lifecycle import plot_revenue_stack_yearly

    cf, kpis, econ = _ppa_cashflow_and_kpis()
    plt.close("all")
    captured: dict = {}
    original = lifecycle_mod.save_figure

    def _keep(out):
        captured["fig"] = plt.gcf()
        return out

    lifecycle_mod.save_figure = _keep
    try:
        plot_revenue_stack_yearly(cf, kpis, tmp_path / "stack.pdf", econ=econ)
    finally:
        lifecycle_mod.save_figure = original

    ax = captured["fig"].axes[0]
    ppa_bars = [c for c in ax.containers if c.get_label() == "PPA revenue"]
    assert ppa_bars, "PPA revenue bar missing from the revenue stack"
    heights = np.array([p.get_height() for p in ppa_bars[0].patches])
    expected = cf.loc[cf["project_year"] >= 1, "ppa_revenue_eur"].to_numpy()
    np.testing.assert_allclose(heights, np.clip(expected, 0.0, None), atol=0.01)
    # In-term years carry the leg; post-term years are zero.
    assert heights[0] == pytest.approx(200_000.0)
    assert heights[4] == pytest.approx(0.0)


def test_negative_stacks_render_in_revenue_views(tmp_path):
    """The signed-sum keep-filter dropped every negative stack: with
    grid charging on, the daily/monthly revenue views showed no
     'Grid-charging cost' at all, and a CfD PPA leg would vanish the
    same way.  Both render now, with one deduped legend entry."""
    import pvbess_opt.plotting.daily as daily_mod
    import pvbess_opt.plotting.monthly as monthly_mod

    n = 96
    res = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=n, freq="h"),
        "profit_export_from_pv_eur": np.full(n, 2.0),
        "profit_export_from_bess_eur": np.full(n, 1.0),
        "expense_charge_bess_grid_eur": np.full(n, 0.7),
        # CfD leg negative throughout (DAM above strike).
        "revenue_pv_ppa_eur": np.full(n, -0.4),
    })

    plt.close("all")
    captured: dict = {}
    original = monthly_mod.save_figure
    monthly_mod.save_figure = lambda out: captured.update(fig=plt.gcf()) or out
    try:
        monthly_mod.plot_monthly_revenue(res, 6, tmp_path)
    finally:
        monthly_mod.save_figure = original
    ax = captured["fig"].axes[0]
    labels = [c.get_label() for c in ax.containers]
    assert "Grid-charging cost" in labels
    assert "PPA revenue" in labels
    legend_labels = [t.get_text() for t in ax.get_legend().get_texts()]
    assert legend_labels.count("PPA revenue") == 1
    assert "Grid-charging cost" in legend_labels

    plt.close("all")
    captured.clear()
    original = daily_mod.save_figure_daily
    daily_mod.save_figure_daily = (
        lambda out, _ds: captured.update(fig=plt.gcf()) or out
    )
    try:
        daily_mod.plot_daily_revenue(res, "2026-06-01", tmp_path)
    finally:
        daily_mod.save_figure_daily = original
    ax = captured["fig"].axes[0]
    stack_labels = [c.get_label() for c in ax.collections]
    assert "Grid-charging cost" in stack_labels
    assert "PPA revenue" in stack_labels


# ---------------------------------------------------------------------------
# Tornado driver + SUMMARY row
# ---------------------------------------------------------------------------


def test_ppa_price_tornado_driver_present_and_monotonic():
    from pvbess_opt.sensitivity import run_sensitivity_analysis

    cf, kpis, econ = _ppa_cashflow_and_kpis()
    caps = {"pv_kwp": 4500.0, "bess_kw": 1000.0, "bess_kwh": 4000.0}
    fin = compute_financial_kpis(cf, econ, capacities=caps, year1_kpis=kpis)
    sens = run_sensitivity_analysis(kpis, econ, caps, fin)
    rows = sens[sens["variable"] == "PpaPrice"].set_index("scenario")
    assert {"base", "low", "high"} <= set(rows.index)
    assert float(rows.loc["base", "value"]) == pytest.approx(
        float(econ["ppa_price_eur_per_mwh"]),
    )
    # A higher strike strictly raises the NPV; lower strictly lowers it.
    assert float(rows.loc["high", "npv_eur"]) > float(fin["npv_eur"])
    assert float(rows.loc["low", "npv_eur"]) < float(fin["npv_eur"])


def test_ppa_price_driver_absent_when_disabled():
    from pvbess_opt.sensitivity import variables_for_npv_sensitivity

    names = {v["name"] for v in variables_for_npv_sensitivity(
        dict(ECONOMICS_SHEET_DEFAULTS),
    )}
    assert "PpaPrice" not in names
    names_on = {v["name"] for v in variables_for_npv_sensitivity(
        dict(ECONOMICS_SHEET_DEFAULTS, ppa_enabled=True),
    )}
    assert "PpaPrice" in names_on


def test_summary_md_ppa_row_only_when_nonzero(tmp_path):
    params = {
        "mode": "merchant", "pv_nameplate_kwp": 4500.0,
        "bess_power_kw": 1000.0, "bess_capacity_kwh": 4000.0,
    }
    base_fin = {"npv_eur": 1.0, "lifetime_ppa_revenue_total_eur": 0.0}
    out = write_summary_md(
        tmp_path / "off.md", kpis_year1={}, financial_kpis=base_fin,
        params=params,
    )
    assert "Lifetime PPA revenue" not in out.read_text()

    on_fin = {"npv_eur": 1.0, "lifetime_ppa_revenue_total_eur": 123_456.0}
    out = write_summary_md(
        tmp_path / "on.md", kpis_year1={}, financial_kpis=on_fin,
        params=params,
    )
    assert "| Lifetime PPA revenue [EUR] | 123,456 |" in out.read_text()
