"""Strict total-injection grid cap (``grid_cap_includes_load``).

Covers the optional ``grid_cap_includes_load`` mode that makes the
grid-export cap bind on the TOTAL plant injection (load-serving flows
plus surplus export) rather than on surplus export alone — a Virtual
Net-Billing physical injection cap.

All cases are intentionally tiny (1 day, dt = 60 min, no BESS) so the
algebra is exact: with ``dt_h = 1`` and a flat 100 % max-injection
profile, the per-step cap equals ``p_grid_export_max_kw`` directly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.io import (
    BESS_SHEET_DEFAULTS,
    ECONOMICS_SHEET_DEFAULTS,
    PROJECT_SHEET_DEFAULTS,
    PV_SHEET_DEFAULTS,
    SIMULATION_SHEET_DEFAULTS,
    read_inputs,
    read_workbook,
    write_workbook,
)
from pvbess_opt.io_read import dump_structured_config, load_structured_config
from pvbess_opt.optimization import (
    build_model,
    run_scenario,
    verify_dispatch_invariants,
)

# Tiny deterministic solves; a 0 gap pins the unique optimum exactly.
SOLVER_KW = {"solver_name": "highs", "mip_gap": 0.0, "time_limit_seconds": 120}


def _tiny_params(**overrides):
    """Minimal no-BESS self_consumption params; cap = p_grid_export_max_kw."""
    params = {
        "dt_minutes": 60,
        "efficiency_charge": 0.97,
        "efficiency_discharge": 0.97,
        "soc_min_frac": 0.20,
        "soc_max_frac": 0.95,
        "initial_soc_frac": 0.50,
        "terminal_soc_equal": True,
        "max_cycles_per_day": 1.0,
        "p_grid_export_max_kw": 5.0,
        "pv_nameplate_kwp": 1000.0,   # PV present
        "bess_power_kw": 0.0,          # no BESS keeps the algebra clean
        "bess_capacity_kwh": 0.0,
        "retail_tariff_eur_per_mwh": 200.0,
        "mode": "self_consumption",
        "allow_bess_grid_charging": False,
        "show_titles": False,
    }
    params.update(overrides)
    return params


def _tiny_ts(pv, load, *, dam=50.0):
    n = len(pv)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=n, freq="h"),
        "pv_kwh": [float(x) for x in pv],
        "load_kwh": [float(x) for x in load],
        "dam_price_eur_per_mwh": [float(dam)] * n,
    })


def _peak_ts():
    """24 h with one surplus peak hour (pv=10, load=4); cap = 5 kWh/step."""
    pv = [0.0] * 24
    load = [0.0] * 24
    pv[12] = 10.0
    load[12] = 4.0
    return _tiny_ts(pv, load)


def _typed_with_flag(flag: bool) -> dict:
    return {
        "ts": _peak_ts(),
        "project": dict(
            PROJECT_SHEET_DEFAULTS,
            grid_cap_includes_load=flag,
            p_grid_export_max_kw=5.0,
        ),
        "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=1000.0),
        "bess": dict(
            BESS_SHEET_DEFAULTS, bess_power_kw=0.0, bess_capacity_kwh=0.0,
        ),
        "economics": dict(ECONOMICS_SHEET_DEFAULTS),
        "simulation": dict(SIMULATION_SHEET_DEFAULTS),
        "max_injection_profile": np.full(24, 100.0, dtype=float),
    }


# ---------------------------------------------------------------------------
# 1. Backward compatibility
# ---------------------------------------------------------------------------


def test_default_is_false():
    assert PROJECT_SHEET_DEFAULTS["grid_cap_includes_load"] is False


def test_flag_absent_equals_flag_false():
    """Flag absent and flag explicitly False produce an identical frame, and
    grid_injection_total_kwh equals grid_export_total_kwh elementwise."""
    ts = _peak_ts()
    res_absent, _ = run_scenario(_tiny_params(), ts, **SOLVER_KW)  # key absent
    res_false, _ = run_scenario(
        _tiny_params(grid_cap_includes_load=False), ts, **SOLVER_KW,
    )
    pd.testing.assert_frame_equal(res_absent, res_false)
    np.testing.assert_allclose(
        res_false["grid_injection_total_kwh"].to_numpy(),
        res_false["grid_export_total_kwh"].to_numpy(),
    )


# ---------------------------------------------------------------------------
# 2. Strict injection-cap basis
# ---------------------------------------------------------------------------


def test_strict_basis_bound_and_export_metric_unchanged():
    ts = _peak_ts()
    res, _ = run_scenario(
        _tiny_params(grid_cap_includes_load=True), ts, **SOLVER_KW,
    )
    inj = res["grid_injection_total_kwh"].to_numpy()
    basis = (
        res["pv_to_load_kwh"] + res["bess_dis_load_kwh"]
        + res["pv_to_grid_kwh"] + res["bess_dis_grid_kwh"]
    ).to_numpy()
    np.testing.assert_allclose(inj, basis)
    # The cap binds on the injection basis.
    cap = res["grid_export_cap_kwh"].to_numpy()
    assert (inj <= cap + 1e-6).all()
    # The export metric is unchanged — still surplus export only.
    np.testing.assert_allclose(
        res["grid_export_total_kwh"].to_numpy(),
        (res["pv_to_grid_kwh"] + res["bess_dis_grid_kwh"]).to_numpy(),
    )


# ---------------------------------------------------------------------------
# 3. The strict cap changes the export split but preserves load priority
# ---------------------------------------------------------------------------


def test_strict_changes_split_but_preserves_priority():
    ts = _peak_ts()  # pv[12]=10, load[12]=4, cap=5, dam>0
    res_f, _ = run_scenario(
        _tiny_params(grid_cap_includes_load=False), ts, **SOLVER_KW,
    )
    res_t, _ = run_scenario(
        _tiny_params(grid_cap_includes_load=True), ts, **SOLVER_KW,
    )
    # False: surplus-export cap binds -> pv_to_grid = 5, curtail = 1.
    assert res_f.loc[12, "pv_to_grid_kwh"] == pytest.approx(5.0, abs=1e-4)
    assert res_f.loc[12, "pv_curtail_kwh"] == pytest.approx(1.0, abs=1e-4)
    # True: injection cap binds (4 forced to load) -> pv_to_grid = 1, curtail = 5.
    assert res_t.loc[12, "pv_to_grid_kwh"] == pytest.approx(1.0, abs=1e-4)
    assert res_t.loc[12, "pv_curtail_kwh"] == pytest.approx(5.0, abs=1e-4)
    # The two runs differ on the export split / curtailment.
    assert abs(res_f.loc[12, "pv_to_grid_kwh"] - res_t.loc[12, "pv_to_grid_kwh"]) > 1.0
    assert abs(res_f.loc[12, "pv_curtail_kwh"] - res_t.loc[12, "pv_curtail_kwh"]) > 1.0
    # Load priority is preserved in BOTH: pv_to_load == min(pv, load) == 4.
    assert res_f.loc[12, "pv_to_load_kwh"] == pytest.approx(4.0, abs=1e-4)
    assert res_t.loc[12, "pv_to_load_kwh"] == pytest.approx(4.0, abs=1e-4)


# ---------------------------------------------------------------------------
# 4. Partial offset: strict cap below the load degrades coverage, never fails
# ---------------------------------------------------------------------------


def test_strict_partial_offset_when_cap_below_load():
    """Strict cap smaller than the load: load priority degrades to the cap.

    Under ``grid_cap_includes_load=True`` the load-serving flow is itself
    injected and so is bound by the per-step cap.  When the cap cannot fit the
    full load the run is NOT infeasible: ``pv_to_load`` is pinned to the cap
    (the load takes all injection capacity, before any surplus export), the
    uncovered load is bought at retail, and the surplus PV is curtailed.
    """
    # min(pv, load) = 6 > cap = 5 at the peak step.
    pv = [0.0] * 24
    load = [0.0] * 24
    pv[12] = 8.0
    load[12] = 6.0
    ts = _tiny_ts(pv, load)
    # Flag True now SOLVES with partial offset: load takes the whole cap (5),
    # the remaining 1 kWh of load is grid-served, surplus PV (8-5=3) curtailed,
    # and no export is possible because the cap is full of load-serving flow.
    res_t, _ = run_scenario(
        _tiny_params(grid_cap_includes_load=True), ts, **SOLVER_KW,
    )
    assert res_t.loc[12, "pv_to_load_kwh"] == pytest.approx(5.0, abs=1e-4)
    assert res_t.loc[12, "grid_to_load_kwh"] == pytest.approx(1.0, abs=1e-4)
    assert res_t.loc[12, "pv_to_grid_kwh"] == pytest.approx(0.0, abs=1e-4)
    assert res_t.loc[12, "pv_curtail_kwh"] == pytest.approx(3.0, abs=1e-4)
    assert res_t.loc[12, "grid_injection_total_kwh"] == pytest.approx(5.0, abs=1e-4)
    # Flag False covers the FULL load (the load-serving flow does not cross the
    # cap) and exports the 2 kWh surplus below the 5 kWh surplus-export cap.
    res_f, _ = run_scenario(
        _tiny_params(grid_cap_includes_load=False), ts, **SOLVER_KW,
    )
    assert res_f.loc[12, "pv_to_load_kwh"] == pytest.approx(6.0, abs=1e-4)
    assert res_f.loc[12, "pv_to_grid_kwh"] == pytest.approx(2.0, abs=1e-4)
    # Dispatch invariants stay clean under partial offset: invariant 9 uses the
    # cap-bounded floor min(pv, load, cap), so the pinned pv_to_load matches it.
    params_t = _tiny_params(grid_cap_includes_load=True)
    _r, _s, full = run_scenario(
        params_t, ts, return_unrounded=True, **SOLVER_KW,
    )
    inv = verify_dispatch_invariants(full, params_t)
    assert inv["invariant_9_pv_load_priority_kwh"] == pytest.approx(0.0, abs=1e-6)
    assert inv["invariant_2_load_balance_kwh"] == pytest.approx(0.0, abs=1e-6)
    assert inv["invariant_7_curtail_behavior_kwh"] == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# 5. Input/output: the flag round-trips and the frame carries the columns
# ---------------------------------------------------------------------------


def test_io_round_trips_flag_and_frame_has_columns(tmp_path):
    typed = _typed_with_flag(True)

    # Workbook write -> read round-trips the project flag.
    xlsx = tmp_path / "inj.xlsx"
    write_workbook(typed, xlsx)
    assert read_workbook(xlsx)["project"]["grid_cap_includes_load"] is True

    # read_inputs carries the flag through to the dispatch params.
    params, ts_loaded = read_inputs(xlsx)
    assert params["grid_cap_includes_load"] is True

    # The YAML/JSON config path carries it too.
    cfg = tmp_path / "inj.yaml"
    dump_structured_config(typed, cfg)
    assert load_structured_config(cfg)["project"]["grid_cap_includes_load"] is True

    # The dispatch frame contains the new + existing cap columns.
    res, _ = run_scenario(params, ts_loaded, **SOLVER_KW)
    assert {"grid_injection_total_kwh", "grid_export_cap_kwh"} <= set(res.columns)


# ---------------------------------------------------------------------------
# 6. Dispatch invariants hold under BOTH cap modes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", [False, True])
def test_dispatch_invariants_clean_in_both_cap_modes(flag):
    """verify_dispatch_invariants must report no violation in either cap mode.

    Regression guard for invariant_7: the strict total-injection cap
    (grid_cap_includes_load=True) curtails PV while surplus export sits
    below the per-step cap, so an invariant_7 that measured surplus export
    instead of the actually-bound total injection would flag a spurious
    "cap not binding yet curtailing" violation.  The cap is checked on the
    correct basis (grid_injection_total_kwh), so curtailment with the cap
    binding is recognised as legitimate.
    """
    ts = _peak_ts()  # pv[12]=10, load[12]=4, cap=5, dam>0
    params = _tiny_params(grid_cap_includes_load=flag)
    _res, _solver, full = run_scenario(
        params, ts, return_unrounded=True, **SOLVER_KW,
    )
    # The peak step curtails: surplus export (1) < cap (5) under the strict
    # basis, yet curtailment is correct because total injection (5) == cap.
    if flag:
        assert full.loc[12, "pv_curtail_kwh"] == pytest.approx(5.0, abs=1e-4)
        assert full.loc[12, "grid_injection_total_kwh"] == pytest.approx(
            5.0, abs=1e-4,
        )
    inv = verify_dispatch_invariants(full, params)
    assert inv["invariant_7_curtail_behavior_kwh"] == pytest.approx(0.0, abs=1e-9)
    assert inv["invariant_9_pv_load_priority_kwh"] == pytest.approx(0.0, abs=1e-6)
    assert inv["invariant_1_pv_balance_kwh"] == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 7. Per-source injection sub-caps round-trip through every IO path
# ---------------------------------------------------------------------------


def test_per_source_injection_profiles_round_trip(tmp_path):
    """max_injection_profile_pv / _bess survive workbook + structured-config IO
    and reach the dispatch params."""
    mi_pv = np.full(24, 70.0, dtype=float)
    mi_bess = np.full(24, 100.0, dtype=float)
    mi_bess[10:15] = 0.0  # battery blocked from injecting midday
    typed = _typed_with_flag(True)
    typed["max_injection_profile_pv"] = mi_pv
    typed["max_injection_profile_bess"] = mi_bess

    # Workbook write -> the two optional sheets appear and round-trip.
    xlsx = tmp_path / "per_source.xlsx"
    write_workbook(typed, xlsx)
    sheet_names = set(pd.ExcelFile(xlsx).sheet_names)
    assert {"max_injection_profile_pv", "max_injection_profile_bess"} <= sheet_names
    back = read_workbook(xlsx)
    np.testing.assert_allclose(np.asarray(back["max_injection_profile_pv"]), mi_pv)
    np.testing.assert_allclose(np.asarray(back["max_injection_profile_bess"]), mi_bess)

    # read_inputs carries them into the flat dispatch params.
    params, _ts = read_inputs(xlsx)
    np.testing.assert_allclose(np.asarray(params["max_injection_profile_pv"]), mi_pv)
    np.testing.assert_allclose(
        np.asarray(params["max_injection_profile_bess"]), mi_bess,
    )

    # YAML/JSON structured config round-trips them too.
    cfg = tmp_path / "per_source.yaml"
    dump_structured_config(typed, cfg)
    loaded = load_structured_config(cfg)
    np.testing.assert_allclose(np.asarray(loaded["max_injection_profile_pv"]), mi_pv)
    np.testing.assert_allclose(
        np.asarray(loaded["max_injection_profile_bess"]), mi_bess,
    )


def test_per_source_profiles_absent_default_to_none(tmp_path):
    """A workbook without the per-source sheets yields None sub-caps (the
    single combined cap still applies, exactly as before)."""
    typed = _typed_with_flag(True)  # no per-source profiles set
    xlsx = tmp_path / "no_per_source.xlsx"
    write_workbook(typed, xlsx)
    sheet_names = set(pd.ExcelFile(xlsx).sheet_names)
    assert "max_injection_profile_pv" not in sheet_names
    assert "max_injection_profile_bess" not in sheet_names
    params, _ts = read_inputs(xlsx)
    assert params["max_injection_profile_pv"] is None
    assert params["max_injection_profile_bess"] is None


# ---------------------------------------------------------------------------
# 8. Per-source caps drive dispatch across every mode / billing combination
# ---------------------------------------------------------------------------


def test_pv_sub_cap_strict_self_consumption_reduces_offset():
    """VNB + PV sub-cap: pv_to_load is pinned to the PV sub-cap floor, the
    uncovered load is grid-served, and the surplus PV is curtailed."""
    ts = _peak_ts()  # pv[12]=10, load[12]=4, cap_total = 5
    mi_pv = np.full(24, 100.0)
    mi_pv[12] = 60.0  # cap_pv[12] = 5 * 0.60 = 3
    params = _tiny_params(grid_cap_includes_load=True)
    params["max_injection_profile_pv"] = mi_pv
    res, _ = run_scenario(params, ts, **SOLVER_KW)
    assert res.loc[12, "pv_to_load_kwh"] == pytest.approx(3.0, abs=1e-4)
    assert res.loc[12, "pv_to_grid_kwh"] == pytest.approx(0.0, abs=1e-4)
    assert res.loc[12, "grid_to_load_kwh"] == pytest.approx(1.0, abs=1e-4)
    assert res.loc[12, "pv_curtail_kwh"] == pytest.approx(7.0, abs=1e-4)
    # The combined cap is still honoured.
    assert res.loc[12, "grid_injection_total_kwh"] <= 5.0 + 1e-6
    # Without the PV sub-cap the same VNB case offsets the full load (4).
    res0, _ = run_scenario(
        _tiny_params(grid_cap_includes_load=True), ts, **SOLVER_KW,
    )
    assert res0.loc[12, "pv_to_load_kwh"] == pytest.approx(4.0, abs=1e-4)


def test_pv_sub_cap_default_self_consumption_limits_surplus():
    """Net billing + PV sub-cap: the full load is still covered (load-serving
    flow is behind the meter); only the PV surplus export is capped."""
    ts = _peak_ts()  # pv[12]=10, load[12]=4
    mi_pv = np.full(24, 100.0)
    mi_pv[12] = 60.0  # cap_pv[12] = 3
    params = _tiny_params(grid_cap_includes_load=False)
    params["max_injection_profile_pv"] = mi_pv
    res, _ = run_scenario(params, ts, **SOLVER_KW)
    assert res.loc[12, "pv_to_load_kwh"] == pytest.approx(4.0, abs=1e-4)
    assert res.loc[12, "pv_to_grid_kwh"] == pytest.approx(3.0, abs=1e-4)
    assert res.loc[12, "pv_curtail_kwh"] == pytest.approx(3.0, abs=1e-4)


def test_pv_sub_cap_merchant_limits_export():
    """Merchant + PV sub-cap: the surplus export is capped by the PV sub-cap."""
    ts = _peak_ts()  # pv[12]=10 (load ignored in merchant)
    mi_pv = np.full(24, 100.0)
    mi_pv[12] = 40.0  # cap_pv[12] = 5 * 0.40 = 2
    params = _tiny_params(mode="merchant")
    params["max_injection_profile_pv"] = mi_pv
    res, _ = run_scenario(params, ts, **SOLVER_KW)
    assert res.loc[12, "pv_to_grid_kwh"] == pytest.approx(2.0, abs=1e-4)
    assert res.loc[12, "pv_curtail_kwh"] == pytest.approx(8.0, abs=1e-4)


def test_bess_sub_cap_blocks_discharge_in_strict_mode():
    """VNB + BESS sub-cap = 0%: the battery cannot inject (discharge) that
    hour, so the load is grid-served — the 'battery 0% midday' case."""
    pv = [10.0, 0.0]
    load = [0.0, 4.0]
    ts = _tiny_ts(pv, load)  # 2 hourly steps from 00:00 -> hour-of-day 0, 1
    params = _tiny_params(
        grid_cap_includes_load=True,
        bess_power_kw=5.0,
        bess_capacity_kwh=10.0,
        efficiency_charge=1.0,
        efficiency_discharge=1.0,
        soc_min_frac=0.0,
        soc_max_frac=1.0,
        initial_soc_frac=0.0,
        terminal_soc_equal=False,
        max_cycles_per_day=10.0,
    )
    # Battery free to discharge -> serves the hour-1 load from storage.
    res_free, _ = run_scenario(params, ts, **SOLVER_KW)
    assert res_free.loc[1, "bess_dis_load_kwh"] == pytest.approx(4.0, abs=1e-4)
    assert res_free.loc[1, "grid_to_load_kwh"] == pytest.approx(0.0, abs=1e-4)
    # Battery blocked at hour 1 (mi_bess = 0 %) -> no discharge at all; grid serves.
    mi_bess = np.full(24, 100.0)
    mi_bess[1] = 0.0
    params_blocked = dict(params)
    params_blocked["max_injection_profile_bess"] = mi_bess
    res_blk, _ = run_scenario(params_blocked, ts, **SOLVER_KW)
    assert res_blk.loc[1, "bess_dis_load_kwh"] == pytest.approx(0.0, abs=1e-4)
    assert res_blk.loc[1, "bess_dis_grid_kwh"] == pytest.approx(0.0, abs=1e-4)
    assert res_blk.loc[1, "grid_to_load_kwh"] == pytest.approx(4.0, abs=1e-4)


def test_per_source_constraints_attached_only_when_profiles_present():
    """EXPORT_CAP_PV / EXPORT_CAP_BESS attach only when their profile is given;
    the combined EXPORT_CAP is always present."""
    ts = _peak_ts()
    m0 = build_model(_tiny_params(), ts)
    assert hasattr(m0, "EXPORT_CAP")
    assert not hasattr(m0, "EXPORT_CAP_PV")
    assert not hasattr(m0, "EXPORT_CAP_BESS")
    params = _tiny_params()
    params["max_injection_profile_pv"] = np.full(24, 70.0)
    params["max_injection_profile_bess"] = np.full(24, 50.0)
    m1 = build_model(params, ts)
    assert hasattr(m1, "EXPORT_CAP")
    assert hasattr(m1, "EXPORT_CAP_PV")
    assert hasattr(m1, "EXPORT_CAP_BESS")


@pytest.mark.parametrize("flag", [True, False])
def test_invariants_clean_with_binding_pv_sub_cap(flag):
    """invariant_7 (curtail) and invariant_9 (priority floor) stay clean when a
    PV sub-cap binds tighter than the combined cap, in BOTH billing modes.

    Regression guard: the priority floor and the curtail-vs-cap check must
    both recognise the PV sub-cap, otherwise a strict run with a binding
    sub-cap trips a spurious invariant violation (fatal under --strict).
    """
    ts = _peak_ts()  # pv[12]=10, load[12]=4, cap_total = 5
    mi_pv = np.full(24, 100.0)
    mi_pv[12] = 60.0  # cap_pv[12] = 3 < cap_total = 5
    params = _tiny_params(grid_cap_includes_load=flag)
    params["max_injection_profile_pv"] = mi_pv
    _res, _solver, full = run_scenario(
        params, ts, return_unrounded=True, **SOLVER_KW,
    )
    # The PV sub-cap is surfaced for the checks.
    assert "grid_export_cap_pv_kwh" in full.columns
    inv = verify_dispatch_invariants(full, params)
    assert inv["invariant_7_curtail_behavior_kwh"] == pytest.approx(0.0, abs=1e-9)
    assert inv["invariant_9_pv_load_priority_kwh"] == pytest.approx(0.0, abs=1e-6)
    assert inv["invariant_1_pv_balance_kwh"] == pytest.approx(0.0, abs=1e-6)
    assert inv["invariant_2_load_balance_kwh"] == pytest.approx(0.0, abs=1e-6)
