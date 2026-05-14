"""Workbook I/O regression tests for ``inputs/input.xlsx``.

The repo's canonical workbook is also the executable contract for the
v0.8 seven-sheet schema. This test loads it, checks every typed key
for type and plausible range, locks down ``p_grid_export_max_kw`` to
the project sheet, walks the timeseries shape, sanity-checks the
curtailment profile, and finally round-trips the typed dict through
``write_workbook`` / ``read_workbook`` to catch any silent re-coding.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.io import read_workbook, write_workbook

ROOT = Path(__file__).resolve().parent.parent
INPUT_XLSX = ROOT / "inputs" / "input.xlsx"


# ---------------------------------------------------------------------------
# Per-sheet typed-section contracts.
# ---------------------------------------------------------------------------

# Each entry: key -> (expected type, lower bound, upper bound).  When the
# bound is None the corresponding inequality is skipped (useful for ints
# like project_start_year that have no meaningful upper bound).

_PROJECT_CONTRACT: dict[str, tuple[type, float | None, float | None]] = {
    "project_lifecycle_years": (int, 1, 100),
    "project_start_year": (int, 1900, 2100),
    "mode": (str, None, None),
    "settlement_minutes": (int, 1, 1440),
    "p_grid_export_max_kw": (float, 0.0, None),
    "retail_tariff_eur_per_mwh": (float, 0.0, None),
    "allow_bess_grid_charging": (bool, None, None),
    "unavailability_pct": (float, 0.0, 100.0),
    "currency_format": (str, None, None),
    "show_titles": (bool, None, None),
}

_PV_CONTRACT: dict[str, tuple[type, float | None, float | None]] = {
    "pv_nameplate_kwp": (float, 0.0, None),
    "specific_production_kwh_per_kwp": (float, 0.0, None),
    "pv_degradation_year1_pct": (float, 0.0, 100.0),
    "pv_degradation_annual_pct": (float, 0.0, 100.0),
    "capex_pv_eur_per_kw": (float, 0.0, None),
    "devex_pv_eur_per_kw": (float, 0.0, None),
    "opex_pv_eur_per_kwp": (float, 0.0, None),
}

_BESS_CONTRACT: dict[str, tuple[type, float | None, float | None]] = {
    "bess_power_kw": (float, 0.0, None),
    "bess_capacity_kwh": (float, 0.0, None),
    "efficiency_charge": (float, 0.0, 1.0),
    "efficiency_discharge": (float, 0.0, 1.0),
    "soc_min_frac": (float, 0.0, 1.0),
    "soc_max_frac": (float, 0.0, 1.0),
    "initial_soc_frac": (float, 0.0, 1.0),
    "terminal_soc_equal": (bool, None, None),
    "max_cycles_per_day": (float, 0.0, None),
    "capex_bess_eur_per_kw": (float, 0.0, None),
    "devex_bess_eur_per_kw": (float, 0.0, None),
    "opex_bess_eur_per_kw": (float, 0.0, None),
    "bess_replacement_year": (int, 0, 200),
    "bess_replacement_cost_pct": (float, 0.0, 100.0),
    "bess_degradation_annual_pct": (float, 0.0, 100.0),
}

_ECONOMICS_CONTRACT: dict[str, tuple[type, float | None, float | None]] = {
    "discount_rate_pct": (float, 0.0, 100.0),
    "opex_inflation_pct": (float, -100.0, 100.0),
    "retail_inflation_pct": (float, -100.0, 100.0),
    "dam_inflation_pct": (float, -100.0, 100.0),
    "aggregator_fee_pct_revenue": (float, 0.0, 100.0),
    "benchmark_lcoe_low_eur_per_mwh": (float, 0.0, None),
    "benchmark_lcoe_high_eur_per_mwh": (float, 0.0, None),
    "benchmark_lcos_low_eur_per_mwh": (float, 0.0, None),
    "benchmark_lcos_high_eur_per_mwh": (float, 0.0, None),
    "sensitivity_enabled": (bool, None, None),
    "sensitivity_capex_delta_pct": (float, 0.0, 100.0),
    "sensitivity_opex_delta_pct": (float, 0.0, 100.0),
    "sensitivity_revenue_delta_pct": (float, 0.0, 100.0),
    "sensitivity_discount_rate_delta_pp": (float, 0.0, 100.0),
}

_SIMULATION_CONTRACT: dict[str, tuple[type, float | None, float | None]] = {
    "uncertainty_enabled": (bool, None, None),
    "uncertainty_compare_sources": (bool, None, None),
    "uncertainty_n_seeds": (int, 1, 10_000),
    "uncertainty_window_hours": (int, 1, 8760),
    "uncertainty_commit_hours": (int, 1, 8760),
    "uncertainty_dam_enabled": (bool, None, None),
    "uncertainty_pv_enabled": (bool, None, None),
    "uncertainty_load_enabled": (bool, None, None),
    "uncertainty_sigma_dam": (float, 0.0, 10.0),
    "uncertainty_sigma_pv": (float, 0.0, 10.0),
    "uncertainty_sigma_load": (float, 0.0, 10.0),
    "plot_daily_scope": (str, None, None),
    "plot_monthly_scope": (str, None, None),
    "plot_yearly_scope": (str, None, None),
}

_SECTION_CONTRACTS: dict[str, dict[str, tuple[type, float | None, float | None]]] = {
    "project": _PROJECT_CONTRACT,
    "pv": _PV_CONTRACT,
    "bess": _BESS_CONTRACT,
    "economics": _ECONOMICS_CONTRACT,
    "simulation": _SIMULATION_CONTRACT,
}


def _check_section(section_name: str, section: dict, contract: dict) -> None:
    for key, (expected_type, lo, hi) in contract.items():
        assert key in section, f"{section_name}.{key} missing"
        value = section[key]
        # bool is a subclass of int — narrow the check.
        if expected_type is bool:
            assert isinstance(value, bool), (
                f"{section_name}.{key}={value!r} expected bool, got {type(value)}"
            )
            continue
        if expected_type is int:
            assert isinstance(value, int) and not isinstance(value, bool), (
                f"{section_name}.{key}={value!r} expected int, got {type(value)}"
            )
        elif expected_type is float:
            assert isinstance(value, (int, float)) and not isinstance(value, bool), (
                f"{section_name}.{key}={value!r} expected float, got {type(value)}"
            )
        elif expected_type is str:
            assert isinstance(value, str), (
                f"{section_name}.{key}={value!r} expected str, got {type(value)}"
            )
        if lo is not None:
            assert float(value) >= float(lo), (
                f"{section_name}.{key}={value!r} below lower bound {lo}"
            )
        if hi is not None:
            assert float(value) <= float(hi), (
                f"{section_name}.{key}={value!r} above upper bound {hi}"
            )


# ---------------------------------------------------------------------------
# Full round-trip
# ---------------------------------------------------------------------------


def test_full_roundtrip(tmp_path):
    typed = read_workbook(INPUT_XLSX)

    # Per-sheet typed-section contract: types + plausible ranges.
    for section_name, contract in _SECTION_CONTRACTS.items():
        _check_section(section_name, typed[section_name], contract)

    # Grid-export limit lives on `project`, not on `pv` / `bess`.
    assert "p_grid_export_max_kw" in typed["project"]
    assert "p_grid_export_max_kw" not in typed["pv"]
    assert "p_grid_export_max_kw" not in typed["bess"]

    # Timeseries shape, columns, NaNs, and monotonic 15-minute index.
    ts = typed["ts"]
    required_cols = {"timestamp", "load_kwh", "pv_kwh", "dam_price_eur_per_mwh"}
    assert required_cols.issubset(ts.columns), (
        f"timeseries missing columns: {required_cols - set(ts.columns)}"
    )
    assert len(ts) == 35040
    for col in ("load_kwh", "pv_kwh", "dam_price_eur_per_mwh"):
        assert not ts[col].isna().any(), f"timeseries.{col} contains NaN"
    timestamps = pd.to_datetime(ts["timestamp"])
    diffs = timestamps.diff().dropna().unique()
    assert len(diffs) == 1, (
        f"timeseries timestamps must be uniform; got {len(diffs)} distinct deltas"
    )
    assert pd.Timedelta(diffs[0]) == pd.Timedelta(minutes=15)
    assert (timestamps.diff().dropna() > pd.Timedelta(0)).all()

    # Curtailment profile: shape (24,) or (24, 12), values in [0, 100].
    profile = np.asarray(typed["curtailment_profile"], dtype=float)
    assert profile.shape == (24,) or profile.shape == (24, 12), (
        f"curtailment_profile shape {profile.shape} not in [(24,), (24, 12)]"
    )
    assert (profile >= 0.0).all() and (profile <= 100.0).all()

    # Round-trip: write the typed dict out, read it back, compare every
    # key under float tolerance.
    dst = tmp_path / "roundtrip.xlsx"
    write_workbook(typed, dst)
    typed_rt = read_workbook(dst)

    for section_name in ("project", "pv", "bess", "economics", "simulation"):
        original = typed[section_name]
        roundtrip = typed_rt[section_name]
        assert set(original) == set(roundtrip), (
            f"{section_name}: key set drifted across round-trip "
            f"(missing: {set(original) - set(roundtrip)}; "
            f"extra: {set(roundtrip) - set(original)})"
        )
        for key, original_value in original.items():
            roundtrip_value = roundtrip[key]
            if isinstance(original_value, float):
                assert roundtrip_value == pytest.approx(original_value, rel=1e-9, abs=1e-9), (
                    f"{section_name}.{key} drifted: {original_value} -> {roundtrip_value}"
                )
            else:
                assert roundtrip_value == original_value, (
                    f"{section_name}.{key} drifted: {original_value!r} -> {roundtrip_value!r}"
                )

    # Curtailment profile round-trips numerically.
    profile_rt = np.asarray(typed_rt["curtailment_profile"], dtype=float)
    assert profile_rt.shape == profile.shape
    assert np.allclose(profile_rt, profile, rtol=1e-9, atol=1e-9)
