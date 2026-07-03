"""Legend-headroom regression smoke test.

Asserts that across all financial / lifecycle / BESS-revenue plots
that render a legend, the legend bounding box does not overlap any
bar patch or non-decorator data marker.

The check renders each plot, keeps the matplotlib figure open via a
monkey-patched ``save_figure``, and inspects the legend bbox against
every ``BarContainer.patches`` and every marker on every ``Line2D``.
Pure horizontal / vertical decorator lines (``axhline`` /
``axvline`` / payback markers) are skipped because their window
extent covers the full axis span and would trip the check on every
legend anchored to ``best`` — the headroom we care about is for
*data* artists.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pytest

from pvbess_opt.plotting import bess_revenue as bess_revenue_mod
from pvbess_opt.plotting import financial as financial_mod
from pvbess_opt.plotting import lifecycle as lifecycle_mod
from pvbess_opt.plotting.bess_revenue import (
    plot_bess_capacity_vs_activation_split,
    plot_bess_revenue_by_month,
)
from pvbess_opt.plotting.financial import (
    plot_cumulative_cashflow,
    plot_monthly_cashflow_year1,
    plot_npv_waterfall,
    plot_payback,
    plot_yearly_cashflow_bars,
)
from pvbess_opt.plotting.lifecycle import (
    plot_lcoe_summary,
    plot_lcos_summary,
    plot_revenue_stack_yearly,
)

# ---------------------------------------------------------------------------
# Fixture cashflow / KPI dicts
# ---------------------------------------------------------------------------


def _yearly_cf() -> pd.DataFrame:
    rows = [{
        "project_year": 0, "calendar_year": 2025,
        "revenue_eur": 0.0,
        "revenue_retail_eur": 0.0,
        "revenue_dam_eur": 0.0,
        "aggregator_fee_eur": 0.0,
        "balancing_capacity_revenue_eur": 0.0,
        "balancing_activation_revenue_eur": 0.0,
        "balancing_revenue_eur": 0.0,
        "opex_eur": 0.0,
        "devex_eur": -75_000.0,
        "capex_eur": -600_000.0,
        "discount_factor": 1.0,
        "discounted_cf_eur": -675_000.0,
        "net_cashflow_eur": -675_000.0,
    }]
    r = 0.07
    for y in range(1, 21):
        df_y = 1 / (1 + r) ** y
        rev_y = 150_000.0 * (1.0 + 0.01 * (y - 1))
        opex_y = -14_000.0
        net = rev_y + opex_y
        rows.append({
            "project_year": y, "calendar_year": 2025 + y,
            "revenue_eur": rev_y,
            "revenue_retail_eur": rev_y * 0.6,
            "revenue_dam_eur": rev_y * 0.4,
            "aggregator_fee_eur": -rev_y * 0.02,
            "balancing_capacity_revenue_eur": 5_000.0,
            "balancing_activation_revenue_eur": 1_500.0,
            "balancing_revenue_eur": 6_500.0,
            "opex_eur": opex_y,
            "devex_eur": 0.0,
            "capex_eur": 0.0,
            "discount_factor": df_y,
            "discounted_cf_eur": (net + 6_500.0) * df_y,
            "net_cashflow_eur": net + 6_500.0,
        })
    df = pd.DataFrame(rows)
    df["cumulative_cf_eur"] = df["net_cashflow_eur"].cumsum()
    df["cumulative_dcf_eur"] = df["discounted_cf_eur"].cumsum()
    return df


def _year1_kpis() -> dict:
    return {
        "profit_load_from_pv_eur": 40_000.0,
        "profit_load_from_bess_eur": 25_000.0,
        "profit_export_from_pv_eur": 30_000.0,
        "profit_export_from_bess_eur": 22_000.0,
        "expense_charge_bess_grid_eur": 4_000.0,
        "bm_fcr_capacity_revenue_eur": 3_500.0,
        "bm_afrr_up_capacity_revenue_eur": 2_500.0,
        "bm_afrr_up_activation_revenue_eur": 1_200.0,
        "bm_afrr_dn_capacity_revenue_eur": 2_000.0,
        "bm_afrr_dn_activation_revenue_eur": 700.0,
        "bm_mfrr_up_capacity_revenue_eur": 900.0,
        "bm_mfrr_up_activation_revenue_eur": 250.0,
        "bm_mfrr_dn_capacity_revenue_eur": 600.0,
        "bm_mfrr_dn_activation_revenue_eur": 180.0,
        "revenue_bess_fcr_eur": 3_500.0,
        "revenue_bess_afrr_up_eur": 3_700.0,
        "revenue_bess_afrr_dn_eur": 2_700.0,
        "revenue_bess_mfrr_up_eur": 1_150.0,
        "revenue_bess_mfrr_dn_eur": 780.0,
        "revenue_bess_dam_eur": 18_000.0,
    }


def _monthly_cf() -> pd.DataFrame:
    return pd.DataFrame({
        "project_year": [1] * 12,
        "calendar_year": [2026] * 12,
        "period": list(range(1, 13)),
        "revenue_eur": [10_000.0 + 800.0 * m for m in range(1, 13)],
        "opex_eur": [-1_200.0] * 12,
        "net_cashflow_eur": [9_000.0 + 800.0 * m for m in range(1, 13)],
    })


def _res_year1() -> pd.DataFrame:
    n = 96 * 30
    rng = pd.date_range("2026-01-01", periods=n, freq="15min")
    rng2 = pd.date_range("2026-02-01", periods=n, freq="15min")
    rng3 = pd.date_range("2026-03-01", periods=n, freq="15min")
    ts = rng.append(rng2).append(rng3)
    rng_np = np.random.default_rng(7)
    return pd.DataFrame({
        "timestamp": ts,
        "profit_export_from_bess_eur": rng_np.uniform(0.5, 2.0, len(ts)),
        "expense_charge_bess_grid_eur": rng_np.uniform(0.0, 0.5, len(ts)),
        "bm_reservation_fcr_kw": np.full(len(ts), 40.0),
        "bm_reservation_afrr_up_kw": np.full(len(ts), 30.0),
        "bm_reservation_afrr_dn_kw": np.full(len(ts), 25.0),
        "bm_reservation_mfrr_up_kw": np.full(len(ts), 10.0),
        "bm_reservation_mfrr_dn_kw": np.full(len(ts), 8.0),
    })


# ---------------------------------------------------------------------------
# Render-then-introspect helper
# ---------------------------------------------------------------------------


def _capture(module, render_fn):
    """Render ``render_fn`` and return the live figure object.

    Bypasses ``save_figure`` so the test can inspect the legend bbox.
    Plots that use ``fig.savefig`` directly (LCOE / LCOS) are handled
    by additionally patching ``plt.close`` so the figure stays alive.
    """
    plt.close("all")
    original_save = getattr(module, "save_figure", None)
    original_close = plt.close
    captured: dict = {}

    def keep_open(out):
        captured["fig"] = plt.gcf()
        return Path(out)

    def no_close(*_args, **_kwargs):
        if "fig" not in captured and plt.get_fignums():
            captured["fig"] = plt.gcf()

    module.save_figure = keep_open
    plt.close = no_close  # type: ignore[assignment]
    try:
        render_fn()
        if "fig" not in captured and plt.get_fignums():
            captured["fig"] = plt.gcf()
    finally:
        module.save_figure = original_save
        plt.close = original_close  # type: ignore[assignment]
    return captured.get("fig")


def _legend_overlap_issues(ax) -> list[str]:
    """Return human-readable issues when the legend bbox overlaps data."""
    fig = ax.figure
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    legend = ax.get_legend()
    if legend is None:
        return []
    lbox = legend.get_window_extent(renderer=renderer)
    issues: list[str] = []

    # Bar patches: each container groups its bars; check every patch.
    for cont in ax.containers:
        patches: Iterable = getattr(cont, "patches", []) or []
        for p in patches:
            try:
                pbox = p.get_window_extent(renderer=renderer)
            except (AttributeError, RuntimeError):
                continue
            if lbox.overlaps(pbox):
                issues.append(
                    f"legend overlaps a bar in container "
                    f"{getattr(cont, 'get_label', lambda: '?')()!r}"
                )
                break

    # Data-marker points on plotted lines.  Decorator helpers
    # (``axhline`` / ``axvline``) draw lines without markers and are
    # skipped.
    for line in ax.lines:
        marker = line.get_marker()
        if marker in (None, "", "None"):
            continue
        xs = np.asarray(line.get_xdata(), dtype=float)
        ys = np.asarray(line.get_ydata(), dtype=float)
        if xs.size == 0:
            continue
        finite = np.isfinite(xs) & np.isfinite(ys)
        xs = xs[finite]
        ys = ys[finite]
        if xs.size == 0:
            continue
        pts_display = ax.transData.transform(np.column_stack([xs, ys]))
        inside_x = (pts_display[:, 0] >= lbox.x0) & (pts_display[:, 0] <= lbox.x1)
        inside_y = (pts_display[:, 1] >= lbox.y0) & (pts_display[:, 1] <= lbox.y1)
        if np.any(inside_x & inside_y):
            issues.append(
                f"legend covers a data marker on line {line.get_label()!r}"
            )

    return issues


# ---------------------------------------------------------------------------
# Per-plot tests
# ---------------------------------------------------------------------------


def test_cumulative_cashflow_legend_clear(tmp_path: Path):
    fig = _capture(
        financial_mod,
        lambda: plot_cumulative_cashflow(_yearly_cf(), tmp_path / "x.pdf"),
    )
    assert fig is not None
    assert _legend_overlap_issues(fig.axes[0]) == []


def test_yearly_cashflow_bars_legend_clear(tmp_path: Path):
    fig = _capture(
        financial_mod,
        lambda: plot_yearly_cashflow_bars(_yearly_cf(), tmp_path / "x.pdf"),
    )
    assert fig is not None
    assert _legend_overlap_issues(fig.axes[0]) == []


def test_npv_waterfall_legend_clear(tmp_path: Path):
    fig = _capture(
        financial_mod,
        lambda: plot_npv_waterfall(_yearly_cf(), tmp_path / "x.pdf"),
    )
    assert fig is not None
    assert _legend_overlap_issues(fig.axes[0]) == []


def test_payback_legend_clear(tmp_path: Path):
    fig = _capture(
        financial_mod,
        lambda: plot_payback(
            _yearly_cf(), tmp_path / "x.pdf",
            simple_payback_years=6.5, discounted_payback_years=8.2,
        ),
    )
    assert fig is not None
    assert _legend_overlap_issues(fig.axes[0]) == []


def test_monthly_cashflow_year1_legend_clear(tmp_path: Path):
    fig = _capture(
        financial_mod,
        lambda: plot_monthly_cashflow_year1(_monthly_cf(), tmp_path / "x.pdf"),
    )
    assert fig is not None
    assert _legend_overlap_issues(fig.axes[0]) == []


def test_revenue_stack_yearly_legend_clear(tmp_path: Path):
    fig = _capture(
        lifecycle_mod,
        lambda: plot_revenue_stack_yearly(
            _yearly_cf(), _year1_kpis(), tmp_path / "x.pdf",
        ),
    )
    assert fig is not None
    assert _legend_overlap_issues(fig.axes[0]) == []


def test_lcoe_summary_legend_clear(tmp_path: Path):
    capacities = {"pv_kwp": 4500.0, "bess_kw": 1000.0, "bess_kwh": 4000.0}
    econ = {"discount_rate_pct": 7.0}
    fin_kpis = {"lcoe_eur_per_mwh": 60.0, "lcos_eur_per_mwh": 200.0}
    fig = _capture(
        lifecycle_mod,
        lambda: plot_lcoe_summary(
            fin_kpis, None, capacities, econ, tmp_path / "x.pdf",
        ),
    )
    assert fig is not None
    assert _legend_overlap_issues(fig.axes[0]) == []


def test_lcos_summary_legend_clear(tmp_path: Path):
    capacities = {"pv_kwp": 4500.0, "bess_kw": 1000.0, "bess_kwh": 4000.0}
    econ = {"discount_rate_pct": 7.0}
    fin_kpis = {"lcoe_eur_per_mwh": 60.0, "lcos_eur_per_mwh": 200.0}
    fig = _capture(
        lifecycle_mod,
        lambda: plot_lcos_summary(
            fin_kpis, None, capacities, econ, tmp_path / "x.pdf",
        ),
    )
    assert fig is not None
    assert _legend_overlap_issues(fig.axes[0]) == []


def test_bess_capacity_vs_activation_split_legend_clear(tmp_path: Path):
    fig = _capture(
        bess_revenue_mod,
        lambda: plot_bess_capacity_vs_activation_split(
            _year1_kpis(), tmp_path / "x.pdf",
        ),
    )
    assert fig is not None
    assert _legend_overlap_issues(fig.axes[0]) == []


def test_bess_revenue_by_month_legend_clear(tmp_path: Path):
    fig = _capture(
        bess_revenue_mod,
        lambda: plot_bess_revenue_by_month(
            _res_year1(), _year1_kpis(), tmp_path / "x.pdf",
        ),
    )
    assert fig is not None
    assert _legend_overlap_issues(fig.axes[0]) == []


def test_reserve_legend_headroom_is_idempotent():
    """A second call on the same axis must be a no-op."""
    from pvbess_opt.plotting.style import reserve_legend_headroom

    plt.close("all")
    _fig, ax = plt.subplots()
    ax.bar([1, 2, 3], [10.0, 20.0, 30.0])
    ax.set_ylim(0.0, 30.0)
    reserve_legend_headroom(ax, loc="best")
    ymin_after_first, ymax_after_first = ax.get_ylim()
    reserve_legend_headroom(ax, loc="best")
    ymin_after_second, ymax_after_second = ax.get_ylim()
    assert ymin_after_first == pytest.approx(ymin_after_second)
    assert ymax_after_first == pytest.approx(ymax_after_second)
    # And the headroom must actually be larger than the original 30.0.
    assert ymax_after_first > 30.0


# ---------------------------------------------------------------------------
# Uncertainty-plot family — MEASURED no-overlap guarantee
# ---------------------------------------------------------------------------
#
# These figures attach their legends through
# style.attach_legend_clear_of_data, which measures the drawn legend
# against the data artists and grows headroom until they no longer
# intersect.  The assertions below re-measure with the same production
# helper (style.legend_overlaps_data) on rendered figures, so a
# regression in any panel of the family fails here.


def _uncertainty_ts(n_steps: int = 96 * 28) -> pd.DataFrame:
    """Four weeks of 15-min synthetic data (DAM + PV + load)."""
    from tests.conftest import _make_short_ts_15min

    return _make_short_ts_15min(n_steps)


def _family_overlap_issues(fig) -> list[str]:
    from pvbess_opt.plotting.style import legend_overlaps_data

    issues: list[str] = []
    for ax in fig.axes:
        if ax.get_legend() is not None:
            issues.extend(legend_overlaps_data(ax))
    return issues


@pytest.mark.parametrize("plot_name", [
    "inputs_forecast_band",
    "coverage_by_horizon",
    "pit_histogram",
    "residual_qq",
    "crps_timeline",
])
def test_uncertainty_family_legends_clear(plot_name, tmp_path):
    from pvbess_opt.plotting import inputs_uncertainty as iu

    ts = _uncertainty_ts()
    render = {
        # The synthetic frame starts June 1 (DOY 152); pick a window
        # inside the four-week span.
        "inputs_forecast_band": lambda: iu.plot_input_forecast_band(
            ts, tmp_path / "band.pdf", week_start_doy=155,
        ),
        "coverage_by_horizon": lambda: iu.plot_uncertainty_coverage_by_horizon(
            ts, tmp_path / "cov.pdf", commit_steps=96,
        ),
        "pit_histogram": lambda: iu.plot_uncertainty_pit_histogram(
            ts, tmp_path / "pit.pdf",
        ),
        "residual_qq": lambda: iu.plot_uncertainty_residual_qq(
            ts, tmp_path / "qq.pdf",
        ),
        "crps_timeline": lambda: iu.plot_uncertainty_crps_timeline(
            ts, tmp_path / "crps.pdf",
        ),
    }[plot_name]
    fig = _capture(iu, render)
    assert fig is not None
    assert _family_overlap_issues(fig) == []


def test_coverage_ticks_end_at_probability_ceiling(tmp_path):
    """The coverage panel keeps a bounded probability scale: ticks end
    at 1.0 and the axis floor stays at 0 (the legend lives in the empty
    lower-right half instead of a headroom band above 1.0)."""
    from pvbess_opt.plotting import inputs_uncertainty as iu

    ts = _uncertainty_ts()
    fig = _capture(
        iu,
        lambda: iu.plot_uncertainty_coverage_by_horizon(
            ts, tmp_path / "cov.pdf", commit_steps=96,
        ),
    )
    assert fig is not None
    ax = fig.axes[0]
    assert max(ax.get_yticks()) <= 1.0 + 1e-9
    assert ax.get_ylim()[0] <= 0.0 + 1e-9


def test_rolling_horizon_distribution_legend_clear(tmp_path):
    """Worst case of the family: narrow ensemble, 4-entry legend
    (P10 / P50 / P90 / perfect-foresight)."""
    from pvbess_opt.plotting import uncertainty as unc

    rng = np.random.default_rng(3)
    mc = pd.DataFrame({
        "seed": np.arange(30),
        "profit_total_eur": 1_180_000.0 + rng.normal(0.0, 400.0, 30),
        "foresight_gap_pct": rng.normal(0.4, 0.1, 30),
    })
    fig = _capture(
        unc,
        lambda: unc.plot_rolling_horizon_distribution(
            mc, tmp_path / "rh.pdf",
            pf_profit_eur=float(mc["profit_total_eur"].max() + 300.0),
        ),
    )
    assert fig is not None
    assert _family_overlap_issues(fig) == []


def test_rolling_horizon_compare_sources_legend_clear(tmp_path):
    """Compare-sources branch: 4 overlapping histograms + PF marker."""
    from pvbess_opt.plotting import uncertainty as unc

    rng = np.random.default_rng(9)
    frames = []
    for i, src in enumerate(("dam", "pv", "load", "all")):
        frames.append(pd.DataFrame({
            "source_set": src,
            "seed": np.arange(12),
            "profit_total_eur": (
                1_180_000.0 - 200.0 * i + rng.normal(0.0, 300.0, 12)
            ),
            "foresight_gap_pct": rng.normal(0.4, 0.1, 12),
        }))
    mc = pd.concat(frames, ignore_index=True)
    fig = _capture(
        unc,
        lambda: unc.plot_rolling_horizon_distribution(
            mc, tmp_path / "rh_cmp.pdf", pf_profit_eur=1_181_500.0,
        ),
    )
    assert fig is not None
    assert _family_overlap_issues(fig) == []


def test_soh_replacement_legend_clear(tmp_path):
    """The SOH trajectory's 'BESS replacement' legend sits clear of the
    marker-carrying SOH curve for both early and late replacements."""
    from pvbess_opt.plotting import degradation as deg_mod

    def _render(frame: pd.DataFrame, out: Path):
        return _capture(
            deg_mod,
            lambda: deg_mod.plot_soh_trajectory(frame, out),
        )

    for repl_year in (3, 10, 18):
        rows, soh = [], 100.0
        for y in range(1, 21):
            if y == repl_year:
                soh = 100.0
            rows.append({
                "project_year": y, "calendar_year": 2025 + y,
                "soh_pct": soh, "capacity_fade_pct": 100.0 - soh,
                "replacement": y == repl_year,
            })
            soh -= 2.4
        fig = _render(pd.DataFrame(rows), tmp_path / f"soh_{repl_year}.pdf")
        assert fig is not None
        assert _legend_overlap_issues(fig.axes[0]) == [], (
            f"replacement year {repl_year}"
        )


def test_coverage_legend_clear_of_curves(tmp_path):
    """The coverage legend (lower right) never covers a vertex of the
    three coverage curves."""
    from pvbess_opt.plotting import inputs_uncertainty as iu

    ts = _uncertainty_ts()
    fig = _capture(
        iu,
        lambda: iu.plot_uncertainty_coverage_by_horizon(
            ts, tmp_path / "cov.pdf", commit_steps=96,
        ),
    )
    assert fig is not None
    ax = fig.axes[0]
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    legend = ax.get_legend()
    assert legend is not None
    lbox = legend.get_window_extent(renderer=renderer)
    for line in ax.lines:
        if line.get_label() not in ("DAM", "PV", "Load"):
            continue  # the nominal axhline spans the axis by design
        pts = ax.transData.transform(
            np.column_stack([line.get_xdata(), line.get_ydata()]),
        )
        inside = (
            (pts[:, 0] >= lbox.x0) & (pts[:, 0] <= lbox.x1)
            & (pts[:, 1] >= lbox.y0) & (pts[:, 1] <= lbox.y1)
        )
        assert not np.any(inside), (
            f"coverage legend covers vertices of {line.get_label()!r}"
        )
