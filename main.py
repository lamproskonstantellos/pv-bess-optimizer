"""Command-line entry point for the PV + BESS dispatch optimizer.

Output layout — written to ``results/<input>_<scenario>_<timestamp>/``::

    00_summary/        SUMMARY.md, run_log.txt
    01_inputs/         input_snapshot.xlsx, assumptions_summary.txt
    02_dispatch/       dispatch_hourly.xlsx (one sheet per calendar year)
    03_results.xlsx    KPIs, cashflows, financial KPIs, sensitivity, ...
    04_financial_plots/ cumulative / waterfall / payback / tornados
    05_energy_plots/<calendar_year>/{daily,monthly,yearly}/...
                       lifetime_summary_<start>-<end>.pdf
    06_uncertainty_plots/ input forecast band, seasonal boxplot,
                       DAM heatmap, forecast-gap comparison

All figures use the IEEE matplotlib preset and are exported as PDF.
Plot titles default to off; toggle with ``show_titles`` in the
``project`` sheet.

Plot-scope flags in ``simulation`` control how many energy PDFs are
produced.  All three share the same vocabulary:

* ``plot_daily_scope``   — none / year1_only / all   (default ``year1_only``)
* ``plot_monthly_scope`` — none / year1_only / all   (default ``all``)
* ``plot_yearly_scope``  — none / year1_only / all   (default ``all``)

A 25-year run with ``plot_daily_scope = "all"`` produces ~9 000 daily
PDFs (3 figures × 365 days × 25 years).  The runner emits a WARNING
at run start in that case so the user can interrupt before the
post-solve fan-out kicks in.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from pvbess_opt.availability import apply_unavailability_derate, availability_factor
from pvbess_opt.economics import (
    build_yearly_cashflow,
    compute_financial_kpis,
    derive_asset_capacities,
    derive_monthly_cashflow,
    read_economic_params,
)
from pvbess_opt.io import (
    PROJECT_SHEET_DEFAULTS,
    copy_input_snapshot,
    make_run_layout,
    read_inputs,
    write_assumptions_summary,
    write_dispatch_artifacts,
    write_results_workbook,
)
from pvbess_opt.kpis import (
    ENERGY_TOLERANCE,
    compute_kpis,
    compute_monthly_kpis,
    verify_energy_balance,
)
from pvbess_opt.lifetime import aggregate_lifetime_to_yearly, build_lifetime_dispatch
from pvbess_opt.modes import resolve_mode
from pvbess_opt.optimization import run_scenario, verify_dispatch_invariants
from pvbess_opt.plotting import (
    apply_ieee_style,
    plot_bess_capacity_vs_activation_split,
    plot_bess_revenue_by_month,
    plot_bess_revenue_waterfall,
    plot_cumulative_cashflow,
    plot_daily_combined,
    plot_daily_combined_merchant,
    plot_daily_combined_merchant_with_soc,
    plot_daily_combined_with_soc,
    plot_daily_dispatch,
    plot_daily_revenue,
    plot_daily_soc,
    plot_daily_supply,
    plot_daily_surplus,
    plot_irr_tornado,
    plot_lcoe_summary,
    plot_lcos_summary,
    plot_lifetime_cycles,
    plot_lifetime_summary,
    plot_monthly_cashflow_year1,
    plot_monthly_combined,
    plot_monthly_combined_merchant,
    plot_monthly_dispatch,
    plot_monthly_revenue,
    plot_monthly_soc,
    plot_monthly_supply,
    plot_monthly_surplus,
    plot_npv_tornado,
    plot_npv_waterfall,
    plot_payback,
    plot_revenue_stack_yearly,
    plot_rolling_horizon_distribution,
    plot_yearly_cashflow_bars,
    plot_yearly_combined,
    plot_yearly_combined_merchant,
    plot_yearly_dispatch,
    plot_yearly_revenue,
    plot_yearly_soc,
    plot_yearly_supply,
    plot_yearly_surplus,
    set_project_mode_label,
    set_scenario_label,
    set_show_titles,
)
from pvbess_opt.plotting.uncertainty import plot_foresight_gap_comparison
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
    mode: str = "self_consumption",
) -> None:
    """Render daily / monthly / yearly plots for a single calendar year.

    In ``self_consumption`` mode this drives the supply / surplus / combined views.
    In ``merchant`` mode the load is pinned to zero so those plots
    collapse — render the dispatch / SOC / revenue trio instead.
    """
    if not pd.api.types.is_datetime64_any_dtype(res_for_year["timestamp"]):
        return
    year_root = _energy_plot_root_for_year(energy_plots_dir, calendar_year)
    timestamps = pd.to_datetime(res_for_year["timestamp"])
    is_merchant = str(mode).lower() == "merchant"

    if daily:
        daily_root = year_root / "daily"
        unique_days = timestamps.dt.date.unique().tolist()
        for day in unique_days:
            date_str = pd.Timestamp(day).strftime("%Y-%m-%d")
            try:
                if is_merchant:
                    plot_daily_dispatch(res_for_year, date_str, daily_root)
                    plot_daily_soc(res_for_year, date_str, daily_root)
                    plot_daily_revenue(res_for_year, date_str, daily_root)
                    plot_daily_combined_merchant(
                        res_for_year, date_str, daily_root,
                    )
                    plot_daily_combined_merchant_with_soc(
                        res_for_year, date_str, daily_root,
                    )
                else:
                    plot_daily_supply(res_for_year, date_str, daily_root)
                    plot_daily_surplus(res_for_year, date_str, daily_root)
                    plot_daily_combined(res_for_year, date_str, daily_root)
                    plot_daily_soc(res_for_year, date_str, daily_root)
                    plot_daily_combined_with_soc(
                        res_for_year, date_str, daily_root,
                    )
            except Exception:
                logger.exception("Daily plot failed for %s", date_str)

    if monthly:
        monthly_root = year_root / "monthly"
        monthly_root.mkdir(parents=True, exist_ok=True)
        months_present = sorted(set(timestamps.dt.month.tolist()))
        for month in months_present:
            try:
                if is_merchant:
                    plot_monthly_dispatch(res_for_year, month, monthly_root)
                    plot_monthly_soc(res_for_year, month, monthly_root)
                    plot_monthly_revenue(res_for_year, month, monthly_root)
                    plot_monthly_combined_merchant(
                        res_for_year, month, monthly_root,
                    )
                else:
                    plot_monthly_supply(res_for_year, month, monthly_root)
                    plot_monthly_surplus(res_for_year, month, monthly_root)
                    plot_monthly_combined(res_for_year, month, monthly_root)
                    plot_monthly_soc(res_for_year, month, monthly_root)
            except Exception:
                logger.exception("Monthly plot failed for month %s", month)

    if yearly:
        yearly_root = year_root / "yearly"
        yearly_root.mkdir(parents=True, exist_ok=True)
        try:
            if is_merchant:
                plot_yearly_dispatch(res_for_year, int(calendar_year), yearly_root)
                plot_yearly_soc(res_for_year, int(calendar_year), yearly_root)
                plot_yearly_revenue(res_for_year, int(calendar_year), yearly_root)
                plot_yearly_combined_merchant(
                    res_for_year, int(calendar_year), yearly_root,
                )
            else:
                plot_yearly_supply(res_for_year, int(calendar_year), yearly_root)
                plot_yearly_surplus(res_for_year, int(calendar_year), yearly_root)
                plot_yearly_combined(res_for_year, int(calendar_year), yearly_root)
                plot_yearly_soc(res_for_year, int(calendar_year), yearly_root)
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
    *,
    mode: str = "self_consumption",
) -> None:
    """Drive the energy-plot fan-out across the project lifetime."""
    daily_scope = str(econ.get("plot_daily_scope", "year1_only"))
    monthly_scope = str(econ.get("plot_monthly_scope", "all"))
    yearly_scope = str(econ.get("plot_yearly_scope", "all"))
    project_start_year = int(
        econ.get("project_start_year", PROJECT_SHEET_DEFAULTS["project_start_year"])
        or PROJECT_SHEET_DEFAULTS["project_start_year"]
    )

    if lifetime_df is None or lifetime_df.empty:
        if pd.api.types.is_datetime64_any_dtype(res_year1["timestamp"]):
            ts_first = pd.to_datetime(res_year1["timestamp"]).dt.year.iloc[0]
            cal_year = int(ts_first)
        else:
            cal_year = project_start_year
        _generate_energy_plots_for_year(
            res_year1, cal_year, energy_plots_dir,
            daily=_scope_active_for_year(daily_scope, 1),
            monthly=_scope_active_for_year(monthly_scope, 1),
            yearly=_scope_active_for_year(yearly_scope, 1),
            mode=mode,
        )
        return

    for cal_year in sorted(lifetime_df["calendar_year"].unique()):
        sub = lifetime_df.loc[
            lifetime_df["calendar_year"] == int(cal_year)
        ].copy()
        proj_year = int(sub["project_year"].iloc[0])
        _generate_energy_plots_for_year(
            sub, int(cal_year), energy_plots_dir,
            daily=_scope_active_for_year(daily_scope, proj_year),
            monthly=_scope_active_for_year(monthly_scope, proj_year),
            yearly=_scope_active_for_year(yearly_scope, proj_year),
            mode=mode,
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


def _generate_uncertainty_plots(
    ts: pd.DataFrame,
    out_dir: Path,
    *,
    diagnostics_enabled: bool = True,
    sigma_dam: float = 0.20,
    sigma_pv: float = 0.12,
    sigma_load: float = 0.05,
    commit_steps: int = 96,
) -> None:
    """Render the input-uncertainty PDFs into ``out_dir``.

    Always writes the forecast band, seasonal boxplot and DAM heatmap.
    When ``diagnostics_enabled`` is True (simulation-sheet flag
    ``uncertainty_diagnostics_enabled``), also writes the four
    forecast-calibration diagnostic plots.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        from pvbess_opt.plotting import (
            plot_dam_intraday_heatmap,
            plot_input_forecast_band,
            plot_input_seasonal_boxplot,
            plot_uncertainty_coverage_by_horizon,
            plot_uncertainty_crps_timeline,
            plot_uncertainty_pit_histogram,
            plot_uncertainty_residual_qq,
        )
        plot_input_forecast_band(
            ts, out_dir / "inputs_forecast_band.pdf",
            week_start_doy=165,
        )
        plot_input_seasonal_boxplot(
            ts, out_dir / "inputs_seasonal_boxplot.pdf",
        )
        plot_dam_intraday_heatmap(
            ts, out_dir / "dam_intraday_heatmap.pdf",
        )
        if diagnostics_enabled:
            sig = dict(sigma_dam=sigma_dam, sigma_pv=sigma_pv, sigma_load=sigma_load)
            plot_uncertainty_coverage_by_horizon(
                ts, out_dir / "coverage_by_horizon.pdf",
                commit_steps=commit_steps, **sig,
            )
            plot_uncertainty_pit_histogram(
                ts, out_dir / "pit_histogram.pdf", **sig,
            )
            plot_uncertainty_crps_timeline(
                ts, out_dir / "crps_timeline.pdf", **sig,
            )
            plot_uncertainty_residual_qq(
                ts, out_dir / "residual_qq.pdf", **sig,
            )
    except Exception:
        logger.exception("Uncertainty plot generation failed")


