"""Perfect-foresight benchmark guard (pipeline).

A stitched rolling-horizon dispatch is PF-feasible, so no realisation
can legitimately beat the true PF optimum — but the benchmark incumbent
is only mip_gap-optimal, and a realisation landing inside that slack
reads as a spurious negative foresight gap.  The pipeline then re-solves
the benchmark at progressively tighter gaps (x0.1 per pass, down to
1e-6) and recomputes the gap column and KPI percentiles against the
final benchmark.  These tests force that path with a fake Monte Carlo
ensemble whose best seed sits 5 % above any reachable incumbent.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.pipeline import _PF_BENCHMARK_GAP_FLOOR, RunConfig, run


def _highs_available() -> bool:
    try:
        import highspy  # noqa: F401
        return True
    except Exception:
        return False


def _one_day_workbook(tmp_path: Path) -> Path:
    from pvbess_opt.io import read_workbook, write_workbook

    repo_root = Path(__file__).resolve().parent.parent
    typed = read_workbook(repo_root / "inputs" / "input.xlsx")
    # Trim to a single day @ 15-min cadence to keep the run fast.
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)
    typed["simulation"]["uncertainty_diagnostics_enabled"] = False
    for scope in ("plot_daily_scope", "plot_monthly_scope", "plot_yearly_scope"):
        typed["simulation"][scope] = "none"
    typed["bess"]["terminal_soc_equal"] = False
    out = tmp_path / "one_day.xlsx"
    write_workbook(typed, out)
    return out


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_guard_retightens_benchmark_and_recomputes_gaps(tmp_path, monkeypatch):
    """A seed above the PF incumbent triggers tighter re-solves; the gap
    column and percentile KPIs are recomputed against the final benchmark
    and the gap used is recorded as ``pf_benchmark_mip_gap``."""
    workbook = _one_day_workbook(tmp_path)

    def fake_mc(params, ts, **kwargs):
        pf = float(kwargs["pf_profit_eur"])
        # One impossible seed 5 % above PF (no re-solve can close it, so
        # the guard walks to the floor) and one ordinary seed below it.
        profits = [pf * 1.05, pf * 0.95]
        return pd.DataFrame({
            "seed": [42, 43],
            "profit_total_eur": profits,
            "grid_export_mwh": [1.0, 1.0],
            "grid_import_mwh": [1.0, 1.0],
            "pv_curtailed_mwh": [0.0, 0.0],
            "bess_cycles_total": [1.0, 1.0],
            "foresight_gap_pct": [100.0 * (1.0 - p / pf) for p in profits],
        })

    monkeypatch.setattr("pvbess_opt.pipeline.monte_carlo_rolling", fake_mc)

    results = run(RunConfig(
        excel=workbook, solver="highs", outdir=tmp_path / "out",
        mip_gap=1e-5, time_limit=60,
        rolling_horizon=True, monte_carlo=2,
    ))

    kpis = results.kpis
    # The guard walked from 1e-5 to the 1e-6 floor.
    assert kpis["pf_benchmark_mip_gap"] == pytest.approx(_PF_BENCHMARK_GAP_FLOOR)

    # The re-solve is visible in the run log.
    run_log = results.out_dir / "00_summary" / "run_log.txt"
    log_text = run_log.read_text(encoding="utf-8")
    assert "re-solving the benchmark at mip_gap=1e-06" in log_text

    # Gap column in the results workbook is consistent with the FINAL
    # benchmark (the headline profit KPI), not the original incumbent.
    pf_final = float(kpis["profit_total_eur"])
    df_mc = pd.read_excel(
        results.out_dir / "03_results.xlsx", sheet_name="rolling_horizon_mc",
    )
    expected = 100.0 * (1.0 - df_mc["profit_total_eur"] / pf_final)
    assert np.allclose(df_mc["foresight_gap_pct"], expected, rtol=1e-9)
    # The impossible seed keeps a negative gap even at the floor.
    assert float(df_mc["foresight_gap_pct"].min()) < 0.0

    # Percentile KPIs recomputed from the recomputed column.
    gap_p50 = float(df_mc["foresight_gap_pct"].quantile(0.50))
    assert kpis["foresight_gap_pct_p50"] == pytest.approx(gap_p50, abs=1e-4)
    assert kpis["mc_n_seeds"] == 2


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_guard_silent_when_benchmark_is_best(tmp_path, monkeypatch):
    """Seeds below the PF incumbent leave the benchmark untouched."""
    workbook = _one_day_workbook(tmp_path)

    def fake_mc(params, ts, **kwargs):
        pf = float(kwargs["pf_profit_eur"])
        profits = [pf * 0.99, pf * 0.97]
        return pd.DataFrame({
            "seed": [42, 43],
            "profit_total_eur": profits,
            "grid_export_mwh": [1.0, 1.0],
            "grid_import_mwh": [1.0, 1.0],
            "pv_curtailed_mwh": [0.0, 0.0],
            "bess_cycles_total": [1.0, 1.0],
            "foresight_gap_pct": [100.0 * (1.0 - p / pf) for p in profits],
        })

    monkeypatch.setattr("pvbess_opt.pipeline.monte_carlo_rolling", fake_mc)

    results = run(RunConfig(
        excel=workbook, solver="highs", outdir=tmp_path / "out",
        mip_gap=1e-3, time_limit=60,
        rolling_horizon=True, monte_carlo=2,
    ))

    kpis = results.kpis
    # No re-solve: the recorded benchmark gap is the configured one.
    assert kpis["pf_benchmark_mip_gap"] == pytest.approx(1e-3)
    run_log = results.out_dir / "00_summary" / "run_log.txt"
    assert "re-solving the benchmark" not in run_log.read_text(encoding="utf-8")
    # Gaps are positive and the p50 matches the column median.
    df_mc = pd.read_excel(
        results.out_dir / "03_results.xlsx", sheet_name="rolling_horizon_mc",
    )
    assert (df_mc["foresight_gap_pct"] > 0.0).all()
    assert kpis["foresight_gap_pct_p50"] == pytest.approx(
        float(df_mc["foresight_gap_pct"].quantile(0.50)), abs=1e-4,
    )
