"""Phase D — empirical timing of rolling-horizon paths.

Three observations:

  D.3.1 — minimal noiseless RH (1 seed, weekly commit, full year):
          tests whether a "1 seed, 365 windows-equivalent" run finishes.

  D.3.2 — single MC seed (daily commit) on the full year: the user
          reported ">1h" with --monte-carlo 30 --commit-hours 24.
          One seed at the same cadence is the same workload divided by 30.

  D.3.3 — three MC seeds (daily commit): scaling check.

We also time ONE single rolling MILP window in isolation so the
back-of-envelope multiply checks out.
"""
from __future__ import annotations

import logging
import os
import resource
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# Silence verbose loggers so wall-clock noise stays clean.
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
# Keep one informational logger live so we can see ::print outputs.
logging.getLogger("pvbess_opt.rolling_horizon").setLevel(logging.WARNING)

from pvbess_opt.io import read_inputs
from pvbess_opt.optimization import build_model, solve_model, model_to_dataframe
from pvbess_opt.rolling_horizon import (
    add_forecast_noise,
    monte_carlo_rolling,
    rolling_horizon_dispatch,
    _hours_to_steps,
)


def peak_rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0  # KB on Linux


def time_single_window(params, ts, *, window_hours, commit_hours, seed):
    """Solve ONE window of the rolling-horizon MILP. Print MILP size."""
    dt_min = int(params["dt_minutes"])
    win_steps = _hours_to_steps(window_hours, dt_min)
    sub = ts.iloc[:win_steps].reset_index(drop=True).copy()
    rng = np.random.default_rng(seed)
    local_commit = _hours_to_steps(commit_hours, dt_min)
    noisy = add_forecast_noise(sub, commit_steps=local_commit, rng=rng)
    t0 = time.perf_counter()
    model = build_model(params, noisy, terminal_soc_free=True)
    build_dt = time.perf_counter() - t0
    n_vars = sum(1 for _ in model.component_data_objects(ctype=None, descend_into=True))
    # Pyomo vars + constraints + binaries
    from pyomo.environ import Var, Constraint, Binary
    n_var = sum(1 for v in model.component_data_objects(Var, descend_into=True))
    n_bin = sum(1 for v in model.component_data_objects(Var, descend_into=True) if v.domain is Binary)
    n_con = sum(1 for c in model.component_data_objects(Constraint, descend_into=True))
    t1 = time.perf_counter()
    solve_model(model, "highs", mip_gap=0.001, time_limit_seconds=120)
    solve_dt = time.perf_counter() - t1
    return {
        "window_hours": window_hours,
        "commit_hours": commit_hours,
        "n_steps_window": int(win_steps),
        "n_var": int(n_var),
        "n_bin": int(n_bin),
        "n_con": int(n_con),
        "build_s": build_dt,
        "solve_s": solve_dt,
    }


def time_full_rh(params, ts, *, window_hours, commit_hours, seed, label):
    t0 = time.perf_counter()
    _full, kpis = rolling_horizon_dispatch(
        params, ts,
        window_hours=window_hours, commit_hours=commit_hours,
        forecast_seed=seed,
        solver_name="highs", mip_gap=0.005, time_limit_seconds=60,
    )
    dt = time.perf_counter() - t0
    print(f"[{label}] wall={dt:.1f}s  profit_eur={kpis.get('profit_total_eur', 0):.2f}  "
          f"peak_rss_kb={peak_rss_mb():.0f}")
    return {"label": label, "wall_s": dt, "profit_eur": kpis.get("profit_total_eur", 0)}


def time_full_mc(params, ts, *, n_seeds, window_hours, commit_hours, base_seed, label):
    t0 = time.perf_counter()
    df = monte_carlo_rolling(
        params, ts,
        n_seeds=n_seeds, base_seed=base_seed,
        pf_profit_eur=None,
        window_hours=window_hours, commit_hours=commit_hours,
        solver_name="highs", mip_gap=0.005, time_limit_seconds=60,
    )
    dt = time.perf_counter() - t0
    print(f"[{label}] wall={dt:.1f}s  n_seeds={n_seeds}  "
          f"peak_rss_kb={peak_rss_mb():.0f}")
    return {"label": label, "wall_s": dt, "n_seeds": n_seeds, "rows": len(df)}


