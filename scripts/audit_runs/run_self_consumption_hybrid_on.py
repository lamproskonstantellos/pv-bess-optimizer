#!/usr/bin/env python
"""Audit driver: self_consumption x hybrid x balancing-ON."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.audit_runs._common import (  # noqa: E402
    all_invariants_pass,
    check_no_nonfinite,
    driver_summary,
    load_canonical_workbook,
    override_config,
    run_pipeline,
    write_result_json,
)

MODE = "self_consumption"
ASSET = "hybrid"
BALANCING = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mc-scenarios", type=int, default=25)
    parser.add_argument(
        "--subsample-steps", type=int, default=672,
        help=(
            "672 = one week at 15-min steps; full-year solve overruns "
            "the 5-min budget (Phase 3 prompt fallback)."
        ),
    )
    args = parser.parse_args()

    base_params, ts = load_canonical_workbook()
    params = override_config(
        base_params, mode=MODE, asset_config=ASSET, balancing_enabled=BALANCING,
    )
    result = run_pipeline(
        params, ts,
        mc_scenarios=args.mc_scenarios,
        subsample_steps=args.subsample_steps,
    )
    json_path = write_result_json(
        mode=MODE, asset_config=ASSET, balancing_enabled=BALANCING,
        pipeline_result=result, mc_scenarios=args.mc_scenarios,
    )
    print(driver_summary(
        mode=MODE, asset_config=ASSET, balancing_enabled=BALANCING,
        pipeline_result=result, json_path=json_path,
    ))
    if not all_invariants_pass(result["invariants"]):
        return 1
    if check_no_nonfinite(result["kpis"]):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
