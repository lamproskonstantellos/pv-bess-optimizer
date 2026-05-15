"""Enumeration tests — verify every plotting function obeys the
round-3 universality rules.  These tests iterate over the full list
of public plotting functions and apply the same assertions to each
one.

If a new plotting function is added without registering it here, the
``test_all_plotting_functions_registered`` guard will fail.
"""

from __future__ import annotations

import inspect
import re

import pytest

from pvbess_opt import plotting as _plotting

# Enumerate every public plotting function across the subpackage.
PLOTTING_FUNCTIONS: tuple[str, ...] = (
    # Energy
    "plot_daily_supply", "plot_daily_surplus", "plot_daily_combined",
    "plot_daily_combined_merchant",
    "plot_daily_combined_with_soc",
    "plot_daily_combined_merchant_with_soc",
    "plot_daily_dispatch", "plot_daily_soc", "plot_daily_revenue",
    "plot_monthly_supply", "plot_monthly_surplus", "plot_monthly_combined",
    "plot_monthly_combined_merchant",
    "plot_monthly_dispatch", "plot_monthly_soc", "plot_monthly_revenue",
    "plot_yearly_supply", "plot_yearly_surplus", "plot_yearly_combined",
    "plot_yearly_combined_merchant",
    "plot_yearly_dispatch", "plot_yearly_soc", "plot_yearly_revenue",
    "plot_lifetime_summary",
    # Financial
    "plot_cumulative_cashflow", "plot_yearly_cashflow_bars",
    "plot_npv_waterfall", "plot_payback", "plot_monthly_cashflow_year1",
    "plot_npv_tornado", "plot_irr_tornado",
    # Lifecycle
    "plot_revenue_stack_yearly", "plot_lifetime_cycles",
    "plot_lcoe_summary", "plot_lcos_summary",
    # Uncertainty
    "plot_rolling_horizon_distribution", "plot_foresight_gap_comparison",
    # Inputs
    "plot_input_forecast_band", "plot_input_seasonal_boxplot",
    "plot_dam_intraday_heatmap",
)


def test_all_plotting_functions_registered():
    """If a new plotting function is added to the public API, it must
    also be registered here.  Catches accidental drift."""
    exported = {
        name for name in dir(_plotting)
        if name.startswith("plot_") and callable(getattr(_plotting, name))
    }
    registered = set(PLOTTING_FUNCTIONS)
    missing = exported - registered
    extra = registered - exported
    assert not missing, (
        f"New plotting functions not registered for universality "
        f"tests: {sorted(missing)}"
    )
    assert not extra, (
        f"Stale entries in universality registry: {sorted(extra)}"
    )


@pytest.mark.parametrize("fn_name", PLOTTING_FUNCTIONS)
def test_no_white_marker_edge(fn_name):
    """No plotting function should pass markeredgecolor='white'."""
    fn = getattr(_plotting, fn_name)
    src = inspect.getsource(fn)
    # Strip comments so historical/explanatory mentions in comments
    # are exempt; only live keyword arguments count.
    src_no_comments = re.sub(r"#.*$", "", src, flags=re.MULTILINE)
    assert 'markeredgecolor="white"' not in src_no_comments, (
        f"{fn_name}: white marker-edge ring not allowed"
    )
    assert "markeredgecolor='white'" not in src_no_comments


@pytest.mark.parametrize("fn_name", PLOTTING_FUNCTIONS)
def test_no_inline_hex_colour(fn_name):
    """Plotting functions must source colours from config, not hex."""
    fn = getattr(_plotting, fn_name)
    src = inspect.getsource(fn)
    # Strip comments first so example hex values in comments are exempt.
    src_no_comments = re.sub(r"#.*$", "", src, flags=re.MULTILINE)
    matches = re.findall(r'["\']#[0-9A-Fa-f]{6}["\']', src_no_comments)
    assert not matches, (
        f"{fn_name}: inline hex colour literal(s) found: {matches}. "
        "Use FINANCIAL_COLORS / financial_color / COLORS instead."
    )


@pytest.mark.parametrize("fn_name", PLOTTING_FUNCTIONS)
def test_no_inline_annotate_with_bbox_and_value(fn_name):
    """Every bbox-wrapped value annotation must go through
    annotate_value_safe."""
    fn = getattr(_plotting, fn_name)
    src = inspect.getsource(fn)
    pattern = re.compile(
        r'\bax\d*\.(annotate|text)\([^)]*bbox\s*=',
        re.DOTALL,
    )
    matches = pattern.findall(src)
    assert not matches, (
        f"{fn_name}: raw ax.annotate/ax.text with bbox found. "
        "Refactor through annotate_value_safe."
    )


