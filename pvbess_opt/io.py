"""Excel input parsing and output writing for the PV+BESS optimizer.

The schema is **seven sheets**, one logical theme per sheet:

* ``timeseries`` — per-step data with lowercase snake_case column names:
  ``timestamp``, ``load_kwh``, ``pv_kwh``, ``dam_price_eur_per_mwh``,
  optional ``retail_price_eur_per_mwh``, optional ``pv_kwh_override``.
  ``pv_kwh_override`` — when populated for every row, the loader uses
  the column verbatim and bypasses the
  ``pv_nameplate_kwp`` × ``specific_production_kwh_per_kwp`` rescaling.
  Use this when you have your own 15-min PV timeseries from another
  model or measurements.
* ``project`` — high-level run config (lifecycle horizon, mode,
  settlement, retail tariff, grid export limit, currency / title flags).
* ``pv`` — PV nameplate, specific production, degradation, CAPEX /
  DEVEX / OPEX.
* ``bess`` — BESS power and capacity, efficiency, SOC bounds, cycles,
  CAPEX / DEVEX / OPEX, replacement and degradation.
* ``economics`` — discount rate, inflation indices, aggregator fee,
  sensitivity deltas.
* ``simulation`` — uncertainty (rolling-horizon Monte Carlo) and plot
  scope flags.
* ``max_injection_profile`` — hour-of-day cap profile (24 rows),
  optionally with one column per calendar month, expressing the share
  of ``p_grid_export_max_kw`` available for export.  Missing → fall
  back to the no-curtailment default (a flat 100 %) and log INFO.

Public loader API
-----------------

* :func:`read_workbook` returns the typed nested dict:

  .. code-block:: python

     {
         "ts": pd.DataFrame,               # lowercase snake_case
         "project":            {...},
         "pv":                 {...},
         "bess":               {...},
         "economics":          {...},
         "simulation":         {...},
         "max_injection_profile": np.ndarray,  # shape (24,) or (24, 12)
         "dt_minutes": int,                # auto-detected from the timeseries
     }

* :func:`read_inputs` returns a flat ``(params, ts)`` tuple suitable for
  the optimizer / KPI / lifetime modules.

Mode-specific timeseries semantics
----------------------------------

* In ``self_consumption`` mode the ``load_kwh`` column is required; missing → ValueError.
* In ``merchant`` mode ``load_kwh`` is optional — if present, the loader
  logs an INFO message and the optimizer pins all load-coverage flows to 0.

Removed keys
--------------------

"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .config import DEFAULT_MAX_INJECTION_PCT_HOURLY
from .constants import (
    BENCHMARK_LCOE_HIGH_EUR_PER_MWH,
    BENCHMARK_LCOE_LOW_EUR_PER_MWH,
    BENCHMARK_LCOS_HIGH_EUR_PER_MWH,
    BENCHMARK_LCOS_LOW_EUR_PER_MWH,
    DEFAULT_SENSITIVITY_DELTA_PCT,
    DEFAULT_SENSITIVITY_DISCOUNT_RATE_DELTA_PP,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BALANCING_SHEET_DEFAULTS",
    "BESS_SHEET_DEFAULTS",
    "ECONOMICS_SHEET_DEFAULTS",
    "FALSY",
    "LAYOUT_SUBDIRS",
    "PROJECT_SHEET_DEFAULTS",
    "PV_SHEET_DEFAULTS",
    "SIMULATION_SHEET_DEFAULTS",
    "TRUTHY",
    "copy_input_snapshot",
    "detect_timestep_minutes",
    "make_run_layout",
    "read_inputs",
    "read_workbook",
    "write_assumptions_summary",
    "write_dispatch_artifacts",
    "write_results_workbook",
    "write_workbook",
]

TRUTHY = {"true", "1", "yes", "y", "t"}
FALSY = {"false", "0", "no", "n", "f"}

# Tokens that disable the grid-export cap (treat as unlimited export).
# An empty cell is also treated as unlimited — see _parse_grid_export_max.
_GRID_EXPORT_UNLIMITED_TOKENS = {
    "inf", "infinity", "unlimited", "disabled", "none",
}

_COERCE_FAILED = object()


# ---------------------------------------------------------------------------
# Canonical defaults (single source of truth)
# ---------------------------------------------------------------------------

PROJECT_SHEET_DEFAULTS: dict[str, Any] = {
    "project_lifecycle_years": 20,
    "project_start_year": 2026,
    "mode": "self_consumption",
    "settlement_minutes": 15,
    "p_grid_export_max_kw": 5000.0,
    "retail_tariff_eur_per_mwh": 120.0,
    "allow_bess_grid_charging": False,
    "unavailability_pct": 1.0,
    "site_capex_eur": 0.0,
    "site_devex_eur": 0.0,
    "currency_format": "auto",
    "show_titles": False,
}

PV_SHEET_DEFAULTS: dict[str, Any] = {
    "pv_nameplate_kwp": 0.0,
    "specific_production_kwh_per_kwp": 1500.0,
    "pv_degradation_year1_pct": 2.5,
    "pv_degradation_annual_pct": 0.55,
    "capex_pv_eur_per_kw": 525.0,
    "devex_pv_eur_per_kw": 60.0,
    "opex_pv_eur_per_kwp": 7.0,
}

BESS_SHEET_DEFAULTS: dict[str, Any] = {
    "bess_power_kw": 0.0,
    "bess_capacity_kwh": 0.0,
    "efficiency_charge": 0.97,
    "efficiency_discharge": 0.97,
    "soc_min_frac": 0.20,
    "soc_max_frac": 0.95,
    "initial_soc_frac": 0.50,
    "terminal_soc_equal": True,
    "max_cycles_per_day": 1.0,
    "capex_bess_eur_per_kw": 200.0,
    "devex_bess_eur_per_kw": 30.0,
    "opex_bess_eur_per_kw": 14.0,
    "bess_replacement_year": 0,
    "bess_replacement_cost_pct": 50.0,
    "bess_degradation_annual_pct": 2.0,
    # LFP cycle-fade default (matches the canonical workbook row in
    # _BESS_ROWS and the schema default); range 0.005-0.010.
    "bess_degradation_pct_per_cycle": 0.008,
}

ECONOMICS_SHEET_DEFAULTS: dict[str, Any] = {
    "discount_rate_pct": 7.0,
    "opex_inflation_pct": 1.0,
    "retail_inflation_pct": 0.0,
    "dam_inflation_pct": 0.0,
    "aggregator_fee_pct_revenue": 10.0,
    "benchmark_lcoe_low_eur_per_mwh": BENCHMARK_LCOE_LOW_EUR_PER_MWH,
    "benchmark_lcoe_high_eur_per_mwh": BENCHMARK_LCOE_HIGH_EUR_PER_MWH,
    "benchmark_lcos_low_eur_per_mwh": BENCHMARK_LCOS_LOW_EUR_PER_MWH,
    "benchmark_lcos_high_eur_per_mwh": BENCHMARK_LCOS_HIGH_EUR_PER_MWH,
    "sensitivity_enabled": True,
    "sensitivity_capex_delta_pct": DEFAULT_SENSITIVITY_DELTA_PCT,
    "sensitivity_opex_delta_pct": DEFAULT_SENSITIVITY_DELTA_PCT,
    "sensitivity_revenue_delta_pct": DEFAULT_SENSITIVITY_DELTA_PCT,
    "sensitivity_discount_rate_delta_pp": DEFAULT_SENSITIVITY_DISCOUNT_RATE_DELTA_PP,
}

BALANCING_SHEET_DEFAULTS: dict[str, Any] = {
    # Master switch — when False the MILP and KPIs are bit-identical to
    # a workbook without the sheet.
    "balancing_enabled": False,
    # Per-product capacity shares (% of bess_power_kw). DAM keeps the
    # majority; the sum across all six lines must stay <= 100 %.
    "dam_capacity_share_pct": 70.0,
    "fcr_capacity_share_pct": 10.0,
    "afrr_up_capacity_share_pct": 8.0,
    "afrr_dn_capacity_share_pct": 7.0,
    "mfrr_up_capacity_share_pct": 3.0,
    "mfrr_dn_capacity_share_pct": 2.0,
    # Per-product bid-acceptance probabilities (% of submitted bids that
    # clear the auction).
    "fcr_bid_acceptance_pct": 70.0,
    "afrr_up_bid_acceptance_pct": 55.0,
    "afrr_dn_bid_acceptance_pct": 55.0,
    "mfrr_up_bid_acceptance_pct": 40.0,
    "mfrr_dn_bid_acceptance_pct": 40.0,
    # Per-product activation probabilities (% of cleared bids that get
    # activated within a settlement period).
    "fcr_activation_probability_pct": 15.0,
    "afrr_up_activation_probability_pct": 10.0,
    "afrr_dn_activation_probability_pct": 8.0,
    "mfrr_up_activation_probability_pct": 5.0,
    "mfrr_dn_activation_probability_pct": 4.0,
    # Per-product fallback capacity prices used when the timeseries
    # column is absent (EUR per MWh).
    "fcr_default_capacity_price_eur_per_mwh": 12.0,
    "afrr_up_default_capacity_price_eur_per_mwh": 18.0,
    "afrr_dn_default_capacity_price_eur_per_mwh": 15.0,
    "mfrr_up_default_capacity_price_eur_per_mwh": 6.0,
    "mfrr_dn_default_capacity_price_eur_per_mwh": 5.0,
    # Per-product fallback activation prices (EUR per MWh). FCR has no
    # activation payment so it is absent here.
    "afrr_up_default_activation_price_eur_per_mwh": 220.0,
    "afrr_dn_default_activation_price_eur_per_mwh": 25.0,
    "mfrr_up_default_activation_price_eur_per_mwh": 180.0,
    "mfrr_dn_default_activation_price_eur_per_mwh": 20.0,
    # FCR-specific duration requirement (hours of sustained output
    # required for the reservation to be certifiable).
    "fcr_required_duration_hours": 0.5,
    # Settlement period (minutes); must equal 60 * dt_hours when
    # balancing_enabled.
    "bm_settlement_minutes": 15,
    # Extra SOC safety buffer applied on top of the worst-case
    # activation reservation (percent of activation energy).
    "bm_soc_headroom_pct": 10.0,
    # Yearly indexation rate applied to balancing revenue lines in the
    # multi-year cashflow.
    "bm_inflation_pct": 2.0,
    # Log-normal sigmas (in percent) used by the Monte Carlo to perturb
    # capacity and activation prices around the deterministic schedule.
    "bm_price_sigma_capacity_pct": 25.0,
    "bm_price_sigma_activation_pct": 35.0,
    # Default Monte Carlo seed for the balancing realisation.
    "bm_random_seed": 1729,
}

SIMULATION_SHEET_DEFAULTS: dict[str, Any] = {
    "uncertainty_enabled": False,
    "uncertainty_compare_sources": False,
    "uncertainty_n_seeds": 30,
    "uncertainty_window_hours": 48,
    "uncertainty_commit_hours": 24,
    "uncertainty_dam_enabled": True,
    "uncertainty_pv_enabled": True,
    "uncertainty_load_enabled": True,
    "uncertainty_sigma_dam": 0.20,
    "uncertainty_sigma_pv": 0.12,
    "uncertainty_sigma_load": 0.05,
    "uncertainty_diagnostics_enabled": True,
    "plot_daily_scope": "year1_only",
    "plot_monthly_scope": "all",
    "plot_yearly_scope": "all",
}

# Sheet → defaults map.  Used by the loader to validate keys per sheet.
_SHEET_DEFAULTS: dict[str, dict[str, Any]] = {
    "project": PROJECT_SHEET_DEFAULTS,
    "pv": PV_SHEET_DEFAULTS,
    "bess": BESS_SHEET_DEFAULTS,
    "economics": ECONOMICS_SHEET_DEFAULTS,
    "simulation": SIMULATION_SHEET_DEFAULTS,
    "balancing": BALANCING_SHEET_DEFAULTS,
}

_KEY_TO_SHEET: dict[str, str] = {}
for _sheet_name, _sheet_defaults in _SHEET_DEFAULTS.items():
    for _key in _sheet_defaults:
        _KEY_TO_SHEET[_key] = _sheet_name


# ---------------------------------------------------------------------------
# Per-key parsing metadata
# ---------------------------------------------------------------------------

_BOOL_KEYS: frozenset[str] = frozenset({
    "show_titles",
    "allow_bess_grid_charging",
    "terminal_soc_equal",
    "sensitivity_enabled",
    "uncertainty_enabled",
    "uncertainty_compare_sources",
    "uncertainty_dam_enabled",
    "uncertainty_pv_enabled",
    "uncertainty_load_enabled",
    "uncertainty_diagnostics_enabled",
    "balancing_enabled",
})
_INT_KEYS: frozenset[str] = frozenset({
    "project_lifecycle_years",
    "project_start_year",
    "settlement_minutes",
    "bess_replacement_year",
    "uncertainty_n_seeds",
    "uncertainty_window_hours",
    "uncertainty_commit_hours",
    "bm_settlement_minutes",
    "bm_random_seed",
})
_STR_KEYS: frozenset[str] = frozenset({
    "mode",
    "currency_format",
    "plot_daily_scope",
    "plot_monthly_scope",
    "plot_yearly_scope",
})
_ALLOWED_VALUES: dict[str, frozenset[str]] = {
    "mode": frozenset({"self_consumption", "merchant"}),
    "currency_format": frozenset({"auto", "millions", "raw"}),
    "plot_daily_scope": frozenset({"none", "year1_only", "all"}),
    "plot_monthly_scope": frozenset({"none", "year1_only", "all"}),
    "plot_yearly_scope": frozenset({"none", "year1_only", "all"}),
}


# ---------------------------------------------------------------------------
# Sheet row templates (used by the workbook writer)
# ---------------------------------------------------------------------------

_PROJECT_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("project_lifecycle_years", PROJECT_SHEET_DEFAULTS["project_lifecycle_years"], "years",
     "Total project horizon used to project Years 0..N."),
    ("project_start_year", 2026, "year",
     "Calendar year of Year 1 (first operating year). CAPEX is paid in "
     "Year 0 (calendar = project_start_year - 1)."),
    ("mode", "self_consumption", "enum",
     "self_consumption | merchant. self_consumption requires a co-located load and enforces load "
     "priority + no simultaneous grid I/O. merchant has no load; PV/BESS "
     "dispatch entirely to DAM."),
    ("settlement_minutes", 15, "int",
     "Greek Self-consumption settles every 15 min per MD YPEN/DAPEEK/93976/2772/2024. "
     "Currently informational; the MILP timestep is auto-detected."),
    ("p_grid_export_max_kw", 5000, "kW",
     "Max grid export (kW). Leave empty or use 'inf' / 'unlimited' / "
     "'disabled' to remove cap; no injection limit is applied."),
    ("retail_tariff_eur_per_mwh", 120, "EUR/MWh",
     "Retail tariff used in self_consumption mode for load coverage."),
    ("allow_bess_grid_charging", False, "bool",
     "If TRUE the BESS may charge from the grid in periods with pv_kwh ~ 0."),
    ("unavailability_pct", 1.0, "%",
     "Annual unavailability (outages / scheduled maintenance) applied as "
     "a post-solve derate on PV generation, BESS discharge, and revenue."),
    ("site_capex_eur", 0.0, "EUR",
     "Site-wide lump-sum CAPEX in absolute EUR for items that are not "
     "naturally per-kWp/per-kW (substation construction, MV/HV grid "
     "upgrades, interconnection works, etc.). Paid in Year 0. Excluded "
     "from LCOE/LCOS by the Lazard convention."),
    ("site_devex_eur", 0.0, "EUR",
     "Site-wide lump-sum DEVEX in absolute EUR (environmental impact "
     "studies, land acquisition fees, permits not expressed per-kW, "
     "etc.). Paid in Year 0. Excluded from LCOE/LCOS by the Lazard "
     "convention."),
    ("currency_format", "auto", "enum",
     "auto | millions | raw — financial-axis label format."),
    ("show_titles", False, "bool",
     "Render plot titles. IEEE figures normally rely on the figure caption."),
)

_PV_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("pv_nameplate_kwp", 0, "kWp",
     "PV nameplate capacity. 0 = no PV in this project."),
    ("specific_production_kwh_per_kwp", 1500, "kWh/kWp/yr",
     "Annual specific production of the PV array. Used for documentation "
     "and as a sanity check; the MILP consumes the timeseries directly."),
    ("pv_degradation_year1_pct", 2.5, "%",
     "Initial light-induced degradation (LID) applied at start of Year 2."),
    ("pv_degradation_annual_pct", 0.55, "%",
     "Linear PV degradation after Year 1 (Tier-1 warranty)."),
    ("capex_pv_eur_per_kw", 525, "EUR/kWp",
     "Per-kWp PV CAPEX. Set 0 if PV already exists."),
    ("devex_pv_eur_per_kw", 60, "EUR/kWp",
     "Per-kWp PV DEVEX (development / permitting). Paid in Year 0."),
    ("opex_pv_eur_per_kwp", 7, "EUR/kWp/yr",
     "Annual O&M for PV."),
)

_BESS_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("bess_power_kw", 0, "kW",
     "BESS power rating (symmetric charge / discharge limit). "
     "0 = no BESS in this project."),
    ("bess_capacity_kwh", 0, "kWh",
     "BESS energy capacity. Pinned to the workbook value (industry "
     "standard for sizing-as-input projects)."),
    ("efficiency_charge", 0.97, "-",
     "Charge efficiency (0..1). Round-trip = "
     "efficiency_charge * efficiency_discharge."),
    ("efficiency_discharge", 0.97, "-",
     "Discharge efficiency (0..1)."),
    ("soc_min_frac", 0.20, "-",
     "Minimum SOC as fraction of nominal capacity (0.20 = 20 %)."),
    ("soc_max_frac", 0.95, "-",
     "Maximum SOC as fraction of nominal capacity (0.95 = 95 %)."),
    ("initial_soc_frac", 0.50, "-",
     "SOC at the first timestep, as a fraction of capacity."),
    ("terminal_soc_equal", True, "bool",
     "If TRUE, force final SOC == initial SOC (closed cycle)."),
    ("max_cycles_per_day", 1.0, "-",
     "Daily equivalent-cycle cap (sum of discharge / capacity)."),
    ("capex_bess_eur_per_kw", 200, "EUR/kW",
     "Per-kW BESS CAPEX (DC + PCS). Set 0 if BESS already exists."),
    ("devex_bess_eur_per_kw", 30, "EUR/kW",
     "Per-kW BESS DEVEX (development / permitting). Paid in Year 0."),
    ("opex_bess_eur_per_kw", 14, "EUR/kW/yr",
     "Annual O&M for BESS."),
    ("bess_replacement_year", 0, "year",
     "Year of BESS cell replacement (0 = no replacement). Typical 10 or 15."),
    ("bess_replacement_cost_pct", 50, "%",
     "Replacement cost as percent of original BESS CAPEX."),
    ("bess_degradation_annual_pct", 2.0, "%",
     "Linear BESS capacity fade. Approximate Tier-1 LFP cell warranty."),
    ("bess_degradation_pct_per_cycle", 0.008, "%",
     "Cycle-based BESS capacity fade per full equivalent cycle, in "
     "percent. LFP default 0.008 (range 0.005-0.010). Set to 0 to "
     "disable cycle aging (calendar-only mode)."),
)

_ECONOMICS_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("discount_rate_pct", 7.0, "%",
     "WACC. Typical EU RES band 6-8 %."),
    ("opex_inflation_pct", 1.0, "%",
     "Annual OPEX escalation rate."),
    ("retail_inflation_pct", 0.0, "%",
     "Annual indexation of retail tariff / PPA revenue (load-coverage). "
     "0 = no indexation."),
    ("dam_inflation_pct", 0.0, "%",
     "Annual indexation of wholesale DAM revenue (exports). Default 0 "
     "since DAM prices are driven by gas/CO2/RES penetration, not CPI. "
     "Industry tools (Lazard, Aurora, Gridcog) use exogenous price "
     "curves, not flat inflation."),
    ("aggregator_fee_pct_revenue", 10.0, "%",
     "Aggregator fee on gross revenue (Gridcog convention; see public "
     "Gridcog cost / pricing docs)."),
    ("benchmark_lcoe_low_eur_per_mwh", BENCHMARK_LCOE_LOW_EUR_PER_MWH, "EUR/MWh",
     "Lower edge of the Lazard 2024 utility-scale PV LCOE band "
     "(EUR-equivalent at ~1.08 EUR/USD). Overrideable per project."),
    ("benchmark_lcoe_high_eur_per_mwh", BENCHMARK_LCOE_HIGH_EUR_PER_MWH, "EUR/MWh",
     "Upper edge of the Lazard 2024 utility-scale PV LCOE band "
     "(EUR-equivalent at ~1.08 EUR/USD)."),
    ("benchmark_lcos_low_eur_per_mwh", BENCHMARK_LCOS_LOW_EUR_PER_MWH, "EUR/MWh",
     "Lower edge of the Lazard 2024 utility-scale 4-hour Li-ion LCOS "
     "band (EUR-equivalent at ~1.08 EUR/USD)."),
    ("benchmark_lcos_high_eur_per_mwh", BENCHMARK_LCOS_HIGH_EUR_PER_MWH, "EUR/MWh",
     "Upper edge of the Lazard 2024 utility-scale 4-hour Li-ion LCOS "
     "band (EUR-equivalent at ~1.08 EUR/USD)."),
    ("sensitivity_enabled", True, "bool",
     "Run a one-at-a-time tornado sensitivity after the base run."),
    ("sensitivity_capex_delta_pct", DEFAULT_SENSITIVITY_DELTA_PCT, "%",
     "Symmetric +/- delta on total CAPEX (incl. DEVEX)."),
    ("sensitivity_opex_delta_pct", DEFAULT_SENSITIVITY_DELTA_PCT, "%",
     "Symmetric +/- delta on total annual OPEX."),
    ("sensitivity_revenue_delta_pct", DEFAULT_SENSITIVITY_DELTA_PCT, "%",
     "Symmetric +/- delta on Year-1 revenue base."),
    ("sensitivity_discount_rate_delta_pp", DEFAULT_SENSITIVITY_DISCOUNT_RATE_DELTA_PP, "pp",
     "Symmetric +/- delta on the discount rate, in percentage points. "
     "NPV tornado only - drops out of IRR tornado by definition."),
)

_SIMULATION_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("uncertainty_enabled", False, "bool",
     "Run rolling-horizon Monte Carlo. Default FALSE (perfect-foresight only)."),
    ("uncertainty_compare_sources", False, "bool",
     "When TRUE run 4 ensembles (DAM-only, PV-only, Load-only, "
     "All-combined) and emit a comparison plot."),
    ("uncertainty_n_seeds", 30, "int",
     "Monte Carlo seeds per ensemble."),
    ("uncertainty_window_hours", 48, "int",
     "Rolling window length."),
    ("uncertainty_commit_hours", 24, "int",
     "Commit slice."),
    ("uncertainty_dam_enabled", True, "bool",
     "Apply DAM noise."),
    ("uncertainty_pv_enabled", True, "bool",
     "Apply PV noise."),
    ("uncertainty_load_enabled", True, "bool",
     "Apply Load noise (ignored in merchant mode)."),
    ("uncertainty_sigma_dam", 0.20, "-",
     "Log-normal sigma for DAM. Default 0.20 (ENTSO-E D+1 benchmark)."),
    ("uncertainty_sigma_pv", 0.12, "-",
     "Log-normal sigma for PV. Default 0.12 (NREL day-ahead PV study)."),
    ("uncertainty_sigma_load", 0.05, "-",
     "Log-normal sigma for Load. Default 0.05 (predictable customer benchmark)."),
    ("uncertainty_diagnostics_enabled", True, "bool",
     "Render the forecast-calibration diagnostic plots (coverage, PIT, "
     "CRPS, residual Q-Q) into 06_uncertainty_plots/. Default TRUE."),
    ("plot_daily_scope", "year1_only", "scope",
     "none | year1_only | all. 'all' produces ~365 * N_years * 3 daily PDFs."),
    ("plot_monthly_scope", "all", "scope",
     "none | year1_only | all."),
    ("plot_yearly_scope", "all", "scope",
     "none | year1_only | all."),
)

_BALANCING_ROWS: tuple[tuple[str, object, str, str], ...] = (
    ("balancing_enabled", False, "bool",
     "Master switch for stochastic balancing market participation "
     "(FCR / aFRR / mFRR). When FALSE the MILP, KPIs and outputs are "
     "bit-identical to a run without the sheet."),
    ("dam_capacity_share_pct", 70.0, "%",
     "Declarative share used only by the validator to ensure the total "
     "across DAM + every balancing product stays <= 100 % of bess_power_kw. "
     "It does not actively cap DAM dispatch in the MILP — DAM consumes the "
     "residual of bess_power_kw not reserved for balancing, implicitly "
     "bounded each step by BM_POWER_UP / BM_POWER_DN."),
    ("fcr_capacity_share_pct", 10.0, "%",
     "Share of bess_power_kw available for FCR reservation (symmetric)."),
    ("afrr_up_capacity_share_pct", 8.0, "%",
     "Share of bess_power_kw available for aFRR-up reservation."),
    ("afrr_dn_capacity_share_pct", 7.0, "%",
     "Share of bess_power_kw available for aFRR-down reservation."),
    ("mfrr_up_capacity_share_pct", 3.0, "%",
     "Share of bess_power_kw available for mFRR-up reservation."),
    ("mfrr_dn_capacity_share_pct", 2.0, "%",
     "Share of bess_power_kw available for mFRR-down reservation."),
    ("fcr_bid_acceptance_pct", 70.0, "%",
     "Probability that a submitted FCR bid clears the auction."),
    ("afrr_up_bid_acceptance_pct", 55.0, "%",
     "Probability that a submitted aFRR-up bid clears the auction."),
    ("afrr_dn_bid_acceptance_pct", 55.0, "%",
     "Probability that a submitted aFRR-down bid clears the auction."),
    ("mfrr_up_bid_acceptance_pct", 40.0, "%",
     "Probability that a submitted mFRR-up bid clears the auction."),
    ("mfrr_dn_bid_acceptance_pct", 40.0, "%",
     "Probability that a submitted mFRR-down bid clears the auction."),
    ("fcr_activation_probability_pct", 15.0, "%",
     "Informational only. FCR is modelled as capacity-only (no activation "
     "payment) and as symmetric in expectation (no SOC drift), so the MILP, "
     "KPIs and Monte Carlo realisation do not consume this value. Retained "
     "for documentation and future use should an FCR activation revenue "
     "stream be added."),
    ("afrr_up_activation_probability_pct", 10.0, "%",
     "Probability a cleared aFRR-up reservation is activated within a "
     "settlement period."),
    ("afrr_dn_activation_probability_pct", 8.0, "%",
     "Probability a cleared aFRR-down reservation is activated."),
    ("mfrr_up_activation_probability_pct", 5.0, "%",
     "Probability a cleared mFRR-up reservation is activated."),
    ("mfrr_dn_activation_probability_pct", 4.0, "%",
     "Probability a cleared mFRR-down reservation is activated."),
    ("fcr_default_capacity_price_eur_per_mwh", 12.0, "EUR/MWh",
     "FCR capacity-price fallback when the timeseries column is absent."),
    ("afrr_up_default_capacity_price_eur_per_mwh", 18.0, "EUR/MWh",
     "aFRR-up capacity-price fallback when the timeseries column is absent."),
    ("afrr_dn_default_capacity_price_eur_per_mwh", 15.0, "EUR/MWh",
     "aFRR-down capacity-price fallback when the timeseries column is absent."),
    ("mfrr_up_default_capacity_price_eur_per_mwh", 6.0, "EUR/MWh",
     "mFRR-up capacity-price fallback when the timeseries column is absent."),
    ("mfrr_dn_default_capacity_price_eur_per_mwh", 5.0, "EUR/MWh",
     "mFRR-down capacity-price fallback when the timeseries column is absent."),
    ("afrr_up_default_activation_price_eur_per_mwh", 220.0, "EUR/MWh",
     "aFRR-up activation-price fallback when the timeseries column is absent."),
    ("afrr_dn_default_activation_price_eur_per_mwh", 25.0, "EUR/MWh",
     "aFRR-down activation-price fallback when the timeseries column is absent."),
    ("mfrr_up_default_activation_price_eur_per_mwh", 180.0, "EUR/MWh",
     "mFRR-up activation-price fallback when the timeseries column is absent."),
    ("mfrr_dn_default_activation_price_eur_per_mwh", 20.0, "EUR/MWh",
     "mFRR-down activation-price fallback when the timeseries column is absent."),
    ("fcr_required_duration_hours", 0.5, "hours",
     "FCR-specific sustained-output requirement; sizes the SOC headroom "
     "reserved for FCR independently of the settlement period."),
    ("bm_settlement_minutes", 15, "int",
     "Balancing-market settlement period in minutes. Must equal "
     "60 * dt_hours when balancing_enabled is TRUE."),
    ("bm_soc_headroom_pct", 10.0, "%",
     "Extra SOC safety buffer applied to the worst-case activation "
     "reservation in both directions."),
    ("bm_inflation_pct", 2.0, "%",
     "Yearly indexation of balancing revenue applied in the multi-year "
     "lifetime cashflow."),
    ("bm_price_sigma_capacity_pct", 25.0, "%",
     "Log-normal sigma for Monte Carlo perturbation of capacity prices."),
    ("bm_price_sigma_activation_pct", 35.0, "%",
     "Log-normal sigma for Monte Carlo perturbation of activation prices."),
    ("bm_random_seed", 1729, "int",
     "Default seed for the balancing Monte Carlo realisation."),
)


_SHEET_ROW_TEMPLATES: dict[
    str, tuple[tuple[str, object, str, str], ...]
] = {
    "project": _PROJECT_ROWS,
    "pv": _PV_ROWS,
    "bess": _BESS_ROWS,
    "economics": _ECONOMICS_ROWS,
    "simulation": _SIMULATION_ROWS,
    "balancing": _BALANCING_ROWS,
}

# Default share of p_grid_export_max_kw available for export (24 hourly
# rows) applied when the workbook omits the max_injection_profile sheet.
# Single source of truth lives in pvbess_opt.config; re-imported above.


# ---------------------------------------------------------------------------
# Sheet builders
# ---------------------------------------------------------------------------


def _build_kv_sheet(
    typed_section: dict[str, Any],
    rows: tuple[tuple[str, object, str, str], ...],
) -> pd.DataFrame:
    out: list[dict[str, Any]] = []
    for key, default, unit, notes in rows:
        value = typed_section.get(key, default)
        out.append(
            {"key": key, "value": value, "unit": unit, "notes": notes},
        )
    return pd.DataFrame(out, columns=["key", "value", "unit", "notes"])


def _hour_interval_labels() -> list[str]:
    """24 strings of the form ``HH:00-HH:00`` covering 00:00 → 24:00."""
    return [f"{h:02d}:00-{(h + 1):02d}:00" for h in range(24)]


def _build_max_injection_sheet(profile: Any) -> pd.DataFrame:
    """Render the ``max_injection_profile`` sheet from a 1-D or 2-D array.

    Accepts:
    * shape ``(24,)`` → single ``max_injection_pct`` column.
    * shape ``(24, 12)`` → per-month columns (``max_injection_pct_jan`` ..
      ``max_injection_pct_dec``).

    The ``hour_of_day`` column is rendered as **24-hour interval
    strings** (``"00:00-01:00"`` … ``"23:00-24:00"``) for human
    readability.  Values are interpreted as the percent of
    ``p_grid_export_max_kw`` available for export in that hour.
    """
    arr = np.asarray(profile, dtype=float)
    hour_labels = _hour_interval_labels()
    if arr.ndim == 1:
        if arr.shape[0] != 24:
            raise ValueError(
                "max_injection_profile must have 24 rows "
                f"(got {arr.shape[0]})."
            )
        return pd.DataFrame({
            "hour_of_day": hour_labels,
            "max_injection_pct": arr,
        })
    if arr.ndim == 2:
        if arr.shape != (24, 12):
            raise ValueError(
                "max_injection_profile (2-D) must be shape (24, 12) "
                f"(got {arr.shape})."
            )
        cols: dict[str, Any] = {"hour_of_day": hour_labels}
        for m_idx, m_name in enumerate(_MONTH_TOKENS):
            cols[f"max_injection_pct_{m_name}"] = arr[:, m_idx]
        return pd.DataFrame(cols)
    raise ValueError(
        "max_injection_profile must be 1-D (24,) or 2-D (24, 12); "
        f"got shape {arr.shape}."
    )


_MONTH_TOKENS: tuple[str, ...] = (
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
)


def write_workbook(typed: dict[str, Any], dst: str | Path) -> Path:
    """Write a workbook from a typed nested dict (seven-sheet schema)."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    project_df = _build_kv_sheet(typed["project"], _PROJECT_ROWS)
    pv_df = _build_kv_sheet(typed["pv"], _PV_ROWS)
    bess_df = _build_kv_sheet(typed["bess"], _BESS_ROWS)
    economics_df = _build_kv_sheet(typed["economics"], _ECONOMICS_ROWS)
    simulation_df = _build_kv_sheet(typed["simulation"], _SIMULATION_ROWS)
    balancing_section = typed.get("balancing") or dict(BALANCING_SHEET_DEFAULTS)
    balancing_df = _build_kv_sheet(balancing_section, _BALANCING_ROWS)

    profile = typed.get("max_injection_profile")
    if profile is None:
        profile = np.full(24, DEFAULT_MAX_INJECTION_PCT_HOURLY, dtype=float)
    max_injection_df = _build_max_injection_sheet(profile)

    with pd.ExcelWriter(dst, engine="openpyxl") as writer:
        typed["ts"].to_excel(writer, sheet_name="timeseries", index=False)
        project_df.to_excel(writer, sheet_name="project", index=False)
        pv_df.to_excel(writer, sheet_name="pv", index=False)
        bess_df.to_excel(writer, sheet_name="bess", index=False)
        economics_df.to_excel(writer, sheet_name="economics", index=False)
        simulation_df.to_excel(writer, sheet_name="simulation", index=False)
        balancing_df.to_excel(writer, sheet_name="balancing", index=False)
        max_injection_df.to_excel(
            writer, sheet_name="max_injection_profile", index=False,
        )
    return dst


