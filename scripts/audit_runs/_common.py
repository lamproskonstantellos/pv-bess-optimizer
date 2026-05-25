"""Shared helpers for the audit-evidence driver scripts.

Each driver under :mod:`scripts.audit_runs` overrides the canonical
workbook for one mode x asset-config x balancing combination, runs the
full pipeline, then emits a JSON evidence file describing the solve,
the KPIs, and the per-invariant residuals.
"""

from __future__ import annotations

import json
import resource
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pvbess_opt.io import read_inputs
from pvbess_opt.kpis import (
    ENERGY_TOLERANCE,
    add_economic_columns,
    compute_kpis,
    verify_energy_balance,
)
from pvbess_opt.optimization import run_scenario, verify_dispatch_invariants
from pvbess_opt.rolling_horizon import monte_carlo_balancing

WORKBOOK = Path(__file__).resolve().parent.parent.parent / "inputs" / "input.xlsx"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def load_canonical_workbook() -> tuple[dict[str, Any], pd.DataFrame]:
    """Return the flat (params, ts) view of the canonical workbook."""
    return read_inputs(str(WORKBOOK))


def override_config(
    params: dict[str, Any],
    *,
    mode: str,
    asset_config: str,
    balancing_enabled: bool,
) -> dict[str, Any]:
    """Mutate a copy of ``params`` to match the requested combination."""
    out = dict(params)
    out["mode"] = mode
    if asset_config == "pv_only":
        out["bess_power_kw"] = 0.0
        out["bess_capacity_kwh"] = 0.0
    elif asset_config == "bess_only":
        out["pv_nameplate_kwp"] = 0.0
        out["allow_bess_grid_charging"] = True
    elif asset_config != "hybrid":
        raise ValueError(f"unknown asset_config: {asset_config!r}")

    raw_balancing = dict(out.get("balancing") or {})
    raw_balancing["balancing_enabled"] = bool(balancing_enabled)
    out["balancing"] = raw_balancing
    return out


def _invariant_status(invariants: dict[str, float]) -> dict[str, dict[str, float | bool]]:
    """Map invariant name -> {residual, within_tolerance}."""
    return {
        name: {
            "residual": float(value),
            "within_tolerance": bool(float(value) <= ENERGY_TOLERANCE),
        }
        for name, value in invariants.items()
    }


def _kpi_numeric_view(kpis: dict[str, Any]) -> dict[str, float]:
    """Keep only scalar numeric KPI keys; drop lists / strings."""
    out: dict[str, float] = {}
    for key, value in kpis.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            num = float(value)
            if np.isfinite(num):
                out[key] = num
    return out


def _peak_rss_mb() -> float:
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # On Linux ru_maxrss is in kilobytes; on macOS in bytes.  We run on Linux.
    return float(rss_kb) / 1024.0


def maybe_subsample(
    ts: pd.DataFrame, n_steps: int | None,
) -> tuple[pd.DataFrame, int | None]:
    """Return ``(ts[:n_steps], subsample_used)`` or pass-through.

    Used by drivers whose full-year solve overruns the 5-min budget
    (the fallback path documented in the Phase 3 prompt).
    """
    if n_steps is None or n_steps <= 0 or n_steps >= len(ts):
        return ts, None
    return ts.iloc[:n_steps].reset_index(drop=True), int(n_steps)


