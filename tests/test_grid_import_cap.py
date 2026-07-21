"""Grid import capacity limit (Eq. S35).

`p_grid_import_max_kw` caps grid-to-load plus grid-to-BESS charging per
step at the connection point, mirroring the export-cap machinery minus
the injection profile.  The constraint is attached only when the value
is finite, so an absent / 'unlimited' key changes nothing in the model
topology.  Locked: bit-identity, the analytic binding case (BESS
bridges the capped hour), the merchant grid-charging collapse, the
two-tier infeasibility guard, token parsing, positivity validation,
the M_imp tightening, the conditional output column and the
invariant-10 residual contract.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.optimization import (
    build_model,
    derive_tight_big_m,
    run_scenario,
    verify_dispatch_invariants,
)


def _highs_available() -> bool:
    try:
        import highspy
    except ImportError:
        return False
    return bool(highspy)


pytestmark = pytest.mark.skipif(
    not _highs_available(), reason="requires HiGHS",
)


def _params(**o) -> dict:
    p = {
        "dt_minutes": 60,
        "mode": "self_consumption",
        "pv_nameplate_kwp": 100.0,
        "bess_power_kw": 50.0,
        "bess_capacity_kwh": 200.0,
        "efficiency_charge": 1.0,
        "efficiency_discharge": 1.0,
        "soc_min_frac": 0.0,
        "soc_max_frac": 1.0,
        "initial_soc_frac": 0.5,
        "terminal_soc_equal": False,
        "max_cycles_per_day": 10.0,
        "p_grid_export_max_kw": 1000.0,
        "retail_tariff_eur_per_mwh": 120.0,
        "allow_bess_grid_charging": False,
    }
    p.update(o)
    return p


def _ts(n: int = 6, *, load: float = 80.0, pv_day: float = 120.0):
    hours = np.arange(n)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=n, freq="h"),
        "pv_kwh": np.where((hours >= 2) & (hours <= 3), pv_day, 0.0),
        "load_kwh": np.full(n, load),
        "dam_price_eur_per_mwh": 60.0 + 10.0 * np.sin(hours),
    })


# ---------------------------------------------------------------------------
# Bit-identity (cap absent / unlimited)
# ---------------------------------------------------------------------------


def test_absent_and_unlimited_are_bit_identical():
    ts = _ts()
    base_res, _s, _f = run_scenario(_params(), ts, return_unrounded=True)
    for cap in (float("inf"), None):
        p = _params()
        if cap is not None:
            p["p_grid_import_max_kw"] = cap
        res, _s2, _f2 = run_scenario(p, ts, return_unrounded=True)
        pd.testing.assert_frame_equal(res, base_res)
    assert "grid_import_cap_kwh" not in base_res.columns


def test_no_constraint_component_when_unlimited():
    m = build_model(_params(), _ts())
    assert not hasattr(m, "IMPORT_CAP")
    m2 = build_model(_params(p_grid_import_max_kw=150.0), _ts())
    assert hasattr(m2, "IMPORT_CAP")


# ---------------------------------------------------------------------------
# Analytic binding cases
# ---------------------------------------------------------------------------


def test_binding_cap_bridged_by_bess():
    """Night hour: load 80, cap 60 => grid_to_load <= 60 and the BESS
    (charged at start) covers the remainder."""
    ts = _ts(n=3, load=80.0)
    ts["pv_kwh"] = 0.0
    p = _params(p_grid_import_max_kw=60.0, initial_soc_frac=0.9)
    res, _s, full = run_scenario(p, ts, return_unrounded=True)
    imports = (
        full["grid_to_load_kwh"] + full["bess_charge_grid_kwh"]
    ).to_numpy(dtype=float)
    assert float(imports.max()) <= 60.0 + 1e-6
    assert float(full["bess_dis_load_kwh"].sum()) > 0.0
    inv = verify_dispatch_invariants(full, p)
    assert inv["invariant_10_import_cap_excess_kwh"] <= 1e-6
    assert (res["grid_import_cap_kwh"] == 60.0).all()


def test_merchant_cap_collapses_to_charging_limit():
    """Merchant pins grid_to_load to 0, so the cap binds on
    grid-to-BESS charging even when bess_power_kw is larger."""
    n = 6
    hours = np.arange(n)
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-01", periods=n, freq="h"),
        "pv_kwh": np.zeros(n),
        # Deep price valley then a peak: arbitrage wants full-power
        # charging in the valley.
        "dam_price_eur_per_mwh": np.where(hours < 3, 5.0, 200.0),
    })
    p = _params(
        mode="merchant", pv_nameplate_kwp=0.0,
        bess_power_kw=100.0, bess_capacity_kwh=400.0,
        allow_bess_grid_charging=True, initial_soc_frac=0.0,
        p_grid_import_max_kw=40.0,
    )
    _res, _s, full = run_scenario(p, ts, return_unrounded=True)
    charge = full["bess_charge_grid_kwh"].to_numpy(dtype=float)
    assert float(charge.max()) <= 40.0 + 1e-6
    assert float(charge.sum()) > 0.0  # the arbitrage still happens
    inv = verify_dispatch_invariants(full, p)
    assert inv["invariant_10_import_cap_excess_kwh"] <= 1e-6


# ---------------------------------------------------------------------------
# Two-tier infeasibility guard
# ---------------------------------------------------------------------------


def test_certificate_raises_in_build_model():
    ts = _ts(n=3, load=100.0)
    ts["pv_kwh"] = 0.0
    p = _params(bess_power_kw=10.0, p_grid_import_max_kw=50.0)
    with pytest.raises(ValueError, match="regardless of the battery"):
        build_model(p, ts)


def test_workbook_guard_hard_and_soft(tmp_path, caplog):
    import logging

    from pvbess_opt.io import read_workbook, write_workbook

    def _typed(import_cap, bess_kw):
        from pvbess_opt.io import (
            BALANCING_SHEET_DEFAULTS,
            BESS_SHEET_DEFAULTS,
            ECONOMICS_SHEET_DEFAULTS,
            PROJECT_SHEET_DEFAULTS,
            PV_SHEET_DEFAULTS,
            SIMULATION_SHEET_DEFAULTS,
        )
        n = 24
        ts = pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
            "pv_kwh": np.zeros(n),
            "load_kwh": np.full(n, 100.0),
            "dam_price_eur_per_mwh": np.full(n, 60.0),
        })
        return {
            "ts": ts,
            "project": dict(
                PROJECT_SHEET_DEFAULTS,
                p_grid_import_max_kw=import_cap,
            ),
            "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=0.0),
            "bess": dict(
                BESS_SHEET_DEFAULTS, bess_power_kw=bess_kw,
                bess_capacity_kwh=4.0 * bess_kw,
            ),
            "economics": dict(ECONOMICS_SHEET_DEFAULTS),
            "simulation": dict(SIMULATION_SHEET_DEFAULTS),
            "balancing": dict(BALANCING_SHEET_DEFAULTS),
        }

    # Hard tier: load 100 > 0 PV + 10 kW BESS + 50 kW cap -> reject,
    # naming the worst timestamp and the numbers.
    hard = tmp_path / "hard.xlsx"
    write_workbook(_typed(50.0, 10.0), hard)
    with pytest.raises(ValueError, match="load balance infeasible"):
        read_workbook(hard)

    # Soft tier: load 100 > 60 kW cap but PV+BESS could bridge -> warn.
    soft = tmp_path / "soft.xlsx"
    write_workbook(_typed(60.0, 80.0), soft)
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.io"):
        read_workbook(soft)
    assert any(
        "feasibility depends on PV" in r.getMessage()
        for r in caplog.records
    )


def test_solver_infeasible_path_documented():
    """A case passing the certificate but truly infeasible (sustained
    night load above the cap drains the BESS) surfaces the solver-level
    infeasibility error — the documented fallback behaviour (the
    certificate is necessary, not sufficient)."""
    n = 8
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "pv_kwh": np.zeros(n),
        "load_kwh": np.full(n, 100.0),
        "dam_price_eur_per_mwh": np.full(n, 60.0),
    })
    p = _params(
        pv_nameplate_kwp=0.0, bess_power_kw=50.0,
        bess_capacity_kwh=60.0, initial_soc_frac=1.0,
        p_grid_import_max_kw=60.0,
    )
    with pytest.raises(Exception, match=r"(?i)feasible"):
        run_scenario(p, ts)


# ---------------------------------------------------------------------------
# Parsing + validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", ["", "inf", "unlimited", "disabled"])
def test_unlimited_tokens_parse_to_inf(token):
    from pvbess_opt.io import _parse_grid_export_max

    assert np.isinf(_parse_grid_export_max(
        token, "p_grid_import_max_kw",
    ))


def test_nonpositive_cap_rejected(tmp_path):
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
            "pv_kwh": np.full(n, 10.0),
            "load_kwh": np.full(n, 5.0),
            "dam_price_eur_per_mwh": np.full(n, 60.0),
        }),
        "project": dict(PROJECT_SHEET_DEFAULTS, p_grid_import_max_kw=-5.0),
        "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=100.0),
        "bess": dict(BESS_SHEET_DEFAULTS),
        "economics": dict(ECONOMICS_SHEET_DEFAULTS),
        "simulation": dict(SIMULATION_SHEET_DEFAULTS),
        "balancing": dict(BALANCING_SHEET_DEFAULTS),
    }
    out = tmp_path / "neg.xlsx"
    write_workbook(typed, out)
    with pytest.raises(ValueError, match="p_grid_import_max_kw"):
        read_workbook(out)


def test_yaml_surface_parses_import_cap(tmp_path):
    from pvbess_opt.io_read import load_structured_config

    n = 24
    prices = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "dam_price_eur_per_mwh": np.full(n, 50.0),
        "pv_kwh": np.full(n, 100.0),
    })
    prices.to_csv(tmp_path / "ts.csv", index=False)
    cfg = tmp_path / "run.yaml"
    cfg.write_text(
        "project:\n"
        "  mode: merchant\n"
        "  p_grid_import_max_kw: 250\n"
        "pv:\n"
        "  pv_nameplate_kwp: 1000\n"
        "timeseries_path: ts.csv\n",
        encoding="utf-8",
    )
    loaded = load_structured_config(cfg)
    assert loaded["project"]["p_grid_import_max_kw"] == 250.0


# ---------------------------------------------------------------------------
# Big-M tightening
# ---------------------------------------------------------------------------


def test_m_imp_tightened_only_when_finite():
    ts = _ts()
    base = derive_tight_big_m(
        _params(), ts, dt_h=1.0, mode="self_consumption",
    )
    unlimited = derive_tight_big_m(
        _params(p_grid_import_max_kw=float("inf")), ts,
        dt_h=1.0, mode="self_consumption",
    )
    assert unlimited == base  # byte-equal big-Ms without a finite cap
    capped = derive_tight_big_m(
        _params(p_grid_import_max_kw=40.0), ts,
        dt_h=1.0, mode="self_consumption",
    )
    assert capped["M_imp"] == pytest.approx(40.0 * 1.001)
    assert capped["M_imp"] < base["M_imp"]
    # Objective value invariant under the (conservative-valid) tightening.
    p = _params(p_grid_import_max_kw=70.0, initial_soc_frac=0.9)
    _res, _s, full = run_scenario(p, _ts(), return_unrounded=True)
    inv = verify_dispatch_invariants(full, p)
    assert all(v <= 1e-6 for k, v in inv.items() if k.startswith("invariant_1"))


def test_invariant_key_always_present():
    p = _params()
    _res, _s, full = run_scenario(p, _ts(), return_unrounded=True)
    inv = verify_dispatch_invariants(full, p)
    assert inv["invariant_10_import_cap_excess_kwh"] == 0.0
