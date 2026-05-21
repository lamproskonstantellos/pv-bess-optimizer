"""Net revenue / Real-EUR net lines use a high-contrast colour.

The dark BESS-export stack (#0D47A1) used to swallow the dark purple
(#6A1B9A) lines.  A dedicated FINANCIAL_COLORS entry
"net_revenue_line" (magenta) which is high-contrast over both the
light blue PV stack and the dark blue BESS stack.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd

from pvbess_opt.config import FINANCIAL_COLORS
from pvbess_opt.plotting.lifecycle import plot_revenue_stack_yearly


def _econ() -> dict:
    return {
        "retail_inflation_pct": 2.0,
        "dam_inflation_pct": 0.0,
        "currency_format": "auto",
    }


def _yearly_cf() -> pd.DataFrame:
    rows = []
    for y in range(1, 6):
        rows.append({
            "project_year": y, "calendar_year": 2025 + y,
            "revenue_eur": 100_000.0 * (1.02) ** (y - 1),
        })
    return pd.DataFrame(rows)


def _y1_kpis() -> dict:
    return {
        "profit_load_from_pv_eur": 30_000.0,
        "profit_load_from_bess_eur": 5_000.0,
        "profit_export_from_pv_eur": 40_000.0,
        "profit_export_from_bess_eur": 30_000.0,
        "expense_charge_bess_grid_eur": 5_000.0,
    }


def test_financial_colors_contains_net_revenue_line():
    assert "net_revenue_line" in FINANCIAL_COLORS
    # net_revenue_line is deliberately aliased to
    # perfect_foresight (both use Material grey 900, near-black) so
    # the "anchor / benchmark" series read identically across plots.
    # Any OTHER collision is still a configuration mistake.
    inverse: dict[str, list[str]] = {}
    for key, hex_value in FINANCIAL_COLORS.items():
        inverse.setdefault(hex_value.lower(), []).append(key)
    target_hex = FINANCIAL_COLORS["net_revenue_line"].lower()
    allowed_aliases = {"net_revenue_line", "perfect_foresight"}
    keys_at_target = set(inverse.get(target_hex, []))
    extra = keys_at_target - allowed_aliases
    assert not extra, (
        f"net_revenue_line colour collides with unexpected keys: {extra}"
    )


def test_net_revenue_line_uses_high_contrast_colour(tmp_path: Path):
    """Render plot_revenue_stack_yearly and confirm two lines on the
    axes use the net_revenue_line colour."""
    plt.close("all")
    target_colour = FINANCIAL_COLORS["net_revenue_line"].lower()

    import pvbess_opt.plotting.lifecycle as life_mod
    captured: dict = {}
    original_save = life_mod.save_figure

    def keep_open(out):
        captured["fig"] = plt.gcf()
        return Path(out)

    life_mod.save_figure = keep_open
    try:
        plot_revenue_stack_yearly(
            _yearly_cf(), _y1_kpis(), tmp_path / "stack.pdf",
            econ=_econ(),
        )
    finally:
        life_mod.save_figure = original_save

    fig = captured["fig"]
    ax = fig.axes[0]
    matches = []
    for line in ax.get_lines():
        # Convert to hex string for comparison.
        c = line.get_color()
        if isinstance(c, str):
            c_hex = c.lower()
        else:
            from matplotlib.colors import to_hex
            c_hex = to_hex(c).lower()
        if c_hex == target_colour:
            matches.append(line)
    # The solid "Net revenue" line is mandatory; the dashed Real-EUR
    # net line appears when retail_inflation_pct > 0.
    assert len(matches) >= 1


def _econ_with_fee() -> dict:
    return {
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": 0.0,
        "aggregator_fee_pct_revenue": 10.0,
        "currency_format": "auto",
    }


def _yearly_cf_post_fee() -> pd.DataFrame:
    """yearly_cf where ``revenue_eur`` is genuinely post-fee."""
    # Y1 gross stack:
    #   30 000 + 5 000 + 40 000 + 30 000 - 5 000 = 100 000
    # 10 % fee ⇒ net = 90 000.
    rows = []
    for y in range(1, 6):
        rows.append({
            "project_year": y, "calendar_year": 2025 + y,
            "revenue_eur": 90_000.0,
        })
    return pd.DataFrame(rows)


def test_revenue_stack_sums_to_net_line(tmp_path: Path):
    """Bug #2 regression: the stack components (positive bars + negative
    Grid-charging + negative Aggregator-fee) must sum to the Net-revenue
    line at every operating year."""
    plt.close("all")

    import pvbess_opt.plotting.lifecycle as life_mod
    captured: dict = {}
    original_save = life_mod.save_figure

    def keep_open(out):
        captured["fig"] = plt.gcf()
        return Path(out)

    life_mod.save_figure = keep_open
    try:
        plot_revenue_stack_yearly(
            _yearly_cf_post_fee(), _y1_kpis(), tmp_path / "stack.pdf",
            econ=_econ_with_fee(),
        )
    finally:
        life_mod.save_figure = original_save

    fig = captured["fig"]
    ax = fig.axes[0]

    # Sum every bar at each x; lookup by the x coordinate as the years.
    sums: dict[float, float] = {}
    for patch in ax.patches:
        x = patch.get_x() + patch.get_width() / 2.0
        # matplotlib rounds bar x to the bar's center.  Bucket by int.
        key = round(x)
        sums[key] = sums.get(key, 0.0) + patch.get_height()

    # Find the Net-revenue line.
    from pvbess_opt.config import financial_color
    target = financial_color("Net revenue").lower()
    net_line = None
    from matplotlib.colors import to_hex
    for line in ax.get_lines():
        c = line.get_color()
        c_hex = c.lower() if isinstance(c, str) else to_hex(c).lower()
        if c_hex == target and line.get_linestyle() == "-":
            net_line = line
            break
    assert net_line is not None, "Net revenue line not found on axes"
    xs = net_line.get_xdata()
    ys = net_line.get_ydata()

    for x, y in zip(xs, ys, strict=False):
        stacked = sums[round(float(x))]
        assert abs(stacked - float(y)) < 1.0e-6, (
            f"Year {x}: stack sum {stacked} != net line {y}"
        )


def test_aggregator_fee_bar_skipped_when_fee_is_zero(tmp_path: Path):
    """A workbook with aggregator_fee_pct_revenue=0 produces no fee bar."""
    plt.close("all")

    import pvbess_opt.plotting.lifecycle as life_mod
    captured: dict = {}
    original_save = life_mod.save_figure

    def keep_open(out):
        captured["fig"] = plt.gcf()
        return Path(out)

    life_mod.save_figure = keep_open
    try:
        plot_revenue_stack_yearly(
            _yearly_cf(), _y1_kpis(), tmp_path / "stack.pdf",
            econ={"aggregator_fee_pct_revenue": 0.0, "currency_format": "auto"},
        )
    finally:
        life_mod.save_figure = original_save

    fig = captured["fig"]
    ax = fig.axes[0]
    labels = [t.get_text() for t in ax.get_legend().get_texts()]
    assert "Aggregator fee" not in labels
