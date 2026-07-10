"""Optimizer floor + share-above-floor (Eqs. E30/E30a).

The plain E13d share becomes the phi-share special case: with
``optimizer_floor_enabled`` the optimizer guarantees a floor
F EUR/kW/yr (availability-scaled, flat nominal) and takes the share of
the margin ABOVE the floor; shortfalls are topped up through the
separate ``optimizer_floor_topup_eur`` column (>= 0, so the fee column
keeps its <= 0 sign contract).  A shared term window gates both.
Locked: zero-default bit-identity (incl. the E13d special case), the
three-regime cent-level algebra, term gating, month-12 top-up booking,
the econ-threaded piecewise sensitivity recompute at the floor kink,
the toll-overlap warning and the registries.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.availability import availability_factor
from pvbess_opt.economics import (
    build_yearly_cashflow,
    compute_financial_kpis,
    derive_monthly_cashflow,
)
from pvbess_opt.io import _SHEET_DEFAULTS, validate_workbook_params

N_YEARS = 5
BESS_KW = 500.0
UNAVAIL_PCT = 2.0
SHARE_PCT = 15.0
FLOOR_RATE = 100.0  # EUR/kW/yr
REV1_DAM_BESS = 35_000.0  # export 40k - grid charge 5k
# Year multipliers driving the DAM margin through the three regimes:
# above the floor, below the floor (but positive), and negative.
DAM_TRAJ = [2.0, 0.5, -0.2, 1.5, 1.5]


def _econ(**o) -> dict:
    econ = {
        "project_lifecycle_years": N_YEARS,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 0.0,
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "unavailability_pct": UNAVAIL_PCT,
        "capex_pv_eur_per_kw": 500.0,
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
        "optimizer_revenue_share_pct": SHARE_PCT,
        "balancing_aggregator_fee_pct_revenue": 10.0,
        "bess_power_kw": BESS_KW,
    }
    econ.update(o)
    return econ


def _floor_econ(**o) -> dict:
    kw = {
        "optimizer_floor_enabled": True,
        "optimizer_floor_eur_per_kw_year": FLOOR_RATE,
        "trajectories": {
            "revenue_dam": {"mode": "replace", "values": list(DAM_TRAJ)},
        },
    }
    kw.update(o)
    return _econ(**kw)


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": BESS_KW, "bess_kwh": 1000.0}


def _kpis(**o) -> dict:
    base = {
        "profit_load_from_pv_eur": 50_000.0,
        "profit_load_from_bess_eur": 0.0,
        "profit_export_from_pv_eur": 30_000.0,
        "profit_export_from_bess_eur": 40_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 115_000.0,
        "bm_total_capacity_revenue_eur": 12_000.0,
        "bm_total_activation_revenue_eur": 3_000.0,
    }
    base.update(o)
    return base


def _floor_level() -> float:
    return FLOOR_RATE * BESS_KW * availability_factor(UNAVAIL_PCT)


def _margin(cf_row) -> float:
    """The 'dam'-basis margin the loop uses for the given year row."""
    return REV1_DAM_BESS * float(cf_row["bess_capacity_factor"])


# ---------------------------------------------------------------------------
# Bit-identity — the E13d special case
# ---------------------------------------------------------------------------


def test_zero_default_bit_identity():
    base = build_yearly_cashflow(_kpis(), _econ(), _caps())
    with_keys = build_yearly_cashflow(
        _kpis(),
        _econ(
            optimizer_floor_enabled=False,
            optimizer_floor_eur_per_kw_year=0.0,
            optimizer_term_year_from=1,
            optimizer_term_year_to=0,
            optimizer_margin_basis="dam",
        ),
        _caps(),
    )
    pd.testing.assert_frame_equal(base, with_keys)
    assert (base["optimizer_floor_topup_eur"] == 0.0).all()
    # The plain share still charges every operating year (whole-life
    # default term).
    assert float(
        base.set_index("project_year").loc[N_YEARS, "optimizer_fee_eur"]
    ) < 0.0


# ---------------------------------------------------------------------------
# E30 — three regimes, cent level
# ---------------------------------------------------------------------------


def test_three_regime_cent_lock():
    cf = build_yearly_cashflow(_kpis(), _floor_econ(), _caps()).set_index(
        "project_year",
    )
    floor = _floor_level()
    share = SHARE_PCT / 100.0
    for y in range(1, N_YEARS + 1):
        m = _margin(cf.loc[y]) * DAM_TRAJ[y - 1]
        expected_fee = -share * max(m - floor, 0.0)
        expected_topup = max(floor - m, 0.0)
        assert float(cf.loc[y, "optimizer_fee_eur"]) == pytest.approx(
            expected_fee, abs=0.01,
        ), y
        assert float(
            cf.loc[y, "optimizer_floor_topup_eur"]
        ) == pytest.approx(expected_topup, abs=0.01), y
    # Regime coverage: y1 fee<0/topup=0; y2 fee=0/0<topup<floor;
    # y3 fee=0/topup>floor (negative-margin year).
    assert float(cf.loc[1, "optimizer_fee_eur"]) < 0.0
    assert float(cf.loc[1, "optimizer_floor_topup_eur"]) == 0.0
    assert float(cf.loc[2, "optimizer_fee_eur"]) == 0.0
    assert 0.0 < float(cf.loc[2, "optimizer_floor_topup_eur"]) < _floor_level()
    assert float(cf.loc[3, "optimizer_floor_topup_eur"]) > _floor_level()
    # Owner's realised optimizer-managed margin never falls below the
    # floor (Eq. E30 identity).
    for y in range(1, N_YEARS + 1):
        m = _margin(cf.loc[y]) * DAM_TRAJ[y - 1]
        realised = (
            m
            + float(cf.loc[y, "optimizer_fee_eur"])
            + float(cf.loc[y, "optimizer_floor_topup_eur"])
        )
        assert realised >= _floor_level() - 1e-9, y


def test_floor_zero_with_switch_changes_only_loss_years():
    plain = build_yearly_cashflow(
        _kpis(),
        _econ(trajectories={
            "revenue_dam": {"mode": "replace", "values": list(DAM_TRAJ)},
        }),
        _caps(),
    ).set_index("project_year")
    zero_floor = build_yearly_cashflow(
        _kpis(), _floor_econ(optimizer_floor_eur_per_kw_year=0.0), _caps(),
    ).set_index("project_year")
    for y in range(1, N_YEARS + 1):
        assert float(zero_floor.loc[y, "optimizer_fee_eur"]) == (
            pytest.approx(float(plain.loc[y, "optimizer_fee_eur"]), abs=1e-9)
        ), y
        m = _margin(plain.loc[y]) * DAM_TRAJ[y - 1]
        expected_topup = max(-m, 0.0)
        assert float(
            zero_floor.loc[y, "optimizer_floor_topup_eur"]
        ) == pytest.approx(expected_topup, abs=0.01), y


def test_term_window_gates_share_and_floor():
    cf = build_yearly_cashflow(
        _kpis(), _floor_econ(optimizer_term_year_to=3), _caps(),
    ).set_index("project_year")
    for y in (4, 5):
        assert float(cf.loc[y, "optimizer_fee_eur"]) == 0.0, y
        assert float(cf.loc[y, "optimizer_floor_topup_eur"]) == 0.0, y
    assert float(cf.loc[1, "optimizer_fee_eur"]) < 0.0
    assert float(cf.loc[2, "optimizer_floor_topup_eur"]) > 0.0


def test_margin_basis_dam_plus_balancing():
    cf = build_yearly_cashflow(
        _kpis(),
        _floor_econ(optimizer_margin_basis="dam_plus_balancing"),
        _caps(),
    ).set_index("project_year")
    floor = _floor_level()
    share = SHARE_PCT / 100.0
    for y in range(1, N_YEARS + 1):
        bess_factor = float(cf.loc[y, "bess_capacity_factor"])
        bal_net = (
            float(cf.loc[y, "balancing_revenue_eur"])
            + float(cf.loc[y, "balancing_aggregator_fee_eur"])
        )
        m = REV1_DAM_BESS * bess_factor * DAM_TRAJ[y - 1] + bal_net
        assert float(cf.loc[y, "optimizer_fee_eur"]) == pytest.approx(
            -share * max(m - floor, 0.0), abs=0.01,
        ), y
        assert float(
            cf.loc[y, "optimizer_floor_topup_eur"]
        ) == pytest.approx(max(floor - m, 0.0), abs=0.01), y


# ---------------------------------------------------------------------------
# Monthly — month-12 top-up booking
# ---------------------------------------------------------------------------


def test_monthly_topup_books_in_month_12():
    from tests.test_monthly_cashflow_reconciliation import _make_res_frame

    res = _make_res_frame()
    econ = _floor_econ()
    yearly = build_yearly_cashflow(_kpis(), econ, _caps())
    monthly, _q = derive_monthly_cashflow(res, yearly, econ)
    yearly_indexed = yearly.set_index("project_year")
    for y in range(1, N_YEARS + 1):
        sub = monthly.loc[monthly["project_year"] == y]
        topup_y = float(yearly_indexed.loc[y, "optimizer_floor_topup_eur"])
        by_month = sub.set_index("period")["optimizer_floor_topup_eur"]
        assert float(by_month.loc[12]) == pytest.approx(topup_y, abs=1e-9), y
        assert (by_month.loc[1:11] == 0.0).all(), y
        assert float(sub["net_cashflow_eur"].sum()) == pytest.approx(
            float(yearly_indexed.loc[y, "net_cashflow_eur"]), abs=1e-6,
        ), y


# ---------------------------------------------------------------------------
# Sensitivity — piecewise recompute at the floor kink
# ---------------------------------------------------------------------------


def test_scale_revenue_kink_matches_from_scratch_rebuild():
    from pvbess_opt.sensitivity import _scale_revenue

    econ = _floor_econ()
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    factor = 0.5  # the low leg pushes year-1 margin through the floor
    scaled = _scale_revenue(cf, factor, econ).set_index("project_year")

    # From-scratch rebuild at the perturbed prices: every price-driven
    # Year-1 base scales by the factor (volume-based charges do not).
    k = _kpis()
    perturbed = {
        key: (v * factor if isinstance(v, float) else v)
        for key, v in k.items()
    }
    rebuilt = build_yearly_cashflow(perturbed, econ, _caps()).set_index(
        "project_year",
    )
    for y in range(1, N_YEARS + 1):
        for col in ("optimizer_fee_eur", "optimizer_floor_topup_eur"):
            assert float(scaled.loc[y, col]) == pytest.approx(
                float(rebuilt.loc[y, col]), abs=0.01,
            ), (y, col)
    # The kink is real: at factor 0.5 the year-1 margin falls below the
    # floor, so a constant-scale of the base fee would be wrong.
    base_fee_y1 = float(cf.set_index("project_year").loc[
        1, "optimizer_fee_eur",
    ])
    assert float(scaled.loc[1, "optimizer_fee_eur"]) != pytest.approx(
        base_fee_y1 * factor, abs=1.0,
    )


def test_scale_revenue_noop_and_legacy_path():
    from pvbess_opt.sensitivity import _recompute_net, _scale_revenue

    econ = _floor_econ()
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    # econ-threaded no-op at factor 1.0 (cent-level: the 'dam' margin
    # is reconstructed from the E25a column decomposition).
    noop = _scale_revenue(cf, 1.0, econ)
    for col in ("optimizer_fee_eur", "optimizer_floor_topup_eur",
                "net_cashflow_eur"):
        assert np.allclose(
            noop[col].to_numpy(), cf[col].to_numpy(), atol=1e-6,
        ), col
    # Legacy 2-arg path stays a bit-exact no-op with the floor off.
    plain_cf = build_yearly_cashflow(_kpis(), _econ(), _caps())
    pd.testing.assert_frame_equal(_scale_revenue(plain_cf, 1.0), plain_cf)
    # The net recompute folds the top-up column.
    recomputed = _recompute_net(cf.copy())
    pd.testing.assert_series_equal(
        recomputed["net_cashflow_eur"], cf["net_cashflow_eur"],
    )


# ---------------------------------------------------------------------------
# Validation and warnings
# ---------------------------------------------------------------------------


def _typed(**econ_overrides) -> dict:
    typed = {
        sheet: dict(defaults) for sheet, defaults in _SHEET_DEFAULTS.items()
    }
    typed["bess"]["bess_power_kw"] = BESS_KW
    typed["economics"].update(econ_overrides)
    return typed


def test_validation_and_toll_overlap_warning(caplog):
    with pytest.raises(ValueError, match="optimizer_floor_eur_per_kw_year"):
        validate_workbook_params(
            _typed(optimizer_floor_eur_per_kw_year=-1.0), dt_minutes=15,
        )
    with pytest.raises(ValueError, match="optimizer_term_year_from"):
        validate_workbook_params(
            _typed(optimizer_term_year_from=0), dt_minutes=15,
        )
    with pytest.raises(ValueError, match="optimizer_margin_basis"):
        validate_workbook_params(
            _typed(optimizer_margin_basis="spread"), dt_minutes=15,
        )
    # Overlapping 'zeroed' toll + enabled floor warns...
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io"):
        validate_workbook_params(
            _typed(
                optimizer_floor_enabled=True,
                optimizer_floor_eur_per_kw_year=FLOOR_RATE,
                bess_toll_eur_per_mw_year=50_000.0,
                bess_toll_year_from=1,
                bess_toll_year_to=5,
                optimizer_term_year_from=3,
                optimizer_term_year_to=0,
            ),
            dt_minutes=15,
        )
    assert "full floor top-up" in caplog.text
    # ...while phase-disjoint windows stay silent.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io"):
        validate_workbook_params(
            _typed(
                optimizer_floor_enabled=True,
                optimizer_floor_eur_per_kw_year=FLOOR_RATE,
                bess_toll_eur_per_mw_year=50_000.0,
                bess_toll_year_from=1,
                bess_toll_year_to=5,
                optimizer_term_year_from=6,
                optimizer_term_year_to=0,
            ),
            dt_minutes=15,
        )
    assert "full floor top-up" not in caplog.text


# ---------------------------------------------------------------------------
# KPI, SUMMARY, theme and LCOE/LCOS invariance
# ---------------------------------------------------------------------------


def _lifetime_yearly() -> pd.DataFrame:
    return pd.DataFrame({
        "project_year": range(1, N_YEARS + 1),
        "pv_generation_mwh": [1500.0 * 0.99 ** (y - 1)
                              for y in range(1, N_YEARS + 1)],
        "bess_discharge_mwh": [300.0 * 0.985 ** (y - 1)
                               for y in range(1, N_YEARS + 1)],
    })


def test_kpi_summary_theme_and_lcoe_invariance():
    from pvbess_opt.io import _SUMMARY_OPTIONAL_FINANCIAL_KEYS
    from pvbess_opt.theme import (
        FINANCIAL_COLORS,
        FINANCIAL_LABELS,
        FINANCIAL_LEGEND_ORDER,
    )

    base_fin = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), _econ(), _caps()), _econ(),
        capacities=_caps(), lifetime_yearly=_lifetime_yearly(),
        year1_kpis=_kpis(),
    )
    cf = build_yearly_cashflow(_kpis(), _floor_econ(), _caps())
    fin = compute_financial_kpis(
        cf, _floor_econ(), capacities=_caps(),
        lifetime_yearly=_lifetime_yearly(), year1_kpis=_kpis(),
    )
    expected = float(
        cf.loc[cf["project_year"] >= 1, "optimizer_floor_topup_eur"].sum()
    )
    assert fin[
        "total_optimizer_floor_topup_eur_lifecycle"
    ] == pytest.approx(expected, abs=0.01)
    assert expected > 0.0
    assert base_fin["total_optimizer_floor_topup_eur_lifecycle"] == 0.0
    assert fin["lcos_eur_per_mwh"] == base_fin["lcos_eur_per_mwh"]
    assert ("total_optimizer_floor_topup_eur_lifecycle",
            "Lifetime optimizer floor top-up [EUR]") in (
        _SUMMARY_OPTIONAL_FINANCIAL_KEYS
    )
    assert "Optimizer floor top-up" in FINANCIAL_LABELS
    assert "Optimizer floor top-up" in FINANCIAL_LEGEND_ORDER
    assert FINANCIAL_COLORS["optimizer_floor_topup"] == "#004D40"