# ---------------------------------------------------------------------------
# Generic value-coercion helpers
# ---------------------------------------------------------------------------


def _coerce(value: Any, cast: type, default: Any) -> Any:
    """Cast ``value`` to ``cast``; return ``default`` on empty/NaN; sentinel on error."""
    if value is None:
        return default
    if isinstance(value, float) and np.isnan(value):
        return default
    if isinstance(value, str) and value.strip() == "":
        return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        return _COERCE_FAILED


def _parse_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and np.isnan(value):
            return default
        return value != 0
    token = str(value).strip().lower()
    if token == "":
        return default
    if token in TRUTHY:
        return True
    if token in FALSY:
        return False
    return default


def _parse_string_enum(
    value: Any, default: str, allowed: frozenset[str], key: str,
) -> str:
    if value is None:
        return default
    if isinstance(value, float) and np.isnan(value):
        return default
    token = str(value).strip().lower()
    if token == "":
        return default
    if token not in allowed:
        if key == "mode":
            raise ValueError(
                f"unknown mode {token!r}; valid modes are "
                f"{sorted(allowed)!r}"
            )
        logger.warning(
            "Workbook value for %r is %r which is not in %s; using default %r.",
            key, value, sorted(allowed), default,
        )
        return default
    return token


# ---------------------------------------------------------------------------
# Sheet → flat-dict reduction (skips separator rows)
# ---------------------------------------------------------------------------


