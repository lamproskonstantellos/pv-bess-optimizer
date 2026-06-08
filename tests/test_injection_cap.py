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
from pvbess_opt.optimization import run_scenario

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
        "settlement_minutes": 60,
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
# 4. Infeasibility: strict cap cannot honour the forced load-priority injection
# ---------------------------------------------------------------------------


def test_strict_infeasible_raises_and_flag_false_solves():
    # min(pv, load) = 6 > cap = 5 at the peak step.
    pv = [0.0] * 24
    load = [0.0] * 24
    pv[12] = 8.0
    load[12] = 6.0
    ts = _tiny_ts(pv, load)
    # Flag True must fail clearly (ValueError from the validator; RuntimeError
    # if it somehow reached the solver — either is acceptable).
    with pytest.raises((ValueError, RuntimeError)):
        run_scenario(_tiny_params(grid_cap_includes_load=True), ts, **SOLVER_KW)
    # The same case with the flag False solves and never relaxes priority.
    res, _ = run_scenario(
        _tiny_params(grid_cap_includes_load=False), ts, **SOLVER_KW,
    )
    assert res.loc[12, "pv_to_load_kwh"] == pytest.approx(6.0, abs=1e-4)


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
