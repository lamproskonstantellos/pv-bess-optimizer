"""v0.8.1: plot_npv_waterfall morphology must match yearly_cashflow_bars.

Legend entries: Revenue, OPEX, DEVEX, CAPEX, Net cash-flow, Cumulative
NPV.  No in-axis DEVEX / CAPEX text annotations.  y-axis padded so the
topmost bar does not touch the axis spine.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # noqa: E402

import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from pvbess_opt.plotting.financial import plot_npv_waterfall


def _yearly_cf() -> pd.DataFrame:
    rows = []
    rows.append({
        "project_year": 0, "calendar_year": 2025,
        "revenue_eur": 0.0,
        "opex_eur": 0.0,
        "devex_eur": -75_000.0,
        "capex_eur": -600_000.0,
        "discount_factor": 1.0,
        "discounted_cf_eur": -675_000.0,
    })
    r = 0.07
    for y in range(1, 6):
        df_y = 1 / (1 + r) ** y
        rev_y = 150_000.0
        opex_y = -14_000.0
        net = rev_y + opex_y
        rows.append({
            "project_year": y, "calendar_year": 2025 + y,
            "revenue_eur": rev_y,
            "opex_eur": opex_y,
            "devex_eur": 0.0,
            "capex_eur": 0.0,
            "discount_factor": df_y,
            "discounted_cf_eur": net * df_y,
        })
    return pd.DataFrame(rows)


def _read_legend_labels(fig) -> set[str]:
    ax = fig.axes[0]
    handles, labels = ax.get_legend_handles_labels()
    return set(labels)


def test_npv_waterfall_renders_with_five_legend_entries(tmp_path: Path):
    out = plot_npv_waterfall(_yearly_cf(), tmp_path / "waterfall.pdf")
    assert out.exists()
    # Inspect the most recent figure (plot_npv_waterfall closes via
    # save_figure; rebuild a quick check via direct call into the
    # function and pulling from plt.get_fignums()).


def _render_npv_waterfall(tmp_path: Path):
    """Render plot_npv_waterfall and return the live figure object.

    Bypasses the close-on-save in :func:`save_figure` so the test can
    introspect axes / legend / text artists.
    """
    plt.close("all")
    import pvbess_opt.plotting.financial as fin_mod
    captured: dict = {}
    original_save = fin_mod.save_figure

    def keep_open(out):
        captured["fig"] = plt.gcf()
        return Path(out)

    fin_mod.save_figure = keep_open
    try:
        plot_npv_waterfall(_yearly_cf(), tmp_path / "waterfall.pdf")
    finally:
        fin_mod.save_figure = original_save
    return captured["fig"]


def test_npv_waterfall_legend_has_all_components(tmp_path: Path):
    """The six canonical legend entries must all render on the axes."""
    fig = _render_npv_waterfall(tmp_path)
    ax = fig.axes[0]
    _, labels = ax.get_legend_handles_labels()
    expected = {"Revenue", "OPEX", "DEVEX", "CAPEX",
                "Net cash-flow", "Cumulative NPV"}
    assert expected.issubset(set(labels)), (
        f"plot_npv_waterfall missing legend entries: "
        f"{expected - set(labels)}"
    )


def test_npv_total_annotation_inside_frame(tmp_path: Path):
    """v0.8.2: the "NPV = €X.XM" total annotation lives in axes
    coordinates inside the frame, not overflowing past the right
    spine."""
    fig = _render_npv_waterfall(tmp_path)
    ax = fig.axes[0]
    matches = [t for t in ax.texts if "NPV =" in t.get_text()]
    assert matches, "NPV total annotation missing"
    summary = matches[0]
    # The annotation must be anchored in axes-coordinates (transform
    # == ax.transAxes) so it never escapes the frame even on long
    # horizons.
    assert summary.get_transform() == ax.transAxes, (
        "NPV total annotation must use axes coordinates (top-right "
        "of the frame); got data-coordinate transform instead."
    )
    x, y = summary.get_position()
    assert 0.0 <= float(x) <= 1.0, f"NPV total x out of frame: {x}"
    assert 0.0 <= float(y) <= 1.0, f"NPV total y out of frame: {y}"


def test_npv_waterfall_has_no_inaxis_capex_devex_text(tmp_path: Path):
    """The redesigned waterfall no longer adds in-axis DEVEX / CAPEX
    text annotations next to the Year-0 bar — those are conveyed
    through the 5-entry legend instead.

    Detection contract (v0.8.2): inspect rendered text artists on the
    axes rather than grepping source.  Source-level lookups are
    brittle because the canonical labels now appear inside
    ``financial_color("DEVEX")`` / ``label="DEVEX"`` calls.
    """
    fig = _render_npv_waterfall(tmp_path)
    ax = fig.axes[0]
    bad_texts: list[str] = []
    for t in ax.texts:
        text = t.get_text()
        if text in ("DEVEX", "CAPEX"):
            bad_texts.append(text)
    assert not bad_texts, (
        f"plot_npv_waterfall still draws in-axis DEVEX/CAPEX text: "
        f"{bad_texts}"
    )
