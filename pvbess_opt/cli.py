"""Command-line entry point for the PV + BESS dispatch optimizer.

Parses arguments into a :class:`pvbess_opt.pipeline.RunConfig` and calls
:func:`pvbess_opt.pipeline.run`.  Run UX (output layout, plot-scope flags,
runtime estimates) is documented on :mod:`pvbess_opt.pipeline`.
"""

from __future__ import annotations

import argparse
import logging
import sys
import zipfile
from pathlib import Path

from pvbess_opt.pipeline import RunConfig, run
from pvbess_opt.scenarios import (
    read_scenarios_block,
    read_scenarios_file,
    run_scenarios,
)
from pvbess_opt.sizing import read_sizing_block, run_sizing

logger = logging.getLogger("pvbess_opt.cli")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PV + BESS dispatch optimizer.",
    )
    parser.add_argument(
        "excel", nargs="?", default="inputs/input.xlsx",
        help="Excel workbook input (default: inputs/input.xlsx).",
    )
    parser.add_argument(
        "--config", default=None,
        help="Structured config file (.yaml / .yml / .json) to run instead "
             "of the Excel workbook.",
    )
    parser.add_argument(
        "--scenarios", default=None,
        help="Scenarios file (.yaml / .yml / .json) to run as a batch "
             "comparison against the base input.",
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
        "--seed", type=int, default=None,
        help="Base seed for the Monte Carlo rolling-horizon ensemble "
             "(default 42).",
    )
    parser.add_argument(
        "--compare-uncertainty-sources", action="store_true", default=False,
        help="Run four MC ensembles (DAM-only, PV-only, Load-only, "
             "All-combined) and emit a comparison plot "
             "(overrides workbook uncertainty_compare_sources).",
    )
    args = parser.parse_args(argv)
    if args.monte_carlo is not None and args.monte_carlo < 0:
        # A negative count fell through every `n_seeds > 0` gate and
        # silently behaved like the documented 0 (deterministic RH).
        parser.error("--monte-carlo must be >= 0 (0 = deterministic run)")
    return args


def _warn_single_run_flags_ignored(args: argparse.Namespace) -> None:
    """Name every explicitly-set single-run flag a batch route discards.

    The scenario batch and the sizing sweep consume the shared run
    setup (--solver / --mode / --outdir / --mip-gap / --time-limit /
    --tee) but not the rolling-horizon / Monte Carlo / strict flags —
    those apply to single runs only.  They were previously accepted and
    silently dropped (exit 0, no message), so e.g. a --scenarios batch
    launched with --monte-carlo 50 looked like a stochastic comparison
    while every row solved deterministically.
    """
    ignored = [
        flag for flag, is_set in (
            ("--strict", bool(args.strict)),
            ("--rolling-horizon", bool(args.rolling_horizon)),
            ("--window-hours", args.window_hours is not None),
            ("--commit-hours", args.commit_hours is not None),
            ("--monte-carlo", args.monte_carlo is not None),
            ("--seed", args.seed is not None),
            (
                "--compare-uncertainty-sources",
                bool(args.compare_uncertainty_sources),
            ),
        ) if is_set
    ]
    if ignored:
        logger.warning(
            "Batch run (--scenarios / scenarios sheet / sizing sheet): "
            "the single-run flag(s) %s are ignored on this route. "
            "Configure the workbook's simulation sheet (uncertainty_* "
            "keys) to run scenarios with those features.",
            ", ".join(ignored),
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.config and args.excel != "inputs/input.xlsx":
        logger.warning(
            "Both a positional workbook (%s) and --config (%s) were given; "
            "the --config file is used and the positional workbook is "
            "ignored.", args.excel, args.config,
        )
    input_path = Path(args.config) if args.config else Path(args.excel)
    if not input_path.exists():
        logger.error("Input file not found: %s", input_path)
        return 2
    if args.scenarios and not Path(args.scenarios).exists():
        # Same user-error class as a missing workbook — same clean exit
        # (previously a raw FileNotFoundError traceback with rc 1).
        logger.error("Scenarios file not found: %s", args.scenarios)
        return 2
    # A solver typo previously surfaced only inside solve_model — after
    # the workbook read and the full model build (minutes at client
    # scale).  Probe availability up front with the same fail-fast
    # message and the missing-input exit code.
    from pvbess_opt.optimization import choose_solver

    try:
        choose_solver(args.solver)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    config = RunConfig(
        excel=input_path,
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
        seed=42 if args.seed is None else args.seed,
        compare_uncertainty_sources=args.compare_uncertainty_sources,
    )
    try:
        sizing_block = read_sizing_block(input_path)
        if args.scenarios:
            # The --scenarios file takes precedence, so a drafting mistake
            # in the workbook's (unused for this run) scenarios sheet must
            # not block the batch — mirror the sizing-sheet warn-and-ignore.
            try:
                sheet_scenarios = read_scenarios_block(input_path)
            except ValueError as exc:
                logger.warning(
                    "--scenarios was supplied; ignoring the workbook's "
                    "scenarios sheet, which failed to parse: %s", exc,
                )
                sheet_scenarios = None
        else:
            sheet_scenarios = read_scenarios_block(input_path)
        if sheet_scenarios and sizing_block and not args.scenarios:
            raise ValueError(
                "Both the 'sizing' and 'scenarios' sheets are enabled; "
                "enable only one (set the other's 'enabled' cell to FALSE)."
            )
        if args.scenarios and sizing_block:
            logger.warning(
                "--scenarios was supplied; the input's enabled sizing "
                "block is ignored for this run (the --scenarios batch "
                "takes precedence)."
            )
        if args.scenarios or sheet_scenarios or sizing_block:
            _warn_single_run_flags_ignored(args)
        if args.scenarios:
            run_scenarios(config, read_scenarios_file(args.scenarios))
        elif sheet_scenarios:
            run_scenarios(config, sheet_scenarios)
        elif sizing_block:
            run_sizing(config, sizing_block)
        else:
            run(config)
    except zipfile.BadZipFile as exc:
        # A corrupt/truncated workbook previously died as a bare
        # 'zipfile.BadZipFile: File is not a zip file' traceback naming
        # no file at all.
        logger.error(
            "Input file %s is not a valid .xlsx workbook (%s).",
            input_path, exc,
        )
        logger.debug("Traceback:", exc_info=True)
        return 2
    except FileNotFoundError as exc:
        # E.g. a config's timeseries_path pointing nowhere: one line
        # naming the file, same exit code as the missing-workbook path.
        # The stack stays available at DEBUG so an internal write-phase
        # FileNotFoundError is still diagnosable.
        logger.error("File not found: %s", exc.filename or exc)
        logger.debug("Traceback:", exc_info=True)
        return 2
    except OSError as exc:
        # ensure_writable_outdir raises a self-explanatory one-liner
        # (unusable --outdir); sibling user-error classes exit 2.
        logger.error("%s", exc)
        logger.debug("Traceback:", exc_info=True)
        return 2
    except ValueError as exc:
        if "Excel file format cannot be determined" in str(exc):
            # A non-Excel file saved with an .xlsx name (CSV/HTML/text)
            # raises pandas' format-sniff ValueError instead of
            # BadZipFile; same user error, same clean exit.
            logger.error(
                "Input file %s is not a valid .xlsx workbook (%s).",
                input_path, exc,
            )
            logger.debug("Traceback:", exc_info=True)
            return 2
        logger.exception("Run failed")
        return 1
    except Exception:
        logger.exception("Run failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