def _generate_financial_plots(
    yearly_cf: pd.DataFrame,
    monthly_cf: pd.DataFrame | None,
    sensitivity_df: pd.DataFrame | None,
    fin_kpis: dict[str, float] | None,
    econ: dict[str, Any],
    plots_dir: Path,
    rolling_mc: pd.DataFrame | None = None,
    rolling_compare_mc: pd.DataFrame | None = None,
    uncertainty_dir: Path | None = None,
    pf_profit_eur: float | None = None,
    *,
    year1_kpis: dict[str, Any] | None = None,
    lifetime_yearly: pd.DataFrame | None = None,
    capacities: dict[str, float] | None = None,
    res_year1: pd.DataFrame | None = None,
) -> None:
    plots_dir.mkdir(parents=True, exist_ok=True)
    start = int(
        econ.get("project_start_year", PROJECT_SHEET_DEFAULTS["project_start_year"])
        or PROJECT_SHEET_DEFAULTS["project_start_year"]
    )
    n_years = int(
        econ.get("project_lifecycle_years",
                 PROJECT_SHEET_DEFAULTS["project_lifecycle_years"])
        or PROJECT_SHEET_DEFAULTS["project_lifecycle_years"]
    )
    end = start + n_years - 1
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
            yearly_cf,
            plots_dir / f"cumulative_cashflow_with_payback_{start}-{end}.pdf",
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
        if rolling_compare_mc is not None and not rolling_compare_mc.empty:
            plot_rolling_horizon_distribution(
                rolling_compare_mc,
                plots_dir / "rolling_horizon_distribution_compare.pdf",
                pf_profit_eur=pf_profit_eur,
            )
            target_dir = uncertainty_dir or plots_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            plot_foresight_gap_comparison(
                rolling_compare_mc,
                target_dir / "rolling_horizon_foresight_gap_comparison.pdf",
            )
        # Lifecycle plots
        if year1_kpis is not None:
            plot_revenue_stack_yearly(
                yearly_cf, year1_kpis,
                plots_dir / f"revenue_stack_yearly_{start}-{end}.pdf",
                econ=econ,
            )
            plot_bess_revenue_waterfall(
                year1_kpis,
                plots_dir / "bess_revenue_waterfall.pdf",
                econ=econ,
            )
            plot_bess_capacity_vs_activation_split(
                year1_kpis,
                plots_dir / "bess_revenue_capacity_vs_activation.pdf",
                econ=econ,
            )
            if res_year1 is not None:
                plot_bess_revenue_by_month(
                    res_year1, year1_kpis,
                    plots_dir / "bess_revenue_by_month.pdf",
                    econ=econ,
                )
        if lifetime_yearly is not None and capacities is not None:
            plot_lifetime_cycles(
                lifetime_yearly,
                float(capacities.get("bess_kwh", 0.0) or 0.0),
                plots_dir / f"lifetime_cycles_{start}-{end}.pdf",
                bess_present=float(capacities.get("bess_kw", 0.0) or 0.0) > 0.0,
            )
        if fin_kpis is not None and capacities is not None:
            plot_lcoe_summary(
                fin_kpis, sensitivity_df, capacities, econ,
                plots_dir / "lcoe_summary.pdf",
            )
            plot_lcos_summary(
                fin_kpis, sensitivity_df, capacities, econ,
                plots_dir / "lcos_summary.pdf",
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
    kpis: dict[str, Any],
    res: pd.DataFrame,
) -> dict[str, Any]:
    """Run the multi-year cash-flow + sensitivity + lifetime pipeline."""
    econ = read_economic_params(excel_path)

    site_capex_eur = float(econ.get("site_capex_eur", 0.0) or 0.0)
    site_devex_eur = float(econ.get("site_devex_eur", 0.0) or 0.0)
    if site_capex_eur > 0.0 or site_devex_eur > 0.0:
        logger.info(
            "[financials] Site-wide lump-sum costs: site_capex_eur = "
            "%.2f EUR, site_devex_eur = %.2f EUR (paid in Year 0).",
            site_capex_eur, site_devex_eur,
        )

    capacities = derive_asset_capacities(econ, params, ts)
    yearly_cf = build_yearly_cashflow(kpis, econ, capacities)
    monthly_cf, quarterly_cf = derive_monthly_cashflow(res, yearly_cf, econ)
    # Symmetric cycle-count input: build_yearly_cashflow already
    # reads bess_total_discharge_mwh from the derated kpis dict, so
    # feed the same number into build_lifetime_dispatch.  Without this
    # the two paths run separate cycle counters that drift by
    # ``unavailability_pct`` over the lifecycle.
    year1_discharge_for_cycles = float(
        kpis.get("bess_total_discharge_mwh", 0.0) or 0.0
    )
    lifetime_df = build_lifetime_dispatch(
        res, econ, capacities,
        year1_discharge_mwh=year1_discharge_for_cycles,
    )
    lifetime_yearly = aggregate_lifetime_to_yearly(lifetime_df)
    # Post-solve unavailability derate on the lifetime
    # totals (PV generation and BESS discharge) so LCOE / LCOS
    # denominators reflect the realistic operating envelope.
    avail_factor = availability_factor(
        float(econ.get("unavailability_pct", 0.0) or 0.0)
    )
    if avail_factor < 1.0 and not lifetime_yearly.empty:
        for col in (
            "pv_generation_mwh", "bess_discharge_mwh", "bess_charge_mwh",
            "pv_to_load_mwh", "pv_to_grid_mwh", "import_to_load_mwh",
            "export_total_mwh", "revenue_eur_total",
        ):
            if col in lifetime_yearly.columns:
                lifetime_yearly[col] = (
                    lifetime_yearly[col].astype(float) * avail_factor
                )
    fin_kpis = compute_financial_kpis(
        yearly_cf, econ,
        capacities=capacities,
        lifetime_yearly=lifetime_yearly,
        year1_kpis=kpis,
    )
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
    mode = resolve_mode(params)
    suffix = "_grid_ch" if params.get("allow_bess_grid_charging") else ""
    return f"{mode}{suffix}"


