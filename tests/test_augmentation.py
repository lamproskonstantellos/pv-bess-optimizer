"""BESS augmentation + day-1 DC overbuild (Eqs. E50-E52).

The pooled capacity engine tracks each installed pool on its own
calendar-plus-cycle fade curve and clamps the plant factor at
nameplate (E50); augmentation events buy the added energy on the
declining cost curve (E51); the overbuild premium loads Year-0 CAPEX
while dispatch stays on nameplate (E52).  Locked here: engine
delegation / closed forms, the cashflow column + Year-0 scaling, the
LCOS numerator membership, monthly reconciliation on event years,
sensitivity-driver membership, theme registration, and the loader's
range / mode / replacement-exclusivity validation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.degradation import build_degradation_report
from pvbess_opt.economics import (
    build_yearly_cashflow,
    compute_financial_kpis,
    derive_monthly_cashflow,
)
from pvbess_opt.lifetime import (
    bess_capacity_factors,
    bess_capacity_factors_pooled,
)


def _econ(**overrides) -> dict:
    econ = {
        "project_lifecycle_years": 12,
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
        "bess_degradation_annual_pct": 3.0,
        "bess_degradation_pct_per_cycle": 0.0,
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
        "pv_export_mwh": 100.0,
        "bess_export_mwh": 300.0,
        "bess_total_discharge_mwh": 500.0,
    }


# ---------------------------------------------------------------------------
# E50 — pooled capacity engine
# ---------------------------------------------------------------------------


def test_pooled_engine_delegates_when_inactive():
    kw = dict(
        d_bess_annual=0.02, d_bess_per_cycle=0.00008,
        year1_discharge_mwh=3_000.0, capacity_mwh=8.0,
    )
    for repl in (0, 6):
        plain = bess_capacity_factors(20, **kw, replacement_year=repl)
        pooled, added = bess_capacity_factors_pooled(
            20, **kw, replacement_year=repl,
        )
        assert pooled == plain
        assert added == {}


def test_pooled_engine_rejects_replacement_combination():
    with pytest.raises(ValueError, match="replacement"):
        bess_capacity_factors_pooled(
            10, d_bess_annual=0.02, capacity_mwh=1.0,
            replacement_year=5, augmentation_years=(8,),
        )


def test_top_up_restores_nameplate_and_matches_closed_form():
    d = 0.03
    factors, added = bess_capacity_factors_pooled(
        12, d_bess_annual=d, year1_discharge_mwh=500.0,
        capacity_mwh=1.0, augmentation_years=(8,),
    )
    # Event year returns exactly to nameplate (E50 clamp = pool sum).
    assert factors[7] == pytest.approx(1.0, abs=1e-12)
    # Added energy tops up the calendar-faded day-1 pool.
    assert added[8] == pytest.approx(1.0 - (1.0 - d) ** 7, rel=1e-12)
    # Calendar-only fade has a closed form per pool: the plant factor
    # is the nameplate-clamped sum of the two pools' calendar curves.
    for y in range(9, 13):
        expected = min(
            1.0,
            (1.0 - d) ** (y - 1) + added[8] * (1.0 - d) ** (y - 8),
        )
        assert factors[y - 1] == pytest.approx(expected, rel=1e-12)


def test_fixed_kwh_mode_adds_the_configured_energy():
    d = 0.03
    factors, added = bess_capacity_factors_pooled(
        12, d_bess_annual=d, capacity_mwh=1.0,
        augmentation_years=(5,), augmentation_mode="fixed_kwh",
        augmentation_kwh=200.0,
    )
    assert added == {5: pytest.approx(0.2)}
    expected_y5 = min(1.0, (1.0 - d) ** 4 + 0.2)
    assert factors[4] == pytest.approx(expected_y5, rel=1e-12)


def test_overbuild_clamps_at_nameplate_until_margin_consumed():
    d = 0.03
    factors, added = bess_capacity_factors_pooled(
        12, d_bess_annual=d, capacity_mwh=1.0, overbuild_frac=0.10,
    )
    assert added == {}
    plain = bess_capacity_factors(
        12, d_bess_annual=d, capacity_mwh=1.0, year1_discharge_mwh=0.0,
    )
    for y in range(1, 13):
        expected = min(1.0, 1.10 * (1.0 - d) ** (y - 1))
        assert factors[y - 1] == pytest.approx(expected, rel=1e-12)
        assert factors[y - 1] >= plain[y - 1] - 1e-12


def test_pro_rata_cycle_apportionment_conserves_plant_throughput():
    """With cycle fade on, the pooled curve still matches a direct
    per-pool simulation — the pro-rata weights sum to one so the plant
    throughput is fully apportioned (no cycles lost or double-counted).
    """
    d_cal, d_cyc = 0.02, 0.0001
    n, e_n, d1 = 10, 2.0, 800.0
    factors, added = bess_capacity_factors_pooled(
        n, d_bess_annual=d_cal, d_bess_per_cycle=d_cyc,
        year1_discharge_mwh=d1, capacity_mwh=e_n,
        augmentation_years=(6,),
    )
    # Reference simulation with explicit pools.
    pools = [{"a": 1, "e": e_n, "k": 0.0}]
    ref = []
    for y in range(1, n + 1):
        if y == 6:
            surv = sum(
                p["e"] * max(
                    0.0, (1 - d_cal) ** (y - p["a"]) - d_cyc * p["k"],
                ) for p in pools
            )
            pools.append({"a": 6, "e": max(0.0, e_n - surv), "k": 0.0})
        phis = [
            max(0.0, (1 - d_cal) ** (y - p["a"]) - d_cyc * p["k"])
            for p in pools
        ]
        avail = sum(p["e"] * phi for p, phi in zip(pools, phis, strict=True))
        f = min(1.0, avail / e_n)
        for p, phi in zip(pools, phis, strict=True):
            if avail > 0 and p["e"] > 0:
                p["k"] += d1 * f * (p["e"] * phi / avail) / p["e"]
        ref.append(f)
    assert factors == pytest.approx(ref, rel=1e-12)
    assert added[6] == pytest.approx(pools[1]["e"], rel=1e-12)


# ---------------------------------------------------------------------------
# E51/E52 — cashflow column, Year-0 scaling, KPIs, LCOS
# ---------------------------------------------------------------------------


def test_zero_default_is_bit_identical():
    econ_absent = _econ()
    econ_defaults = _econ(
        bess_overbuild_pct=0.0,
        bess_augmentation_years=None,
        bess_augmentation_mode="top_up",
        bess_augmentation_kwh=0.0,
        bess_cost_decline_pct_per_year=0.0,
    )
    cf_a = build_yearly_cashflow(_kpis(), econ_absent, _caps())
    cf_b = build_yearly_cashflow(_kpis(), econ_defaults, _caps())
    pd.testing.assert_frame_equal(cf_a, cf_b)
    assert (cf_a["augmentation_capex_eur"] == 0.0).all()
    kpi_a = compute_financial_kpis(cf_a, econ_absent)
    assert kpi_a["total_augmentation_capex_eur_lifecycle"] == 0.0
    # Degradation report: no augmentation column when events are off.
    soc = np.tile([0.0, 500.0, 1000.0, 500.0], 24)
    rep = build_degradation_report(
        soc, capacity_kwh=1000.0, soc_min_frac=0.0, soc_max_frac=1.0,
        degradation_pct_per_cycle=0.0, project_years=12, start_year=2026,
        degradation_annual_pct=3.0,
    )
    assert "augmentation_added_kwh" not in rep.columns


def test_event_capex_matches_e51_closed_form():
    d_cal = 0.03
    for decline in (0.0, 5.0):
        econ = _econ(
            bess_augmentation_years="8",
            bess_cost_decline_pct_per_year=decline,
        )
        cf = build_yearly_cashflow(_kpis(), econ, _caps())
        added_kwh = 1000.0 * (1.0 - (1.0 - d_cal) ** 7)
        expected = -added_kwh * 100.0 * (1.0 - decline / 100.0) ** 8
        got = float(cf.loc[
            cf["project_year"] == 8, "augmentation_capex_eur",
        ].iloc[0])
        assert got == pytest.approx(expected, abs=0.01), decline
        # Every non-event year stays exactly zero.
        others = cf.loc[cf["project_year"] != 8, "augmentation_capex_eur"]
        assert (others == 0.0).all()
        # The capacity factor returns to 1.0 in the event year.
        assert float(cf.loc[
            cf["project_year"] == 8, "bess_capacity_factor",
        ].iloc[0]) == pytest.approx(1.0)
        # net folds the event in (E51 is a net_cashflow component).
        base = build_yearly_cashflow(_kpis(), _econ(), _caps())
        assert float(cf.loc[cf["project_year"] == 8, "net_cashflow_eur"]
                     .iloc[0]) < float(
            base.loc[base["project_year"] == 8, "net_cashflow_eur"].iloc[0]
        )


def test_overbuild_scales_year0_capex_and_lcos():
    ob = 20.0
    econ = _econ(bess_overbuild_pct=ob, bess_augmentation_years="8")
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    base = build_yearly_cashflow(_kpis(), _econ(), _caps())
    y0 = float(cf.loc[cf["project_year"] == 0, "capex_eur"].iloc[0])
    y0_base = float(base.loc[base["project_year"] == 0, "capex_eur"].iloc[0])
    # Year-0 delta is exactly the overbuild premium on the BESS block.
    assert y0 - y0_base == pytest.approx(-0.20 * 100.0 * 1000.0)

    # LCOS numerator: overbuild premium at disc_y0 + discounted events.
    lifetime_yearly = pd.DataFrame({
        "project_year": list(range(1, 13)),
        "bess_discharge_mwh": [500.0] * 12,
        "pv_generation_mwh": [1500.0] * 12,
    })
    kpi = compute_financial_kpis(
        cf, econ, capacities=_caps(), lifetime_yearly=lifetime_yearly,
        year1_kpis=_kpis(),
    )
    disc = cf.set_index("project_year")["discount_factor"]
    aug_col = cf.set_index("project_year")["augmentation_capex_eur"]
    expected_capex = (
        100.0 * 1000.0 * 1.20 * float(disc.loc[0])
        + sum(
            -float(aug_col.loc[y]) * float(disc.loc[y])
            for y in range(1, 13) if abs(float(aug_col.loc[y])) > 0
        )
    )
    assert kpi["lcos_disc_bess_capex_eur"] == pytest.approx(
        expected_capex, rel=1e-9,
    )
    # Lifetime total mirrors the signed column sum.
    assert kpi["total_augmentation_capex_eur_lifecycle"] == pytest.approx(
        float(aug_col.loc[1:].sum()), abs=0.01,
    )


def test_monthly_reconciliation_books_event_in_month_12():
    econ = _econ(bess_augmentation_years="4,8")
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    n = 96
    res = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "savings_self_consumption_eur": np.full(n, 10.0),
        "profit_export_from_pv_eur": np.full(n, 5.0),
        "profit_export_from_bess_eur": np.full(n, 5.0),
        "expense_charge_bess_grid_eur": np.full(n, 1.0),
        "pv_kwh": np.full(n, 100.0),
    })
    monthly, quarterly = derive_monthly_cashflow(res, cf, econ)
    for y in range(1, 13):
        y_total = float(cf.loc[
            cf["project_year"] == y, "augmentation_capex_eur",
        ].iloc[0])
        rows = monthly.loc[monthly["project_year"] == y]
        assert float(rows["augmentation_capex_eur"].sum()) == pytest.approx(
            y_total, abs=1e-6,
        ), y
        # Investment-event convention: months 1-11 carry nothing.
        assert (rows.loc[rows["period"] != 12,
                         "augmentation_capex_eur"] == 0.0).all()
        # Monthly nets reconcile the yearly net exactly.
        y_net = float(cf.loc[
            cf["project_year"] == y, "net_cashflow_eur",
        ].iloc[0])
        assert float(rows["net_cashflow_eur"].sum()) == pytest.approx(
            y_net, abs=1e-6,
        ), y
    assert "augmentation_capex_eur" in quarterly.columns


def test_sensitivity_drivers_treat_it_as_unit_cost():
    from pvbess_opt.economics import TAX_LAYER_COLUMNS
    from pvbess_opt.sensitivity import (
        _recompute_net,
        _scale_capex,
        _scale_revenue,
    )

    econ = _econ(bess_augmentation_years="8")
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    aug_8 = float(cf.loc[
        cf["project_year"] == 8, "augmentation_capex_eur",
    ].iloc[0])
    assert aug_8 < 0.0
    # CAPEX driver scales the unit-cost-linked event.
    up = _scale_capex(cf, 1.1)
    assert float(up.loc[up["project_year"] == 8,
                        "augmentation_capex_eur"].iloc[0]) == pytest.approx(
        1.1 * aug_8,
    )
    # Revenue driver leaves the investment outflow untouched.
    rev = _scale_revenue(cf, 1.1, econ)
    assert float(rev.loc[rev["project_year"] == 8,
                         "augmentation_capex_eur"].iloc[0]) == pytest.approx(
        aug_8,
    )
    # The recomputed net matches the builder's on the unscaled frame.
    base = cf.drop(columns=list(TAX_LAYER_COLUMNS))
    recomputed = _recompute_net(base.copy())
    pd.testing.assert_series_equal(
        recomputed["net_cashflow_eur"], base["net_cashflow_eur"],
    )


def test_tax_layer_depreciates_events_like_replacement_tranches():
    econ = _econ(
        bess_augmentation_years="4",
        corporate_tax_rate_pct=25.0,
        depreciation_years_bess=5,
    )
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    econ_base = _econ(
        corporate_tax_rate_pct=25.0, depreciation_years_bess=5,
    )
    base = build_yearly_cashflow(_kpis(), econ_base, _caps())
    aug_4 = -float(cf.loc[
        cf["project_year"] == 4, "augmentation_capex_eur",
    ].iloc[0])
    # The tranche enters service in year 5 (month-12 booking convention)
    # and adds base/5 of straight-line depreciation per year.
    dep_5 = float(cf.loc[cf["project_year"] == 5, "depreciation_eur"].iloc[0])
    dep_5_base = float(base.loc[
        base["project_year"] == 5, "depreciation_eur",
    ].iloc[0])
    assert dep_5 - dep_5_base == pytest.approx(aug_4 / 5.0, rel=1e-9)
    dep_4 = float(cf.loc[cf["project_year"] == 4, "depreciation_eur"].iloc[0])
    dep_4_base = float(base.loc[
        base["project_year"] == 4, "depreciation_eur",
    ].iloc[0])
    assert dep_4 == pytest.approx(dep_4_base, rel=1e-12)


def test_degradation_report_carries_added_kwh():
    soc = np.tile([0.0, 500.0, 1000.0, 500.0], 24)
    rep = build_degradation_report(
        soc, capacity_kwh=1000.0, soc_min_frac=0.0, soc_max_frac=1.0,
        degradation_pct_per_cycle=0.0, project_years=12, start_year=2026,
        degradation_annual_pct=3.0, augmentation_years=(8,),
    )
    assert "augmentation_added_kwh" in rep.columns
    added = rep.set_index("project_year")["augmentation_added_kwh"]
    assert float(added.loc[8]) == pytest.approx(
        1000.0 * (1.0 - 0.97 ** 7), abs=0.01,
    )
    assert (added.drop(8) == 0.0).all()
    soh = rep.set_index("project_year")["soh_pct"]
    assert float(soh.loc[8]) == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Theme registration + validation surface
# ---------------------------------------------------------------------------


def test_theme_registration():
    from pvbess_opt.theme import (
        FINANCIAL_LABELS,
        FINANCIAL_LEGEND_ORDER,
        financial_color,
    )
    assert "Augmentation CAPEX" in FINANCIAL_LABELS
    assert "Augmentation CAPEX" in FINANCIAL_LEGEND_ORDER
    assert financial_color("Augmentation CAPEX") == "#E57373"


def test_workbook_validation(tmp_path):
    from pvbess_opt.io import (
        parse_augmentation_years,
        read_workbook,
        write_workbook,
    )

    # CSV parser surface.
    assert parse_augmentation_years(None) == ()
    assert parse_augmentation_years("") == ()
    assert parse_augmentation_years("8,15") == (8, 15)
    assert parse_augmentation_years("15, 8, 8") == (8, 15)
    assert parse_augmentation_years(8.0) == (8,)
    assert parse_augmentation_years([8, 15]) == (8, 15)
    with pytest.raises(ValueError, match="comma-separated"):
        parse_augmentation_years("8;15")
    with pytest.raises(ValueError, match="whole project years"):
        parse_augmentation_years("8.5")

    from pvbess_opt.io import (
        BALANCING_SHEET_DEFAULTS,
        BESS_SHEET_DEFAULTS,
        ECONOMICS_SHEET_DEFAULTS,
        PROJECT_SHEET_DEFAULTS,
        PV_SHEET_DEFAULTS,
        SIMULATION_SHEET_DEFAULTS,
        _typed_to_flat,
    )

    _counter = iter(range(100))

    def _write(**bess_overrides):
        path = tmp_path / f"wb{next(_counter)}.xlsx"
        n = 24
        typed = {
            "ts": pd.DataFrame({
                "timestamp": pd.date_range(
                    "2026-01-01", periods=n, freq="h",
                ),
                "pv_kwh": np.full(n, 100.0),
                "dam_price_eur_per_mwh": np.full(n, 60.0),
            }),
            "project": dict(
                PROJECT_SHEET_DEFAULTS, mode="merchant",
                project_lifecycle_years=12,
            ),
            "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=1000.0),
            "bess": dict(
                BESS_SHEET_DEFAULTS, bess_power_kw=500.0,
                bess_capacity_kwh=1000.0, **bess_overrides,
            ),
            "economics": dict(ECONOMICS_SHEET_DEFAULTS),
            "simulation": dict(SIMULATION_SHEET_DEFAULTS),
            "balancing": dict(BALANCING_SHEET_DEFAULTS),
        }
        write_workbook(typed, path)
        return path

    # Round-trip: the keys survive the workbook and land in the flat
    # params through _typed_to_flat.
    typed_back = read_workbook(_write(
        bess_augmentation_years="4,8",
        bess_overbuild_pct=10.0,
        bess_cost_decline_pct_per_year=5.0,
    ))
    _params, _ts_out = _typed_to_flat(typed_back)
    assert _params["bess_augmentation_years"] == "4,8"
    assert _params["bess_overbuild_pct"] == 10.0
    assert _params["bess_cost_decline_pct_per_year"] == 5.0
    assert _params["bess_augmentation_mode"] == "top_up"

    # Exclusivity with the single replacement (scheduled and auto).
    with pytest.raises(ValueError, match="supersedes"):
        read_workbook(_write(
            bess_augmentation_years="8", bess_replacement_year=6,
        ))
    with pytest.raises(ValueError, match="supersedes"):
        read_workbook(_write(
            bess_overbuild_pct=10.0, bess_replacement_year="auto",
        ))

    # Range and mode-consistency checks.
    with pytest.raises(ValueError, match=r"\[0, 100\]"):
        read_workbook(_write(bess_overbuild_pct=150.0))
    with pytest.raises(ValueError, match=r"\[0, 30\]"):
        read_workbook(_write(
            bess_augmentation_years="8",
            bess_cost_decline_pct_per_year=40.0,
        ))
    with pytest.raises(ValueError, match=r"1\.\.project_lifecycle_years"):
        read_workbook(_write(bess_augmentation_years="15"))
    with pytest.raises(ValueError, match="fixed_kwh"):
        read_workbook(_write(
            bess_augmentation_years="8",
            bess_augmentation_mode="fixed_kwh",
        ))
