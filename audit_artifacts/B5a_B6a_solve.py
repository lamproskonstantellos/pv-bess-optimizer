"""B.5.a — energy balance residuals on the default workbook.
B.6.a — deterministic-solve reproducibility (KPIs should match bit-for-bit
        across two HiGHS runs of the same workbook).
Also captures wall-clock timing of a single annual MILP solve.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from pvbess_opt.io import read_inputs
from pvbess_opt.kpis import compute_kpis, verify_energy_balance
from pvbess_opt.optimization import run_scenario, verify_dispatch_invariants


def solve_once(params, ts, label):
    t0 = time.perf_counter()
    res, solver = run_scenario(
        params, ts, solver_name="highs", mip_gap=0.001, time_limit_seconds=1800,
    )
    dt = time.perf_counter() - t0
    invariants = verify_dispatch_invariants(res, params)
    residuals = verify_energy_balance(res, params, raise_on_failure=False)
    kpis = compute_kpis(res, params, verify_balance=False)
    return {
        "label": label,
        "solver": solver,
        "wallclock_s": dt,
        "invariants": invariants,
        "residuals": residuals,
        "kpis": {
            k: kpis[k] for k in (
                "profit_total_eur",
                "pv_generation_mwh",
                "bess_total_charge_mwh",
                "bess_total_discharge_mwh",
                "system_total_export_mwh",
                "system_total_import_mwh",
                "pv_energy_curtailed_mwh",
                "bess_equivalent_cycles_total",
                "soc_initial_pct",
                "soc_min_pct",
                "soc_max_pct",
                "soc_avg_pct",
            ) if k in kpis
        },
    }, res


def main():
    print(f"[load] {time.strftime('%H:%M:%S')}")
    params, ts = read_inputs("inputs/input.xlsx")
    n = len(ts)
    print(f"[load] timeseries rows={n}  dt_minutes={params['dt_minutes']}")
    print(f"[load] params: pv_kwp={params['pv_nameplate_kwp']}  "
          f"bess_kw={params['bess_power_kw']}  bess_kwh={params['bess_capacity_kwh']}  "
          f"grid_unlimited={params.get('grid_export_unlimited')}  "
          f"p_export_internal={params['p_grid_export_max_kw']}")

    print(f"\n[solve] run 1 ... {time.strftime('%H:%M:%S')}")
    r1, res1 = solve_once(params, ts, "run1")
    print(f"  wallclock = {r1['wallclock_s']:.1f} s")
    print(f"  invariants: {r1['invariants']}")
    print(f"  energy_balance: {r1['residuals']}")

    print(f"\n[solve] run 2 ... {time.strftime('%H:%M:%S')}")
    r2, res2 = solve_once(params, ts, "run2")
    print(f"  wallclock = {r2['wallclock_s']:.1f} s")

    # Reproducibility comparison.
    diffs = {}
    for k in r1["kpis"]:
        v1, v2 = r1["kpis"][k], r2["kpis"][k]
        diffs[k] = {"r1": v1, "r2": v2, "abs_diff": abs(float(v1) - float(v2))}
    print("\n[B.6.a — reproducibility]")
    for k, d in diffs.items():
        print(f"  {k:40s} r1={d['r1']:>18.4f}  r2={d['r2']:>18.4f}  "
              f"abs_diff={d['abs_diff']:.6g}")

    # Per-timestep diff on every numeric column for the dispatch frame.
    cols = [c for c in res1.columns if c != "timestamp"]
    max_diffs = {c: float((res1[c] - res2[c]).abs().max()) for c in cols}
    largest = sorted(max_diffs.items(), key=lambda kv: -kv[1])[:5]
    print("\n  top-5 per-step dispatch column diffs (max abs):")
    for c, v in largest:
        print(f"    {c:30s} max_abs_diff={v:.6g}")

    out = {
        "n_rows": int(n),
        "dt_minutes": int(params["dt_minutes"]),
        "grid_export_unlimited": bool(params.get("grid_export_unlimited")),
        "run1": r1,
        "run2": r2,
        "kpis_diff": diffs,
        "max_per_step_diff": max_diffs,
    }
    Path("audit_artifacts/B5a_B6a_solve.json").write_text(json.dumps(out, indent=2, default=str))
    print("\n[done] wrote audit_artifacts/B5a_B6a_solve.json")


if __name__ == "__main__":
    main()
