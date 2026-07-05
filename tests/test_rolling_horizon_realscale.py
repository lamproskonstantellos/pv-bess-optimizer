"""Real-scale CI guard for the rolling-horizon path.

Exercises the full 35,040-row default workbook through a *single seed*
of :func:`rolling_horizon_dispatch` and asserts:

1. Profit returned is finite and within a wide sanity envelope.
2. Wall-clock stays under ``TIME_BUDGET_SEC``. The budget is set well
   above the audit-machine measurement (~89 s for one seed at
   ``window_hours=48`` / ``commit_hours=24``) so the guard fires only
   on a real regression (e.g. >2x slowdown), not on noisy CI runners.

This is the only test in the suite that drives the real workbook
through the rolling-horizon path. Keep it to ONE seed; the Monte Carlo
ensemble has its own deterministic reproducibility test on the short
fixture.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import pytest

from pvbess_opt.io import read_inputs
from pvbess_opt.rolling_horizon import rolling_horizon_dispatch

WORKBOOK = Path(__file__).resolve().parents[1] / "inputs" / "input.xlsx"

# Audit-machine single-seed wall-clock was ~89 s. Budget 5x = 445 s
# triggers on a real regression but tolerates a slow CI runner.
TIME_BUDGET_SEC = 445.0


@pytest.mark.slow
@pytest.mark.skipif(not WORKBOOK.exists(), reason="default workbook not present")
def test_rolling_horizon_realscale_single_seed_under_budget() -> None:
    params, ts = read_inputs(str(WORKBOOK))

    t0 = time.perf_counter()
    full, kpis = rolling_horizon_dispatch(
        params,
        ts,
        window_hours=48,
        commit_hours=24,
        forecast_seed=42,
    )
    wall = time.perf_counter() - t0

    profit = float(kpis.get("profit_total_eur", float("nan")))

    # Sanity: profit is finite and roughly in the right order of magnitude
    # for the shipped 15 MW PV + 15 MW / 30 MWh BESS workbook (the
    # deterministic benchmark is ~2.5 M EUR at this config); allow a wide
    # envelope so we catch sign flips or crashes, not algorithmic
    # refinements.
    assert math.isfinite(profit), f"profit not finite: {profit}"
    assert 1.0e6 < profit < 5.0e6, f"profit out of envelope: {profit:.0f} EUR"

    # Time budget: catches a 3-5x per-window regression.
    assert wall < TIME_BUDGET_SEC, (
        f"rolling_horizon_dispatch wall-clock {wall:.1f}s "
        f"exceeded budget {TIME_BUDGET_SEC:.0f}s — possible perf regression"
    )

    # Frame shape sanity.
    assert len(full) == len(ts), f"expected {len(ts)} rows, got {len(full)}"
