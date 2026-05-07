"""Build the case-study ``inputs/input.xlsx`` workbook.

Generates a synthetic-but-realistic Greek profile:

* 8 760 hourly rows for 2026
* 4 500 kWp PV with sinusoidal seasonal envelope x diurnal sine
* 5 MW peak load (residential / commercial mix)
* DAM curve avg ~100 EUR/MWh +/- 50 EUR/MWh diurnal
* 3-5 negative-price hours seeded so the no-sim-IO logic and the
  sign-aware noise (Phase B) actually exercise

Run from the repo root::

    python scripts/build_input_xlsx.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUT_XLSX = REPO_ROOT / "inputs" / "input.xlsx"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pvbess_opt.io import write_workbook  # noqa: E402


def build_timeseries(year: int = 2026) -> pd.DataFrame:
    """Generate an 8760-row hourly timeseries for ``year``.

    PV: 4 500 kWp, sinusoidal seasonal envelope (peak in July, trough in
    January) x diurnal sine (clipped at 0).  load: 5 MW peak with
    residential/commercial mix (morning + evening peaks).  DAM:
    ~100 EUR/MWh average, diurnal variation, with 4 deliberately
    negative hours to exercise the sign-aware noise / no-sim-IO logic.
    """
    rng = np.random.default_rng(seed=20260101)
    timestamps = pd.date_range(
        start=f"{year}-01-01 00:00", periods=8760, freq="h",
    )
    hours = np.arange(8760, dtype=float)
    day_of_year = (hours // 24).astype(int) + 1

    # PV: 4500 kWp peak.  Seasonal: max in DOY 172 (~21 Jun), min in DOY 355.
    # Diurnal: sin(pi * (h - 6) / 12) on [6,18], else 0.
    seasonal = 0.55 + 0.45 * np.cos(2 * np.pi * (day_of_year - 172) / 365.25)
    h_of_day = (hours % 24).astype(float)
    diurnal = np.where(
        (h_of_day >= 6) & (h_of_day <= 18),
        np.sin(np.pi * (h_of_day - 6) / 12.0),
        0.0,
    )
    pv_kwp = 4500.0
    pv_kwh = pv_kwp * seasonal * diurnal
    pv_kwh += rng.normal(0.0, 30.0, size=8760)
    pv_kwh = np.maximum(pv_kwh, 0.0)

    # Load: 5 MW peak, ~3 MW base, morning and evening bumps.
    base = 3000.0
    morning = 1500.0 * np.exp(-((h_of_day - 9) ** 2) / 8.0)
    evening = 2000.0 * np.exp(-((h_of_day - 19) ** 2) / 6.0)
    load_kwh = base + morning + evening
    load_kwh += rng.normal(0.0, 80.0, size=8760)
    load_kwh = np.maximum(load_kwh, 800.0)

    # DAM: ~100 EUR/MWh with +/- 50 diurnal swing, low at midday (PV
    # surplus) and high at evening peak.
    dam = 100.0 - 50.0 * np.sin(np.pi * (h_of_day - 6) / 12.0)
    dam += rng.normal(0.0, 10.0, size=8760)
    # Seed 4 negative-price hours (early-morning windy weekend slots).
    negative_idx = [
        24 * 5 + 3,    # Saturday Jan 6th, 03:00
        24 * 47 + 4,   # mid-Feb 04:00
        24 * 102 + 2,  # mid-Apr 02:00
        24 * 250 + 3,  # early Sep 03:00
    ]
    for idx in negative_idx:
        dam[idx] = -25.0 + rng.normal(0.0, 3.0)

    return pd.DataFrame({
        "timestamp": timestamps,
        "load_kwh": np.round(load_kwh, 4),
        "pv_kwh": np.round(pv_kwh, 4),
        "dam_price_eur_per_mwh": np.round(dam, 4),
    })


def build_typed_dict() -> dict:
    """Assemble the typed nested dict for the case-study run."""
    ts = build_timeseries(2026)
    project = {
        "system": {
            "pv_nameplate_kwp": 4500.0,
            "bess_power_kw": 5000.0,
            "bess_capacity_kwh": 20000.0,
            "efficiency_charge": 0.97,
            "efficiency_discharge": 0.97,
            "soc_min_frac": 0.20,
            "soc_max_frac": 0.95,
            "initial_soc_frac": 0.50,
            "terminal_soc_equal": True,
            "p_charge_max_kw": 5000.0,
            "p_dis_max_kw": 5000.0,
            "battery_hours": 4.0,
            "max_cycles_per_day": 1.0,
            "p_grid_export_max_kw": 5000.0,
        },
        "regulatory": {
            "mode": "vnb",
            "retail_tariff_eur_per_mwh": 132.0,
            "curtailment_pct": 27.0,
            "allow_bess_grid_charging": False,
            "settlement_minutes": 15,
        },
        "optimization": {
            "weight_curtail_tiebreak": 1.0e-5,
            "weight_cycles_term": 0.0,
            "solver_mip_gap": 0.001,
            "solver_time_limit_seconds": 1800,
        },
    }
    economic = {
        "project_lifecycle_years": 25,
        "project_start_year": 2026,
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 1.0,
        "revenue_inflation_pct": 2.0,
        "capex_pv_eur_per_kw": 525.0,
        "capex_bess_eur_per_kw": 200.0,
        "capex_licenses_eur_per_kw": 90.0,
        "opex_pv_eur_per_kwp": 7.0,
        "opex_bess_eur_per_kw": 14.0,
        "pv_degradation_year1_pct": 2.5,
        "pv_degradation_annual_pct": 0.55,
        "bess_degradation_annual_pct": 2.0,
        "bess_replacement_year": 0,
        "bess_replacement_cost_pct": 50.0,
        "sensitivity_enabled": True,
        "sensitivity_capex_delta_pct": 10.0,
        "sensitivity_opex_delta_pct": 10.0,
        "sensitivity_revenue_delta_pct": 10.0,
        "sensitivity_discount_rate_delta_pp": 2.0,
        "show_titles": False,
        "currency_format": "auto",
        "plot_daily_year1": True,
        "plot_monthly_scope": "all",
        "plot_yearly_scope": "all",
    }
    return {"ts": ts, "project": project, "economic": economic}


def main() -> int:
    typed = build_typed_dict()
    out = write_workbook(typed, INPUT_XLSX)
    n_neg = int((typed["ts"]["dam_price_eur_per_mwh"] < 0).sum())
    print(
        f"Wrote {out} (timeseries rows={len(typed['ts'])}, "
        f"negative-price hours={n_neg})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
