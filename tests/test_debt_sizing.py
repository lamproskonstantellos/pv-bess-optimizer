"""Target-DSCR debt sizing (Eqs. E41-E43).

``debt_sizing_mode = target_dscr`` inverts the amortization schedule:
given a target coverage ratio and the sizing-case CFADS, the maximum
sustainable debt comes out in closed form per repayment profile,
capped at the Year-0 outlay, with gearing reported as an OUTPUT.
Locked here: the sizing inversion round-trip per profile (replaying
the sized debt through the schedule reproduces the target exactly),
the closed forms against brute-force bisection, the outlay cap, the
infeasible-target all-equity completion, the frozen-debt convention
under perturbed frames, manual-mode bit-identity, the input-echo
warning, validation and the SUMMARY block.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import (
    _amortization_schedule,
    _cfads_for_schedule,
    _leverage_kpis,
    build_debt_schedule,
    build_yearly_cashflow,
    compute_financial_kpis,
    resolve_debt_sizing,
    size_debt,
)
from pvbess_opt.io import validate_workbook_params, write_summary_md

# Irregular but positive CFADS: a replacement-style dip in year 4.
IRREGULAR_CFADS = [220.0, 240.0, 210.0, 90.0, 230.0, 250.0, 240.0, 260.0]

SIZING_KPI_KEYS = (
    "debt_capacity_eur", "sized_debt_eur", "gearing_sized_pct",
    "gearing_input_pct", "target_dscr", "dscr_target_met",
    "binding_dscr_year",
)


def _sizing_econ(repayment: str = "annuity", **o) -> dict:
    econ = {
        "debt_sizing_mode": "target_dscr",
        "target_dscr": 1.30,
        "debt_interest_rate_pct": 6.0,
        "debt_tenor_years": 8,
        "debt_repayment": repayment,
        "gearing_pct": 0.0,
    }
    econ.update(o)
    return econ


def _replay_dscrs(sizing, econ, cfads) -> list[float]:
    """Per-year CFADS/service after feeding the sized debt back
    through the forward schedule — the inversion round-trip."""
    rate = float(econ["debt_interest_rate_pct"]) / 100.0
    tenor = int(econ["debt_tenor_years"])
    repayment = str(econ["debt_repayment"])
    sched = _amortization_schedule(
        sizing.sized_debt_eur, rate, tenor, repayment,
        cfads=list(cfads) if repayment == "sculpted" else None,
    )
    out = []
    for row in sched:
        svc = row["debt_service_eur"]
        if svc > 0.0:
            out.append(cfads[int(row["year"]) - 1] / svc)
    return out


# ---------------------------------------------------------------------------
# Sizing inversion round-trip (the headline contract)
# ---------------------------------------------------------------------------


def test_annuity_sizing_roundtrip():
    econ = _sizing_econ("annuity")
    sizing = size_debt(IRREGULAR_CFADS, econ, 1e9)
    assert sizing.dscr_target_met
    # Level service binds where CFADS is lowest (year 4).
    assert sizing.binding_year == 4
    dscrs = _replay_dscrs(sizing, econ, IRREGULAR_CFADS)
    assert min(dscrs) == pytest.approx(1.30, rel=1e-9)
    assert all(d >= 1.30 - 1e-9 for d in dscrs)


def test_linear_sizing_roundtrip():
    econ = _sizing_econ("linear")
    sizing = size_debt(IRREGULAR_CFADS, econ, 1e9)
    assert sizing.dscr_target_met
    dscrs = _replay_dscrs(sizing, econ, IRREGULAR_CFADS)
    assert min(dscrs) == pytest.approx(1.30, rel=1e-9)
    assert all(d >= 1.30 - 1e-9 for d in dscrs)
    # The binding year is where the replay hits the target.
    replay_min_year = int(np.argmin(dscrs)) + 1
    assert sizing.binding_year == replay_min_year


def test_sculpted_sizing_roundtrip():
    econ = _sizing_econ("sculpted")
    sizing = size_debt(IRREGULAR_CFADS, econ, 1e9)
    assert sizing.dscr_target_met
    assert sizing.binding_year == 0  # level coverage: no single binder
    dscrs = _replay_dscrs(sizing, econ, IRREGULAR_CFADS)
    # Eq. E42 inverse of E40a: coverage equals the target in EVERY
    # positive-CFADS year (the final-year sweep keeps the last year on
    # target too because the balance closes exactly).
    for d in dscrs:
        assert d == pytest.approx(1.30, rel=1e-9)


def test_linear_closed_form_matches_bisection():
    """Guards the E42 algebra: bisection on min-DSCR(B) == target."""
    econ = _sizing_econ("linear", target_dscr=1.45)
    sizing = size_debt(IRREGULAR_CFADS, econ, 1e9)
    rate, tenor = 0.06, 8

    def min_dscr(debt: float) -> float:
        sched = _amortization_schedule(debt, rate, tenor, "linear")
        return min(
            IRREGULAR_CFADS[int(r["year"]) - 1] / r["debt_service_eur"]
            for r in sched
        )

    low, high = 1.0, 1e7
    for _ in range(200):
        mid = 0.5 * (low + high)
        if min_dscr(mid) >= 1.45:
            low = mid
        else:
            high = mid
    assert sizing.debt_capacity_eur == pytest.approx(low, rel=1e-6)


def test_annuity_zero_rate_closed_form():
    econ = _sizing_econ("annuity", debt_interest_rate_pct=0.0)
    sizing = size_debt(IRREGULAR_CFADS, econ, 1e9)
    # r = 0: B* = T x min CFADS / target.
    assert sizing.debt_capacity_eur == pytest.approx(
        8 * 90.0 / 1.30, rel=1e-12,
    )


# ---------------------------------------------------------------------------
# Cap, infeasibility, edge cases (Eq. E43)
# ---------------------------------------------------------------------------


def test_sized_debt_capped_at_outlay():
    econ = _sizing_econ("annuity")
    sizing = size_debt(IRREGULAR_CFADS, econ, 300.0)
    assert sizing.debt_capacity_eur > 300.0
    assert sizing.sized_debt_eur == pytest.approx(300.0)
    assert sizing.gearing_sized_pct == pytest.approx(100.0)
    assert sizing.dscr_target_met  # less debt -> higher coverage


@pytest.mark.parametrize("repayment", ["annuity", "linear"])
def test_negative_cfads_year_infeasible(repayment):
    cfads = list(IRREGULAR_CFADS)
    cfads[3] = -50.0
    sizing = size_debt(cfads, _sizing_econ(repayment), 1e9)
    assert sizing.debt_capacity_eur == 0.0
    assert sizing.sized_debt_eur == 0.0
    assert sizing.gearing_sized_pct == 0.0
    assert not sizing.dscr_target_met
    assert sizing.binding_year == 4


def test_sculpted_skips_negative_years():
    cfads = list(IRREGULAR_CFADS)
    cfads[3] = -50.0
    econ = _sizing_econ("sculpted")
    sizing = size_debt(cfads, econ, 1e9)
    assert sizing.dscr_target_met
    rate = 0.06
    expected = sum(
        max(c, 0.0) * (1.0 + rate) ** (-y)
        for y, c in enumerate(cfads, start=1)
    ) / 1.30
    assert sizing.debt_capacity_eur == pytest.approx(expected, rel=1e-12)
    # All-nonpositive CFADS: nothing to lend against.
    dead = size_debt([-10.0] * 8, econ, 1e9)
    assert dead.debt_capacity_eur == 0.0
    assert not dead.dscr_target_met


def test_short_cfads_zero_padded_to_tenor():
    """A CFADS vector shorter than the tenor pads with 0 — the padded
    years bound annuity/linear capacity at 0 (predictable, never
    silent-crash), matching the _amortization_schedule convention."""
    sizing = size_debt([200.0, 200.0], _sizing_econ("annuity"), 1e9)
    assert sizing.debt_capacity_eur == 0.0
    assert not sizing.dscr_target_met


# ---------------------------------------------------------------------------
# resolve_debt_sizing: freeze-once semantics + input-echo warning
# ---------------------------------------------------------------------------


def _yearly_frame(n: int = 8) -> pd.DataFrame:
    return pd.DataFrame({
        "project_year": range(n + 1),
        "net_cashflow_eur": [-1200.0, *IRREGULAR_CFADS[:n]],
    })


def test_resolve_manual_mode_returns_none_untouched():
    econ = {"gearing_pct": 60.0, "debt_repayment": "annuity"}
    before = dict(econ)
    assert resolve_debt_sizing(_yearly_frame(), econ) is None
    assert econ == before


def test_resolve_stashes_frozen_keys():
    econ = _sizing_econ("annuity")
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # gearing 0: no echo warning
        sizing = resolve_debt_sizing(_yearly_frame(), econ)
    assert sizing is not None
    assert econ["_sized_debt_eur"] == pytest.approx(sizing.sized_debt_eur)
    assert econ["_debt_capacity_eur"] == pytest.approx(
        sizing.debt_capacity_eur,
    )
    assert econ["_gearing_sized_pct"] == pytest.approx(
        sizing.gearing_sized_pct,
    )
    assert econ["_binding_dscr_year"] == sizing.binding_year
    assert econ["_dscr_target_met"] is True


def test_gearing_input_echo_warning():
    econ = _sizing_econ("annuity", gearing_pct=55.0)
    with pytest.warns(UserWarning, match="input echo only"):
        resolve_debt_sizing(_yearly_frame(), econ)
    # The input key itself is never rewritten.
    assert econ["gearing_pct"] == 55.0


def test_leverage_and_schedule_consume_sized_debt():
    """With the frozen key present, gearing_pct = 0 still produces a
    levered run at exactly the sized debt (Eq. E43 resolution)."""
    econ = _sizing_econ("annuity")
    yearly = _yearly_frame()
    resolve_debt_sizing(yearly, econ)
    net_cf = yearly["net_cashflow_eur"].to_numpy(dtype=float)
    eq_irr, min_dscr, avg_dscr = _leverage_kpis(net_cf, econ)
    assert np.isfinite(eq_irr)
    assert min_dscr == pytest.approx(1.30, rel=1e-9)
    assert avg_dscr >= min_dscr
    sched = build_debt_schedule(yearly, econ)
    assert sched is not None
    assert float(sched["dscr"].min()) == pytest.approx(1.30, abs=1e-3)
    assert sched["debt_balance_eur"].iloc[-1] == pytest.approx(0.0, abs=1e-6)
    # The schedule's principal sums to the sized debt, not gearing x inv.
    assert float(sched["principal_eur"].sum()) == pytest.approx(
        econ["_sized_debt_eur"], abs=0.05,
    )


# ---------------------------------------------------------------------------
# KPI surface + frozen debt under perturbation + bit-identity
# ---------------------------------------------------------------------------

N_YEARS = 8


def _pipeline_econ(**o) -> dict:
    econ = {
        "project_lifecycle_years": N_YEARS,
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
        "debt_interest_rate_pct": 5.0,
        "debt_tenor_years": 6,
        "debt_repayment": "annuity",
    }
    econ.update(o)
    return econ


def _caps() -> dict:
    return {"pv_kwp": 1000.0, "bess_kw": 500.0, "bess_kwh": 1000.0}


def _kpis() -> dict:
    return {
        "profit_load_from_pv_eur": 60_000.0,
        "profit_load_from_bess_eur": 10_000.0,
        "profit_export_from_pv_eur": 40_000.0,
        "profit_export_from_bess_eur": 40_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
        "profit_total_eur": 145_000.0,
    }


def test_compute_financial_kpis_sizing_family():
    econ = _pipeline_econ(debt_sizing_mode="target_dscr", target_dscr=1.4)
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    resolve_debt_sizing(cf, econ)
    fin = compute_financial_kpis(cf, econ)
    assert fin["sized_debt_eur"] > 0.0
    assert fin["debt_capacity_eur"] >= fin["sized_debt_eur"]
    assert 0.0 < fin["gearing_sized_pct"] <= 100.0
    assert fin["target_dscr"] == 1.4
    assert fin["dscr_target_met"] == 1.0
    assert fin["gearing_input_pct"] == 0.0
    assert fin["min_dscr"] == pytest.approx(1.4, abs=1e-3)
    # gearing_pct stays the raw input echo in every mode.
    assert fin["gearing_pct"] == 0.0
    for key in SIZING_KPI_KEYS:
        assert isinstance(fin[key], float), key


def test_sizing_family_nan_in_manual_mode():
    econ = _pipeline_econ(gearing_pct=60.0)
    fin = compute_financial_kpis(
        build_yearly_cashflow(_kpis(), econ, _caps()), econ,
    )
    for key in SIZING_KPI_KEYS:
        assert np.isnan(fin[key]), key


def test_manual_mode_bit_identity():
    """debt_sizing_mode absent vs explicit 'manual': identical frames
    and identical KPI dicts (the zero-default contract)."""
    econ_absent = _pipeline_econ(gearing_pct=55.0)
    econ_manual = _pipeline_econ(gearing_pct=55.0,
                                 debt_sizing_mode="manual",
                                 target_dscr=1.30,
                                 debt_sizing_case="base")
    cf_a = build_yearly_cashflow(_kpis(), econ_absent, _caps())
    cf_m = build_yearly_cashflow(_kpis(), econ_manual, _caps())
    assert resolve_debt_sizing(cf_m, econ_manual) is None
    pd.testing.assert_frame_equal(cf_a, cf_m)
    fin_a = compute_financial_kpis(cf_a, econ_absent)
    fin_m = compute_financial_kpis(cf_m, econ_manual)
    assert set(fin_a) == set(fin_m)
    for key, val in fin_a.items():
        other = fin_m[key]
        if isinstance(val, float) and np.isnan(val):
            assert isinstance(other, float) and np.isnan(other), key
        else:
            assert other == val, key


def test_frozen_debt_under_perturbation():
    """The sensitivity contract: a perturbed frame reports the SAME
    committed debt — sizing never re-runs per perturbation."""
    from pvbess_opt.sensitivity import _scale_revenue

    econ = _pipeline_econ(debt_sizing_mode="target_dscr", target_dscr=1.4)
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    resolve_debt_sizing(cf, econ)
    base = compute_financial_kpis(cf, econ)
    perturbed = compute_financial_kpis(_scale_revenue(cf, 1.2, econ), econ)
    assert perturbed["sized_debt_eur"] == base["sized_debt_eur"]
    assert perturbed["gearing_sized_pct"] == base["gearing_sized_pct"]
    # Coverage moves with the perturbed cashflow while debt stays put.
    assert perturbed["min_dscr"] > base["min_dscr"]


def test_infeasible_target_completes_all_equity():
    """A CFADS <= 0 year inside the tenor: capacity 0, flag False, the
    run still completes with NaN leverage KPIs (all-equity)."""
    econ = _pipeline_econ(
        debt_sizing_mode="target_dscr", target_dscr=1.3,
        opex_pv_eur_per_kwp=500.0,  # sinks every operating year
    )
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    assert float(cf["net_cashflow_eur"].iloc[1]) < 0.0
    sizing = resolve_debt_sizing(cf, econ)
    assert sizing is not None and not sizing.dscr_target_met
    fin = compute_financial_kpis(cf, econ)
    assert fin["sized_debt_eur"] == 0.0
    assert fin["dscr_target_met"] == 0.0
    assert np.isnan(fin["equity_irr_pct"])
    assert np.isnan(fin["min_dscr"])


def test_sculpted_tax_layer_threads_cfads():
    """Regression: the tax layer's E20 interest schedule must thread
    the CFADS vector under the sculpted profile instead of raising
    'sculpted repayment requires the yearly cashflow'."""
    econ = _pipeline_econ(
        gearing_pct=60.0, debt_repayment="sculpted",
        corporate_tax_rate_pct=22.0,
        depreciation_years_pv=4, depreciation_years_bess=2,
        depreciation_years_site=8, tax_loss_carryforward_years=0,
    )
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    net_cf = cf["net_cashflow_eur"].to_numpy(dtype=float)
    sched = _amortization_schedule(
        0.60 * (-float(net_cf[0])), 0.05, 6, "sculpted",
        cfads=_cfads_for_schedule(net_cf, 6, "sculpted"),
    )
    for row in sched:
        assert float(
            cf.loc[cf["project_year"] == int(row["year"]),
                   "debt_interest_eur"].iloc[0]
        ) == pytest.approx(row["interest_eur"], abs=1e-6)


# ---------------------------------------------------------------------------
# Validation + SUMMARY block
# ---------------------------------------------------------------------------


def _typed_with_econ(**econ) -> dict:
    return {
        "project": {"mode": "self_consumption"},
        "pv": {"pv_nameplate_kwp": 500.0},
        "bess": {"bess_power_kw": 100.0, "bess_capacity_kwh": 200.0},
        "economics": dict(econ),
        "simulation": {},
        "balancing": {"balancing_enabled": False},
    }


def test_validation_rejects_target_below_one():
    typed = _typed_with_econ(debt_sizing_mode="target_dscr",
                             target_dscr=0.9)
    with pytest.raises(ValueError, match="target_dscr"):
        validate_workbook_params(typed, dt_minutes=60)


def test_validation_rejects_zero_tenor_in_sizing_mode():
    typed = _typed_with_econ(debt_sizing_mode="target_dscr",
                             debt_tenor_years=0)
    with pytest.raises(ValueError, match="debt_tenor_years"):
        validate_workbook_params(typed, dt_minutes=60)


@pytest.mark.parametrize("case", ["p90", "low_price"])
def test_validation_rejects_reserved_sizing_cases(case):
    typed = _typed_with_econ(debt_sizing_mode="target_dscr",
                             debt_sizing_case=case)
    with pytest.raises(ValueError, match="not available"):
        validate_workbook_params(typed, dt_minutes=60)


def test_validation_sizing_keys_inert_in_manual_mode():
    # Out-of-covenant values sit inert while the mode is manual.
    typed = _typed_with_econ(debt_sizing_mode="manual", target_dscr=0.5,
                             debt_sizing_case="p90")
    validate_workbook_params(typed, dt_minutes=60)


def _summary(fin: dict, tmp_path) -> str:
    out = tmp_path / "SUMMARY.md"
    write_summary_md(
        out,
        kpis_year1={"profit_total_eur": 1.0},
        financial_kpis=fin,
        params={"mode": "merchant", "pv_nameplate_kwp": 1000.0},
    )
    return out.read_text(encoding="utf-8")


def test_summary_debt_sizing_block(tmp_path):
    econ = _pipeline_econ(debt_sizing_mode="target_dscr", target_dscr=1.4)
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    resolve_debt_sizing(cf, econ)
    text = _summary(compute_financial_kpis(cf, econ), tmp_path)
    assert "## Debt sizing (target DSCR)" in text
    assert "| Target DSCR [-] | 1.4 |" in text
    assert "| Sized debt [EUR] |" in text
    assert "| Sized gearing [%] |" in text
    assert "| DSCR target met | yes |" in text
    assert "not achievable" not in text


def test_summary_debt_sizing_infeasible_line(tmp_path):
    econ = _pipeline_econ(
        debt_sizing_mode="target_dscr", target_dscr=1.3,
        opex_pv_eur_per_kwp=500.0,
    )
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    resolve_debt_sizing(cf, econ)
    text = _summary(compute_financial_kpis(cf, econ), tmp_path)
    assert "| DSCR target met | no |" in text
    assert (
        "Target DSCR not achievable on the sizing case; "
        "debt capacity is zero and the run completes all-equity."
    ) in text


def test_summary_block_absent_in_manual_mode(tmp_path):
    econ = _pipeline_econ(gearing_pct=60.0)
    cf = build_yearly_cashflow(_kpis(), econ, _caps())
    text = _summary(compute_financial_kpis(cf, econ), tmp_path)
    assert "Debt sizing" not in text
    assert "Gearing input" not in text


def test_scenario_overrides_accept_sizing_keys():
    """The dotted economics.<key> targets flow from the workbook
    schema automatically — a scenario can flip a run into sizing
    mode."""
    from pvbess_opt.scenarios import validate_scenario_overrides

    validate_scenario_overrides({
        "name": "levered",
        "economics": {
            "debt_sizing_mode": "target_dscr",
            "target_dscr": 1.4,
            "debt_sizing_case": "base",
        },
    })
    with pytest.raises(ValueError, match="debt_sizing_mod"):
        validate_scenario_overrides({
            "name": "typo",
            "economics": {"debt_sizing_mod": "target_dscr"},
        })