def _check_strict_invariants(invariants: dict[str, float]) -> None:
    tol = ENERGY_TOLERANCE
    # Invariant 6 is an integer count and piggybacks on the same tol;
    # the smallest non-zero count is 1, which trivially exceeds tol=1e-3.
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


def _emit_bess_utilisation_audit(
    kpis: dict[str, Any], params: dict[str, Any],
) -> None:
    """Log the Year-1 BESS utilisation diagnostics in a single block.

    Warns when actual utilisation falls below 30 % of the theoretical
    annual cycle budget — the two usual causes (load ≫ PV with
    grid-charging disabled, or DAM-arbitrage economics marginal) are
    named so the user knows where to look.
    """
    diag = kpis.get("bess_utilization_diagnostics")
    bess_power_kw = float(params.get("bess_power_kw", 0.0) or 0.0)
    if not diag or bess_power_kw <= 0.0:
        return
    key_width = max(len(k) for k in diag.keys()) + 2
    lines = ["[BESS utilisation audit]"]
    for k, v in diag.items():
        lines.append(f"  {k.ljust(key_width)}{v}")
    logger.info("\n".join(lines))
    util = float(diag.get("bess_utilization_pct", 0.0))
    if util < 30.0:
        logger.warning(
            "BESS Year-1 utilisation is %.1f %% of the theoretical annual "
            "cycle budget (< 30 %%).  Likely causes: (a) load >> PV with "
            "allow_bess_grid_charging disabled, leaving no surplus to "
            "charge from; (b) DAM-arbitrage economics marginal versus "
            "battery degradation.  Consider enabling "
            "allow_bess_grid_charging or resizing the BESS.",
            util,
        )


