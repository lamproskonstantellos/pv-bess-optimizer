"""Financial-plot label / colour / legend-order consistency.

Centralization contract: every label drawn on a financial /
lifecycle / uncertainty plot must come from
:data:`pvbess_opt.theme.FINANCIAL_LABELS`, with its colour resolved
through :func:`pvbess_opt.theme.financial_color` and its legend
order driven by :func:`pvbess_opt.theme.apply_financial_legend`.

The pattern mirrors ``ALL_LABELS`` / ``COLORS`` / ``LEGEND_ORDER``
already used by the energy plots.
"""

from __future__ import annotations

import logging
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
import pytest

from pvbess_opt.plotting.financial import (
    plot_cumulative_cashflow,
    plot_dscr_profile,
    plot_monthly_cashflow_year1,
    plot_npv_waterfall,
    plot_payback,
    plot_yearly_cashflow_bars,
)
from pvbess_opt.plotting.lifecycle import plot_revenue_stack_yearly
from pvbess_opt.theme import (
    FINANCIAL_COLORS,
    FINANCIAL_LABEL_TO_COLOR_KEY,
    FINANCIAL_LABELS,
    FINANCIAL_LEGEND_ORDER,
    apply_financial_legend,
    financial_color,
)

# ---------------------------------------------------------------------------
# Static contracts
# ---------------------------------------------------------------------------


def test_every_financial_label_has_a_colour():
    for lab in FINANCIAL_LABELS:
        c = financial_color(lab)
        assert isinstance(c, str) and c.startswith("#"), (
            f"financial_color({lab!r}) returned {c!r}"
        )


def test_financial_label_to_color_key_covers_every_label():
    for lab in FINANCIAL_LABELS:
        assert lab in FINANCIAL_LABEL_TO_COLOR_KEY, (
            f"FINANCIAL_LABEL_TO_COLOR_KEY missing {lab!r}"
        )
        assert FINANCIAL_LABEL_TO_COLOR_KEY[lab] in FINANCIAL_COLORS


def test_financial_legend_order_covers_all_labels():
    assert set(FINANCIAL_LABELS) == set(FINANCIAL_LEGEND_ORDER)


def test_financial_color_raises_on_unknown_label():
    with pytest.raises(ValueError, match="not canonical"):
        financial_color("Made-up label")


# ---------------------------------------------------------------------------
# Render-time contracts
# ---------------------------------------------------------------------------


def _yearly_cf() -> pd.DataFrame:
    rows = [{
        "project_year": 0, "calendar_year": 2025,
        "revenue_eur": 0.0, "opex_eur": 0.0,
        "capex_eur": -600_000.0, "devex_eur": -75_000.0,
        "net_cashflow_eur": -675_000.0,
        "discount_factor": 1.0, "discounted_cf_eur": -675_000.0,
        "cumulative_cf_eur": -675_000.0,
        "cumulative_dcf_eur": -675_000.0,
    }]
    r = 0.07
    cum = -675_000.0
    cum_d = -675_000.0
    for y in range(1, 6):
        df_y = 1.0 / (1.0 + r) ** y
        net = 150_000.0
        cum += net
        cum_d += net * df_y
        rows.append({
            "project_year": y, "calendar_year": 2025 + y,
            "revenue_eur": 150_000.0, "opex_eur": -10_000.0,
            "capex_eur": 0.0, "devex_eur": 0.0,
            "net_cashflow_eur": net,
            "discount_factor": df_y, "discounted_cf_eur": net * df_y,
            "cumulative_cf_eur": cum, "cumulative_dcf_eur": cum_d,
        })
    return pd.DataFrame(rows)


def _monthly_cf() -> pd.DataFrame:
    rows = []
    for m in range(1, 13):
        rows.append({
            "project_year": 1, "calendar_year": 2026, "period": m,
            "revenue_eur": 15_000.0,
            "opex_eur": -1_500.0,
            "net_cashflow_eur": 13_500.0,
        })
    return pd.DataFrame(rows)


def _debt_schedule() -> pd.DataFrame:
    return pd.DataFrame({
        "year": [1.0, 2.0, 3.0, 4.0, 5.0],
        "dscr": [1.6, 1.55, 1.5, 1.45, 1.4],
    })


def _y1_kpis() -> dict:
    return {
        "profit_load_from_pv_eur": 30_000.0,
        "profit_load_from_bess_eur": 5_000.0,
        "profit_export_from_pv_eur": 40_000.0,
        "profit_export_from_bess_eur": 30_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
    }


def _render_with_open_figure(plot_fn, *args, **kwargs):
    """Invoke a plot function but keep the figure open afterwards."""
    plt.close("all")
    import pvbess_opt.plotting.financial as fin_mod
    import pvbess_opt.plotting.lifecycle as life_mod
    captured: dict = {}
    original_fin_save = fin_mod.save_figure
    original_life_save = life_mod.save_figure

    def keep_open(out):
        captured["fig"] = plt.gcf()
        return Path(out)

    fin_mod.save_figure = keep_open
    life_mod.save_figure = keep_open
    try:
        plot_fn(*args, **kwargs)
    finally:
        fin_mod.save_figure = original_fin_save
        life_mod.save_figure = original_life_save
    return captured["fig"]