def _flat_dict_from_sheet(df: pd.DataFrame) -> dict[str, Any]:
    """Reduce a (key, value, ...) sheet to ``{key: value}``, skipping separators."""
    if "key" not in df.columns or "value" not in df.columns:
        return {}
    out: dict[str, Any] = {}
    for _, row in df.iterrows():
        raw_key = row.get("key")
        if not isinstance(raw_key, str):
            continue
        key = raw_key.strip()
        if not key or key.startswith("#"):
            continue
        out[key] = row.get("value")
    return out


# ---------------------------------------------------------------------------
# Per-sheet typed parser
# ---------------------------------------------------------------------------


def _parse_value(key: str, raw: Any, default: Any) -> Any:
    if key in _BOOL_KEYS:
        return _parse_bool(raw, bool(default))
    if key in _STR_KEYS:
        return _parse_string_enum(
            raw, str(default), _ALLOWED_VALUES.get(key, frozenset()), key,
        )
    if key in _INT_KEYS:
        coerced = _coerce(raw, int, default)
        if coerced is _COERCE_FAILED:
            logger.warning(
                "Workbook value for %r could not be parsed as int "
                "(got %r); using default %r.", key, raw, default,
            )
            return default
        return coerced
    coerced = _coerce(raw, float, default)
    if coerced is _COERCE_FAILED:
        logger.warning(
            "Workbook value for %r could not be parsed as float "
            "(got %r); using default %r.", key, raw, default,
        )
        return default
    return coerced


