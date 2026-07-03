"""Scope alignment between the MC ensemble and the perfect-foresight benchmark.

Locks the two halves of the foresight-comparison contract
(``pvbess_opt/conventions.md`` — "Perfect-foresight benchmark and the MC
ensemble share one scope"):

1. Rolling-horizon KPIs carry the same unavailability derate as the
   pipeline's headline Year-1 KPIs, so ``foresight_gap_pct`` is
   derate-invariant.
2. When ``terminal_soc_equal`` is true, the stitched rolling-horizon
   dispatch honours the benchmark's year-close SOC condition, so with
   zero forecast noise the gap collapses to ~0 and with noise no seed
   beats the benchmark beyond solver tolerance.

Plus the legend contract of the distribution plot: entries carry the
bare marker names (P10 / P50 / P90 / Perfect foresight) with no euro
values, so the figure drops into a paper unchanged.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pytest

from pvbess_opt.availability import apply_unavailability_derate
from pvbess_opt.kpis import compute_kpis
from pvbess_opt.optimization import run_scenario, verify_dispatch_invariants
from pvbess_opt.rolling_horizon import monte_carlo_rolling, rolling_horizon_dispatch

# Tight gap so solver slack cannot blur the scope assertions.
_TIGHT = {"solver_name": "highs", "mip_gap": 1.0e-6, "time_limit_seconds": 60}


def _pf_benchmark(params: dict, ts: pd.DataFrame) -> float:
    """Perfect-foresight profit with the pipeline's headline scope."""
    res, _solver = run_scenario(params, ts, **_TIGHT)
    kpis = compute_kpis(res, params, verify_balance=False)
    kpis = apply_unavailability_derate(
        kpis, float(params.get("unavailability_pct", 0.0) or 0.0),
    )
    return float(kpis["profit_total_eur"])


def test_sigma_zero_gap_collapses_to_zero(short_params, short_ts):
    """Zero forecast noise => RH profit == PF profit within solver slack.

    The shipped regression compared a RAW seed profit against the
    DERATED benchmark, which sent the gap ~1 pp negative per percent of
    unavailability; the un-closed terminal SOC of the last window added
    a further spurious RH advantage.  Both are fixed; the gap must now
    sit at ~0 (tolerance covers two 1e-6-gap solves + round(2) KPIs).
    """
    params = dict(short_params)
    params["unavailability_pct"] = 5.0
    params["terminal_soc_equal"] = True

    pf = _pf_benchmark(params, short_ts)
    mc = monte_carlo_rolling(
        params, short_ts,
        n_seeds=1, base_seed=11,
        pf_profit_eur=pf,
        sigma_dam=0.0, sigma_pv=0.0, sigma_load=0.0,
        window_hours=48, commit_hours=24,
        **_TIGHT,
    )
    gap = float(mc["foresight_gap_pct"].iloc[0])
    assert abs(gap) < 0.05, f"sigma->0 foresight gap should be ~0, got {gap:+.4f} %"


def test_rolling_kpis_carry_headline_derate(short_params, short_ts):
    """RH KPI scope == headline KPI scope: 10 % unavailability scales the
    realised profit by exactly 0.9 relative to the pristine run."""
    pristine = dict(short_params)
    pristine["unavailability_pct"] = 0.0
    derated = dict(pristine)
    derated["unavailability_pct"] = 10.0

    _full0, k0 = rolling_horizon_dispatch(
        pristine, short_ts, window_hours=24, commit_hours=12,
        forecast_seed=None, **_TIGHT,
    )
    _full1, k1 = rolling_horizon_dispatch(
        derated, short_ts, window_hours=24, commit_hours=12,
        forecast_seed=None, **_TIGHT,
    )
    assert k0["availability_factor"] == pytest.approx(1.0)
    assert k1["availability_factor"] == pytest.approx(0.9)
    assert float(k1["profit_total_eur"]) == pytest.approx(
        0.9 * float(k0["profit_total_eur"]), rel=1e-6, abs=0.05,
    )
    assert float(k1["pv_generation_mwh"]) == pytest.approx(
        0.9 * float(k0["pv_generation_mwh"]), rel=1e-6, abs=1e-4,
    )


def test_rolling_dispatch_closes_the_year_cycle(short_params, short_ts):
    """terminal_soc_equal=True => the stitched frame ends at the
    year-initial SOC (invariant 8 on the committed dispatch)."""
    params = dict(short_params)
    params["terminal_soc_equal"] = True
    full, _kpis = rolling_horizon_dispatch(
        params, short_ts, window_hours=24, commit_hours=12,
        forecast_seed=3, sigma_dam=0.1, sigma_pv=0.1, sigma_load=0.05,
        **_TIGHT,
    )
    inv = verify_dispatch_invariants(full, params, mode=params["mode"])
    assert inv["invariant_8_soc_closed_cycle_kwh"] < 1.0e-3, (
        "stitched RH dispatch must honour the benchmark's closed cycle, "
        f"got residual {inv['invariant_8_soc_closed_cycle_kwh']:.6g} kWh"
    )


def test_no_seed_beats_pf_beyond_solver_slack(short_params, short_ts):
    """With noise on, every seed's realised profit <= PF + mip_gap slack
    and the P50 gap is non-negative up to that slack."""
    params = dict(short_params)
    params["unavailability_pct"] = 2.0
    params["terminal_soc_equal"] = True

    pf = _pf_benchmark(params, short_ts)
    mc = monte_carlo_rolling(
        params, short_ts,
        n_seeds=3, base_seed=7,
        pf_profit_eur=pf,
        window_hours=24, commit_hours=12,
        **_TIGHT,
    )
    slack = 2.0e-3 * abs(pf) + 1.0  # two solves at mip_gap + KPI rounding
    assert float(mc["profit_total_eur"].max()) <= pf + slack, (
        f"a seed beat the PF benchmark: max={mc['profit_total_eur'].max():.2f} "
        f"vs pf={pf:.2f}"
    )
    assert float(mc["foresight_gap_pct"].quantile(0.5)) >= -0.2


def test_distribution_plot_legend_carries_bare_names(tmp_path):
    """Legend entries are the bare marker names (P10 / P50 / P90 /
    Perfect foresight): no euro values, no '=' annotations.  The
    quoted numbers live in SUMMARY.md; the axis carries the units."""
    from pvbess_opt.plotting import uncertainty as unc_mod

    rng = np.random.default_rng(5)
    profits = 1_180_000.0 + rng.normal(0.0, 400.0, 30)
    mc = pd.DataFrame({
        "seed": np.arange(30),
        "profit_total_eur": profits,
        "foresight_gap_pct": rng.normal(0.4, 0.1, 30),
    })
    pf = float(profits.max() + 300.0)

    plt.close("all")
    captured: dict = {}
    original = unc_mod.save_figure

    def _keep(out):
        captured["fig"] = plt.gcf()
        return out

    unc_mod.save_figure = _keep
    try:
        unc_mod.plot_rolling_horizon_distribution(
            mc, tmp_path / "rh.pdf", pf_profit_eur=pf,
        )
    finally:
        unc_mod.save_figure = original

    fig = captured["fig"]
    legend = fig.axes[0].get_legend()
    texts = [t.get_text() for t in legend.get_texts()]
    assert sorted(texts) == ["P10", "P50", "P90", "Perfect foresight"], texts
    assert not any("€" in t or "=" in t for t in texts), texts
