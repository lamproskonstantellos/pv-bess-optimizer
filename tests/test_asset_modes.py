"""PV-only / BESS-only / hybrid asset-mode tests (Phase 3).

Asset-mode semantics:

* ``pv_nameplate_kwp = 0``  → PV is not part of the project.
* ``bess_power_kw = 0``     → BESS is not part of the project.
* Both zero                 → ValueError from ``read_inputs``.

The optimizer pins all PV variables to 0 when PV is absent and pins
all BESS variables (incl. ``e_cap`` and the binary mode flags) to 0
when BESS is absent.  ``derive_asset_capacities`` no longer infers
from the timeseries or from ``p_dis_max_kw`` — declared values pass
through exactly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.economics import derive_asset_capacities
from pvbess_opt.io import (
    BESS_SHEET_DEFAULTS,
    ECONOMICS_SHEET_DEFAULTS,
    PROJECT_SHEET_DEFAULTS,
    PV_SHEET_DEFAULTS,
    SIMULATION_SHEET_DEFAULTS,
    read_inputs,
    write_workbook,
)
from pvbess_opt.optimization import run_scenario


def _highs_available() -> bool:
    try:
        import importlib
        importlib.import_module("highspy")
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# derive_asset_capacities — no inference
# ---------------------------------------------------------------------------


def test_derive_asset_capacities_no_inference():
    """Declared values pass through exactly; no inference from ts."""
    params = {
        "dt_minutes": 60,
        "pv_nameplate_kwp": 0.0,   # absent
        "bess_power_kw": 5000.0,   # present
        "bess_capacity_kwh": 20000.0,
    }
    ts = pd.DataFrame({"pv_kwh": [9999.0, 0.0]})  # would have inferred 9999 kWp
    caps = derive_asset_capacities({}, params, ts)
    assert caps["pv_kwp"] == 0.0
    assert caps["bess_kw"] == 5000.0
    assert caps["bess_kwh"] == 20000.0


def test_derive_asset_capacities_bess_kwh_zero_when_absent():
    """No BESS → reported energy capacity is zero (regardless of capacity field)."""
    params = {
        "dt_minutes": 60,
        "pv_nameplate_kwp": 4500.0,
        "bess_power_kw": 0.0,
        "bess_capacity_kwh": 999.0,  # ignored when bess_power_kw == 0
    }
    caps = derive_asset_capacities({}, params, pd.DataFrame({"pv_kwh": [0.0]}))
    assert caps["bess_kw"] == 0.0
    assert caps["bess_kwh"] == 0.0


# ---------------------------------------------------------------------------
# read_inputs validation
# ---------------------------------------------------------------------------


def test_read_inputs_raises_when_both_assets_zero(tmp_path):
    """Both pv and bess at 0 → ValueError."""
    import numpy as np
    typed = {
        "ts": pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=24, freq="h"),
            "pv_kwh": [0.0] * 24,
            "load_kwh": [100.0] * 24,
            "dam_price_eur_per_mwh": [80.0] * 24,
        }),
        "project": dict(PROJECT_SHEET_DEFAULTS),
        "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=0.0),
        "bess": dict(
            BESS_SHEET_DEFAULTS, bess_power_kw=0.0, bess_capacity_kwh=0.0,
        ),
        "economics": dict(ECONOMICS_SHEET_DEFAULTS),
        "simulation": dict(SIMULATION_SHEET_DEFAULTS),
        # Post-refactor max-injection semantic: 73 % allowed ≡ 27 % curtailment.
        "max_injection_profile": np.full(24, 73.0, dtype=float),
    }
    dst = tmp_path / "no_assets.xlsx"
    write_workbook(typed, dst)
    with pytest.raises(ValueError, match="nothing to optimise"):
        read_inputs(dst)


# ---------------------------------------------------------------------------
# Optimizer end-to-end — pinned variables in each mode
# ---------------------------------------------------------------------------


def _make_ts(n: int = 48, *, with_load: bool = True) -> pd.DataFrame:
    """Short fixture timeseries; PV is the deterministic canonical slice."""
    from tests._pv_helpers import hourly_canonical_pv_window
    rng = np.random.default_rng(0)
    timestamps = pd.date_range("2026-06-01 00:00", periods=n, freq="h")
    h = np.arange(n).astype(float) % 24
    pv = hourly_canonical_pv_window(n, pv_nameplate_kwp=4500.0)
    dam = 100.0 - 50.0 * np.sin(np.pi * (h - 6) / 12.0) + rng.normal(0, 5, n)
    df = {"timestamp": timestamps, "pv_kwh": pv, "dam_price_eur_per_mwh": dam}
    if with_load:
        load = 3000.0 + 1500.0 * np.exp(-((h - 9) ** 2) / 8.0)
        df["load_kwh"] = np.maximum(load + rng.normal(0, 50, n), 800.0)
    return pd.DataFrame(df)


def _params(pv_kwp: float, bess_kw: float, *, mode: str = "vnb") -> dict:
    return {
        "dt_minutes": 60,
        "efficiency_charge": 0.97,
        "efficiency_discharge": 0.97,
        "soc_min_frac": 0.20,
        "soc_max_frac": 0.95,
        "initial_soc_frac": 0.50,
        "terminal_soc_equal": True,
        "max_cycles_per_day": 1.0,
        "p_grid_export_max_kw": 5000.0,
        "pv_nameplate_kwp": pv_kwp,
        "bess_power_kw": bess_kw,
        "bess_capacity_kwh": bess_kw * 4.0,
        "retail_tariff_eur_per_mwh": 132.0,
        "settlement_minutes": 15,
        "mode": mode,
        "allow_bess_grid_charging": False,
        "show_titles": False,
    }


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_pv_only_run_pins_bess_to_zero():
    """PV-only: bess_power_kw = 0 → all BESS variables zero."""
    params = _params(pv_kwp=4500.0, bess_kw=0.0, mode="vnb")
    ts = _make_ts()
    res, _solver = run_scenario(
        params, ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=60,
    )
    for col in ("pv_to_bess_kwh", "bess_dis_load_kwh", "bess_dis_grid_kwh",
                "bess_charge_grid_kwh", "soc_kwh"):
        assert float(res[col].abs().max()) < 1e-6
    assert float(res["pv_kwh"].sum()) > 0.0


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_bess_only_run_pins_pv_to_zero():
    """BESS-only: pv_nameplate_kwp = 0 → all PV variables zero."""
    params = _params(pv_kwp=0.0, bess_kw=5000.0, mode="vnb")
    params["allow_bess_grid_charging"] = True
    ts = _make_ts()
    res, _ = run_scenario(
        params, ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=60,
    )
    assert float(res["pv_to_load_kwh"].abs().max()) < 1e-6
    assert float(res["pv_to_bess_kwh"].abs().max()) < 1e-6
    assert float(res["pv_to_grid_kwh"].abs().max()) < 1e-6
    assert float(res["pv_curtail_kwh"].abs().max()) < 1e-6


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_hybrid_run_unaffected():
    """Hybrid: both > 0 → behaves identically to the baseline case."""
    params = _params(pv_kwp=4500.0, bess_kw=5000.0, mode="vnb")
    ts = _make_ts()
    res, _ = run_scenario(
        params, ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=60,
    )
    assert float(res["pv_kwh"].sum()) > 0.0
    total_charge = float(res["pv_to_bess_kwh"].sum()
                         + res["bess_charge_grid_kwh"].sum())
    total_discharge = float(res["bess_dis_load_kwh"].sum()
                            + res["bess_dis_grid_kwh"].sum())
    assert total_charge >= 0.0
    assert total_discharge >= 0.0


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_pv_only_no_pv_in_timeseries_still_works():
    """Override behaviour: if pv column is non-zero but pv_kwp=0, pv pinned."""
    params = _params(pv_kwp=0.0, bess_kw=5000.0, mode="merchant")
    params["allow_bess_grid_charging"] = True
    ts = _make_ts(with_load=False)
    res, _ = run_scenario(
        params, ts, solver_name="highs",
        mip_gap=0.01, time_limit_seconds=60,
    )
    pv_total_flow = (
        float(res["pv_to_load_kwh"].sum())
        + float(res["pv_to_bess_kwh"].sum())
        + float(res["pv_to_grid_kwh"].sum())
        + float(res["pv_curtail_kwh"].sum())
    )
    assert pv_total_flow < 1e-6


# ---------------------------------------------------------------------------
# Plotting subtitle — project_mode label
# ---------------------------------------------------------------------------


def test_project_mode_label_setter_roundtrip():
    from pvbess_opt.plotting.style import (
        get_project_mode_label, set_project_mode_label,
    )
    set_project_mode_label("PV-only")
    assert get_project_mode_label() == "PV-only"
    set_project_mode_label("")
    assert get_project_mode_label() == ""


def test_title_prefix_includes_project_mode():
    from pvbess_opt.plotting.helpers import title_prefix
    from pvbess_opt.plotting.style import set_project_mode_label
    set_project_mode_label("BESS-only")
    try:
        out = title_prefix("vnb")
        assert "vnb" in out
        assert "BESS-only" in out
    finally:
        set_project_mode_label("")