@pytest.mark.parametrize("fn_name", PLOTTING_FUNCTIONS)
def test_no_italic_prose_captions(fn_name):
    """fontstyle='italic' is only allowed for explicitly-marked
    in-legend / value-label use — never as a free-floating caption."""
    fn = getattr(_plotting, fn_name)
    src = inspect.getsource(fn)
    matches = re.findall(r'fontstyle\s*=\s*["\']italic["\']', src)
    if matches:
        doc = (fn.__doc__ or "").lower()
        if "italic allowed" not in doc:
            pytest.fail(
                f"{fn_name}: fontstyle='italic' present.  If this is an "
                "intentional value-axis label, add 'italic allowed' to "
                "the docstring.  Otherwise remove it."
            )


@pytest.mark.parametrize("fn_name", [
    "plot_monthly_supply", "plot_monthly_surplus", "plot_monthly_combined",
    "plot_monthly_combined_merchant",
    "plot_monthly_dispatch", "plot_monthly_soc", "plot_monthly_revenue",
])
def test_monthly_uses_dd_mm_yyyy_format(fn_name):
    """Every monthly plot uses _setup_day_axis (DD-MM-YYYY)."""
    fn = getattr(_plotting, fn_name)
    src = inspect.getsource(fn)
    has_helper = "_setup_day_axis" in src
    has_format = '"%d-%m-%Y"' in src or "'%d-%m-%Y'" in src
    assert has_helper or has_format, (
        f"{fn_name}: must use _setup_day_axis or '%d-%m-%Y' format"
    )


@pytest.mark.parametrize("fn_name", [
    "plot_yearly_supply", "plot_yearly_surplus", "plot_yearly_combined",
    "plot_yearly_combined_merchant",
    "plot_yearly_dispatch", "plot_yearly_soc", "plot_yearly_revenue",
])
def test_yearly_uses_mm_yyyy_format(fn_name):
    """Every yearly plot uses _setup_month_axis (MM-YYYY)."""
    fn = getattr(_plotting, fn_name)
    src = inspect.getsource(fn)
    has_helper = "_setup_month_axis" in src
    has_format = '"%m-%Y"' in src or "'%m-%Y'" in src
    assert has_helper or has_format, (
        f"{fn_name}: must use _setup_month_axis or '%m-%Y' format"
    )


@pytest.mark.parametrize(
    "fn_name",
    ["plot_monthly_soc", "plot_yearly_soc", "plot_daily_soc"],
)
def test_soc_plots_have_dual_axis(fn_name):
    """SOC plots must include both SOC (%) and SOC (kWh) axes.

    Only the SOC-only plots fall under this convention.  The combined
    energy + SOC plots (``plot_daily_combined_with_soc`` and its
    merchant sibling) follow a different convention — Energy on the
    left, SOC (%) on the right — so they are intentionally excluded.
    """
    fn = getattr(_plotting, fn_name)
    src = inspect.getsource(fn)
    assert "twinx" in src, (
        f"{fn_name}: must include ax.twinx() for the SOC (kWh) right axis"
    )
    assert '"SOC (%)"' in src or "'SOC (%)'" in src, (
        f"{fn_name}: must label the left axis 'SOC (%)'"
    )
    assert '"SOC (kWh)"' in src or "'SOC (kWh)'" in src, (
        f"{fn_name}: must label the right axis 'SOC (kWh)'"
    )


@pytest.mark.parametrize("fn_name", PLOTTING_FUNCTIONS)
def test_apply_universal_margins_called(fn_name):
    """Every plotting function must call ``apply_universal_margins``
    on its axes, unless its docstring carries the explicit marker
    ``margins: delegated`` (used when padding is set inside a shared
    helper).
    """
    fn = getattr(_plotting, fn_name)
    src = inspect.getsource(fn)
    doc = (fn.__doc__ or "").lower()
    if "margins: delegated" in doc:
        return
    assert "apply_universal_margins" in src, (
        f"{fn_name}: must call apply_universal_margins(ax) before "
        "save_figure, or carry 'margins: delegated' in its docstring."
    )
