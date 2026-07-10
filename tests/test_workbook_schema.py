"""Workbook-schema tests.

Covers:

* The core sheet layout (``timeseries`` / ``project`` / ``pv`` / ``bess`` /
  ``economics`` / ``simulation`` / ``balancing`` /
  ``max_injection_profile``) plus the optional ``sizing`` / ``scenarios``
  / ``trajectories`` sheets.
* Round-trip preservation through ``write_workbook`` /
  ``read_workbook``.
* Sheet-aware unknown-key warnings.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.io import (
    BALANCING_SHEET_DEFAULTS,
    BESS_SHEET_DEFAULTS,
    ECONOMICS_SHEET_DEFAULTS,
    PPA_SHEET_DEFAULTS,
    PROJECT_SHEET_DEFAULTS,
    PV_SHEET_DEFAULTS,
    SIMULATION_SHEET_DEFAULTS,
    _parse_kv_sheet,
    read_workbook,
    write_workbook,
)

# ---------------------------------------------------------------------------
# Sheet defaults — keys per sheet
# ---------------------------------------------------------------------------


def test_project_sheet_keys():
    expected = {
        "project_lifecycle_years", "project_start_year", "mode",
        "p_grid_export_max_kw",
        "retail_tariff_eur_per_mwh", "allow_bess_grid_charging",
        "grid_charging_fee_eur_per_mwh", "grid_charging_fee_exempt",
        "grid_cap_includes_load",
        "unavailability_pct", "site_capex_eur", "site_devex_eur",
        "currency_format", "show_titles",
    }
    assert set(PROJECT_SHEET_DEFAULTS) == expected


def test_pv_sheet_keys():
    expected = {
        "pv_source",
        "latitude", "longitude", "tilt", "azimuth", "losses_pct",
        "weather_year", "raddatabase", "timeseries_path",
        "pv_nameplate_kwp",
        "pv_degradation_year1_pct", "pv_degradation_annual_pct",
        "capex_pv_eur_per_kw", "devex_pv_eur_per_kw",
        "opex_pv_eur_per_kwp",
    }
    assert set(PV_SHEET_DEFAULTS) == expected


def test_bess_sheet_keys():
    expected = {
        "bess_power_kw", "bess_capacity_kwh",
        "efficiency_charge", "efficiency_discharge",
        "soc_min_frac", "soc_max_frac", "initial_soc_frac",
        "terminal_soc_equal", "max_cycles_per_day",
        "capex_bess_eur_per_kwh", "devex_bess_eur_per_kw",
        "opex_bess_eur_per_kw",
        "bess_replacement_year", "bess_replacement_cost_pct",
        "bess_degradation_annual_pct", "bess_degradation_pct_per_cycle",
        "bess_eol_soh_pct",
        "bess_wear_cost_eur_per_mwh",
    }
    assert set(BESS_SHEET_DEFAULTS) == expected


def test_economics_sheet_keys():
    expected = {
        "discount_rate_pct", "opex_inflation_pct",
        "retail_inflation_pct", "dam_inflation_pct",
        "aggregator_fee_pct_revenue",
        "route_to_market_fee_eur_per_mwh",
        "optimizer_revenue_share_pct",
        "optimizer_floor_enabled", "optimizer_floor_eur_per_kw_year",
        "optimizer_term_year_from", "optimizer_term_year_to",
        "optimizer_margin_basis",
        "balancing_aggregator_fee_pct_revenue",
        "bess_toll_eur_per_mw_year", "bess_toll_year_from",
        "bess_toll_year_to", "bess_toll_merchant_treatment",
        "bess_toll_indexation_pct",
        "state_support_eur_per_mw_year", "state_support_year_from",
        "state_support_year_to",
        "state_support_clawback_threshold_eur_per_mw_year",
        "state_support_clawback_share_pct", "state_support_indexation_pct",
        "benchmark_lcoe_low_eur_per_mwh",
        "benchmark_lcoe_high_eur_per_mwh",
        "benchmark_lcos_low_eur_per_mwh",
        "benchmark_lcos_high_eur_per_mwh",
        "sensitivity_enabled", "sensitivity_capex_delta_pct",
        "sensitivity_opex_delta_pct", "sensitivity_revenue_delta_pct",
        "sensitivity_discount_rate_delta_pp",
        "sensitivity_ppa_price_delta_pct",
        "gearing_pct", "debt_interest_rate_pct", "debt_tenor_years",
        "debt_repayment",
        "grid_co2_intensity_kg_per_mwh", "grid_co2_annual_decline_pct",
    }
    assert set(ECONOMICS_SHEET_DEFAULTS) == expected


def test_balancing_sheet_keys():
    products = ("fcr", "afrr_up", "afrr_dn", "mfrr_up", "mfrr_dn")
    expected = {
        "balancing_enabled", "dam_capacity_share_pct",
        *(f"{p}_capacity_share_pct" for p in products),
        *(f"{p}_bid_acceptance_pct" for p in products),
        *(f"{p}_activation_probability_pct" for p in products),
        *(f"{p}_default_capacity_price_eur_per_mwh" for p in products),
        *(
            f"{p}_default_activation_price_eur_per_mwh"
            for p in products if p != "fcr"
        ),
        "fcr_required_duration_hours",
        "bm_settlement_minutes", "bm_soc_headroom_pct", "bm_inflation_pct",
        "bm_price_sigma_capacity_pct", "bm_price_sigma_activation_pct",
        "bm_mc_scenarios", "bm_random_seed",
    }
    assert set(BALANCING_SHEET_DEFAULTS) == expected


def test_ppa_sheet_keys():
    expected = {
        "ppa_enabled", "ppa_structure", "ppa_settlement",
        "ppa_price_eur_per_mwh", "ppa_volume_share_pct",
        "ppa_term_years", "ppa_inflation_pct",
        "ppa_negative_price_rule",
    }
    assert set(PPA_SHEET_DEFAULTS) == expected


def test_simulation_sheet_keys():
    expected = {
        "uncertainty_enabled", "uncertainty_compare_sources",
        "uncertainty_n_seeds", "uncertainty_window_hours",
        "uncertainty_commit_hours",
        "uncertainty_dam_enabled", "uncertainty_pv_enabled",
        "uncertainty_load_enabled",
        "uncertainty_sigma_dam", "uncertainty_sigma_pv",
        "uncertainty_sigma_load",
        "uncertainty_diagnostics_enabled",
        "imbalance_enabled", "imbalance_pricing",
        "imbalance_price_mult_short", "imbalance_price_mult_long",
        "plot_daily_scope", "plot_monthly_scope", "plot_yearly_scope",
    }
    assert set(SIMULATION_SHEET_DEFAULTS) == expected


# ---------------------------------------------------------------------------
# Repository workbook — all sheets exposed
# ---------------------------------------------------------------------------


def test_all_sheets_present(repo_input_xlsx):
    sheets = pd.ExcelFile(repo_input_xlsx).sheet_names
    assert set(sheets) == {
        "timeseries", "project", "pv", "bess", "economics",
        "simulation", "balancing", "ppa", "max_injection_profile",
        "max_injection_profile_pv", "max_injection_profile_bess",
        "sizing", "scenarios", "trajectories",
    }


def test_repo_workbook_kv_sheets_match_schema(repo_input_xlsx):
    """Every kv sheet of the shipped workbook carries exactly the schema keys."""
    defaults = {
        "project": PROJECT_SHEET_DEFAULTS,
        "pv": PV_SHEET_DEFAULTS,
        "bess": BESS_SHEET_DEFAULTS,
        "economics": ECONOMICS_SHEET_DEFAULTS,
        "balancing": BALANCING_SHEET_DEFAULTS,
        "ppa": PPA_SHEET_DEFAULTS,
        "simulation": SIMULATION_SHEET_DEFAULTS,
    }
    for sheet, schema in defaults.items():
        df = pd.read_excel(repo_input_xlsx, sheet_name=sheet)
        keys = set(df.iloc[:, 0].dropna().astype(str))
        assert keys == set(schema), f"sheet={sheet}"


def test_repo_workbook_loads_typed_dict(repo_input_xlsx):
    typed = read_workbook(repo_input_xlsx)
    for section in ("project", "pv", "bess", "economics", "simulation"):
        assert section in typed and isinstance(typed[section], dict)
    assert typed["project"]["mode"] == "self_consumption"
    # The case-study workbook ships the absolute 15 MW PV profile
    # (consumed verbatim, no rescale).
    assert typed["pv"]["pv_nameplate_kwp"] == pytest.approx(15000.0)
    assert typed["bess"]["bess_power_kw"] == pytest.approx(15000.0)
    assert "max_injection_profile" in typed
    profile = np.asarray(typed["max_injection_profile"])
    assert profile.shape == (24,) or profile.shape == (24, 12)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def _build_minimal_typed(year: int = 2026) -> dict:
    n = 24
    ts = pd.DataFrame({
        "timestamp": pd.date_range(f"{year}-01-01", periods=n, freq="h"),
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
        # max-injection semantic: 73 % of the export cap allowed.
        "max_injection_profile": np.full(24, 73.0, dtype=float),
    }


def test_round_trip(tmp_path):
    typed = _build_minimal_typed()
    dst = tmp_path / "roundtrip.xlsx"
    write_workbook(typed, dst)
    out = read_workbook(dst)
    for section in ("project", "pv", "bess", "economics", "simulation"):
        for key in typed[section]:
            assert out[section][key] == typed[section][key], (
                f"section={section} key={key}"
            )


def test_round_trip_emits_no_warnings(tmp_path, caplog):
    typed = _build_minimal_typed()
    dst = tmp_path / "roundtrip_clean.xlsx"
    write_workbook(typed, dst)
    with caplog.at_level("WARNING", logger="pvbess_opt.io"):
        read_workbook(dst)
    assert not any(
        rec.levelno >= logging.WARNING and rec.name.startswith("pvbess_opt.io")
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Per-sheet unknown-key warnings
# ---------------------------------------------------------------------------


def test_unknown_keys_warn_per_sheet(caplog):
    flat = {"discount_rate_pct": 8.0, "definitely_not_a_real_key": 1.0}
    with caplog.at_level("WARNING"):
        parsed = _parse_kv_sheet("economics", flat)
    assert parsed["discount_rate_pct"] == 8.0
    assert any(
        "definitely_not_a_real_key" in rec.getMessage()
        and "economics" in rec.getMessage()
        for rec in caplog.records
    )


def test_misplaced_key_routes_warning_to_correct_sheet(caplog):
    """capex_pv_eur_per_kw on the economics sheet warns about pv sheet."""
    flat = {"capex_pv_eur_per_kw": 525.0}
    with caplog.at_level("WARNING"):
        _parse_kv_sheet("economics", flat)
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "capex_pv_eur_per_kw" in msgs
    assert "pv" in msgs


# ---------------------------------------------------------------------------
# Max-injection-profile loader
# ---------------------------------------------------------------------------


def test_max_injection_profile_missing_logs_info(tmp_path, caplog):
    typed = _build_minimal_typed()
    dst = tmp_path / "no_max_inj.xlsx"
    write_workbook(typed, dst)
    # Re-open and drop the max_injection_profile sheet with openpyxl —
    # cell types stay faithful.  (A pandas read->write round trip can
    # coerce numeric 0/1 cells into genuine boolean cells, which the
    # boolean-in-numeric-field guard rightly rejects.)
    from openpyxl import load_workbook

    wb = load_workbook(dst)
    del wb["max_injection_profile"]
    wb.save(dst)
    with caplog.at_level("INFO", logger="pvbess_opt.io"):
        out = read_workbook(dst)
    # No-curtailment default (100 %) applied.
    assert np.allclose(np.asarray(out["max_injection_profile"]), 100.0)
    assert any(
        "max_injection_profile" in rec.getMessage().lower()
        for rec in caplog.records
    )