def _parse_grid_export_max(raw: Any, default: Any) -> float:
    """Parse ``p_grid_export_max_kw``.

    Returns ``float('inf')`` when the cap is disabled (empty cell, or one
    of the ``_GRID_EXPORT_UNLIMITED_TOKENS`` strings, case-insensitive).
    A finite positive float is returned unchanged.  Negative or zero
    values are returned as-is so the loader can raise a validation error;
    unparseable values fall back to ``default`` with a warning.
    """
    if raw is None:
        return float("inf")
    if isinstance(raw, float) and np.isnan(raw):
        return float("inf")
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token == "" or token in _GRID_EXPORT_UNLIMITED_TOKENS:
            return float("inf")
        try:
            value = float(raw)
        except ValueError:
            logger.warning(
                "Workbook value for 'p_grid_export_max_kw' could not be "
                "parsed (got %r); using default %r.", raw, default,
            )
            return float(default)
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "Workbook value for 'p_grid_export_max_kw' could not be "
                "parsed (got %r); using default %r.", raw, default,
            )
            return float(default)
    if np.isinf(value):
        return float("inf")
    return value


def _parse_kv_sheet(
    sheet_name: str, flat: dict[str, Any],
) -> dict[str, Any]:
    defaults = _SHEET_DEFAULTS[sheet_name]
    out = dict(defaults)
    for key, raw in flat.items():
        if key in defaults:
            if key == "p_grid_export_max_kw":
                out[key] = _parse_grid_export_max(raw, defaults[key])
            else:
                out[key] = _parse_value(key, raw, defaults[key])
            continue
        # Unknown key for this sheet — but maybe it belongs to another sheet?
        if key in _KEY_TO_SHEET:
            logger.warning(
                "Key %r found on %r sheet but belongs to %r sheet; ignored.",
                key, sheet_name, _KEY_TO_SHEET[key],
            )
            continue
        logger.warning(
            "%s sheet key %r is unknown; ignored.", sheet_name, key,
        )
    return out


