"""Perfect-foresight benchmark guard (pipeline).

A stitched rolling-horizon dispatch is PF-feasible, so no realisation
can legitimately beat the true PF optimum — but the benchmark incumbent
is only mip_gap-optimal, and a realisation landing inside that slack
reads as a spurious negative foresight gap.  The pipeline then re-solves
the benchmark at progressively tighter gaps and recomputes the gap
column and KPI percentiles against the final benchmark.  A re-solve is
accepted only when it improves the incumbent: when the time limit binds,
a deterministic solver returns the same incumbent regardless of the
requested gap, so escalation stops after one unimproved probe instead of
burning the time limit again and again.

These tests drive the pipeline end-to-end with a wrapped
``run_scenario`` (one real solve, deterministic fakes after it) and a
fake Monte Carlo ensemble, so every path is exercised without depending
on solver timing.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import pvbess_opt.pipeline as pipeline_mod
from pvbess_opt.pipeline import RunConfig, run


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


def _fake_mc(profit_factors):
    """Fake monte_carlo_rolling: seeds at fixed multiples of the PF
    profit it is handed, with the real column schema."""

    def fake(params, ts, **kwargs):
        pf = float(kwargs["pf_profit_eur"])
        profits = [pf * f for f in profit_factors]
        n = len(profits)
        return pd.DataFrame({
            "seed": list(range(42, 42 + n)),
            "profit_total_eur": profits,
            "grid_export_mwh": [1.0] * n,
            "grid_import_mwh": [1.0] * n,
            "pv_curtailed_mwh": [0.0] * n,
            "bess_cycles_total": [1.0] * n,
            "foresight_gap_pct": [100.0 * (1.0 - p / pf) for p in profits],
        })

    return fake


def _wrap_run_scenario(monkeypatch, *, improve_factor: float | None):
    """Wrap pipeline.run_scenario: the first call solves for real and is
    cached; later calls return copies of the SAME solution (a stalled,
    time-limited re-solve) — or, when ``improve_factor`` is set, copies
    carrying a scaled ``retail_price_eur_per_mwh`` column so
    ``compute_kpis`` (which re-derives the per-step EUR columns from
    prices) sees a strictly better incumbent."""
    real = pipeline_mod.run_scenario
    state: dict = {"calls": 0}

    def wrapper(params, ts, **kwargs):
        state["calls"] += 1
        if "base" not in state:
            state["base"] = real(params, ts, **kwargs)
        res, solver, res_full = state["base"]
        res = res.copy()
        if improve_factor is not None and state["calls"] >= 2:
            tariff = float(params.get("retail_tariff_eur_per_mwh", 0.0) or 0.0)
            res["retail_price_eur_per_mwh"] = tariff * improve_factor
        return res, solver, res_full.copy()

    monkeypatch.setattr(pipeline_mod, "run_scenario", wrapper)
    return state


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_guard_stops_after_one_unimproved_probe(tmp_path, monkeypatch):
    """A stalled re-solve (identical incumbent, i.e. the time limit
    binds) stops the escalation after ONE probe: the previous benchmark
    is kept, ``pf_benchmark_mip_gap`` stays at the configured gap, and
    no further solves are burned."""
    workbook = _one_day_workbook(tmp_path)
    state = _wrap_run_scenario(monkeypatch, improve_factor=None)
    monkeypatch.setattr(
        "pvbess_opt.pipeline.monte_carlo_rolling", _fake_mc([1.05, 0.95]),
    )

    results = run(RunConfig(
        excel=workbook, solver="highs", outdir=tmp_path / "out",
        mip_gap=1e-3, time_limit=60,
        rolling_horizon=True, monte_carlo=2,
    ))

    # Exactly one probe beyond the base solve — no wasted escalation.
    assert state["calls"] == 2
    kpis = results.kpis
    assert kpis["pf_benchmark_mip_gap"] == pytest.approx(1e-3)

    log_text = (
        results.out_dir / "00_summary" / "run_log.txt"
    ).read_text(encoding="utf-8")
    assert log_text.count("re-solving the benchmark") == 1
    assert "did not improve" in log_text
    assert "raise --time-limit" in log_text

    # Gaps are consistent with the KEPT (original) benchmark and the
    # impossible seed keeps its negative gap.
    pf_final = float(kpis["profit_total_eur"])
    df_mc = pd.read_excel(
        results.out_dir / "03_results.xlsx", sheet_name="rolling_horizon_mc",
    )
    expected = 100.0 * (1.0 - df_mc["profit_total_eur"] / pf_final)
    assert np.allclose(df_mc["foresight_gap_pct"], expected, rtol=1e-9)
    assert float(df_mc["foresight_gap_pct"].min()) < 0.0
    assert kpis["foresight_gap_pct_p50"] == pytest.approx(
        float(df_mc["foresight_gap_pct"].quantile(0.50)), abs=1e-4,
    )


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_guard_accepts_improving_resolve(tmp_path, monkeypatch):
    """A re-solve that genuinely improves the incumbent is accepted:
    the benchmark, the gap column and the percentile KPIs all move to
    the better solution and ``pf_benchmark_mip_gap`` records the gap of
    the accepted solve."""
    workbook = _one_day_workbook(tmp_path)
    # +50 % retail on the re-solve: load-coverage profit dominates the
    # one-day self-consumption run, so the incumbent rises far above
    # the fake seeds at 1.01-1.02 x PF and the loop ends after ONE
    # accepted re-solve.
    state = _wrap_run_scenario(monkeypatch, improve_factor=1.5)
    monkeypatch.setattr(
        "pvbess_opt.pipeline.monte_carlo_rolling", _fake_mc([1.02, 1.01]),
    )

    results = run(RunConfig(
        excel=workbook, solver="highs", outdir=tmp_path / "out",
        mip_gap=1e-3, time_limit=60,
        rolling_horizon=True, monte_carlo=2,
    ))

    assert state["calls"] == 2
    kpis = results.kpis
    # The accepted re-solve ran at the tightened gap.
    assert kpis["pf_benchmark_mip_gap"] == pytest.approx(1e-4)

    log_text = (
        results.out_dir / "00_summary" / "run_log.txt"
    ).read_text(encoding="utf-8")
    assert log_text.count("re-solving the benchmark") == 1
    assert "did not improve" not in log_text

    # The final benchmark is the improved incumbent and every gap is
    # positive against it.
    pf_final = float(kpis["profit_total_eur"])
    df_mc = pd.read_excel(
        results.out_dir / "03_results.xlsx", sheet_name="rolling_horizon_mc",
    )
    assert float(df_mc["profit_total_eur"].max()) < pf_final
    expected = 100.0 * (1.0 - df_mc["profit_total_eur"] / pf_final)
    assert np.allclose(df_mc["foresight_gap_pct"], expected, rtol=1e-9)
    assert (df_mc["foresight_gap_pct"] > 0.0).all()


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_guard_silent_when_benchmark_is_best(tmp_path, monkeypatch):
    """Seeds below the PF incumbent leave the benchmark untouched."""
    workbook = _one_day_workbook(tmp_path)
    state = _wrap_run_scenario(monkeypatch, improve_factor=None)
    monkeypatch.setattr(
        "pvbess_opt.pipeline.monte_carlo_rolling", _fake_mc([0.99, 0.97]),
    )

    results = run(RunConfig(
        excel=workbook, solver="highs", outdir=tmp_path / "out",
        mip_gap=1e-3, time_limit=60,
        rolling_horizon=True, monte_carlo=2,
    ))

    assert state["calls"] == 1  # base solve only — the guard never fired
    kpis = results.kpis
    assert kpis["pf_benchmark_mip_gap"] == pytest.approx(1e-3)
    run_log = results.out_dir / "00_summary" / "run_log.txt"
    assert "re-solving the benchmark" not in run_log.read_text(encoding="utf-8")
    df_mc = pd.read_excel(
        results.out_dir / "03_results.xlsx", sheet_name="rolling_horizon_mc",
    )
    assert (df_mc["foresight_gap_pct"] > 0.0).all()
    assert kpis["foresight_gap_pct_p50"] == pytest.approx(
        float(df_mc["foresight_gap_pct"].quantile(0.50)), abs=1e-4,
    )
