"""Site-wide lump-sum CAPEX / DEVEX feature.

``site_capex_eur`` / ``site_devex_eur`` are absolute-EUR project-sheet
keys for items that are not naturally per-kWp or per-kW (substation,
grid upgrades, interconnection, environmental studies, ...).  Both are
paid in Year 0, fold into the Year-0 ``capex_eur`` / ``devex_eur`` rows,
flow through NPV / IRR / ROI / BCR / payback, and are excluded from
LCOE / LCOS (Lazard convention).
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import (
    build_yearly_cashflow,
    compute_financial_kpis,
)
from pvbess_opt.io import (
    BESS_SHEET_DEFAULTS,
    ECONOMICS_SHEET_DEFAULTS,
    PROJECT_SHEET_DEFAULTS,
    PV_SHEET_DEFAULTS,
    SIMULATION_SHEET_DEFAULTS,
    read_workbook,
    write_workbook,
)

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _econ(**overrides) -> dict:
    out: dict = {}
    for d in (
        PROJECT_SHEET_DEFAULTS, PV_SHEET_DEFAULTS, BESS_SHEET_DEFAULTS,
        ECONOMICS_SHEET_DEFAULTS, SIMULATION_SHEET_DEFAULTS,
    ):
        out.update(d)
    out.update(overrides)
    return out


def _caps() -> dict:
    return {"pv_kwp": 4500.0, "bess_kw": 5000.0, "bess_kwh": 20000.0}


def _kpis(profit: float = 900_000.0) -> dict:
    return {
        "profit_total_eur": profit,
        "pv_generation_mwh": 4500.0 * 1.5,
        "bess_total_discharge_mwh": 1000.0,
    }


def _lifetime_yearly(econ: dict) -> pd.DataFrame:
    n = int(econ["project_lifecycle_years"])
    return pd.DataFrame({
        "project_year": list(range(1, n + 1)),
        "pv_generation_mwh": [6750.0] * n,
        "bess_discharge_mwh": [1000.0] * n,
    })


def _year0(df: pd.DataFrame, col: str) -> float:
    return float(df.loc[df["project_year"] == 0, col].iloc[0])


def _minimal_typed() -> dict:
    n = 24
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "pv_kwh": [100.0] * n,
        "load_kwh": [50.0] * n,
        "dam_price_eur_per_mwh": [80.0] * n,
    })
    return {
        "ts": ts,
        "project": dict(PROJECT_SHEET_DEFAULTS),
        "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=1000.0),
        "bess": dict(
            BESS_SHEET_DEFAULTS, bess_power_kw=500.0, bess_capacity_kwh=2000.0,
        ),
        "economics": dict(ECONOMICS_SHEET_DEFAULTS),
        "simulation": dict(SIMULATION_SHEET_DEFAULTS),
        "max_injection_profile": np.full(24, 73.0, dtype=float),
    }


# ---------------------------------------------------------------------------
# 1-2. Defaults
# ---------------------------------------------------------------------------


def test_site_capex_eur_default_is_zero(tmp_path):
    typed = _minimal_typed()
    # Drop the keys so the loader must apply the default.
    typed["project"].pop("site_capex_eur", None)
    dst = tmp_path / "no_site_capex.xlsx"
    write_workbook(typed, dst)
    out = read_workbook(dst)
    assert float(out["project"]["site_capex_eur"]) == 0.0


def test_site_devex_eur_default_is_zero(tmp_path):
    typed = _minimal_typed()
    typed["project"].pop("site_devex_eur", None)
    dst = tmp_path / "no_site_devex.xlsx"
    write_workbook(typed, dst)
    out = read_workbook(dst)
    assert float(out["project"]["site_devex_eur"]) == 0.0


# ---------------------------------------------------------------------------
# 3-4. Folded into Year 0
# ---------------------------------------------------------------------------


def test_site_capex_folded_into_year0():
    base = build_yearly_cashflow(_kpis(), _econ(), _caps())
    with_site = build_yearly_cashflow(
        _kpis(), _econ(site_capex_eur=500_000.0), _caps(),
    )
    delta = _year0(with_site, "capex_eur") - _year0(base, "capex_eur")
    assert delta == pytest.approx(-500_000.0, abs=1e-6)


def test_site_devex_folded_into_year0():
    base = build_yearly_cashflow(_kpis(), _econ(), _caps())
    with_site = build_yearly_cashflow(
        _kpis(), _econ(site_devex_eur=250_000.0), _caps(),
    )
    delta = _year0(with_site, "devex_eur") - _year0(base, "devex_eur")
    assert delta == pytest.approx(-250_000.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 5. Totals KPIs include the lump sum
# ---------------------------------------------------------------------------


def test_site_costs_appear_in_total_capex_kpi():
    econ = _econ(site_capex_eur=500_000.0, site_devex_eur=250_000.0)
    base_fin = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), _econ(), _caps()), _econ(),
    )
    fin = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), econ, _caps()), econ,
    )
    assert fin["total_capex_eur"] == pytest.approx(
        base_fin["total_capex_eur"] - 500_000.0, abs=1e-2,
    )
    assert fin["total_devex_eur"] == pytest.approx(
        base_fin["total_devex_eur"] - 250_000.0, abs=1e-2,
    )


# ---------------------------------------------------------------------------
# 6-7. NPV / IRR sensitivity to the lump sum
# ---------------------------------------------------------------------------


def test_npv_decreases_by_lump_sum():
    # Zero discount rate: a Year-0 outflow of X drops NPV by exactly X.
    econ0 = _econ(discount_rate_pct=0.0)
    econX = _econ(discount_rate_pct=0.0, site_capex_eur=1_000_000.0)
    npv0 = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), econ0, _caps()), econ0,
    )["npv_eur"]
    npvX = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), econX, _caps()), econX,
    )["npv_eur"]
    assert (npvX - npv0) == pytest.approx(-1_000_000.0, abs=1.0)


def test_irr_decreases_when_site_capex_rises():
    irrs = []
    for site in (0.0, 500_000.0, 1_500_000.0):
        econ = _econ(site_capex_eur=site)
        fin = compute_financial_kpis(
            build_yearly_cashflow(_kpis(), econ, _caps()), econ,
        )
        irrs.append(float(fin["irr_pct"]))
    assert irrs[0] > irrs[1] > irrs[2]


# ---------------------------------------------------------------------------
# 8-9. LCOE / LCOS are unaffected (Lazard convention)
# ---------------------------------------------------------------------------


def test_lcoe_unchanged_by_site_capex():
    caps = _caps()
    econ0 = _econ()
    econX = _econ(site_capex_eur=1_000_000.0, site_devex_eur=500_000.0)
    fin0 = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), econ0, caps), econ0,
        capacities=caps, lifetime_yearly=_lifetime_yearly(econ0),
        year1_kpis=_kpis(),
    )
    finX = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), econX, caps), econX,
        capacities=caps, lifetime_yearly=_lifetime_yearly(econX),
        year1_kpis=_kpis(),
    )
    assert finX["lcoe_eur_per_mwh"] == pytest.approx(
        fin0["lcoe_eur_per_mwh"], rel=1e-12, abs=1e-9,
    )


def test_lcos_unchanged_by_site_capex():
    caps = _caps()
    econ0 = _econ()
    econX = _econ(site_capex_eur=1_000_000.0, site_devex_eur=500_000.0)
    fin0 = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), econ0, caps), econ0,
        capacities=caps, lifetime_yearly=_lifetime_yearly(econ0),
        year1_kpis=_kpis(),
    )
    finX = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), econX, caps), econX,
        capacities=caps, lifetime_yearly=_lifetime_yearly(econX),
        year1_kpis=_kpis(),
    )
    assert finX["lcos_eur_per_mwh"] == pytest.approx(
        fin0["lcos_eur_per_mwh"], rel=1e-12, abs=1e-9,
    )


# ---------------------------------------------------------------------------
# 10. Workbook round-trip
# ---------------------------------------------------------------------------


def test_workbook_roundtrip_preserves_site_costs(tmp_path):
    typed = _minimal_typed()
    typed["project"]["site_capex_eur"] = 750_000.0
    typed["project"]["site_devex_eur"] = 125_000.0
    dst = tmp_path / "site.xlsx"
    write_workbook(typed, dst)
    out = read_workbook(dst)
    assert float(out["project"]["site_capex_eur"]) == 750_000.0
    assert float(out["project"]["site_devex_eur"]) == 125_000.0


# ---------------------------------------------------------------------------
# 11. CAPEX sensitivity scales the lump sum too
# ---------------------------------------------------------------------------


def test_sensitivity_capex_includes_lump_sum():
    from pvbess_opt.sensitivity import run_sensitivity_analysis

    econ = _econ(
        site_capex_eur=1_000_000.0,
        sensitivity_capex_delta_pct=10.0,
    )
    base_fin = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), econ, _caps()), econ,
    )
    sens = run_sensitivity_analysis(_kpis(), econ, _caps(), base_fin)
    capex_rows = sens[sens["variable"] == "CAPEX"]
    base_value = float(
        capex_rows.loc[capex_rows["scenario"] == "base", "value"].iloc[0]
    )
    high_value = float(
        capex_rows.loc[capex_rows["scenario"] == "high", "value"].iloc[0]
    )

    # The base CAPEX driver must include the site lump sum.
    econ_no_site = _econ(sensitivity_capex_delta_pct=10.0)
    base_fin_no = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), econ_no_site, _caps()), econ_no_site,
    )
    sens_no = run_sensitivity_analysis(
        _kpis(), econ_no_site, _caps(), base_fin_no,
    )
    base_value_no = float(
        sens_no.loc[
            (sens_no["variable"] == "CAPEX") & (sens_no["scenario"] == "base"),
            "value",
        ].iloc[0]
    )
    # Driver is the (negative) total Year-0 outlay; adding 1.0M makes it
    # 1.0M more negative.
    assert (base_value - base_value_no) == pytest.approx(-1_000_000.0, abs=1.0)
    # +10 % scenario scales the whole outlay, lump sum included.
    assert high_value == pytest.approx(base_value * 1.10, rel=1e-9)


# ---------------------------------------------------------------------------
# 12. Assumptions summary
# ---------------------------------------------------------------------------


def test_assumptions_summary_lists_site_costs(tmp_path):
    from pvbess_opt.io import write_assumptions_summary

    params = {"mode": "self_consumption", "site_capex_eur": 500_000.0,
              "site_devex_eur": 250_000.0}
    econ = _econ(site_capex_eur=500_000.0, site_devex_eur=250_000.0)
    out = tmp_path / "assumptions.txt"
    write_assumptions_summary(out, params, econ)
    text = out.read_text(encoding="utf-8")
    assert "site_capex_eur" in text
    assert "site_devex_eur" in text


# ---------------------------------------------------------------------------
# 13-14. Plots render with a non-zero lump sum
# ---------------------------------------------------------------------------


def test_yearly_cashflow_bars_renders_with_lump_sum(tmp_path):
    from pvbess_opt.plotting.financial import plot_yearly_cashflow_bars

    econ = _econ(site_capex_eur=1_000_000.0, site_devex_eur=500_000.0)
    yearly_cf = build_yearly_cashflow(_kpis(), econ, _caps())
    out = plot_yearly_cashflow_bars(yearly_cf, tmp_path / "bars.pdf", econ=econ)
    assert out.exists()


def test_npv_waterfall_renders_with_lump_sum(tmp_path):
    from pvbess_opt.plotting.financial import plot_npv_waterfall

    econ = _econ(site_capex_eur=1_000_000.0, site_devex_eur=500_000.0)
    yearly_cf = build_yearly_cashflow(_kpis(), econ, _caps())
    out = plot_npv_waterfall(yearly_cf, tmp_path / "waterfall.pdf", econ=econ)
    assert out.exists()


# ---------------------------------------------------------------------------
# 15. Headline-KPI regression guard: defaults (lump sums 0) leave the
#     pipeline bit-identical to a no-lump-sum run.
# ---------------------------------------------------------------------------


def test_default_lump_sums_leave_kpis_identical():
    caps = _caps()
    econ_default = _econ()  # site_capex_eur / site_devex_eur both 0.0
    # An econ dict that omits the keys entirely (older-workbook shape).
    econ_absent = {k: v for k, v in econ_default.items()
                   if k not in ("site_capex_eur", "site_devex_eur")}
    fin_default = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), econ_default, caps), econ_default,
        capacities=caps, lifetime_yearly=_lifetime_yearly(econ_default),
        year1_kpis=_kpis(),
    )
    fin_absent = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), econ_absent, caps), econ_absent,
        capacities=caps, lifetime_yearly=_lifetime_yearly(econ_absent),
        year1_kpis=_kpis(),
    )
    for key, expected in fin_absent.items():
        actual = fin_default[key]
        if isinstance(expected, float) and not isinstance(expected, bool):
            if np.isnan(expected):
                assert np.isnan(actual), key
            else:
                assert actual == pytest.approx(expected, rel=1e-9, abs=1e-6), key
        else:
            assert actual == expected, key