# ---------------------------------------------------------------------------
# Max-injection profile parser
# ---------------------------------------------------------------------------


_HOUR_PARSE_RE = re.compile(r"^\s*(\d{1,2})")


def _parse_hour_of_day(value: Any) -> int:
    """Coerce an ``hour_of_day`` cell into an integer 0..23.

    Accepts an integer 0..23 and the 24-hour interval
    string format (``"00:00-01:00"`` … ``"23:00-24:00"``).  The
    parser is forgiving: any leading 1-2 digit run is taken as the
    start hour.  Out-of-range values raise ``ValueError``.
    """
    if isinstance(value, (int, np.integer)):
        h = int(value)
    elif isinstance(value, (float, np.floating)):
        if np.isnan(value):
            raise ValueError("hour_of_day cell is NaN")
        h = int(value)
    else:
        s = str(value).strip()
        m = _HOUR_PARSE_RE.match(s)
        if not m:
            raise ValueError(
                f"cannot parse hour_of_day value {value!r}; "
                "expected an integer 0..23 or an interval like '00:00-01:00'"
            )
        h = int(m.group(1))
    if h < 0 or h > 23:
        raise ValueError(
            f"hour_of_day must be in 0..23 (got {h} from {value!r})"
        )
    return h


def _normalise_hourly_profile_frame(
    df: pd.DataFrame, *, sheet_name: str,
) -> pd.DataFrame:
    """Validate columns / row count and lowercase column names."""
    if df is None or df.empty:
        raise ValueError(f"{sheet_name} sheet is empty.")
    cols = {c.strip().lower() for c in df.columns}
    if "hour_of_day" not in cols:
        raise ValueError(
            f"{sheet_name} sheet must contain a 'hour_of_day' column."
        )
    df_norm = df.rename(columns={c: c.strip().lower() for c in df.columns})
    df_norm["hour_of_day"] = df_norm["hour_of_day"].map(_parse_hour_of_day)
    df_norm = df_norm.sort_values("hour_of_day").reset_index(drop=True)
    if len(df_norm) != 24:
        raise ValueError(
            f"{sheet_name} sheet must have exactly 24 rows "
            f"(got {len(df_norm)})."
        )
    hours = df_norm["hour_of_day"].astype(int).to_numpy()
    if not np.array_equal(hours, np.arange(24)):
        raise ValueError(
            f"{sheet_name} 'hour_of_day' column must cover 0..23 "
            f"exactly once; got {hours.tolist()}."
        )
    return df_norm


def _extract_profile(
    df_norm: pd.DataFrame, *, scalar_col: str, monthly_prefix: str,
) -> np.ndarray:
    """Pull the (24,) or (24, 12) array from a normalised profile frame."""
    monthly_cols = [f"{monthly_prefix}_{m}" for m in _MONTH_TOKENS]
    if all(col in df_norm.columns for col in monthly_cols):
        arr = np.zeros((24, 12), dtype=float)
        for m_idx, m_name in enumerate(_MONTH_TOKENS):
            arr[:, m_idx] = (
                df_norm[f"{monthly_prefix}_{m_name}"]
                .astype(float).to_numpy()
            )
        return arr
    if scalar_col in df_norm.columns:
        return df_norm[scalar_col].astype(float).to_numpy()
    raise ValueError(
        f"profile sheet must contain either a '{scalar_col}' column "
        f"(24x1) or all 12 '{monthly_prefix}_<month>' columns (24x12)."
    )


def _parse_max_injection_profile_sheet(df: pd.DataFrame) -> np.ndarray:
    """Parse the new-schema ``max_injection_profile`` sheet.

    Returns a (24,) or (24, 12) array of percent-of-grid-export values.
    """
    df_norm = _normalise_hourly_profile_frame(df, sheet_name="max_injection_profile")
    return _extract_profile(
        df_norm,
        scalar_col="max_injection_pct",
        monthly_prefix="max_injection_pct",
    )


# ---------------------------------------------------------------------------
# Timeseries normalisation + dt auto-detection
# ---------------------------------------------------------------------------


def _normalise_timeseries(ts: pd.DataFrame, *, mode: str) -> pd.DataFrame:
    """Validate timeseries columns and forward-fill numeric NaNs."""
    if "timestamp" not in ts.columns:
        raise ValueError("timeseries sheet must contain a 'timestamp' column.")
    if "pv_kwh" not in ts.columns:
        raise ValueError("timeseries sheet must contain a 'pv_kwh' column.")

    if mode == "self_consumption" and "load_kwh" not in ts.columns:
        raise ValueError(
            "timeseries sheet must contain a 'load_kwh' column when mode='self_consumption'."
        )
    if mode == "merchant" and "load_kwh" in ts.columns:
        logger.info("merchant mode: load_kwh column ignored")

    for col in ("load_kwh", "pv_kwh", "dam_price_eur_per_mwh", "retail_price_eur_per_mwh"):
        if col in ts.columns:
            numeric = ts[col].astype(float)
            nan_mask = numeric.isna()
            nan_count = int(nan_mask.sum())
            if nan_count > 0:
                first_nan = ts.loc[nan_mask.idxmax(), "timestamp"]
                logger.warning(
                    "Column '%s' had %d NaN value(s) filled via ffill/bfill; "
                    "first NaN at %s. Check the input timeseries for gaps.",
                    col, nan_count, first_nan,
                )
            ts[col] = numeric.ffill().bfill()
    # pv_kwh_override deliberately stays out of the ffill/bfill loop so
    # partial NaN survives long enough for _resolve_pv_column to raise.
    return ts


def detect_timestep_minutes(ts: pd.DataFrame) -> int:
    """Auto-detect the MILP timestep (in minutes) from the timeseries."""
    idx = pd.to_datetime(ts["timestamp"]).sort_values()
    diffs = idx.diff().dropna()
    if diffs.empty:
        raise ValueError(
            "timeseries has fewer than 2 rows; cannot determine timestep."
        )
    if diffs.nunique() > 1:
        sample = diffs.value_counts().head().to_dict()
        raise ValueError(
            "Irregular timestep detected in 'timeseries' "
            f"(distinct step sizes: {sample}). Run "
            "`python scripts/resample_timeseries.py <workbook>` to harmonise "
            "the resolution before optimising."
        )
    delta = diffs.iloc[0]
    return int(delta.total_seconds() / 60)


# ---------------------------------------------------------------------------
# Public loader API
# ---------------------------------------------------------------------------


_V08_REQUIRED_SHEETS: frozenset[str] = frozenset({
    "timeseries", "project", "pv", "bess", "economics", "simulation",
})


