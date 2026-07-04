"""Export a curated set of result figures as PNG for the README gallery.

GitHub cannot inline the PDF report figures, so this script renders two
representative full-year scenarios through the normal pipeline with the
figure format switched to PNG — the single switch
:func:`pvbess_opt.plotting.style.set_figure_format`, so no plotting code
is forked — and copies a small curated set into ``docs/assets/`` with
descriptive names.

Scenarios (both on the shipped ``inputs/input.xlsx``: PV 15 MWp,
BESS 15 MW / 60 MWh, 20-year horizon, 7 % discount, BESS grid
charging enabled):

* **Merchant + balancing** — DAM dispatch with FCR/aFRR/mFRR
  participation on; yields the revenue stack (with balancing products),
  the BESS-revenue waterfall, the LCOS and LCOE bands, the
  cumulative-cashflow / payback chart, the NPV waterfall, the SOH
  trajectory and the NPV tornado.

The foresight-gap distribution figure in the gallery is NOT produced
here: it comes from a rolling-horizon Monte Carlo run
(``pvbess inputs/input.xlsx --rolling-horizon --monte-carlo 8``),
whose ``rolling_horizon_distribution`` figure is exported to
``docs/assets/self_consumption_foresight_distribution.png``.
* **Self-consumption** — retail-settled load coverage, no balancing;
  yields the daily dispatch + SOC trace and the revenue stack.

Re-runnable; overwrites the gallery PNGs in place.  Run from anywhere::

    python scripts/export_readme_figures.py
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pvbess_opt.io import read_workbook, write_workbook  # noqa: E402
from pvbess_opt.pipeline import RunConfig, run  # noqa: E402
from pvbess_opt.plotting.style import set_figure_format  # noqa: E402

logger = logging.getLogger("export_readme_figures")

ASSETS_DIR = REPO_ROOT / "docs" / "assets"

# A representative summer day (15-min steps) for the daily dispatch trace —
# ~21 June, when both PV and the load shape are most illustrative.
_SUMMER_DAY_START_STEP = 96 * 171
_STEPS_PER_DAY = 96

# Curated figure picks: {glob under the run folder: destination name}.
# The financial figures come from full-year runs; the daily dispatch trace
# comes from a one-day run (see ``_build_workbook``).
# One shared per-mode figure set: BOTH gallery sections carry the SAME
# plot types, so a reader can compare the two business models figure by
# figure.  {glob under the run folder: gallery basename}.
_MODE_FIGURES = {
    "05_energy_plots/energy_sankey.png": "energy_flow.png",
    "04_financial_plots/revenue_stack_yearly_*.png": "revenue_stack.png",
    "04_financial_plots/bess_revenue_waterfall.png":
        "bess_revenue_waterfall.png",
    "04_financial_plots/monthly_cashflow_*.png": "monthly_cashflow.png",
    "04_financial_plots/cumulative_cashflow_with_payback_*.png":
        "cumulative_cashflow.png",
    "04_financial_plots/npv_waterfall_*.png": "npv_waterfall.png",
    "04_financial_plots/sensitivity_npv_tornado.png": "npv_tornado.png",
    "04_financial_plots/lcoe_summary.png": "lcoe_band.png",
    "04_financial_plots/lcos_summary.png": "lcos_band.png",
    "04_financial_plots/soh_trajectory.png": "soh_trajectory.png",
}
_DAILY_FIGURES = {
    "05_energy_plots/**/daily_combined_*with_soc_*.png":
        "daily_dispatch_soc.png",
}


def _prefixed(mapping: dict[str, str], prefix: str) -> dict[str, str]:
    return {glob: f"{prefix}_{name}" for glob, name in mapping.items()}


_MERCHANT_FIGURES = _prefixed(_MODE_FIGURES, "merchant")
_SELF_CONSUMPTION_FINANCIAL_FIGURES = _prefixed(
    _MODE_FIGURES, "self_consumption",
)
_SELF_CONSUMPTION_DAILY_FIGURES = _prefixed(_DAILY_FIGURES, "self_consumption")
_MERCHANT_DAILY_FIGURES = _prefixed(_DAILY_FIGURES, "merchant")


def _build_workbook(
    dst_dir: Path, *, mode: str, balancing: bool, one_day: bool,
    balancing_aggregator_fee_pct: float = 0.0,
) -> Path:
    """Materialise a single-scenario workbook from the shipped input.

    ``one_day`` slices the timeseries to a single representative summer day
    and keeps the daily energy plot (for the dispatch + SOC trace);
    otherwise the full year runs with the energy-plot fan-out switched off
    (only the financial figures are needed, and the per-day plots over a
    full year dominate the wall-clock).

    ``balancing_aggregator_fee_pct`` sets the optional BSP / route-to-market
    fee so the gallery's balancing scenario visibly carries the deduction
    (it defaults to 0 on the shipped workbook); the merchant+balancing
    figures use a representative non-zero value to show the new line.
    """
    typed = read_workbook(REPO_ROOT / "inputs" / "input.xlsx")
    typed["project"]["mode"] = mode
    # Grid charging on in every gallery scenario so the figures show the
    # complete feature set: grid->BESS flows in the dispatch trace and
    # the Grid-charging cost bar in the revenue stacks.
    typed["project"]["allow_bess_grid_charging"] = True
    typed["balancing"]["balancing_enabled"] = balancing
    typed["economics"]["balancing_aggregator_fee_pct_revenue"] = (
        balancing_aggregator_fee_pct
    )
    typed["ppa"]["ppa_enabled"] = False
    typed["simulation"]["uncertainty_enabled"] = False
    # The gallery uses none of the uncertainty diagnostic plots, which
    # render over the full series and dominate the wall-clock; skip them.
    typed["simulation"]["uncertainty_diagnostics_enabled"] = False
    typed["simulation"]["plot_monthly_scope"] = "none"
    typed["simulation"]["plot_yearly_scope"] = "none"
    if one_day:
        start = _SUMMER_DAY_START_STEP
        typed["ts"] = typed["ts"].iloc[
            start:start + _STEPS_PER_DAY
        ].reset_index(drop=True)
        typed["simulation"]["plot_daily_scope"] = "year1_only"
    else:
        typed["simulation"]["plot_daily_scope"] = "none"
    stem = f"{mode}{'_day' if one_day else ''}"
    out = dst_dir / f"{stem}.xlsx"
    write_workbook(typed, out)
    return out


def _run(dst_dir: Path, *, mode: str, balancing: bool, one_day: bool,
         mip_gap: float, time_limit: int,
         balancing_aggregator_fee_pct: float = 0.0) -> Path:
    wb = _build_workbook(
        dst_dir, mode=mode, balancing=balancing, one_day=one_day,
        balancing_aggregator_fee_pct=balancing_aggregator_fee_pct,
    )
    run(RunConfig(
        excel=wb, solver="highs", outdir=dst_dir / "out",
        mip_gap=mip_gap, time_limit=time_limit,
    ))
    runs = sorted((dst_dir / "out").glob(f"{wb.stem}_*"))
    if not runs:
        raise RuntimeError(f"no run folder produced for {wb.stem!r}")
    return runs[-1]


def _curate(run_dir: Path, mapping: dict[str, str]) -> list[Path]:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for pattern, dest_name in mapping.items():
        matches = sorted(run_dir.glob(pattern))
        if not matches:
            logger.warning("no figure matched %r under %s", pattern, run_dir)
            continue
        dest = ASSETS_DIR / dest_name
        shutil.copyfile(matches[0], dest)
        written.append(dest)
        logger.info("wrote %s", dest.relative_to(REPO_ROOT))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mip-gap", type=float, default=0.01)
    parser.add_argument("--time-limit", type=int, default=1800)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    written: list[Path] = []
    set_figure_format("png")
    try:
        with tempfile.TemporaryDirectory(prefix="readme_figs_") as tmp:
            tmp_dir = Path(tmp)
            logger.info("merchant + balancing — full-year financials ...")
            merchant_run = _run(
                tmp_dir / "merchant", mode="merchant", balancing=True,
                one_day=False, mip_gap=args.mip_gap, time_limit=args.time_limit,
                # Representative BSP / route-to-market fee so the gallery's
                # revenue stack and waterfall show the deduction line.
                balancing_aggregator_fee_pct=10.0,
            )
            written += _curate(merchant_run, _MERCHANT_FIGURES)

            logger.info("self-consumption — full-year financials ...")
            sc_run = _run(
                tmp_dir / "sc", mode="self_consumption", balancing=False,
                one_day=False, mip_gap=args.mip_gap, time_limit=args.time_limit,
            )
            written += _curate(sc_run, _SELF_CONSUMPTION_FINANCIAL_FIGURES)

            logger.info("self-consumption — representative-day dispatch ...")
            sc_day_run = _run(
                tmp_dir / "sc_day", mode="self_consumption", balancing=False,
                one_day=True, mip_gap=args.mip_gap, time_limit=args.time_limit,
            )
            written += _curate(sc_day_run, _SELF_CONSUMPTION_DAILY_FIGURES)

            logger.info("merchant — representative-day dispatch ...")
            merchant_day_run = _run(
                tmp_dir / "merchant_day", mode="merchant", balancing=False,
                one_day=True, mip_gap=args.mip_gap,
                time_limit=args.time_limit,
            )
            written += _curate(merchant_day_run, _MERCHANT_DAILY_FIGURES)
    finally:
        set_figure_format("pdf")

    logger.info("exported %d gallery figures to %s", len(written), ASSETS_DIR)
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
