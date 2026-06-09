"""Run pipeline for the PV + BESS dispatch optimizer.

:func:`run` solves, post-processes, archives and plots a single scenario
and returns a :class:`Results`.  It is the importable, testable entry
point; :mod:`pvbess_opt.cli` is a thin wrapper that parses CLI arguments
into a :class:`RunConfig` and calls :func:`run`.

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
"""

from __future__ import annotations

import logging
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from pvbess_opt.availability import apply_unavailability_derate, availability_factor
from pvbess_opt.degradation import build_degradation_report
from pvbess_opt.economics import (
    build_debt_schedule,
    build_yearly_cashflow,
    compute_financial_kpis,
    derive_asset_capacities,
    derive_monthly_cashflow,
    read_economic_params,
)
from pvbess_opt.emissions import build_emissions_report
from pvbess_opt.io import (
    PROJECT_SHEET_DEFAULTS,
    copy_input_snapshot,
    make_run_layout,
    read_inputs,
    write_assumptions_summary,
    write_dispatch_artifacts,
    write_results_workbook,
)
from pvbess_opt.io_read import is_structured_config, materialize_to_xlsx
from pvbess_opt.kpis import (
    ENERGY_TOLERANCE,
    compute_kpis,
    compute_monthly_kpis,
    verify_energy_balance,
)
from pvbess_opt.lifetime import aggregate_lifetime_to_yearly, build_lifetime_dispatch
from pvbess_opt.modes import resolve_mode
from pvbess_opt.optimization import (
    BALANCING_INVARIANT_KEYS,
    run_scenario,
    verify_dispatch_invariants,
)
from pvbess_opt.plotting import (
    apply_ieee_style,
    plot_bess_capacity_vs_activation_split,
    plot_bess_revenue_by_month,
    plot_bess_revenue_waterfall,
    plot_cfe_duration_curve,
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
    plot_energy_sankey,
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
    plot_soh_trajectory,
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

logger = logging.getLogger("pvbess_opt.pipeline")


@dataclass
class RunConfig:
    """Parameters for a single pipeline run (decoupled from argparse)."""

    excel: Path
    solver: str = "highs"
    outdir: Path = field(default_factory=lambda: Path("results"))
    mode: str | None = None
    strict: bool = False
    mip_gap: float = 0.001
    time_limit: int = 1800
    tee: bool = False
    rolling_horizon: bool = False
    window_hours: int | None = None
    commit_hours: int | None = None
    monte_carlo: int | None = None
    seed: int = 42
    compare_uncertainty_sources: bool = False


@dataclass
class Results:
    """Outputs of a single pipeline run."""

    out_dir: Path
    kpis: dict[str, Any]
    financial_kpis: dict[str, float] | None = None
    yearly_cashflow: pd.DataFrame | None = None
    lifetime_yearly: pd.DataFrame | None = None
    sensitivity: pd.DataFrame | None = None


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
    sys.stdout = tee_out
    sys.stderr = tee_out
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
            plot_uncertainty_coverage_by_horizon(
                ts, out_dir / "coverage_by_horizon.pdf",
                commit_steps=commit_steps,
                sigma_dam=sigma_dam, sigma_pv=sigma_pv, sigma_load=sigma_load,
            )
            plot_uncertainty_pit_histogram(
                ts, out_dir / "pit_histogram.pdf",
                sigma_dam=sigma_dam, sigma_pv=sigma_pv, sigma_load=sigma_load,
            )
            plot_uncertainty_crps_timeline(
                ts, out_dir / "crps_timeline.pdf",
                sigma_dam=sigma_dam, sigma_pv=sigma_pv, sigma_load=sigma_load,
            )
            plot_uncertainty_residual_qq(
                ts, out_dir / "residual_qq.pdf",
                sigma_dam=sigma_dam, sigma_pv=sigma_pv, sigma_load=sigma_load,
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
            "export_total_mwh", "revenue_eur_dam_retail",
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
        "debt_schedule": build_debt_schedule(yearly_cf, econ),
    }


def _build_degradation_report(
    res: pd.DataFrame, params: dict[str, Any], econ: dict[str, Any],
    kpis: dict[str, Any] | None = None,
) -> pd.DataFrame | None:
    """Post-hoc SOH / capacity-fade report from the SOC trace.

    Returns None when there is no BESS or no SOC trace.  This is a
    diagnostic — it does not feed back into the NPV (the replacement CAPEX
    in the finance layer already charges degradation), so the wear cost is
    never double-counted.  The SOH curve uses the same calendar-plus-cycle
    fade model as the finance layer, fed the same Year-1 discharge throughput
    (``bess_total_discharge_mwh`` from the derated KPI dict), so it agrees
    with ``bess_factor`` / the cashflow.
    """
    if float(params.get("bess_capacity_kwh", 0.0) or 0.0) <= 0.0:
        return None
    if "soc_kwh" not in res.columns:
        return None
    year1_discharge_mwh = (
        float(kpis.get("bess_total_discharge_mwh", 0.0) or 0.0)
        if kpis is not None else None
    )
    return build_degradation_report(
        res["soc_kwh"],
        capacity_kwh=float(params.get("bess_capacity_kwh", 0.0) or 0.0),
        soc_min_frac=float(params.get("soc_min_frac", 0.0) or 0.0),
        soc_max_frac=float(params.get("soc_max_frac", 1.0) or 1.0),
        degradation_pct_per_cycle=float(
            econ.get("bess_degradation_pct_per_cycle", 0.0) or 0.0
        ),
        degradation_annual_pct=float(
            econ.get("bess_degradation_annual_pct", 0.0) or 0.0
        ),
        year1_discharge_mwh=year1_discharge_mwh,
        project_years=int(
            econ.get("project_lifecycle_years",
                     PROJECT_SHEET_DEFAULTS["project_lifecycle_years"])
            or PROJECT_SHEET_DEFAULTS["project_lifecycle_years"]
        ),
        start_year=int(
            econ.get("project_start_year",
                     PROJECT_SHEET_DEFAULTS["project_start_year"])
            or PROJECT_SHEET_DEFAULTS["project_start_year"]
        ),
        replacement_year=int(econ.get("bess_replacement_year", 0) or 0),
    )


def _build_emissions_report(
    res: pd.DataFrame, econ: dict[str, Any],
) -> pd.DataFrame | None:
    """Post-hoc grid-emissions / 24/7 CFE report from the solved dispatch.

    Returns None unless a grid carbon intensity is configured — a scalar
    ``grid_co2_intensity_kg_per_mwh`` or a per-step ``grid_co2_kg_per_mwh``
    dispatch column.  This is a diagnostic only: it never feeds back into the
    dispatch or the NPV, so an unconfigured run is unchanged.
    """
    scalar_ci = float(econ.get("grid_co2_intensity_kg_per_mwh", 0.0) or 0.0)
    has_series = "grid_co2_kg_per_mwh" in res.columns
    if scalar_ci <= 0.0 and not has_series:
        return None
    return build_emissions_report(
        res,
        grid_ci_kg_per_mwh=scalar_ci,
        grid_ci_annual_decline_pct=float(
            econ.get("grid_co2_annual_decline_pct", 0.0) or 0.0
        ),
        project_years=int(
            econ.get("project_lifecycle_years",
                     PROJECT_SHEET_DEFAULTS["project_lifecycle_years"])
            or PROJECT_SHEET_DEFAULTS["project_lifecycle_years"]
        ),
        start_year=int(
            econ.get("project_start_year",
                     PROJECT_SHEET_DEFAULTS["project_start_year"])
            or PROJECT_SHEET_DEFAULTS["project_start_year"]
        ),
    )


def _scenario_slug(params: dict[str, Any]) -> str:
    """Return the ``<mode>[_grid_ch]`` folder slug."""
    mode = resolve_mode(params)
    suffix = "_grid_ch" if params.get("allow_bess_grid_charging") else ""
    return f"{mode}{suffix}"


def _check_strict_invariants(invariants: dict[str, float]) -> None:
    tol = ENERGY_TOLERANCE
    # Invariant 6 is an integer count and piggybacks on the same tol;
    # the smallest non-zero count is 1, which trivially exceeds tol=1e-3.
    # Invariant B1 reports a percent excess; allow a 0.5 % tolerance to
    # stay aligned with the loader's epsilon (_validate_balancing_config).
    bal_b1 = "invariant_b1_capacity_share_sum_pct_excess"
    bal_b2 = "invariant_b2_reservation_share_cap_excess_kw"
    skip_keys = {"invariant_5_no_sim_grid_io_max_product_kwh2", bal_b1, bal_b2}
    offenders = {
        k: v for k, v in invariants.items()
        if v > tol and k not in skip_keys
    }
    sim_io = invariants["invariant_5_no_sim_grid_io_max_product_kwh2"]
    if sim_io > tol ** 2:
        offenders["invariant_5_no_sim_grid_io_max_product_kwh2"] = sim_io
    # Balancing-specific tolerances.
    if invariants.get(bal_b1, 0.0) > 0.5:
        offenders[bal_b1] = float(invariants[bal_b1])
    if invariants.get(bal_b2, 0.0) > tol:
        offenders[bal_b2] = float(invariants[bal_b2])
    # Sanity guard against API drift — the verifier must always emit
    # every balancing-invariant key, even when the block did not fire.
    missing = [k for k in BALANCING_INVARIANT_KEYS if k not in invariants]
    if missing:
        raise AssertionError(
            "verify_dispatch_invariants is missing balancing-invariant "
            f"keys: {missing}"
        )
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
    config: RunConfig, econ: dict[str, Any], mode: str,
) -> dict[str, Any]:
    """Merge CLI overrides on top of the workbook ``# uncertainty`` group."""
    enabled = bool(config.rolling_horizon) or bool(econ.get("uncertainty_enabled", False))
    compare = (
        bool(config.compare_uncertainty_sources)
        or bool(econ.get("uncertainty_compare_sources", False))
    )
    n_seeds = (
        int(config.monte_carlo) if config.monte_carlo is not None
        else int(econ.get("uncertainty_n_seeds", 30) or 30)
    )
    window = (
        int(config.window_hours) if config.window_hours is not None
        else int(econ.get("uncertainty_window_hours", 48) or 48)
    )
    commit = (
        int(config.commit_hours) if config.commit_hours is not None
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
        "base_seed": int(config.seed),
    }