# Tolerance for "the workbook PV total already matches the user's
# pv_nameplate_kwp × specific_production_kwh_per_kwp target" — below
# this relative threshold the loader does **not** rescale.
_PV_RESCALE_REL_TOLERANCE: float = 1.0e-12


def _rescale_pv_to_user_target(
    ts: pd.DataFrame,
    *,
    pv_nameplate_kwp: float,
    specific_production_kwh_per_kwp: float,
) -> pd.DataFrame:
    """Rescale ``ts['pv_kwh']`` to match the user's
    ``pv_nameplate_kwp × specific_production_kwh_per_kwp`` target.

    The shape is preserved exactly (multiplicative scaling).  Returns
    a new DataFrame.  Skipped (pass-through) when:

    * either knob is zero or negative (PV is "absent" or unspecified);
    * the workbook PV column sums to zero;
    * the current annual total already matches the target within
      ``1e-12`` relative.
    """
    if "pv_kwh" not in ts.columns:
        return ts
    if pv_nameplate_kwp <= 0.0 or specific_production_kwh_per_kwp <= 0.0:
        return ts

    current_total = float(ts["pv_kwh"].astype(float).sum())
    if current_total <= 0.0:
        return ts

    target_total = (
        float(pv_nameplate_kwp) * float(specific_production_kwh_per_kwp)
    )
    rel_diff = abs(current_total - target_total) / max(target_total, 1.0e-9)
    if rel_diff <= _PV_RESCALE_REL_TOLERANCE:
        return ts

    factor = target_total / current_total
    out = ts.copy()
    out["pv_kwh"] = out["pv_kwh"].astype(float) * factor
    logger.info(
        "PV column rescaled: workbook annual %.1f kWh → user target %.1f "
        "kWh (factor %.6f) from pv_nameplate_kwp=%.1f kWp × "
        "specific_production=%.4f kWh/kWp.",
        current_total, target_total, factor,
        pv_nameplate_kwp, specific_production_kwh_per_kwp,
    )
    return out


def _resolve_pv_column(
    ts: pd.DataFrame,
    *,
    pv_nameplate_kwp: float,
    specific_production_kwh_per_kwp: float,
) -> pd.DataFrame:
    """Resolve ``pv_kwh`` from either the override column or rescaling.

    Four cases handled in order:

    1. ``pv_kwh_override`` column absent — fall through to the
       ``pv_nameplate_kwp`` × ``specific_production_kwh_per_kwp`` rescale.
    2. Column present, all-null — treat as absent and rescale.
    3. Column present, all non-null — overwrite ``pv_kwh`` with the
       override values verbatim, drop the override column from the
       returned frame, log INFO with the annual sum + implied SP.
    4. Column present, partial NaN — raise ``ValueError`` with a hint.
    """
    if "pv_kwh_override" not in ts.columns:
        return _rescale_pv_to_user_target(
            ts,
            pv_nameplate_kwp=pv_nameplate_kwp,
            specific_production_kwh_per_kwp=specific_production_kwh_per_kwp,
        )
    override = ts["pv_kwh_override"]
    n_total = len(override)
    n_null = int(override.isna().sum())
    if n_null == n_total:
        out = ts.drop(columns=["pv_kwh_override"])
        return _rescale_pv_to_user_target(
            out,
            pv_nameplate_kwp=pv_nameplate_kwp,
            specific_production_kwh_per_kwp=specific_production_kwh_per_kwp,
        )
    if n_null > 0:
        raise ValueError(
            f"pv_kwh_override has {n_null} NaN values out of {n_total}. "
            "Either fill every row (15-min cadence, all 35040 values) "
            "or leave the column entirely empty — the loader will then "
            "fall back to pv_kwh × pv_nameplate_kwp × "
            "specific_production_kwh_per_kwp rescaling."
        )
    out = ts.copy()
    out["pv_kwh"] = override.astype(float)
    out = out.drop(columns=["pv_kwh_override"])
    annual_sum = float(override.sum())
    if pv_nameplate_kwp > 0:
        implied_sp = annual_sum / pv_nameplate_kwp
        logger.info(
            "PV column: using pv_kwh_override verbatim (annual sum "
            "%.1f kWh, implied specific production %.1f kWh/kWp at "
            "pv_nameplate_kwp=%.1f). Confirm pv_nameplate_kwp matches "
            "the asset that produced this series.",
            annual_sum, implied_sp, pv_nameplate_kwp,
        )
        if implied_sp < 500.0 or implied_sp > 2500.0:
            logger.warning(
                "PV column: implied specific production %.1f kWh/kWp "
                "at pv_nameplate_kwp=%.1f falls outside the plausible "
                "500-2500 kWh/kWp band. Check pv_nameplate_kwp.",
                implied_sp, pv_nameplate_kwp,
            )
    else:
        logger.info(
            "PV column: using pv_kwh_override verbatim (annual sum "
            "%.1f kWh). pv_nameplate_kwp = 0 — no implied SP check.",
            annual_sum,
        )
    return out


# ---------------------------------------------------------------------------
# Balancing market validation + timeseries fallback
# ---------------------------------------------------------------------------

# Keys whose value is read as the share of bess_power_kw allocated to a
# specific market product. The DAM line is included so the sum across
# every consumer of the BESS power budget is bounded by 100 %.
# Note: only the *sum* is enforced. The individual ``dam_capacity_share_pct``
# value is declarative — DAM dispatch is bounded indirectly by
# ``BM_POWER_UP`` / ``BM_POWER_DN`` consuming the residual of bess_power_kw
# left over after the balancing reservations in each step.
_BALANCING_SHARE_KEYS: tuple[str, ...] = (
    "dam_capacity_share_pct",
    "fcr_capacity_share_pct",
    "afrr_up_capacity_share_pct",
    "afrr_dn_capacity_share_pct",
    "mfrr_up_capacity_share_pct",
    "mfrr_dn_capacity_share_pct",
)

_BALANCING_PROBABILITY_KEYS: tuple[str, ...] = (
    "fcr_bid_acceptance_pct",
    "afrr_up_bid_acceptance_pct",
    "afrr_dn_bid_acceptance_pct",
    "mfrr_up_bid_acceptance_pct",
    "mfrr_dn_bid_acceptance_pct",
    "fcr_activation_probability_pct",
    "afrr_up_activation_probability_pct",
    "afrr_dn_activation_probability_pct",
    "mfrr_up_activation_probability_pct",
    "mfrr_dn_activation_probability_pct",
)

_BALANCING_PRICE_KEYS: tuple[str, ...] = (
    "fcr_default_capacity_price_eur_per_mwh",
    "afrr_up_default_capacity_price_eur_per_mwh",
    "afrr_dn_default_capacity_price_eur_per_mwh",
    "mfrr_up_default_capacity_price_eur_per_mwh",
    "mfrr_dn_default_capacity_price_eur_per_mwh",
    "afrr_up_default_activation_price_eur_per_mwh",
    "afrr_dn_default_activation_price_eur_per_mwh",
    "mfrr_up_default_activation_price_eur_per_mwh",
    "mfrr_dn_default_activation_price_eur_per_mwh",
)

# Mapping of timeseries column name to the scalar fallback key when the
# column is missing. FCR has no activation price by design.
_BALANCING_TS_COLUMN_DEFAULTS: dict[str, str] = {
    "fcr_capacity_price_eur_per_mwh": "fcr_default_capacity_price_eur_per_mwh",
    "afrr_up_capacity_price_eur_per_mwh":
        "afrr_up_default_capacity_price_eur_per_mwh",
    "afrr_dn_capacity_price_eur_per_mwh":
        "afrr_dn_default_capacity_price_eur_per_mwh",
    "mfrr_up_capacity_price_eur_per_mwh":
        "mfrr_up_default_capacity_price_eur_per_mwh",
    "mfrr_dn_capacity_price_eur_per_mwh":
        "mfrr_dn_default_capacity_price_eur_per_mwh",
    "afrr_up_activation_price_eur_per_mwh":
        "afrr_up_default_activation_price_eur_per_mwh",
    "afrr_dn_activation_price_eur_per_mwh":
        "afrr_dn_default_activation_price_eur_per_mwh",
    "mfrr_up_activation_price_eur_per_mwh":
        "mfrr_up_default_activation_price_eur_per_mwh",
    "mfrr_dn_activation_price_eur_per_mwh":
        "mfrr_dn_default_activation_price_eur_per_mwh",
}