def main():
    print(f"[boot] cwd={os.getcwd()}  py={sys.version_info[:3]}")
    params, ts = read_inputs("inputs/input.xlsx")
    print(f"[load] rows={len(ts)}  dt_min={params['dt_minutes']}")
    n_rows = len(ts)
    dt_min = int(params["dt_minutes"])

    # --- D.2: time ONE rolling-horizon window (typical user config) ---
    print("\n=== D.2 — single window MILP timing (window=48h, commit=24h, dt=15min) ===")
    one = time_single_window(params, ts, window_hours=48, commit_hours=24, seed=42)
    print(f"  steps_in_window={one['n_steps_window']}  "
          f"vars={one['n_var']}  binaries={one['n_bin']}  constraints={one['n_con']}")
    print(f"  build={one['build_s']:.3f}s  solve={one['solve_s']:.3f}s  "
          f"window_total={(one['build_s']+one['solve_s']):.3f}s")
    # back-of-envelope projections
    windows_per_year_commit24 = -(-n_rows // _hours_to_steps(24, dt_min))
    windows_per_year_commit168 = -(-n_rows // _hours_to_steps(168, dt_min))
    proj_per_solve = (one['build_s'] + one['solve_s'])
    print(f"  windows/year @ commit=24h: {windows_per_year_commit24}")
    print(f"  windows/year @ commit=168h: {windows_per_year_commit168}")
    print(f"  projected 1-seed full-year wall (commit=24h): "
          f"{windows_per_year_commit24 * proj_per_solve / 60.0:.1f} min")
    print(f"  projected 30-seed full-year wall (commit=24h): "
          f"{windows_per_year_commit24 * proj_per_solve * 30 / 3600.0:.2f} h")
    print(f"  projected 1-seed full-year wall (commit=168h): "
          f"{windows_per_year_commit168 * proj_per_solve / 60.0:.1f} min")

    # --- D.3.1: 1 seed full-year (window=48h commit=24h) ---
    # This is exactly the user's failing config with n_seeds = 1 instead
    # of 30, so the wall-clock is 1/30 of what they'd have observed.
    print("\n=== D.3.1 — 1 seed full year (window=48h, commit=24h) ===")
    r1 = time_full_rh(
        params, ts,
        window_hours=48, commit_hours=24, seed=42, label="rh_w48_c24_s1",
    )

    # --- D.3.2: same again — repeat for noise floor on the wall-clock ---
    print("\n=== D.3.2 — 1 seed full year repeat (variance check) ===")
    r2 = time_full_rh(
        params, ts,
        window_hours=48, commit_hours=24, seed=43, label="rh_w48_c24_s1_repeat",
    )

    # --- D.3.3: 3 seeds via monte_carlo_rolling (linear scaling check) ---
    print("\n=== D.3.3 — 3 seeds via monte_carlo_rolling (linearity check) ===")
    r3 = time_full_mc(
        params, ts,
        n_seeds=3, window_hours=48, commit_hours=24, base_seed=42, label="mc_w48_c24_s3",
    )

    # --- B.6.b reproducibility on a *short* MC run ---
    # Use 1 seed, 48h window, 24h commit (= the user's config).
    print("\n=== B.6.b — Monte Carlo seed reproducibility (1 seed) ===")
    a1 = monte_carlo_rolling(
        params, ts,
        n_seeds=1, base_seed=42, pf_profit_eur=None,
        window_hours=48, commit_hours=24,
        solver_name="highs", mip_gap=0.005, time_limit_seconds=60,
    )
    a2 = monte_carlo_rolling(
        params, ts,
        n_seeds=1, base_seed=42, pf_profit_eur=None,
        window_hours=48, commit_hours=24,
        solver_name="highs", mip_gap=0.005, time_limit_seconds=60,
    )
    print(f"  run_a profit={a1['profit_total_eur'].iloc[0]:.4f}")
    print(f"  run_b profit={a2['profit_total_eur'].iloc[0]:.4f}")
    print(f"  abs_diff={abs(float(a1['profit_total_eur'].iloc[0]) - float(a2['profit_total_eur'].iloc[0])):.6g}")
    try:
        pd.testing.assert_frame_equal(a1, a2)
        print("  pd.testing.assert_frame_equal: PASS")
    except AssertionError as e:
        print(f"  pd.testing.assert_frame_equal: FAIL — {e}")

    print("\n[done]")


if __name__ == "__main__":
    main()
