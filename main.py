"""Command-line entry point for the PV + BESS dispatch optimizer.

Output layout — written to ``results/<input>_<scenario>_<timestamp>/``::

    00_summary/        SUMMARY.md, run_log.txt
    01_inputs/         input_snapshot.xlsx, assumptions_summary.txt
    02_dispatch/       dispatch_hourly.xlsx (one sheet per calendar year)
    03_results.xlsx    KPIs, cashflows, financial KPIs, sensitivity, ...
    04_financial_plots/ cumulative / waterfall / payback / tornados
    05_energy_plots/<calendar_year>/{daily,monthly,yearly}/...
                       lifetime_summary_<start>-<end>.pdf

All figures use the IEEE matplotlib preset and are exported as PDF.
Plot titles default to off; toggle with ``show_titles`` in the
``economic`` sheet.

Plot-scope flags in ``economic`` control how many energy PDFs are
produced (a 25-year run with daily-all would be ~9 000 PDFs):

* ``plot_daily_year1``   — render Year-1 daily plots (TRUE/FALSE)
* ``plot_monthly_scope`` — none / year1_only / all   (default ``all``)
* ``plot_yearly_scope``  — none / all                 (default ``all``)
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from pvbess_opt.economics import (
    build_yearly_cashflow,
    compute_financial_kpis,
    derive_asset_capacities,
    derive_monthly_cashflow,
    read_economic_params,
)
from pvbess_opt.io import (
    copy_input_snapshot,
    make_run_layout,
    read_inputs,
    write_assumptions_summary,
    write_dispatch_artifacts,
    write_results_workbook,
)
from pvbess_opt.kpis import compute_kpis, compute_monthly_kpis, verify_energy_balance
from pvbess_opt.lifetime import aggregate_lifetime_to_yearly, build_lifetime_dispatch
from pvbess_opt.optimization import run_scenario, verify_dispatch_invariants
from pvbess_opt.plotting import (
    apply_ieee_style,
    plot_cumulative_cashflow,
    plot_daily_combined,
    plot_daily_supply,
    plot_daily_surplus,
    plot_irr_tornado,
    plot_lifetime_summary,
    plot_monthly_cashflow_year1,
    plot_monthly_combined,
    plot_monthly_supply,
    plot_monthly_surplus,
    plot_npv_tornado,
    plot_npv_waterfall,
    plot_payback,
    plot_rolling_horizon_distribution,
    plot_yearly_cashflow_bars,
    plot_yearly_combined,
    plot_yearly_supply,
    plot_yearly_surplus,
    set_scenario_label,
    set_show_titles,
)
from pvbess_opt.rolling_horizon import monte_carlo_rolling, rolling_horizon_dispatch
from pvbess_opt.sensitivity import run_sensitivity_analysis

logger = logging.getLogger("pvbess_opt.main")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


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
        "--mode", default=None, choices=("vnb", "merchant"),
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

    # Rolling-horizon (Phase B) flags.
    parser.add_argument(
        "--rolling-horizon", action="store_true",
        help="Run a rolling-horizon dispatch with imperfect foresight.",
    )
    parser.add_argument(
        "--window-hours", type=int, default=48,
        help="Rolling-horizon window length in hours (default 48).",
    )
    parser.add_argument(
        "--commit-hours", type=int, default=24,
        help="Rolling-horizon commit slice in hours (default 24).",
    )
    parser.add_argument(
        "--monte-carlo", type=int, default=0,
        help="Number of Monte Carlo seeds for the rolling-horizon run "
             "(0 = single deterministic noiseless rolling horizon).",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Base seed for the Monte Carlo rolling-horizon ensemble.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# stdout capture (for 00_summary/run_log.txt)
# ---------------------------------------------------------------------------


class _Tee:
    """Tiny tee writer — duplicates writes to a file and a real stream."""

    def __init__(self, stream: Any, log_path: Path) -> None:
        self._stream = stream
        self._log_path = Path(log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._log_path, "w", encoding="utf-8", buffering=1)

    def write(self, data: str) -> int:
        self._stream.write(data)
        self._fh.write(data)
        return len(data)

    def flush(self) -> None:
        self._stream.flush()
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass


@contextmanager
def _tee_stdout_to_log(log_path: Path) -> Iterator[None]:
    """Context manager that mirrors stdout / stderr to ``log_path``."""
    original_out = sys.stdout
    original_err = sys.stderr
    tee_out = _Tee(original_out, log_path)
    sys.stdout = tee_out  # type: ignore[assignment]
    sys.stderr = tee_out  # type: ignore[assignment]
    try:
        yield
    finally:
        sys.stdout = original_out
        sys.stderr = original_err
        tee_out.close()


# ---------------------------------------------------------------------------
# Energy-plot dispatch (per calendar year, honours scope flags)
# ---------------------------------------------------------------------------


def _energy_plot_root_for_year(
    energy_plots_dir: Path, calendar_year: int,
) -> Path:
    return energy_plots_dir / str(int(calendar_year))


def _generate_energy_plots_for_year(
    res_for_year: pd.DataFrame,
    calendar_year: int,
    energy_plots_dir: Path,
    *,
    daily: bool,
    monthly: bool,
    yearly: bool,
) -> None:
    """Render daily / monthly / yearly plots for a single calendar year."""
    if not pd.api.types.is_datetime64_any_dtype(res_for_year["timestamp"]):
        return
    year_root = _energy_plot_root_for_year(energy_plots_dir, calendar_year)
    timestamps = pd.to_datetime(res_for_year["timestamp"])

    if daily:
        daily_root = year_root / "daily"
        unique_days = timestamps.dt.date.unique().tolist()
        for day in unique_days:
            date_str = pd.Timestamp(day).strftime("%Y-%m-%d")
            try:
                plot_daily_supply(res_for_year, date_str, daily_root)
                plot_daily_surplus(res_for_year, date_str, daily_root)
                plot_daily_combined(res_for_year, date_str, daily_root)
            except Exception:
                logger.exception("Daily plot failed for %s", date_str)

    if monthly:
        monthly_root = year_root / "monthly"
        monthly_root.mkdir(parents=True, exist_ok=True)
        months_present = sorted(set(timestamps.dt.month.tolist()))
        for month in months_present:
            try:
                plot_monthly_supply(res_for_year, month, monthly_root)
                plot_monthly_surplus(res_for_year, month, monthly_root)
                plot_monthly_combined(res_for_year, month, monthly_root)
            except Exception:
                logger.exception("Monthly plot failed for month %s", month)

    if yearly:
        yearly_root = year_root / "yearly"
        yearly_root.mkdir(parents=True, exist_ok=True)
        try:
            plot_yearly_supply(res_for_year, int(calendar_year), yearly_root)
            plot_yearly_surplus(res_for_year, int(calendar_year), yearly_root)
            plot_yearly_combined(res_for_year, int(calendar_year), yearly_root)
        except Exception:
            logger.exception(
                "Yearly plot failed for year %s", calendar_year,
            )


def _scope_active_for_year(
    scope: str, project_year: int,
) -> bool:
    """Translate the ``year1_only`` / ``all`` / ``none`` policy to a bool."""
    scope = (scope or "").strip().lower()
    if scope == "all":
        return True
    if scope == "year1_only":
        return project_year == 1
    return False


def _generate_all_energy_plots(
    res_year1: pd.DataFrame,
    lifetime_df: pd.DataFrame | None,
    lifetime_yearly: pd.DataFrame | None,
    econ: dict[str, Any],
    energy_plots_dir: Path,
) -> None:
    """Drive the energy-plot fan-out across the project lifetime."""
    daily_year1 = bool(econ.get("plot_daily_year1", True))
    monthly_scope = str(econ.get("plot_monthly_scope", "all"))
    yearly_scope = str(econ.get("plot_yearly_scope", "all"))
    project_start_year = int(econ.get("project_start_year", 2026) or 2026)

    if lifetime_df is None or lifetime_df.empty:
        if pd.api.types.is_datetime64_any_dtype(res_year1["timestamp"]):
            ts_first = pd.to_datetime(res_year1["timestamp"]).dt.year.iloc[0]
            cal_year = int(ts_first)
        else:
            cal_year = project_start_year
        _generate_energy_plots_for_year(
            res_year1, cal_year, energy_plots_dir,
            daily=daily_year1,
            monthly=_scope_active_for_year(monthly_scope, 1),
            yearly=_scope_active_for_year(yearly_scope, 1),
        )
        return

    for cal_year in sorted(lifetime_df["calendar_year"].unique()):
        sub = lifetime_df.loc[
            lifetime_df["calendar_year"] == int(cal_year)
        ].copy()
        proj_year = int(sub["project_year"].iloc[0])
        daily_active = daily_year1 and proj_year == 1
        _generate_energy_plots_for_year(
            sub, int(cal_year), energy_plots_dir,
            daily=daily_active,
            monthly=_scope_active_for_year(monthly_scope, proj_year),
            yearly=_scope_active_for_year(yearly_scope, proj_year),
        )

    if (
        lifetime_yearly is not None and not lifetime_yearly.empty
        and yearly_scope.lower() != "none"
    ):
        try:
            start = int(lifetime_yearly["calendar_year"].iloc[0])
            end = int(lifetime_yearly["calendar_year"].iloc[-1])
            plot_lifetime_summary(
                lifetime_yearly,
                energy_plots_dir / f"lifetime_summary_{start}-{end}.pdf",
            )
        except Exception:
            logger.exception("Lifetime summary plot failed")


# ---------------------------------------------------------------------------
# Financial plot orchestration
# ---------------------------------------------------------------------------


def _generate_financial_plots(
    yearly_cf: pd.DataFrame,
    monthly_cf: pd.DataFrame | None,
    sensitivity_df: pd.DataFrame | None,
    fin_kpis: dict[str, float] | None,
    econ: dict[str, Any],
    plots_dir: Path,
    rolling_mc: pd.DataFrame | None = None,
    pf_profit_eur: float | None = None,
) -> None:
    plots_dir.mkdir(parents=True, exist_ok=True)
    start = int(econ.get("project_start_year", 2026) or 2026)
    end = start + int(econ.get("project_lifecycle_years", 25) or 25) - 1
    try:
        plot_cumulative_cashflow(
            yearly_cf, plots_dir / f"cumulative_cashflow_{start}-{end}.pdf",
        )
        plot_yearly_cashflow_bars(
            yearly_cf, plots_dir / f"yearly_cashflow_bars_{start}-{end}.pdf",
        )
        plot_npv_waterfall(
            yearly_cf, plots_dir / f"npv_waterfall_{start}-{end}.pdf",
        )
        simple_pb = (
            float(fin_kpis.get("simple_payback_years", float("nan")))
            if fin_kpis else float("nan")
        )
        disc_pb = (
            float(fin_kpis.get("discounted_payback_years", float("nan")))
            if fin_kpis else float("nan")
        )
        plot_payback(
            yearly_cf, plots_dir / "payback_visualization.pdf",
            simple_payback_years=simple_pb,
            discounted_payback_years=disc_pb,
        )
        if monthly_cf is not None and not monthly_cf.empty:
            plot_monthly_cashflow_year1(
                monthly_cf, plots_dir / f"monthly_cashflow_{start}.pdf",
            )
        if sensitivity_df is not None and not sensitivity_df.empty:
            plot_npv_tornado(
                sensitivity_df, fin_kpis or {}, econ,
                plots_dir / "sensitivity_npv_tornado.pdf",
            )
            plot_irr_tornado(
                sensitivity_df, fin_kpis or {}, econ,
                plots_dir / "sensitivity_irr_tornado.pdf",
            )
        if rolling_mc is not None and not rolling_mc.empty:
            plot_rolling_horizon_distribution(
                rolling_mc,
                plots_dir / "rolling_horizon_distribution.pdf",
                pf_profit_eur=pf_profit_eur,
            )
    except Exception:
        logger.exception("Financial plot generation failed")


# ---------------------------------------------------------------------------
# Scenario runner
# ---------------------------------------------------------------------------


def _build_financials(
    excel_path: Path,
    params: dict[str, Any],
    ts: pd.DataFrame,
    e_cap_kwh: float,
    kpis: dict[str, Any],
    res: pd.DataFrame,
) -> dict[str, Any]:
    """Run the multi-year cash-flow + sensitivity + lifetime pipeline."""
    econ = read_economic_params(excel_path)

    capacities = derive_asset_capacities(econ, params, ts, e_cap_kwh)
    yearly_cf = build_yearly_cashflow(kpis, econ, capacities)
    monthly_cf, quarterly_cf = derive_monthly_cashflow(res, yearly_cf, econ)
    fin_kpis = compute_financial_kpis(yearly_cf, econ)
    lifetime_df = build_lifetime_dispatch(res, econ, capacities)
    lifetime_yearly = aggregate_lifetime_to_yearly(lifetime_df)
    sensitivity_df: pd.DataFrame | None
    if bool(econ.get("sensitivity_enabled", True)):
        sensitivity_df = run_sensitivity_analysis(
            kpis, econ, capacities, fin_kpis,
        )
    else:
        sensitivity_df = None

    return {
        "econ": econ,
        "capacities": capacities,
        "yearly_cf": yearly_cf,
        "monthly_cf": monthly_cf,
        "quarterly_cf": quarterly_cf,
        "fin_kpis": fin_kpis,
        "lifetime_df": lifetime_df,
        "lifetime_yearly": lifetime_yearly,
        "sensitivity": sensitivity_df,
    }


def _scenario_slug(params: dict[str, Any]) -> str:
    """Return the ``<mode>[_grid_ch]`` folder slug."""
    mode = str(params.get("mode", "vnb")).lower()
    suffix = "_grid_ch" if params.get("allow_bess_grid_charging") else ""
    return f"{mode}{suffix}"


def _check_strict_invariants(invariants: dict[str, float]) -> None:
    tol = 1.0e-3
    offenders = {
        k: v for k, v in invariants.items()
        if v > tol and k != "invariant_5_no_sim_grid_io_max_product_kwh2"
    }
    sim_io = invariants["invariant_5_no_sim_grid_io_max_product_kwh2"]
    if sim_io > tol ** 2:
        offenders["invariant_5_no_sim_grid_io_max_product_kwh2"] = sim_io
    if offenders:
        raise AssertionError(
            "Strict-mode invariant violations: "
            + ", ".join(f"{k}={v:.6g}" for k, v in offenders.items())
        )


def _run_one(
    params: dict[str, Any],
    ts: pd.DataFrame,
    args: argparse.Namespace,
    base_name: str,
    timestamp: str,
) -> tuple[Path, dict[str, Any]]:
    """Solve, post-process, archive and plot a single scenario."""
    slug = _scenario_slug(params)
    set_scenario_label(slug)

    folder = f"{base_name}_{slug}_{timestamp}"
    out_dir = Path(args.outdir) / folder
    layout = make_run_layout(out_dir)
    log_path = layout["summary"] / "run_log.txt"

    with _tee_stdout_to_log(log_path):
        print(f"[run] mode={params.get('mode')}  "
              f"allow_bess_grid_charging={params.get('allow_bess_grid_charging')}  "
              f"slug={slug!r}")
        print(f"[io] output dir: {out_dir.resolve()}")

        # Perfect-foresight base run (also serves as the rolling-horizon
        # benchmark when --rolling-horizon is on).
        res, e_cap_kwh, resolved_solver = run_scenario(
            params, ts, solver_name=args.solver,
            mip_gap=args.mip_gap, time_limit_seconds=args.time_limit,
            tee=args.tee,
        )
        residuals = verify_energy_balance(
            res, params, raise_on_failure=False,
        )
        print(
            f"[verify] solver={resolved_solver}  residuals(kWh): "
            + ", ".join(f"{k}={v:.3g}" for k, v in residuals.items())
        )

        invariants = verify_dispatch_invariants(
            res, params, mode=str(params.get("mode", "vnb")),
        )
        print(
            "[invariants] "
            + ", ".join(f"{k}={v:.3g}" for k, v in invariants.items())
        )
        if args.strict:
            _check_strict_invariants(invariants)

        kpis = compute_kpis(
            res, params, e_cap_kwh, verify_balance=False,
        )
        kpis_monthly = compute_monthly_kpis(res)

        # Optional rolling-horizon run (writes its KPIs alongside the
        # perfect-foresight benchmark for comparison).
        rolling_mc_df: pd.DataFrame | None = None
        if args.rolling_horizon:
            pf_profit_eur = float(kpis.get("profit_total_eur", 0.0))
            n_seeds = int(args.monte_carlo)
            if n_seeds > 0:
                print(f"[rolling] running {n_seeds} MC seeds "
                      f"(window={args.window_hours}h, commit={args.commit_hours}h, "
                      f"base_seed={args.seed})")
                rolling_mc_df = monte_carlo_rolling(
                    params, ts,
                    n_seeds=n_seeds,
                    base_seed=int(args.seed),
                    pf_profit_eur=pf_profit_eur,
                    window_hours=int(args.window_hours),
                    commit_hours=int(args.commit_hours),
                    solver_name=args.solver,
                    mip_gap=args.mip_gap,
                    time_limit_seconds=args.time_limit,
                    tee=args.tee,
                )
                # Add P10/P50/P90 KPIs.
                gap_p = rolling_mc_df["foresight_gap_pct"].quantile([0.10, 0.50, 0.90])
                kpis["foresight_gap_pct_p10"] = float(round(gap_p.loc[0.10], 4))
                kpis["foresight_gap_pct_p50"] = float(round(gap_p.loc[0.50], 4))
                kpis["foresight_gap_pct_p90"] = float(round(gap_p.loc[0.90], 4))
                kpis["mc_n_seeds"] = int(n_seeds)
                kpis["mc_window_hours"] = int(args.window_hours)
                kpis["mc_commit_hours"] = int(args.commit_hours)
            else:
                # Single deterministic noiseless rolling horizon.
                rh_full, rh_kpis = rolling_horizon_dispatch(
                    params, ts,
                    window_hours=int(args.window_hours),
                    commit_hours=int(args.commit_hours),
                    forecast_seed=None,
                    evaluate_with_actuals=True,
                    solver_name=args.solver,
                    mip_gap=args.mip_gap,
                    time_limit_seconds=args.time_limit,
                    tee=args.tee,
                )
                kpis["rolling_horizon_profit_eur"] = float(
                    rh_kpis.get("profit_total_eur", 0.0)
                )

        bundle = _build_financials(
            Path(args.excel), params, ts, e_cap_kwh, kpis, res,
        )
        econ = bundle["econ"]

        snap = copy_input_snapshot(
            Path(args.excel), layout["inputs"], "snapshot",
        )
        if snap is not None:
            renamed = layout["inputs"] / "input_snapshot.xlsx"
            snap.replace(renamed)
        write_assumptions_summary(
            layout["inputs"] / "assumptions_summary.txt", params, econ,
        )

        write_dispatch_artifacts(
            layout["dispatch"], res, bundle.get("lifetime_df"),
            project_start_year=int(econ.get("project_start_year", 2026) or 2026),
        )

        write_results_workbook(
            out_dir / "03_results.xlsx",
            res_year1=res,
            kpis_year1=kpis,
            kpis_monthly_year1=kpis_monthly,
            yearly_cf=bundle.get("yearly_cf"),
            monthly_cf=bundle.get("monthly_cf"),
            quarterly_cf=bundle.get("quarterly_cf"),
            financial_kpis=bundle.get("fin_kpis"),
            sensitivity=bundle.get("sensitivity"),
            lifetime_yearly=bundle.get("lifetime_yearly"),
            economic_assumptions=econ,
            rolling_horizon_mc=rolling_mc_df,
        )

        if bundle.get("yearly_cf") is not None:
            _generate_financial_plots(
                bundle["yearly_cf"],
                bundle.get("monthly_cf"),
                bundle.get("sensitivity"),
                bundle.get("fin_kpis"),
                econ,
                layout["financial_plots"],
                rolling_mc=rolling_mc_df,
                pf_profit_eur=float(kpis.get("profit_total_eur", 0.0)),
            )

        _generate_all_energy_plots(
            res, bundle.get("lifetime_df"), bundle.get("lifetime_yearly"),
            econ,
            layout["energy_plots"],
        )

        print(f"=== KPIs ({slug}) ===")
        for key, value in kpis.items():
            print(f"{key}: {value}")
        if bundle.get("fin_kpis"):
            print(f"=== Financial KPIs ({slug}) ===")
            for key, value in bundle["fin_kpis"].items():
                print(f"{key}: {value}")
        print(f"[io] outputs under: {out_dir.resolve()}")
    return out_dir, kpis


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

    params, ts = read_inputs(excel_path)
    apply_ieee_style()
    set_show_titles(params.get("show_titles", False))

    if args.mode is not None:
        params["mode"] = args.mode

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = excel_path.stem
    try:
        _run_one(params, ts, args, base_name, timestamp)
    except Exception:
        logger.exception("Run failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