def _validate_balancing_config(
    balancing: dict[str, Any], dt_minutes: int,
) -> None:
    """Validate the balancing-market config against the rules in the design note.

    Skipped silently when ``balancing_enabled`` is False so workbooks that
    carry the sheet for documentation but disable the feature still load.
    """
    if not bool(balancing.get("balancing_enabled", False)):
        return

    for key in _BALANCING_SHARE_KEYS:
        value = float(balancing.get(key, 0.0) or 0.0)
        if value < 0.0:
            raise ValueError(
                f"balancing sheet key {key!r} must be non-negative; "
                f"got {value!r}."
            )

    share_sum = sum(
        float(balancing.get(key, 0.0) or 0.0)
        for key in _BALANCING_SHARE_KEYS
    )
    if share_sum > 100.0 + 1e-9:
        raise ValueError(
            "balancing sheet capacity shares sum to "
            f"{share_sum:.3f} % which exceeds 100 % of bess_power_kw "
            f"(keys: {list(_BALANCING_SHARE_KEYS)}). Reduce one or more "
            "shares so the total stays at or below 100 %."
        )

    for key in _BALANCING_PROBABILITY_KEYS:
        value = float(balancing.get(key, 0.0) or 0.0)
        if value < 0.0 or value > 100.0:
            raise ValueError(
                f"balancing sheet key {key!r} must be a probability in "
                f"[0, 100]; got {value!r}."
            )

    for key in _BALANCING_PRICE_KEYS:
        value = float(balancing.get(key, 0.0) or 0.0)
        if value < 0.0:
            raise ValueError(
                f"balancing sheet key {key!r} must be non-negative; "
                f"got {value!r}."
            )

    duration = float(balancing.get("fcr_required_duration_hours", 0.0) or 0.0)
    if duration <= 0.0:
        raise ValueError(
            "balancing sheet key 'fcr_required_duration_hours' must be "
            f"strictly positive; got {duration!r}."
        )

    settlement = int(balancing.get("bm_settlement_minutes", 0) or 0)
    if settlement != int(dt_minutes):
        raise ValueError(
            "balancing sheet key 'bm_settlement_minutes' is "
            f"{settlement} min but the timeseries cadence is "
            f"{int(dt_minutes)} min. Resample the timeseries (see "
            "scripts/resample_timeseries.py) or set "
            f"bm_settlement_minutes = {int(dt_minutes)} to match."
        )

    headroom = float(balancing.get("bm_soc_headroom_pct", 0.0) or 0.0)
    if headroom < 0.0 or headroom > 50.0:
        raise ValueError(
            "balancing sheet key 'bm_soc_headroom_pct' must be in "
            f"[0, 50]; got {headroom!r}."
        )


def _apply_balancing_timeseries_fallback(
    ts: pd.DataFrame, balancing: dict[str, Any],
) -> pd.DataFrame:
    """Fill in any missing balancing-price column with its scalar default.

    No-op when ``balancing_enabled`` is False (the columns may be absent
    and the loader leaves them alone). When enabled, each missing column
    is appended with the scalar value from the balancing config and a
    single WARNING is emitted naming the column and the default used.
    """
    if not bool(balancing.get("balancing_enabled", False)):
        return ts

    out = ts
    n_rows = len(out)
    for col, default_key in _BALANCING_TS_COLUMN_DEFAULTS.items():
        if col in out.columns:
            continue
        default_value = float(balancing.get(default_key, 0.0) or 0.0)
        if out is ts:  # avoid the copy until we know we need one
            out = ts.copy()
        out[col] = np.full(n_rows, default_value, dtype=float)
        logger.warning(
            "balancing timeseries column %r is missing; filling with "
            "the scalar default %.4f EUR/MWh from balancing sheet "
            "key %r.",
            col, default_value, default_key,
        )
    return out


def read_workbook(xlsx_path: str | Path) -> dict[str, Any]:
    """Read the input workbook and return the typed nested dict."""
    xlsx_path = Path(xlsx_path)
    sheets = set(pd.ExcelFile(xlsx_path).sheet_names)

    missing = _V08_REQUIRED_SHEETS - sheets
    if missing:
        raise ValueError(
            f"Workbook {xlsx_path!s} is missing required sheets: "
            f"{sorted(missing)}. Found: {sorted(sheets)}."
        )

    typed: dict[str, Any] = {}
    for sheet_name in ("project", "pv", "bess", "economics", "simulation"):
        flat = _flat_dict_from_sheet(
            pd.read_excel(xlsx_path, sheet_name=sheet_name),
        )
        typed[sheet_name] = _parse_kv_sheet(sheet_name, flat)
        if (
            sheet_name == "bess"
            and "bess_degradation_pct_per_cycle" not in flat
        ):
            # Workbooks that omit the cycle-fade coefficient default it
            # to 0.0 so the run uses calendar-only fade.
            typed["bess"]["bess_degradation_pct_per_cycle"] = 0.0
            logger.info(
                "[bess] bess_degradation_pct_per_cycle not found in "
                "workbook; defaulting to 0.0 (calendar-only mode)."
            )

    # Optional ``balancing`` sheet — when absent every key falls back to
    # the defaults declared above so ``balancing_enabled`` resolves to
    # False and the rest of the loader behaves exactly as before.
    if "balancing" in sheets:
        balancing_flat = _flat_dict_from_sheet(
            pd.read_excel(xlsx_path, sheet_name="balancing"),
        )
        typed["balancing"] = _parse_kv_sheet("balancing", balancing_flat)
    else:
        typed["balancing"] = dict(BALANCING_SHEET_DEFAULTS)

    # A finite grid-export cap must be strictly positive.  An empty cell
    # or an 'unlimited' token resolves to float('inf') (cap disabled).
    grid_cap = typed["project"]["p_grid_export_max_kw"]
    if not np.isinf(grid_cap) and float(grid_cap) <= 0.0:
        raise ValueError(
            "p_grid_export_max_kw must be a positive number, or empty / "
            "'inf' / 'unlimited' / 'disabled' to remove the cap; got "
            f"{grid_cap!r}."
        )

    if "max_injection_profile" in sheets:
        try:
            profile = _parse_max_injection_profile_sheet(
                pd.read_excel(xlsx_path, sheet_name="max_injection_profile"),
            )
        except ValueError as exc:
            raise ValueError(f"max_injection_profile: {exc}") from exc
    else:
        logger.info(
            "max_injection_profile sheet not found in %s; falling back "
            "to constant %.1f %% for every hour.",
            xlsx_path, DEFAULT_MAX_INJECTION_PCT_HOURLY,
        )
        profile = np.full(
            24, DEFAULT_MAX_INJECTION_PCT_HOURLY, dtype=float,
        )

    mode = str(typed["project"]["mode"]).lower()
    ts = _normalise_timeseries(
        pd.read_excel(xlsx_path, sheet_name="timeseries", parse_dates=["timestamp"]),
        mode=mode,
    )
    ts = _resolve_pv_column(
        ts,
        pv_nameplate_kwp=float(typed["pv"].get("pv_nameplate_kwp", 0.0) or 0.0),
        specific_production_kwh_per_kwp=float(
            typed["pv"].get("specific_production_kwh_per_kwp", 0.0) or 0.0,
        ),
    )
    dt_minutes = detect_timestep_minutes(ts)
    _validate_balancing_config(typed["balancing"], dt_minutes)
    ts = _apply_balancing_timeseries_fallback(ts, typed["balancing"])
    out: dict[str, Any] = {
        "ts": ts,
        "max_injection_profile": profile,
        "dt_minutes": dt_minutes,
    }
    out.update(typed)
    return out