def test_monthly_cashflow_legend_uses_net_cash_flow(tmp_path):
    """The line label is "Net cash-flow", not "Net"."""
    fig = _render_with_open_figure(
        plot_monthly_cashflow_year1,
        _monthly_cf(), tmp_path / "monthly.pdf",
    )
    ax = fig.axes[0]
    _, labels = ax.get_legend_handles_labels()
    assert "Net cash-flow" in labels, (
        f"plot_monthly_cashflow_year1 must label its line "
        f"'Net cash-flow' (got {labels})"
    )
    assert "Net" not in labels, (
        f"unexpected bare 'Net' label in monthly cashflow legend "
        f"(got {labels})"
    )


def test_all_financial_plots_emit_only_canonical_labels(tmp_path, caplog):
    """Render every centralised financial plot and assert no
    non-canonical legend warnings are logged.

    Every rendered legend label is an exact canonical name; the
    prefix-match fallback for annotated labels is locked separately by
    :func:`test_apply_financial_legend_accepts_year_annotated_payback`.
    """
    yc = _yearly_cf()
    mc = _monthly_cf()
    kpis = _y1_kpis()

    plots = [
        (plot_cumulative_cashflow, (yc, tmp_path / "cum.pdf"), {}),
        (plot_yearly_cashflow_bars, (yc, tmp_path / "bars.pdf"), {}),
        (plot_npv_waterfall, (yc, tmp_path / "wf.pdf"), {}),
        (plot_payback, (yc, tmp_path / "pb.pdf"),
         {"simple_payback_years": 5.0, "discounted_payback_years": 7.0}),
        (plot_monthly_cashflow_year1, (mc, tmp_path / "monthly.pdf"), {}),
        (plot_revenue_stack_yearly, (yc, kpis, tmp_path / "stack.pdf"),
         {"econ": {"retail_inflation_pct": 2.0}}),
        (plot_dscr_profile, (_debt_schedule(), tmp_path / "dscr.pdf"),
         {"target_dscr": 1.3}),
    ]
    with caplog.at_level(logging.WARNING, logger="pvbess_opt.theme"):
        for fn, args, kwargs in plots:
            _render_with_open_figure(fn, *args, **kwargs)

    non_canonical = [
        rec for rec in caplog.records
        if rec.name == "pvbess_opt.theme"
        and "Non-canonical financial legend label" in rec.getMessage()
    ]
    assert not non_canonical, (
        "Non-canonical financial labels rendered:\n"
        + "\n".join(rec.getMessage() for rec in non_canonical)
    )


def test_payback_legend_labels_are_bare_canonical_names(tmp_path):
    """The payback markers carry the bare canonical names with no
    numeric annotation: the values live in SUMMARY.md and the KPI
    sheet, so the figure drops into a paper unchanged."""
    fig = _render_with_open_figure(
        plot_payback, _yearly_cf(), tmp_path / "pb.pdf",
        simple_payback_years=5.0, discounted_payback_years=7.0,
    )
    ax = fig.axes[0]
    _, labels = ax.get_legend_handles_labels()
    assert "Simple payback" in labels, labels
    assert "Discounted payback" in labels, labels
    assert not any("yr" in lbl or ":" in lbl for lbl in labels), labels


def test_net_revenue_line_uses_iee_charcoal():
    """IEEE emphasis colour swap (magenta -> charcoal)."""
    assert FINANCIAL_COLORS["net_revenue_line"] == "#212121"


def test_apply_financial_legend_orders_by_canonical_list():
    """Smoke-test the helper directly: legend order should follow
    :data:`FINANCIAL_LEGEND_ORDER` regardless of insertion order."""
    plt.close("all")
    fig, ax = plt.subplots()
    # Insert in reverse-canonical order — apply_financial_legend
    # must restore the canonical order.
    ax.plot([0, 1], [0, 1], color=financial_color("OPEX"), label="OPEX")
    ax.plot([0, 1], [1, 0], color=financial_color("Revenue"), label="Revenue")
    ax.plot([0, 1], [0, 0],
            color=financial_color("Net cash-flow"), label="Net cash-flow")
    apply_financial_legend(ax)
    leg = ax.get_legend()
    labels = [t.get_text() for t in leg.get_texts()]
    assert labels == ["Net cash-flow", "Revenue", "OPEX"]
    plt.close(fig)


def test_apply_financial_legend_accepts_year_annotated_payback():
    """Year-annotated labels like "Simple payback: 5.0 yr" should map
    to their canonical key via the prefix-match path — no warning."""
    plt.close("all")
    fig, ax = plt.subplots()
    ax.axvline(2030, color=financial_color("Simple payback"),
               label="Simple payback: 5.0 yr")
    ax.axvline(2032, color=financial_color("Discounted payback"),
               label="Discounted payback: 7.0 yr")
    apply_financial_legend(ax)
    leg = ax.get_legend()
    labels = [t.get_text() for t in leg.get_texts()]
    # Canonical order keeps Simple payback ahead of Discounted payback.
    assert labels == [
        "Simple payback: 5.0 yr",
        "Discounted payback: 7.0 yr",
    ]
    plt.close(fig)
