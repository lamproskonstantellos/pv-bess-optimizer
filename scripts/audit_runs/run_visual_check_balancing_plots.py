#!/usr/bin/env python
"""Visual sanity sweep for the balancing / BESS-revenue plot family.

Renders every plot in :mod:`pvbess_opt.plotting.bess_revenue` and
:mod:`pvbess_opt.plotting.balancing` for the canonical
self_consumption x hybrid x balancing-ON case and bundles them into a
single PDF under
``scripts/audit_runs/results/visual_check_balancing_plots.pdf``.

Drives the house-style conventions check:

* Month axis reads ``MM-YYYY`` (not ``Jan`` … ``Dec``).
* Currency axes route through ``euro_axis_formatter`` (no ``1e6`` ticks).
* Titles carry the ``(scenario; project_mode)`` prefix.
* ``apply_universal_margins`` ran last so annotations stay inside the frame.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.audit_runs._common import (  # noqa: E402
    load_canonical_workbook,
    override_config,
    run_pipeline,
)

MODE = "self_consumption"
ASSET = "hybrid"
BALANCING = True


def main() -> int:
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    from pvbess_opt.kpis import compute_kpis
    from pvbess_opt.plotting import balancing as pb
    from pvbess_opt.plotting import bess_revenue as br
    from pvbess_opt.plotting import style
    from pvbess_opt.rolling_horizon import monte_carlo_balancing

    base_params, ts = load_canonical_workbook()
    params = override_config(
        base_params, mode=MODE, asset_config=ASSET, balancing_enabled=BALANCING,
    )
    result = run_pipeline(
        params, ts, mc_scenarios=50, subsample_steps=672,
    )
    res = result["_dispatch_frame"]
    kpis = compute_kpis(res, params, verify_balance=False)
    mc = monte_carlo_balancing(res, params, n_scenarios=50, seed=1729)

    style.apply_ieee_style()
    style.set_show_titles(True)
    style.set_scenario_label(MODE)
    style.set_project_mode_label("Hybrid PV+BESS")

    captured: list = []
    original_save = style.save_figure

    def _capture(figpath: Path) -> Path:
        captured.append(plt.gcf())
        return Path(figpath).with_suffix(".pdf")

    style.save_figure = _capture  # type: ignore[assignment]
    pb.save_figure = _capture     # type: ignore[attr-defined]
    br.save_figure = _capture     # type: ignore[attr-defined]
    try:
        scratch = (
            Path(__file__).resolve().parent / "results"
            / "_visual_check_scratch"
        )
        scratch.mkdir(parents=True, exist_ok=True)
        econ = {"currency_format": "auto"}
        br.plot_bess_revenue_waterfall(
            kpis, scratch / "waterfall.pdf", econ=econ,
        )
        br.plot_bess_capacity_vs_activation_split(
            kpis, scratch / "cap_vs_act.pdf", econ=econ,
        )
        br.plot_bess_revenue_by_month(
            res, kpis, scratch / "by_month.pdf", econ=econ,
        )
        pb.plot_balancing_reservation_profile(
            res, scratch / "reservation_profile.pdf",
        )
        pb.plot_balancing_mc_distribution(
            mc, scratch / "mc_distribution.pdf", econ=econ,
        )
    finally:
        style.save_figure = original_save  # type: ignore[assignment]
        pb.save_figure = original_save     # type: ignore[attr-defined]
        br.save_figure = original_save     # type: ignore[attr-defined]

    out_pdf = (
        Path(__file__).resolve().parent / "results"
        / "visual_check_balancing_plots.pdf"
    )
    with PdfPages(out_pdf) as combined:
        for fig in captured:
            combined.savefig(fig, bbox_inches="tight")
            plt.close(fig)
    print(f"Visual check PDF: {out_pdf}  ({len(captured)} pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
