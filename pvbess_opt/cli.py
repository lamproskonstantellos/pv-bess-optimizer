"""Command-line entry point for the PV + BESS dispatch optimizer.

Parses arguments into a :class:`pvbess_opt.pipeline.RunConfig` and calls
:func:`pvbess_opt.pipeline.run`.  Run UX (output layout, plot-scope flags,
runtime estimates) is documented on :mod:`pvbess_opt.pipeline`.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from pvbess_opt.pipeline import RunConfig, run

logger = logging.getLogger("pvbess_opt.cli")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PV + BESS dispatch optimizer.",
    )
    parser.add_argument(
        "excel", nargs="?", default="inputs/input.xlsx",
        help="Excel input file (default: inputs/input.xlsx)",
    )
    parser.add_argument("--solver", default="highs", help="gurobi | highs | cbc")
    parser.add_argument("--outdir", default="results",
                        help="output base directory")
    parser.add_argument(
        "--mode", default=None, choices=("self_consumption", "merchant"),
        help="Override regulatory mode (default: read from workbook).",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Turn dispatch-invariant violations from warnings into errors.",
    )
    parser.add_argument(
        "--mip-gap", type=float, default=0.001,
        help="Solver MIP gap (default 0.001).",
    )
    parser.add_argument(
        "--time-limit", type=int, default=1800,
        help="Solver time limit in seconds (default 1800).",
    )
    parser.add_argument("--tee", action="store_true",
                        help="Print solver output.")

    # Rolling-horizon flags.  These act as CLI overrides of
    # the workbook ``# uncertainty`` group; when omitted, the workbook
    # value applies (None sentinel signals "not provided").
    parser.add_argument(
        "--rolling-horizon", action="store_true", default=False,
        help="Force-enable rolling-horizon dispatch with imperfect "
             "foresight (overrides workbook uncertainty_enabled).",
    )
    parser.add_argument(
        "--window-hours", type=int, default=None,
        help="Rolling-horizon window length in hours "
             "(overrides workbook uncertainty_window_hours).",
    )
    parser.add_argument(
        "--commit-hours", type=int, default=None,
        help="Rolling-horizon commit slice in hours "
             "(overrides workbook uncertainty_commit_hours).",
    )
    parser.add_argument(
        "--monte-carlo", type=int, default=None,
        help="Number of Monte Carlo seeds (overrides workbook "
             "uncertainty_n_seeds; 0 = single deterministic noiseless RH).",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Base seed for the Monte Carlo rolling-horizon ensemble.",
    )
    parser.add_argument(
        "--compare-uncertainty-sources", action="store_true", default=False,
        help="Run four MC ensembles (DAM-only, PV-only, Load-only, "
             "All-combined) and emit a comparison plot "
             "(overrides workbook uncertainty_compare_sources).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    excel_path = Path(args.excel)
    if not excel_path.exists():
        logger.error("Input file not found: %s", excel_path)
        return 2

    config = RunConfig(
        excel=excel_path,
        solver=args.solver,
        outdir=Path(args.outdir),
        mode=args.mode,
        strict=args.strict,
        mip_gap=args.mip_gap,
        time_limit=args.time_limit,
        tee=args.tee,
        rolling_horizon=args.rolling_horizon,
        window_hours=args.window_hours,
        commit_hours=args.commit_hours,
        monte_carlo=args.monte_carlo,
        seed=args.seed,
        compare_uncertainty_sources=args.compare_uncertainty_sources,
    )
    try:
        run(config)
    except Exception:
        logger.exception("Run failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