def _typed_to_flat(
    typed: dict[str, Any],
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Translate the typed dict to the flat ``(params, ts)`` shape."""
    project = typed["project"]
    pv = typed["pv"]
    bess = typed["bess"]
    sim = typed["simulation"]
    ts = typed["ts"]

    bess_power_kw = float(bess["bess_power_kw"])
    bess_capacity_kwh = float(bess["bess_capacity_kwh"])
    pv_nameplate_kwp = float(pv["pv_nameplate_kwp"])

    # Resolve the grid-export cap.  When the workbook value is empty or an
    # 'unlimited' token it parses to float('inf'); we substitute a finite
    # Big-M large enough never to bind so the MILP topology is unchanged
    # and the behaviour stays solver-agnostic (HiGHS / Gurobi / CBC).
    raw_grid_cap = float(project["p_grid_export_max_kw"])
    grid_export_unlimited = bool(np.isinf(raw_grid_cap))
    if grid_export_unlimited:
        p_grid_export_cap_milp = max(
            2.0 * (pv_nameplate_kwp + bess_power_kw),
            1.0e6,
        )
        logger.info(
            "[simulation] Grid export cap disabled (unlimited). "
            "Curtailment will be zero. Internal MILP bound: %.0f kW.",
            p_grid_export_cap_milp,
        )
    else:
        p_grid_export_cap_milp = raw_grid_cap

    params: dict[str, Any] = {
        "dt_minutes": int(typed["dt_minutes"]),
        # bess
        "efficiency_charge": float(bess["efficiency_charge"]),
        "efficiency_discharge": float(bess["efficiency_discharge"]),
        "soc_min_frac": float(bess["soc_min_frac"]),
        "soc_max_frac": float(bess["soc_max_frac"]),
        "initial_soc_frac": float(bess["initial_soc_frac"]),
        "terminal_soc_equal": bool(bess["terminal_soc_equal"]),
        "max_cycles_per_day": float(bess["max_cycles_per_day"]),
        "bess_power_kw": bess_power_kw,
        "bess_capacity_kwh": bess_capacity_kwh,
        # pv
        "pv_nameplate_kwp": pv_nameplate_kwp,
        # project
        "p_grid_export_max_kw": p_grid_export_cap_milp,
        # Contract fields: not consumed by the internal dispatch but part of
        # the published params schema (asserted by the test suite / available
        # to API consumers), so they are retained intentionally.
        "grid_export_unlimited": grid_export_unlimited,
        "retail_tariff_eur_per_mwh": float(project["retail_tariff_eur_per_mwh"]),
        "settlement_minutes": int(project["settlement_minutes"]),
        "mode": str(project["mode"]),
        "allow_bess_grid_charging": bool(project["allow_bess_grid_charging"]),
        "unavailability_pct": float(project["unavailability_pct"]),
        "site_capex_eur": float(project.get("site_capex_eur", 0.0) or 0.0),
        "site_devex_eur": float(project.get("site_devex_eur", 0.0) or 0.0),
        "show_titles": bool(project["show_titles"]),
        # Max-injection cap profile (24,) or (24, 12), in percent of
        # p_grid_export_max_kw.  Expanded to a per-step array by the
        # max-injection helper module before entering the MILP.
        "max_injection_profile": typed.get("max_injection_profile"),
        # Balancing-market section, forwarded as a nested dict so the
        # MILP, KPI, lifetime and Monte Carlo paths can opt in without
        # changing the flat-params contract.
        "balancing": dict(
            typed.get("balancing") or BALANCING_SHEET_DEFAULTS,
        ),
        # simulation
        "plot_daily_scope": str(sim["plot_daily_scope"]),
        "plot_monthly_scope": str(sim["plot_monthly_scope"]),
        "plot_yearly_scope": str(sim["plot_yearly_scope"]),
    }
    return params, ts


def read_inputs(xlsx_path: str | Path) -> tuple[dict[str, Any], pd.DataFrame]:
    """Return ``(params, ts)`` — the flat shape used by the optimizer.

    Raises ``ValueError`` when both ``pv_nameplate_kwp`` and
    ``bess_power_kw`` are zero (no asset to optimise).
    """
    typed = read_workbook(xlsx_path)
    params, ts = _typed_to_flat(typed)
    if (
        float(params.get("pv_nameplate_kwp", 0.0) or 0.0) <= 0.0
        and float(params.get("bess_power_kw", 0.0) or 0.0) <= 0.0
    ):
        raise ValueError(
            "Both pv_nameplate_kwp and bess_power_kw are zero — "
            "nothing to optimise."
        )
    return params, ts


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


_ECON_UNITS: dict[str, str] = {}
for _rows in _SHEET_ROW_TEMPLATES.values():
    for _key, _default, _unit, _notes in _rows:
        _ECON_UNITS.setdefault(_key, _unit)


def _format_assumptions(econ: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, value in econ.items():
        rows.append({"key": key, "value": value, "unit": _ECON_UNITS.get(key, "")})
    return pd.DataFrame(rows, columns=["key", "value", "unit"])


def copy_input_snapshot(src_xlsx: Path, out_dir: Path, tag: str) -> Path | None:
    """Copy the input workbook into ``out_dir`` with a tag suffix."""
    src_xlsx = Path(src_xlsx)
    if not src_xlsx.exists():
        return None
    dst = out_dir / f"{src_xlsx.stem}_{tag}{src_xlsx.suffix}"
    dst.write_bytes(src_xlsx.read_bytes())
    return dst


# ---------------------------------------------------------------------------
# 00..05 numbered output layout
# ---------------------------------------------------------------------------

LAYOUT_SUBDIRS: tuple[str, ...] = (
    "00_summary",
    "01_inputs",
    "02_dispatch",
    "04_financial_plots",
    "05_energy_plots",
    "06_uncertainty_plots",
)


def make_run_layout(out_dir: Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name in LAYOUT_SUBDIRS:
        sub = out_dir / name
        sub.mkdir(parents=True, exist_ok=True)
        paths[name.split("_", 1)[1]] = sub
    paths["root"] = out_dir
    return paths


def write_assumptions_summary(
    out_path: Path,
    params: dict[str, Any],
    econ: dict[str, Any] | None,
) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("PV+BESS optimizer - assumptions snapshot")
    lines.append("=" * 60)
    lines.append("")
    lines.append("[params]")
    for key in sorted(params):
        if key.startswith("_"):
            continue
        # Hide the array-valued max_injection_profile from the snapshot —
        # it's already in the workbook's max_injection_profile sheet.
        if key == "max_injection_profile":
            continue
        lines.append(f"  {key} = {params[key]!r}")
    lines.append("")
    lines.append("[economic]")
    if econ:
        for key in sorted(econ):
            lines.append(f"  {key} = {econ[key]!r}")
    out_path.write_text("\n".join(lines) + "\n")
    return out_path


def write_dispatch_artifacts(
    dispatch_dir: Path,
    res_year1: pd.DataFrame,
    lifetime_df: pd.DataFrame | None,
    *,
    project_start_year: int = PROJECT_SHEET_DEFAULTS["project_start_year"],
) -> dict[str, Path]:
    """Write the ``02_dispatch/`` artefacts."""
    dispatch_dir = Path(dispatch_dir)
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    out = dispatch_dir / "dispatch_hourly.xlsx"

    if lifetime_df is not None and not lifetime_df.empty:
        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            for cy in sorted(lifetime_df["calendar_year"].unique()):
                sheet = str(int(cy))
                lifetime_df.loc[lifetime_df["calendar_year"] == cy].to_excel(
                    writer, sheet_name=sheet, index=False,
                )
    else:
        if pd.api.types.is_datetime64_any_dtype(res_year1["timestamp"]):
            cal_year = int(
                pd.to_datetime(res_year1["timestamp"]).dt.year.iloc[0]
            )
        else:
            cal_year = int(project_start_year)
        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            res_year1.to_excel(writer, sheet_name=str(cal_year), index=False)

    return {"hourly_xlsx": out}


def _flatten_kpis_for_sheet(kpis: dict[str, Any]) -> dict[str, Any]:
    """Flatten nested-dict KPI values into prefixed scalar rows.

    The ``kpis_year1`` sheet is a flat ``metric``/``value`` table, so a
    nested dict (e.g. ``bess_utilization_diagnostics``) would otherwise be
    stringified into a single ``{...}`` cell.  Each sub-key is hoisted to
    its own row instead; the in-memory KPI dict keeps the nested form for
    API consumers.
    """
    flat: dict[str, Any] = {}
    for key, value in kpis.items():
        if isinstance(value, dict):
            prefix = (
                "bess_util"
                if key == "bess_utilization_diagnostics"
                else key
            )
            for sub_key, sub_value in value.items():
                if key == "bess_utilization_diagnostics":
                    name = f"bess_util_{sub_key.removeprefix('bess_')}"
                else:
                    name = f"{prefix}_{sub_key}"
                flat[name] = sub_value
        else:
            flat[key] = value
    return flat


def write_results_workbook(
    out_path: Path,
    res_year1: pd.DataFrame,
    kpis_year1: dict[str, Any],
    kpis_monthly_year1: pd.DataFrame | None,
    *,
    yearly_cf: pd.DataFrame | None = None,
    monthly_cf: pd.DataFrame | None = None,
    quarterly_cf: pd.DataFrame | None = None,
    financial_kpis: dict[str, Any] | None = None,
    sensitivity: pd.DataFrame | None = None,
    lifetime_yearly: pd.DataFrame | None = None,
    economic_assumptions: dict[str, Any] | None = None,
    rolling_horizon_mc: pd.DataFrame | None = None,
    rolling_horizon_compare_mc: pd.DataFrame | None = None,
) -> Path:
    """Write the consolidated ``03_results.xlsx`` workbook."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        pd.DataFrame(
            list(_flatten_kpis_for_sheet(kpis_year1).items()),
            columns=["metric", "value"],
        ).to_excel(writer, sheet_name="kpis_year1", index=False)
        if kpis_monthly_year1 is not None and not kpis_monthly_year1.empty:
            kpis_monthly_year1.reset_index(names="month").to_excel(
                writer, sheet_name="kpis_monthly_year1", index=False,
            )
        res_year1.to_excel(writer, sheet_name="dispatch_year1", index=False)
        if yearly_cf is not None and not yearly_cf.empty:
            yearly_cf.to_excel(writer, sheet_name="cashflow_yearly", index=False)
        if quarterly_cf is not None and not quarterly_cf.empty:
            quarterly_cf.to_excel(
                writer, sheet_name="cashflow_quarterly", index=False,
            )
        if monthly_cf is not None and not monthly_cf.empty:
            monthly_cf.to_excel(writer, sheet_name="cashflow_monthly", index=False)
        if financial_kpis:
            pd.DataFrame(
                list(financial_kpis.items()), columns=["metric", "value"],
            ).to_excel(writer, sheet_name="financial_kpis", index=False)
        if sensitivity is not None and not sensitivity.empty:
            sensitivity.to_excel(
                writer, sheet_name="sensitivity_analysis", index=False,
            )
        if lifetime_yearly is not None and not lifetime_yearly.empty:
            lifetime_yearly.to_excel(
                writer, sheet_name="lifetime_dispatch_yearly", index=False,
            )
        if rolling_horizon_mc is not None and not rolling_horizon_mc.empty:
            rolling_horizon_mc.to_excel(
                writer, sheet_name="rolling_horizon_mc", index=False,
            )
        if (
            rolling_horizon_compare_mc is not None
            and not rolling_horizon_compare_mc.empty
        ):
            rolling_horizon_compare_mc.to_excel(
                writer, sheet_name="rolling_horizon_compare_mc", index=False,
            )
        if economic_assumptions:
            _format_assumptions(economic_assumptions).to_excel(
                writer, sheet_name="economic_assumptions", index=False,
            )
    return out_path
