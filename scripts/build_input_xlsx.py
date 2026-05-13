"""Build the case-study ``inputs/input.xlsx`` workbook (v0.8 schema).

PV column policy
----------------

The canonical reference at ``data/pv_shape_15min.csv`` is the
real-world 8 MW site shape (35 040 rows @ 15-min cadence, 12 568
961,75 kWh annual ⇒ 1571,12 kWh/kWp specific production).  The
case-study workbook ships **scaled to 1 MW × 1500 kWh/kWp/year**
(1 500 000 kWh annual) — a tidy round-number default for new users.
The shape (every per-step ratio) is identical to the canonical 8 MW
reference; only the multiplicative scale differs.

The ``pv_nameplate_kwp`` and ``specific_production_kwh_per_kwp``
defaults on the ``pv`` sheet are pinned to ``1000.0`` and ``1500.0``
so they exactly match the shape that lives next to them in the
``timeseries`` sheet.

When a user later opens the workbook and changes
``pv_nameplate_kwp`` and / or ``specific_production_kwh_per_kwp`` to
their own project numbers, the ``timeseries`` sheet is **NOT** edited
— the rescaling happens **inside the model loader**
(:func:`pvbess_opt.io.read_workbook`) at runtime: the workbook shape
is multiplied by ``new_target_total / current_total`` so the
optimiser sees a series whose annual sum equals the user's
``pv_nameplate_kwp * specific_production_kwh_per_kwp`` exactly,
shape preserved.

Load and DAM remain synthetic with their own deterministic seed
(unrelated to the PV path).

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
PV_SHAPE_CSV = REPO_ROOT / "data" / "pv_shape_15min.csv"
PV_SHAPE_EXPECTED_ROWS = 35040  # full year @ 15-minute cadence

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pvbess_opt.io import write_workbook  # noqa: E402


def generate_pv_timeseries(
    pv_nameplate_kwp: float,
    specific_production_kwh_per_kwp: float,
    *,
    shape_csv: Path = PV_SHAPE_CSV,
) -> np.ndarray:
    """Deterministic data-driven PV generator.

    Loads the canonical 15-min shape from ``data/pv_shape_15min.csv``,
    normalises it to unit sum, scales by
    ``pv_nameplate_kwp * specific_production_kwh_per_kwp``.

    No noise.  No randomness.  Same inputs → identical bit-exact output.
    """
    shape_raw = pd.read_csv(shape_csv)["pv_kwh_8mw_reference"].to_numpy(
        dtype=float,
    )
    assert len(shape_raw) == PV_SHAPE_EXPECTED_ROWS, (
        f"Expected {PV_SHAPE_EXPECTED_ROWS} rows, got {len(shape_raw)}"
    )
    assert (shape_raw >= 0).all(), "Shape must be non-negative"

    if pv_nameplate_kwp <= 0.0:
        return np.zeros_like(shape_raw)

    shape_sum = float(shape_raw.sum())
    if shape_sum <= 0.0:
        return np.zeros_like(shape_raw)
    shape_unit = shape_raw / shape_sum
    annual_kwh = float(pv_nameplate_kwp) * float(specific_production_kwh_per_kwp)
    return shape_unit * annual_kwh


# The canonical reference (data/pv_shape_15min.csv) is from a real
# 8 MW site with 1571,12 kWh/kWp specific production.
CANONICAL_PV_NAMEPLATE_KWP: float = 8000.0
# Implied by the reference dataset: 12 568 961,75 kWh / 8 000 kWp.
CANONICAL_PV_SPECIFIC_PRODUCTION_KWH_PER_KWP: float = 1571.12021875

# The case-study default workbook ships with a tidy 1 MW × 1500 kWh/kWp
# scaling (1 500 000 kWh annual) — same shape, different magnitude.
DEFAULT_PV_NAMEPLATE_KWP: float = 1000.0
DEFAULT_PV_SPECIFIC_PRODUCTION_KWH_PER_KWP: float = 1500.0


def build_timeseries(
    year: int = 2026,
    target_minutes: int = 15,
    *,
    pv_nameplate_kwp: float = DEFAULT_PV_NAMEPLATE_KWP,
    specific_production_kwh_per_kwp: float = (
        DEFAULT_PV_SPECIFIC_PRODUCTION_KWH_PER_KWP
    ),
    seed: int = 20260101,
) -> pd.DataFrame:
    """Generate a 35 040-step (15-minute) timeseries for ``year``.

    PV is **deterministic**, derived from the canonical shape at
    ``data/pv_shape_15min.csv`` and scaled to
    ``pv_nameplate_kwp * specific_production_kwh_per_kwp`` exactly.

    Load and DAM remain synthetic with the documented seed (this
    script is the case-study fixture builder; downstream the user
    supplies real load / DAM in their own workbook).

    Only ``target_minutes = 15`` is supported now that the PV path is
    data-driven on the 15-minute reference shape.
    """
    if target_minutes != 15:
        raise ValueError(
            "data-driven PV path supports target_minutes=15 only "
            f"(got {target_minutes!r}); the canonical shape is at "
            "data/pv_shape_15min.csv."
        )

    rng = np.random.default_rng(seed=seed)
    n_steps_per_day = 24 * 60 // target_minutes
    n_steps = 365 * n_steps_per_day
    dt_hours = target_minutes / 60.0

    timestamps = pd.date_range(
        start=f"{year}-01-01 00:00", periods=n_steps, freq=f"{target_minutes}min",
    )
    step_idx = np.arange(n_steps, dtype=float)
    h_of_day = (step_idx % n_steps_per_day) * dt_hours

    pv_kwh = generate_pv_timeseries(
        pv_nameplate_kwp=pv_nameplate_kwp,
        specific_production_kwh_per_kwp=specific_production_kwh_per_kwp,
    )

    base_kw = 3000.0
    morning_kw = 1500.0 * np.exp(-((h_of_day - 9) ** 2) / 8.0)
    evening_kw = 2000.0 * np.exp(-((h_of_day - 19) ** 2) / 6.0)
    load_kw = base_kw + morning_kw + evening_kw
    load_kwh = load_kw * dt_hours
    load_kwh += rng.normal(0.0, 80.0 * dt_hours, size=n_steps)
    load_kwh = np.maximum(load_kwh, 800.0 * dt_hours)

    n_hours = 8760
    n_per_hour = 60 // target_minutes
    h_idx = np.arange(n_hours, dtype=float)
    h_of_day_hourly = h_idx % 24
    dam_hourly = 100.0 - 50.0 * np.sin(np.pi * (h_of_day_hourly - 6) / 12.0)
    dam_hourly += rng.normal(0.0, 10.0, size=n_hours)
    negative_hours = [
        24 * 5 + 3,    # Saturday Jan 6th, 03:00
        24 * 47 + 4,   # mid-Feb 04:00
        24 * 102 + 2,  # mid-Apr 02:00
        24 * 250 + 3,  # early Sep 03:00
    ]
    for h in negative_hours:
        dam_hourly[h] = -25.0 + rng.normal(0.0, 3.0)
    dam = np.repeat(dam_hourly, n_per_hour)

    return pd.DataFrame({
        "timestamp": timestamps,
        "load_kwh": np.round(load_kwh, 4),
        "pv_kwh": pv_kwh,
        "dam_price_eur_per_mwh": np.round(dam, 4),
    })


def build_typed_dict() -> dict:
    """Assemble the typed nested dict for the case-study run (v0.8 schema)."""
    pv_nameplate_kwp = DEFAULT_PV_NAMEPLATE_KWP
    specific_production_kwh_per_kwp = (
        DEFAULT_PV_SPECIFIC_PRODUCTION_KWH_PER_KWP
    )
    ts = build_timeseries(
        2026,
        pv_nameplate_kwp=pv_nameplate_kwp,
        specific_production_kwh_per_kwp=specific_production_kwh_per_kwp,
    )
    project = {
        "project_lifecycle_years": 25,
        "project_start_year": 2026,
        "mode": "vnb",
        "settlement_minutes": 15,
        # Project-wide cap applied to the combined PV + BESS export flow.
        "p_grid_export_max_kw": 5000.0,
        "retail_tariff_eur_per_mwh": 132.0,
        "allow_bess_grid_charging": False,
        "unavailability_pct": 1.0,
        "currency_format": "auto",
        "show_titles": False,
    }
    pv = {
        "pv_nameplate_kwp": pv_nameplate_kwp,
        "specific_production_kwh_per_kwp": specific_production_kwh_per_kwp,
        "pv_degradation_year1_pct": 2.5,
        "pv_degradation_annual_pct": 0.55,
        "capex_pv_eur_per_kw": 525.0,
        "devex_pv_eur_per_kw": 60.0,
        "opex_pv_eur_per_kwp": 7.0,
    }
    bess = {
        "bess_power_kw": 5000.0,
        "bess_capacity_kwh": 20000.0,
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
    }
    economics = {
        "discount_rate_pct": 7.0,
        "opex_inflation_pct": 1.0,
        "revenue_inflation_pct": 2.0,
        "aggregator_fee_pct_revenue": 10.0,
        "sensitivity_enabled": True,
        "sensitivity_capex_delta_pct": 10.0,
        "sensitivity_opex_delta_pct": 10.0,
        "sensitivity_revenue_delta_pct": 10.0,
        "sensitivity_discount_rate_delta_pp": 2.0,
    }
    simulation = {
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
        "plot_daily_scope": "year1_only",
        "plot_monthly_scope": "all",
        "plot_yearly_scope": "all",
    }
    # 24 hourly rows at constant 27 % — reproduces the v0.7 scalar baseline.
    curtailment_profile = np.full(24, 27.0, dtype=float)
    return {
        "ts": ts,
        "project": project,
        "pv": pv,
        "bess": bess,
        "economics": economics,
        "simulation": simulation,
        "curtailment_profile": curtailment_profile,
    }


def main() -> int:
    typed = build_typed_dict()
    out = write_workbook(typed, INPUT_XLSX)
    n_neg = int((typed["ts"]["dam_price_eur_per_mwh"] < 0).sum())
    pv_total_mwh = float(typed["ts"]["pv_kwh"].sum()) / 1000.0
    pv_kwp = float(typed["pv"]["pv_nameplate_kwp"])
    target = float(typed["pv"]["specific_production_kwh_per_kwp"])
    realised = pv_total_mwh * 1000.0 / pv_kwp if pv_kwp > 0 else 0.0
    print(
        f"Wrote {out} (timeseries rows={len(typed['ts'])}, "
        f"negative-price steps={n_neg}, "
        f"pv annual={pv_total_mwh:.1f} MWh, "
        f"specific production target={target:.0f} kWh/kWp, "
        f"realised={realised:.6f} kWh/kWp)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
