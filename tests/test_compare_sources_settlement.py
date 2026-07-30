"""Compare-sources mode must not lose armed per-seed features.

The compare branch of the pipeline runs four Monte Carlo ensembles
(DAM / PV / Load / All) but previously called ``monte_carlo_rolling``
WITHOUT the imbalance settlement arguments the plain branch passes —
with ``imbalance_enabled = TRUE`` the Eq. E28 settlement silently
vanished from every delivered financial (and ``risk_metrics_enabled``
was skipped with a warning blaming ``uncertainty_enabled``/``n_seeds``,
both of which were set).  The CLI ``--window-hours``/``--commit-hours``
overrides also bypassed the loader's ``window >= 2 x commit`` imbalance
lookahead gate, delivering all-zero settlement KPIs plus a degenerate
"Monte Carlo" whose every seed was identical, silently.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest

from pvbess_opt.pipeline import RunConfig, _resolve_uncertainty_config, run

ROOT = Path(__file__).resolve().parent.parent


def _highs_available() -> bool:
    try:
        import highspy  # noqa: F401
        return True
    except Exception:
        return False


def _one_day_workbook(tmp_path: Path, **simulation_overrides) -> Path:
    from pvbess_opt.io import read_workbook, write_workbook

    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)
    typed["simulation"]["uncertainty_diagnostics_enabled"] = False
    for scope in ("plot_daily_scope", "plot_monthly_scope", "plot_yearly_scope"):
        typed["simulation"][scope] = "none"
    typed["bess"]["terminal_soc_equal"] = False
    typed["simulation"].update(simulation_overrides)
    out = tmp_path / "one_day.xlsx"
    write_workbook(typed, out)
    return out


def test_cli_window_commit_overrides_revalidate_the_lookahead_gate():
    econ = {
        "uncertainty_enabled": True,
        "uncertainty_n_seeds": 2,
        "uncertainty_window_hours": 12,
        "uncertainty_commit_hours": 6,
        "imbalance_enabled": True,
    }
    # The workbook 12/6 passed the loader gate; the CLI 6/6 starves the
    # nomination lookahead and must be rejected with the loader's error.
    bad = RunConfig(excel="x.xlsx", window_hours=6, commit_hours=6)
    with pytest.raises(ValueError, match="2 x uncertainty_commit_hours"):
        _resolve_uncertainty_config(bad, econ, "self_consumption")
    # No overrides -> the stored values pass unchanged.
    ok = RunConfig(excel="x.xlsx")
    cfg = _resolve_uncertainty_config(ok, econ, "self_consumption")
    assert (cfg["window_hours"], cfg["commit_hours"]) == (12, 6)
    # Overrides that keep the gate satisfied stay legal.
    wide = RunConfig(excel="x.xlsx", window_hours=16, commit_hours=8)
    cfg = _resolve_uncertainty_config(wide, econ, "self_consumption")
    assert (cfg["window_hours"], cfg["commit_hours"]) == (16, 8)


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_compare_sources_keeps_settlement_and_risk_metrics(
    tmp_path, monkeypatch, caplog,
):
    workbook = _one_day_workbook(
        tmp_path,
        uncertainty_enabled=True,
        uncertainty_compare_sources=True,
        uncertainty_n_seeds=2,
        uncertainty_window_hours=12,
        uncertainty_commit_hours=6,
        imbalance_enabled=True,
        risk_metrics_enabled=True,
    )

    calls: list[dict] = []

    def fake_mc(params, ts, **kwargs):
        calls.append(dict(kwargs))
        pf = float(kwargs["pf_profit_eur"])
        profits = [pf * 0.98, pf * 0.96]
        return pd.DataFrame({
            "seed": [42, 43],
            "profit_total_eur": profits,
            "grid_export_mwh": [1.0, 1.0],
            "grid_import_mwh": [1.0, 1.0],
            "pv_curtailed_mwh": [0.0, 0.0],
            "bess_cycles_total": [1.0, 1.0],
            "foresight_gap_pct": [2.0, 4.0],
            "imbalance_cost_eur": [-120.0, -80.0],
            "imbalance_short_mwh": [0.4, 0.2],
            "imbalance_long_mwh": [0.1, 0.3],
            "imbalance_cost_pv_only_eur": [-150.0, -100.0],
            "bess_imbalance_hedge_value_eur": [30.0, 20.0],
        })

    monkeypatch.setattr("pvbess_opt.pipeline.monte_carlo_rolling", fake_mc)

    with caplog.at_level(logging.WARNING):
        results = run(RunConfig(
            excel=workbook, solver="highs", outdir=tmp_path / "out",
            mip_gap=1e-3, time_limit=120,
        ))

    # All four ensembles ran, each with the settlement armed.
    assert len(calls) == 4
    assert all(c.get("imbalance_enabled") is True for c in calls)

    # The settlement aggregates reach the delivered KPIs (mean of the
    # 'all' ensemble; every fake ensemble returns the same values).
    assert results.kpis["imbalance_cost_year1_eur"] == pytest.approx(-100.0)
    assert results.kpis["bess_imbalance_hedge_value_mean_eur"] == (
        pytest.approx(25.0)
    )

    # VaR/CVaR runs on the compare frame's 'all' ensemble instead of
    # being skipped with a false diagnosis.
    assert "npv_var_eur" in results.kpis
    assert not any(
        "no rolling-horizon Monte Carlo seeds are available"
        in r.getMessage() for r in caplog.records
    )


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_all_ensemble_honours_per_source_toggles(tmp_path, monkeypatch):
    """Final-round-2 regression: the 'all' ensemble — which feeds the DELIVERED
    settlement aggregates and NPV tail risk — ran with hard-coded
    all-True noise flags, re-introducing a source the workbook had
    explicitly disabled (uncertainty_dam_enabled = FALSE); the plain-MC
    path honoured the toggle, so the two modes delivered different
    financials for the identical workbook."""
    workbook = _one_day_workbook(
        tmp_path,
        uncertainty_enabled=True,
        uncertainty_compare_sources=True,
        uncertainty_n_seeds=2,
        uncertainty_window_hours=12,
        uncertainty_commit_hours=6,
        uncertainty_dam_enabled=False,
    )

    calls: list[dict] = []

    def fake_mc(params, ts, **kwargs):
        calls.append(dict(kwargs))
        pf = float(kwargs["pf_profit_eur"])
        return pd.DataFrame({
            "seed": [42, 43],
            "profit_total_eur": [pf * 0.99, pf * 0.97],
            "grid_export_mwh": [1.0, 1.0],
            "grid_import_mwh": [1.0, 1.0],
            "pv_curtailed_mwh": [0.0, 0.0],
            "bess_cycles_total": [1.0, 1.0],
            "foresight_gap_pct": [1.0, 3.0],
        })

    monkeypatch.setattr("pvbess_opt.pipeline.monte_carlo_rolling", fake_mc)
    run(RunConfig(
        excel=workbook, solver="highs", outdir=tmp_path / "out",
        mip_gap=1e-3, time_limit=120,
    ))

    assert len(calls) == 4
    # The three single-source ensembles keep their fixed diagnostic
    # definitions...
    dam_only, pv_only, load_only, all_combined = calls
    assert (dam_only["enable_dam"], dam_only["enable_pv"]) == (True, False)
    assert (pv_only["enable_dam"], pv_only["enable_pv"]) == (False, True)
    assert load_only["enable_load"] is True
    # ...while the 'all' ensemble honours the workbook toggles exactly
    # like the plain-MC path (DAM noise disabled here).
    assert all_combined["enable_dam"] is False
    assert all_combined["enable_pv"] is True


def test_degenerate_zero_noise_monte_carlo_is_rejected():
    """Final-round-3 regression: uncertainty_enabled with n_seeds > 0 and every
    noise source toggled off ran the full MC cost to deliver a point
    mass — identical seeds, imbalance settlement frozen at 0.00 and
    VaR == CVaR — with no message anywhere."""
    econ_all_off = {
        "uncertainty_enabled": True,
        "uncertainty_n_seeds": 2,
        "uncertainty_dam_enabled": False,
        "uncertainty_pv_enabled": False,
        "uncertainty_load_enabled": False,
    }
    with pytest.raises(ValueError, match="no effective noise source"):
        _resolve_uncertainty_config(
            RunConfig(excel="x.xlsx"), econ_all_off, "self_consumption",
        )
    # Merchant forces load noise off, so load-only resolves to zero
    # effective sources too.
    econ_load_only = {
        "uncertainty_enabled": True,
        "uncertainty_n_seeds": 2,
        "uncertainty_dam_enabled": False,
        "uncertainty_pv_enabled": False,
        "uncertainty_load_enabled": True,
    }
    with pytest.raises(ValueError, match="no effective noise source"):
        _resolve_uncertainty_config(
            RunConfig(excel="x.xlsx"), econ_load_only, "merchant",
        )
    # The same workbook stays legal in self-consumption (load noise
    # perturbs a real column) and as a deterministic n_seeds = 0 run.
    cfg = _resolve_uncertainty_config(
        RunConfig(excel="x.xlsx"), econ_load_only, "self_consumption",
    )
    assert cfg["enable_load"] is True
    # The guard must fire at the SMALLEST armed seed count too — the
    # mutation matrix showed an off-by-one weakening (n_seeds > 1)
    # survived when only n_seeds = 2 was probed.
    with pytest.raises(ValueError, match="no effective noise source"):
        _resolve_uncertainty_config(
            RunConfig(excel="x.xlsx"),
            {**econ_all_off, "uncertainty_n_seeds": 1}, "self_consumption",
        )
    # The deterministic-run escape hatch is CLI --monte-carlo 0 (the
    # loader rejects a workbook uncertainty_n_seeds < 1).
    cfg = _resolve_uncertainty_config(
        RunConfig(excel="x.xlsx", monte_carlo=0),
        econ_all_off, "self_consumption",
    )
    assert cfg["n_seeds"] == 0


def test_compare_flag_without_uncertainty_warns(caplog):
    """Final-round-3 regression: --compare-uncertainty-sources on a workbook
    with uncertainty_enabled = FALSE (and no --rolling-horizon) was a
    silent no-op — exit 0, zero ensembles, zero mention of the flag."""
    with caplog.at_level(logging.WARNING):
        cfg = _resolve_uncertainty_config(
            RunConfig(excel="x.xlsx", compare_uncertainty_sources=True),
            {"uncertainty_enabled": False}, "self_consumption",
        )
    assert cfg["enabled"] is False
    assert any(
        "has no effect" in r.getMessage() for r in caplog.records
    )


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_merchant_compare_skips_the_inert_load_ensemble(tmp_path, monkeypatch):
    """Final-round-3 regression: merchant compare-sources ran the fixed
    (False, False, True) 'load' diagnostic ensemble — perturbing a column
    the model never reads, wasting a quarter of the compare wall-clock —
    and delivered a plausible-looking foresight_gap_pct_p50_load for a
    plant with no load."""
    workbook = _one_day_workbook(
        tmp_path,
        uncertainty_enabled=True,
        uncertainty_compare_sources=True,
        uncertainty_n_seeds=2,
        uncertainty_window_hours=12,
        uncertainty_commit_hours=6,
    )

    calls: list[dict] = []

    def fake_mc(params, ts, **kwargs):
        calls.append(dict(kwargs))
        pf = float(kwargs["pf_profit_eur"])
        return pd.DataFrame({
            "seed": [42, 43],
            "profit_total_eur": [pf * 0.99, pf * 0.97],
            "grid_export_mwh": [1.0, 1.0],
            "grid_import_mwh": [1.0, 1.0],
            "pv_curtailed_mwh": [0.0, 0.0],
            "bess_cycles_total": [1.0, 1.0],
        })

    monkeypatch.setattr("pvbess_opt.pipeline.monte_carlo_rolling", fake_mc)
    run(RunConfig(
        excel=workbook, solver="highs", outdir=tmp_path / "out",
        mip_gap=1e-3, time_limit=120, mode="merchant",
    ))

    # Three ensembles: dam, pv, all — the inert 'load' set is skipped.
    assert len(calls) == 3
    dam_only, pv_only, all_combined = calls
    assert (dam_only["enable_dam"], dam_only["enable_pv"]) == (True, False)
    assert (pv_only["enable_dam"], pv_only["enable_pv"]) == (False, True)
    assert all_combined["enable_dam"] is True
    assert all_combined["enable_load"] is False

    # The delivered KPI sheet carries p50 rows only for the ensembles
    # that ran — no NaN foresight_gap_pct_p50_load.
    results = next((tmp_path / "out").rglob("03_results.xlsx"))
    kpi = pd.read_excel(results, sheet_name="kpis_year1")
    metrics = set(kpi["metric"].astype(str))
    assert "foresight_gap_pct_p50_dam" in metrics
    assert "foresight_gap_pct_p50_all" in metrics
    assert "foresight_gap_pct_p50_load" not in metrics


def test_ida_only_monte_carlo_is_rejected_as_degenerate():
    """Convergence-round regression: enable_ida counted as an effective
    noise source, but the seed noise perturbs only the ida FORECAST —
    Stage-1 never reads ida_price and the Stage-2 redispatch settles on
    the original noise-free intraday prices, so an ida-only config
    delivered the exact point mass the guard exists to reject."""
    econ_ida_only = {
        "uncertainty_enabled": True,
        "uncertainty_n_seeds": 2,
        "uncertainty_dam_enabled": False,
        "uncertainty_pv_enabled": False,
        "uncertainty_load_enabled": False,
        "uncertainty_ida_enabled": True,
        "id_enabled": True,
    }
    with pytest.raises(ValueError, match="does not by itself differentiate"):
        _resolve_uncertainty_config(
            RunConfig(excel="x.xlsx"), econ_ida_only, "merchant",
        )
    # The message names the right knob when ida noise is off by the
    # USER'S toggle (the venue itself is on).
    econ_user_off = {**econ_ida_only, "uncertainty_ida_enabled": False}
    with pytest.raises(ValueError, match="uncertainty_ida_enabled is FALSE"):
        _resolve_uncertainty_config(
            RunConfig(excel="x.xlsx"), econ_user_off, "merchant",
        )
    # ...and the venue when it is the venue that is off.
    econ_venue_off = {**econ_ida_only, "id_enabled": False}
    with pytest.raises(ValueError, match="id_enabled is FALSE"):
        _resolve_uncertainty_config(
            RunConfig(excel="x.xlsx"), econ_venue_off, "merchant",
        )
    # ida noise stacked on a real source stays legal.
    cfg = _resolve_uncertainty_config(
        RunConfig(excel="x.xlsx"),
        {**econ_ida_only, "uncertainty_dam_enabled": True}, "merchant",
    )
    assert cfg["enable_ida"] is True and cfg["enable_dam"] is True