def _project_mode_label(params: dict[str, Any]) -> str:
    """Return ``"PV-only"`` / ``"BESS-only"`` / ``"Hybrid PV+BESS"``."""
    pv_present = float(params.get("pv_nameplate_kwp", 0.0) or 0.0) > 0.0
    bess_present = float(params.get("bess_power_kw", 0.0) or 0.0) > 0.0
    if pv_present and bess_present:
        return "Hybrid PV+BESS"
    if pv_present:
        return "PV-only"
    if bess_present:
        return "BESS-only"
    return ""


_COMPARE_SOURCE_FLAGS: tuple[tuple[str, bool, bool, bool], ...] = (
    ("dam", True, False, False),
    ("pv", False, True, False),
    ("load", False, False, True),
    ("all", True, True, True),
)


def _resolve_uncertainty_config(
    args: argparse.Namespace, econ: dict[str, Any], mode: str,
) -> dict[str, Any]:
    """Merge CLI overrides on top of the workbook ``# uncertainty`` group."""
    enabled = bool(args.rolling_horizon) or bool(econ.get("uncertainty_enabled", False))
    compare = (
        bool(args.compare_uncertainty_sources)
        or bool(econ.get("uncertainty_compare_sources", False))
    )
    n_seeds = (
        int(args.monte_carlo) if args.monte_carlo is not None
        else int(econ.get("uncertainty_n_seeds", 30) or 30)
    )
    window = (
        int(args.window_hours) if args.window_hours is not None
        else int(econ.get("uncertainty_window_hours", 48) or 48)
    )
    commit = (
        int(args.commit_hours) if args.commit_hours is not None
        else int(econ.get("uncertainty_commit_hours", 24) or 24)
    )
    enable_dam = bool(econ.get("uncertainty_dam_enabled", True))
    enable_pv = bool(econ.get("uncertainty_pv_enabled", True))
    enable_load = bool(econ.get("uncertainty_load_enabled", True))
    if mode == "merchant" and enable_load:
        logger.info(
            "merchant mode: ignoring uncertainty_load_enabled (no load to perturb)"
        )
        enable_load = False
    return {
        "enabled": enabled,
        "compare_sources": compare,
        "n_seeds": n_seeds,
        "window_hours": window,
        "commit_hours": commit,
        "enable_dam": enable_dam,
        "enable_pv": enable_pv,
        "enable_load": enable_load,
        "sigma_dam": float(econ.get("uncertainty_sigma_dam", 0.20) or 0.20),
        "sigma_pv": float(econ.get("uncertainty_sigma_pv", 0.12) or 0.12),
        "sigma_load": float(econ.get("uncertainty_sigma_load", 0.05) or 0.05),
        "base_seed": int(args.seed),
    }


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
    set_project_mode_label(_project_mode_label(params))

    folder = f"{base_name}_{slug}_{timestamp}"
    out_dir = Path(args.outdir) / folder
    layout = make_run_layout(out_dir)
    log_path = layout["summary"] / "run_log.txt"

    # Load the economic group up front so the uncertainty config can be
    # resolved before the perfect-foresight solve produces its KPIs.
    econ_pre = read_economic_params(Path(args.excel))
    unc_cfg = _resolve_uncertainty_config(
        args, econ_pre, mode=resolve_mode(params),
    )

    # plot_daily_scope = "all" with a long horizon produces ~9 000 PDFs
    # for a 25-year run.  Warn loudly so the user can interrupt before
    # the post-solve fan-out kicks in.
    if str(econ_pre.get("plot_daily_scope", "year1_only")).strip().lower() == "all":
        n_years = int(
            econ_pre.get("project_lifecycle_years",
                         PROJECT_SHEET_DEFAULTS["project_lifecycle_years"])
            or PROJECT_SHEET_DEFAULTS["project_lifecycle_years"]
        )
        approx_pdfs = 365 * max(n_years, 1) * 3
        logger.warning(
            "plot_daily_scope='all' selected: ~%d daily PDFs will be "
            "generated across %d operating years (3 figures/day). "
            "Set plot_daily_scope=year1_only to keep iteration fast.",
            approx_pdfs, n_years,
        )

    # Upfront Monte-Carlo runtime estimate.  The default 30-seed ensemble
    # takes ~1 h on a 4-vCPU machine; compare-sources (x4) ~4 h.  Warn
    # before the solve so the user can interrupt.
    if unc_cfg["enabled"] and unc_cfg["n_seeds"] > 0:
        n_seeds = int(unc_cfg["n_seeds"])
        n_sources = 4 if unc_cfg["compare_sources"] else 1
        # ~126.5 s per full-year (35,040-step) seed; scale by the actual
        # step count so coarser cadences / shorter horizons estimate lower.
        per_seed_s = len(ts) / 35040.0 * 126.5
        total_s = per_seed_s * n_seeds * n_sources
        total_min = int(-(-total_s // 60))  # ceil to the nearest minute
        logger.warning(
            "[mc-runtime-estimate] rolling-horizon Monte-Carlo enabled: "
            "%d seed(s) x %d source set(s) ~ %d min projected wall-clock "
            "(~%.0f s/seed at %d steps).",
            n_seeds, n_sources, total_min, per_seed_s, len(ts),
        )

    with _tee_stdout_to_log(log_path):
        print(f"[run] mode={params.get('mode')}  "
              f"allow_bess_grid_charging={params.get('allow_bess_grid_charging')}  "
              f"slug={slug!r}")
        print(f"[io] output dir: {out_dir.resolve()}")

        # Perfect-foresight base run (also serves as the rolling-horizon
        # benchmark when --rolling-horizon is on).
        res, resolved_solver, res_full = run_scenario(
            params, ts, solver_name=args.solver,
            mip_gap=args.mip_gap, time_limit_seconds=args.time_limit,
            tee=args.tee, return_unrounded=True,
        )
        # Verify on the full-precision frame so the sum-based invariant_4
        # is not tripped by round(4) accumulation; KPIs / output use res.
        residuals = verify_energy_balance(
            res_full, params, raise_on_failure=False,
        )
        print(
            f"[verify] solver={resolved_solver}  residuals(kWh): "
            + ", ".join(f"{k}={v:.3g}" for k, v in residuals.items())
        )

        invariants = verify_dispatch_invariants(
            res_full, params, mode=resolve_mode(params),
        )
        print(
            "[invariants] "
            + ", ".join(f"{k}={v:.3g}" for k, v in invariants.items())
        )
        if args.strict:
            _check_strict_invariants(invariants)

        kpis = compute_kpis(res, params, verify_balance=False)
        # Post-solve unavailability derate.  Multiplies a
        # curated set of MWh / EUR keys by (1 - unavailability_pct/100).
        kpis = apply_unavailability_derate(
            kpis, float(params.get("unavailability_pct", 0.0) or 0.0),
        )
        _emit_bess_utilisation_audit(kpis, params)
        kpis_monthly = compute_monthly_kpis(res)

        # Optional rolling-horizon run (writes its KPIs alongside the
        # perfect-foresight benchmark for comparison).
        rolling_mc_df: pd.DataFrame | None = None
        rolling_compare_df: pd.DataFrame | None = None
        if unc_cfg["enabled"]:
            pf_profit_eur = float(kpis.get("profit_total_eur", 0.0))
            n_seeds = int(unc_cfg["n_seeds"])
            window_h = int(unc_cfg["window_hours"])
            commit_h = int(unc_cfg["commit_hours"])
            base_seed = int(unc_cfg["base_seed"])

            if unc_cfg["compare_sources"] and n_seeds > 0:
                print(
                    f"[rolling] compare-sources mode: 4 ensembles x {n_seeds} seeds "
                    f"(window={window_h}h, commit={commit_h}h, base_seed={base_seed})"
                )
                ensembles: list[pd.DataFrame] = []
                for src, en_dam, en_pv, en_load in _COMPARE_SOURCE_FLAGS:
                    sub = monte_carlo_rolling(
                        params, ts,
                        n_seeds=n_seeds,
                        base_seed=base_seed,
                        pf_profit_eur=pf_profit_eur,
                        sigma_dam=unc_cfg["sigma_dam"],
                        sigma_pv=unc_cfg["sigma_pv"],
                        sigma_load=unc_cfg["sigma_load"],
                        enable_dam=en_dam,
                        enable_pv=en_pv,
                        enable_load=en_load,
                        window_hours=window_h,
                        commit_hours=commit_h,
                        solver_name=args.solver,
                        mip_gap=args.mip_gap,
                        time_limit_seconds=args.time_limit,
                        tee=args.tee,
                    )
                    sub.insert(0, "source_set", src)
                    ensembles.append(sub)
                    p50 = float(sub["foresight_gap_pct"].quantile(0.50))
                    kpis[f"foresight_gap_pct_p50_{src}"] = float(round(p50, 4))
                rolling_compare_df = pd.concat(ensembles, ignore_index=True)
                kpis["mc_n_seeds"] = int(n_seeds)
                kpis["mc_window_hours"] = int(window_h)
                kpis["mc_commit_hours"] = int(commit_h)
            elif n_seeds > 0:
                print(
                    f"[rolling] running {n_seeds} MC seeds "
                    f"(window={window_h}h, commit={commit_h}h, "
                    f"base_seed={base_seed})"
                )
                rolling_mc_df = monte_carlo_rolling(
                    params, ts,
                    n_seeds=n_seeds,
                    base_seed=base_seed,
                    pf_profit_eur=pf_profit_eur,
                    sigma_dam=unc_cfg["sigma_dam"],
                    sigma_pv=unc_cfg["sigma_pv"],
                    sigma_load=unc_cfg["sigma_load"],
                    enable_dam=unc_cfg["enable_dam"],
                    enable_pv=unc_cfg["enable_pv"],
                    enable_load=unc_cfg["enable_load"],
                    window_hours=window_h,
                    commit_hours=commit_h,
                    solver_name=args.solver,
                    mip_gap=args.mip_gap,
                    time_limit_seconds=args.time_limit,
                    tee=args.tee,
                )
                gap_p = rolling_mc_df["foresight_gap_pct"].quantile([0.10, 0.50, 0.90])
                kpis["foresight_gap_pct_p10"] = float(round(gap_p.loc[0.10], 4))
                kpis["foresight_gap_pct_p50"] = float(round(gap_p.loc[0.50], 4))
                kpis["foresight_gap_pct_p90"] = float(round(gap_p.loc[0.90], 4))
                kpis["mc_n_seeds"] = int(n_seeds)
                kpis["mc_window_hours"] = int(window_h)
                kpis["mc_commit_hours"] = int(commit_h)
            else:
                # Single deterministic noiseless rolling horizon.
                _rh_full, rh_kpis = rolling_horizon_dispatch(
                    params, ts,
                    window_hours=window_h,
                    commit_hours=commit_h,
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
            Path(args.excel), params, ts, kpis, res,
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
            project_start_year=int(
                econ.get("project_start_year", PROJECT_SHEET_DEFAULTS["project_start_year"])
                or PROJECT_SHEET_DEFAULTS["project_start_year"]
            ),
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
            rolling_horizon_compare_mc=rolling_compare_df,
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
                rolling_compare_mc=rolling_compare_df,
                uncertainty_dir=layout["uncertainty_plots"],
                pf_profit_eur=float(kpis.get("profit_total_eur", 0.0)),
                year1_kpis=kpis,
                lifetime_yearly=bundle.get("lifetime_yearly"),
                capacities=bundle.get("capacities"),
                res_year1=res,
            )

        _generate_all_energy_plots(
            res, bundle.get("lifetime_df"), bundle.get("lifetime_yearly"),
            econ,
            layout["energy_plots"],
            mode=resolve_mode(params),
        )

        _dt_min = int(params.get("dt_minutes", 60) or 60)
        _commit_steps = max(
            1, round(int(unc_cfg["commit_hours"]) * 60 / _dt_min)
        )
        _generate_uncertainty_plots(
            ts, layout["uncertainty_plots"],
            diagnostics_enabled=bool(
                econ_pre.get("uncertainty_diagnostics_enabled", True)
            ),
            sigma_dam=unc_cfg["sigma_dam"],
            sigma_pv=unc_cfg["sigma_pv"],
            sigma_load=unc_cfg["sigma_load"],
            commit_steps=_commit_steps,
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
