#!/usr/bin/env python
"""Audit driver: merchant x hybrid x balancing-ON.

Runs the full pipeline for this combination and writes the JSON
evidence file under ``scripts/audit_runs/results/``.  ``--with-pdf``
additionally assembles a combined PDF of the BESS-revenue and
balancing plots for visual inspection alongside the JSON.
"""

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

MODE = "merchant"
ASSET = "hybrid"
BALANCING = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mc-scenarios", type=int, default=25,
        help="Number of Monte Carlo realisations for balancing revenue.",
    )
    parser.add_argument(
        "--subsample-steps", type=int, default=672,
        help=(
            "672 = one week at 15-min steps; full-year solve overruns the "
            "5-min budget (audit-prompt budget fallback)."
        ),
    )
    parser.add_argument(
        "--with-pdf", action="store_true",
        help="Also render the combined audit-evidence PDF.",
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

    if args.with_pdf:
        _render_combined_pdf(params, result["_dispatch_frame"])

    if not all_invariants_pass(result["invariants"]):
        return 1
    if check_no_nonfinite(result["kpis"]):
        return 2
    return 0


def _render_combined_pdf(params: dict, res) -> None:
    """Render the audit-evidence PDF for the primary balancing-on combo.

    Each plotting helper in this project saves+closes its own figure;
    we monkey-patch ``save_figure`` for the duration of the call so
    every figure is captured into a single ``PdfPages`` bundle instead.
    """
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    from pvbess_opt.kpis import compute_kpis
    from pvbess_opt.plotting import balancing as pb
    from pvbess_opt.plotting import bess_revenue as br
    from pvbess_opt.plotting import style
    from pvbess_opt.rolling_horizon import monte_carlo_balancing

    style.apply_ieee_style()

    kpis = compute_kpis(res, params, verify_balance=False)
    mc = monte_carlo_balancing(res, params, n_scenarios=25, seed=1729)

    out_pdf = (
        Path(__file__).resolve().parent / "results" / "merchant_hybrid_on.pdf"
    )

    captured: list = []
    original_save = style.save_figure

    def _capture(figpath: Path) -> Path:
        fig = plt.gcf()
        captured.append(fig)
        # Return a synthetic path; the helpers do not consume the value.
        return Path(figpath).with_suffix(".pdf")

    style.save_figure = _capture  # type: ignore[assignment]
    # ``balancing.py`` and ``bess_revenue.py`` imported ``save_figure``
    # by name, so we also need to patch their module-local references.
    pb.save_figure = _capture  # type: ignore[attr-defined]
    br.save_figure = _capture  # type: ignore[attr-defined]
    try:
        scratch = (
            Path(__file__).resolve().parent / "results" / "_pdf_scratch"
        )
        scratch.mkdir(parents=True, exist_ok=True)
        br.plot_bess_revenue_waterfall(kpis, scratch / "waterfall.pdf")
        br.plot_bess_capacity_vs_activation_split(
            kpis, scratch / "cap_vs_act.pdf",
        )
        br.plot_bess_revenue_by_month(
            res, kpis, scratch / "by_month.pdf",
        )
        pb.plot_balancing_reservation_profile(
            res, scratch / "reservation_profile.pdf",
        )
        pb.plot_balancing_mc_distribution(
            mc, scratch / "mc_distribution.pdf",
        )
    finally:
        style.save_figure = original_save  # type: ignore[assignment]
        pb.save_figure = original_save  # type: ignore[attr-defined]
        br.save_figure = original_save  # type: ignore[attr-defined]

    with PdfPages(out_pdf) as combined:
        for fig in captured:
            combined.savefig(fig, bbox_inches="tight")
            plt.close(fig)
    print(f"PDF written: {out_pdf}  ({len(captured)} pages)")


if __name__ == "__main__":
    raise SystemExit(main())