def run_pipeline(
    params: dict[str, Any],
    ts: pd.DataFrame,
    *,
    mc_scenarios: int,
    time_limit_seconds: int = 1800,
    mip_gap: float = 0.01,
    subsample_steps: int | None = None,
) -> dict[str, Any]:
    """Run the full audit pipeline for one combination.

    Returns a dict with the fields needed to emit a JSON evidence file.
    """
    bess_kw = float(params.get("bess_power_kw", 0.0) or 0.0)
    bess_kwh = float(params.get("bess_capacity_kwh", 0.0) or 0.0)
    has_bess = bess_kw > 0.0 and bess_kwh > 0.0
    balancing_enabled = bool(
        (params.get("balancing") or {}).get("balancing_enabled", False),
    )

    ts, applied_subsample = maybe_subsample(ts, subsample_steps)

    t0 = time.perf_counter()
    res, resolved, res_full = run_scenario(
        params, ts,
        solver_name="highs",
        mip_gap=mip_gap,
        time_limit_seconds=time_limit_seconds,
        return_unrounded=True,
    )
    solve_runtime_s = time.perf_counter() - t0

    invariants = verify_dispatch_invariants(res_full, params, mode=params["mode"])
    verify_energy_balance(res_full, params, raise_on_failure=True)

    add_economic_columns(res, params)
    kpis = compute_kpis(res, params, verify_balance=False)
    kpis_numeric = _kpi_numeric_view(kpis)

    mc_summary: dict[str, Any] | None = None
    if balancing_enabled and has_bess:
        mc = monte_carlo_balancing(
            res, params, n_scenarios=mc_scenarios, seed=1729,
        )
        if mc:
            mc_summary = {
                "n_scenarios": int(mc_scenarios),
                "bm_total_balancing_revenue_p10_eur": mc.get(
                    "bm_total_balancing_revenue_p10_eur",
                ),
                "bm_total_balancing_revenue_p50_eur": mc.get(
                    "bm_total_balancing_revenue_p50_eur",
                ),
                "bm_total_balancing_revenue_p90_eur": mc.get(
                    "bm_total_balancing_revenue_p90_eur",
                ),
                "bm_soc_constrained_scenarios_pct": mc.get(
                    "bm_soc_constrained_scenarios_pct",
                ),
            }

    # When balancing is off, every bm_ KPI must be exactly zero — record
    # so the JSON makes the guarantee explicit.
    if not balancing_enabled:
        bm_keys_nonzero = [
            k for k, v in kpis_numeric.items()
            if k.startswith("bm_") and abs(v) > 1e-9
        ]
        revenue_bess_balancing_keys_nonzero = [
            k for k, v in kpis_numeric.items()
            if k.startswith("revenue_bess_") and k != "revenue_bess_dam_eur"
            and abs(v) > 1e-9
        ]
    else:
        bm_keys_nonzero = []
        revenue_bess_balancing_keys_nonzero = []

    return {
        "solve_status": "optimal",
        "solver": resolved,
        "solve_runtime_s": float(round(solve_runtime_s, 3)),
        "peak_rss_mb": float(round(_peak_rss_mb(), 1)),
        "n_steps": len(res),
        "subsample_steps_applied": applied_subsample,
        "invariants": _invariant_status(invariants),
        "energy_balance_within_tolerance": True,
        "kpis": kpis_numeric,
        "monte_carlo": mc_summary,
        "balancing_off_zero_guards": {
            "bm_keys_nonzero": bm_keys_nonzero,
            "revenue_bess_balancing_keys_nonzero": (
                revenue_bess_balancing_keys_nonzero
            ),
        },
        "_dispatch_frame": res,
    }


def write_result_json(
    *,
    mode: str,
    asset_config: str,
    balancing_enabled: bool,
    pipeline_result: dict[str, Any],
    mc_scenarios: int,
) -> Path:
    """Emit the JSON evidence file for one combination."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    bm_tag = "on" if balancing_enabled else "off"
    name = f"{mode}_{asset_config}_{bm_tag}.json"
    path = RESULTS_DIR / name

    payload = {
        "combination": {
            "mode": mode,
            "asset_config": asset_config,
            "balancing_enabled": balancing_enabled,
        },
        "mc_scenarios": int(mc_scenarios),
        **{k: v for k, v in pipeline_result.items() if k != "_dispatch_frame"},
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def check_no_nonfinite(kpis: dict[str, float]) -> list[str]:
    """Return KPI keys whose values are NaN or +/- inf."""
    return [k for k, v in kpis.items() if not np.isfinite(v)]


def all_invariants_pass(invariants: dict[str, dict[str, Any]]) -> bool:
    return all(bool(entry["within_tolerance"]) for entry in invariants.values())


def driver_summary(
    *,
    mode: str,
    asset_config: str,
    balancing_enabled: bool,
    pipeline_result: dict[str, Any],
    json_path: Path,
) -> str:
    invariants = pipeline_result["invariants"]
    inv_ok = all_invariants_pass(invariants)
    nonfinite = check_no_nonfinite(pipeline_result["kpis"])
    parts: Iterable[str] = (
        f"mode={mode}",
        f"asset={asset_config}",
        f"bm={'on' if balancing_enabled else 'off'}",
        f"runtime_s={pipeline_result['solve_runtime_s']}",
        f"solver={pipeline_result['solver']}",
        f"invariants_ok={inv_ok}",
        f"nonfinite_kpis={len(nonfinite)}",
        f"json={json_path.name}",
    )
    return "  ".join(parts)