def _run_one(
    params: dict[str, Any],
    ts: pd.DataFrame,
    config: RunConfig,
    base_name: str,
    timestamp: str,
) -> Results:
    """Solve, post-process, archive and plot a single scenario."""
    slug = _scenario_slug(params)
    set_scenario_label(slug)
    set_project_mode_label(_project_mode_label(params))

    folder = f"{base_name}_{slug}_{timestamp}"
    out_dir = Path(config.outdir) / folder
    layout = make_run_layout(out_dir)
    log_path = layout["summary"] / "run_log.txt"

    # Load the economic group up front so the uncertainty config can be
    # resolved before the perfect-foresight solve produces its KPIs.
    econ_pre = read_economic_params(Path(config.excel))
    unc_cfg = _resolve_uncertainty_config(
        config, econ_pre, mode=resolve_mode(params),
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

    # Upfront perfect-foresight + balancing solve estimate. Reference
    # points from full-scale reference runs: 35 040 steps + balancing +
    # self_consumption + bess-only ~565 s; merchant + hybrid + balancing
    # on 672 steps ~1.2 s. Scale roughly linearly with n_steps.
    if bool(params.get("balancing", {}).get("balancing_enabled", False)):
        ref_steps = 35040.0
        ref_seconds = 565.0
        est_s = max(1.0, len(ts) / ref_steps * ref_seconds)
        est_min = est_s / 60.0
        logger.warning(
            "[balancing-runtime-estimate] balancing MILP enabled: %d steps "
            "=> projected solve wall-clock ~%.1f s (~%.1f min). The MILP "
            "runs silently by default; pass --tee for live HiGHS chatter, "
            "or grep for '[milp-solve]' in the run log for the start / "
            "done markers.",
            len(ts), est_s, est_min,
        )

    with _tee_stdout_to_log(log_path):
        print(f"[run] mode={params.get('mode')}  "
              f"allow_bess_grid_charging={params.get('allow_bess_grid_charging')}  "
              f"slug={slug!r}")
        print(f"[io] output dir: {out_dir.resolve()}")

        # Perfect-foresight base run (also serves as the rolling-horizon
        # benchmark when --rolling-horizon is on).
        res, resolved_solver, res_full = run_scenario(
            params, ts, solver_name=config.solver,
            mip_gap=config.mip_gap, time_limit_seconds=config.time_limit,
            tee=config.tee, return_unrounded=True,
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
        if config.strict:
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
                        solver_name=config.solver,
                        mip_gap=config.mip_gap,
                        time_limit_seconds=config.time_limit,
                        tee=config.tee,
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
                    solver_name=config.solver,
                    mip_gap=config.mip_gap,
                    time_limit_seconds=config.time_limit,
                    tee=config.tee,
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
                    solver_name=config.solver,
                    mip_gap=config.mip_gap,
                    time_limit_seconds=config.time_limit,
                    tee=config.tee,
                )
                kpis["rolling_horizon_profit_eur"] = float(
                    rh_kpis.get("profit_total_eur", 0.0)
                )

        bundle = _build_financials(
            Path(config.excel), params, ts, kpis, res,
        )
        econ = bundle["econ"]

        snap = copy_input_snapshot(
            Path(config.excel), layout["inputs"], "snapshot",
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

        degradation_df = _build_degradation_report(res, params, econ, kpis)
        emissions_df = _build_emissions_report(res, econ)

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
            degradation=degradation_df,
            debt_schedule=bundle.get("debt_schedule"),
            emissions=emissions_df,
        )
        if degradation_df is not None and not degradation_df.empty:
            plot_soh_trajectory(
                degradation_df, layout["financial_plots"] / "soh_trajectory.pdf",
            )
        if emissions_df is not None and not emissions_df.empty:
            try:
                plot_energy_sankey(
                    res, layout["financial_plots"] / "energy_sankey.pdf",
                )
                plot_cfe_duration_curve(
                    res, layout["financial_plots"] / "cfe_duration_curve.pdf",
                )
            except Exception:
                logger.exception("Emissions / CFE plot generation failed")

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
    return Results(
        out_dir=out_dir,
        kpis=kpis,
        financial_kpis=bundle.get("fin_kpis"),
        yearly_cashflow=bundle.get("yearly_cf"),
        lifetime_yearly=bundle.get("lifetime_yearly"),
        sensitivity=bundle.get("sensitivity"),
    )


# ---------------------------------------------------------------------------
# Programmatic entry point
# ---------------------------------------------------------------------------


def run(config: RunConfig) -> Results:
    """Read the input, solve, post-process, archive and plot one run.

    Accepts an Excel workbook or a structured (YAML/JSON) config; a
    structured config is materialized to an equivalent workbook so both
    inputs flow through the same read path and produce identical results.
    """
    src = Path(config.excel)
    base_name = src.stem
    if is_structured_config(src):
        tmp_dir = Path(tempfile.mkdtemp(prefix="pvbess_cfg_"))
        run_config = replace(config, excel=materialize_to_xlsx(src, tmp_dir))
    else:
        run_config = config
    params, ts = read_inputs(run_config.excel)
    apply_ieee_style()
    set_show_titles(params.get("show_titles", False))
    if run_config.mode is not None:
        params["mode"] = run_config.mode
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return _run_one(params, ts, run_config, base_name, timestamp)
