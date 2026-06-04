"""Revenue stack reconciles to the net-revenue line when balancing is on.

These cases used to fail because ``plot_revenue_stack_yearly`` scaled
balancing-product bars by the DAM ratio:

* Self-consumption only (Year-1 DAM = 0) → balancing bars collapsed to
  zero while the net line still added the balancing total.
* ``bm_inflation_pct`` != ``dam_inflation_pct`` → balancing bars
  drifted from the actual cashflow row monotonically.

The fix scales balancing bars by the BESS capacity-fade factor indexed
by ``bm_inflation_pct``, mirroring :func:`build_yearly_cashflow`.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd

from pvbess_opt.plotting.lifecycle import plot_revenue_stack_yearly
from pvbess_opt.theme import financial_color


def _stack_sums_and_net_line(fig) -> tuple[dict[int, float], dict[int, float]]:
    ax = fig.axes[0]
    sums: dict[int, float] = {}
    for patch in ax.patches:
        x = patch.get_x() + patch.get_width() / 2.0
        key = round(x)
        sums[key] = sums.get(key, 0.0) + patch.get_height()

    from matplotlib.colors import to_hex
    target = financial_color("Net revenue").lower()
    net_line = None
    for line in ax.get_lines():
        c = line.get_color()
        c_hex = c.lower() if isinstance(c, str) else to_hex(c).lower()
        if c_hex == target and line.get_linestyle() == "-":
            net_line = line
            break
    assert net_line is not None
    xs = net_line.get_xdata()
    ys = net_line.get_ydata()
    line_vals = {round(float(x)): float(y) for x, y in zip(xs, ys, strict=False)}
    return sums, line_vals


def _capture_fig(yearly_cf: pd.DataFrame, year1_kpis: dict, econ: dict, tmp_path: Path):
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
            yearly_cf, year1_kpis, tmp_path / "stack.pdf", econ=econ,
        )
    finally:
        life_mod.save_figure = original_save
    return captured["fig"]


def _yearly_cf_self_consumption_balancing() -> pd.DataFrame:
    """Self-consumption only (retail-only), balancing enabled."""
    rows = []
    retail_y1 = 50_000.0
    bm_y1 = 20_000.0
    bess_factors = [1.0, 0.99, 0.98, 0.97, 0.96]
    for i, y in enumerate(range(1, 6)):
        bess_factor = bess_factors[i]
        retail_y = retail_y1 * (1.02) ** (y - 1)
        bm_y = bm_y1 * bess_factor * (1.0 + 0.02) ** (y - 1)
        rows.append({
            "project_year": y, "calendar_year": 2025 + y,
            "revenue_eur": retail_y,
            "revenue_retail_eur": retail_y,
            "revenue_dam_eur": 0.0,
            "balancing_revenue_eur": bm_y,
            "bess_capacity_factor": bess_factor,
            "aggregator_fee_eur": 0.0,
        })
    return pd.DataFrame(rows)


def _y1_kpis_self_consumption_balancing() -> dict:
    # retail Y1 = 35_000 + 15_000 = 50_000 matches the cashflow above.
    return {
        "profit_load_from_pv_eur": 35_000.0,
        "profit_load_from_bess_eur": 15_000.0,
        "profit_export_from_pv_eur": 0.0,
        "profit_export_from_bess_eur": 0.0,
        "expense_charge_bess_grid_eur": 0.0,
        # Balancing per-product Y1 — sum to 20_000 to match
        # _yearly_cf_self_consumption_balancing()'s bm_y1.
        "revenue_bess_fcr_eur": 8_000.0,
        "revenue_bess_afrr_up_eur": 4_000.0,
        "revenue_bess_afrr_dn_eur": 3_000.0,
        "revenue_bess_mfrr_up_eur": 3_000.0,
        "revenue_bess_mfrr_dn_eur": 2_000.0,
    }


def _y1_kpis_dam_plus_balancing() -> dict:
    # retail Y1 = 30_000, DAM Y1 = 40_000 (matches
    # _yearly_cf_dam_plus_balancing).  Balancing total Y1 = 20_000.
    return {
        "profit_load_from_pv_eur": 20_000.0,
        "profit_load_from_bess_eur": 10_000.0,
        "profit_export_from_pv_eur": 25_000.0,
        "profit_export_from_bess_eur": 15_000.0,
        "expense_charge_bess_grid_eur": 0.0,
        "revenue_bess_fcr_eur": 8_000.0,
        "revenue_bess_afrr_up_eur": 4_000.0,
        "revenue_bess_afrr_dn_eur": 3_000.0,
        "revenue_bess_mfrr_up_eur": 3_000.0,
        "revenue_bess_mfrr_dn_eur": 2_000.0,
    }


def test_self_consumption_balancing_reconciles_to_net_line(tmp_path: Path):
    """Year-1 DAM = 0, balancing on: the stack must sum to the net line."""
    econ = {
        "retail_inflation_pct": 2.0,
        "dam_inflation_pct": 0.0,
        "bm_inflation_pct": 2.0,
        "currency_format": "auto",
    }
    fig = _capture_fig(
        _yearly_cf_self_consumption_balancing(),
        _y1_kpis_self_consumption_balancing(),
        econ, tmp_path,
    )
    sums, line_vals = _stack_sums_and_net_line(fig)
    for x, expected in line_vals.items():
        assert abs(sums[x] - expected) < 1.0, (
            f"Year {x}: stack sum {sums[x]} != net line {expected}"
        )


def _yearly_cf_dam_plus_balancing(bm_inflation: float, dam_inflation: float):
    """Mixed DAM + balancing scenario with explicit inflation knobs."""
    rows = []
    retail_y1 = 30_000.0
    dam_y1 = 40_000.0
    bm_y1 = 20_000.0
    bess_factors = [1.0, 0.99, 0.98, 0.97, 0.96]
    for i, y in enumerate(range(1, 6)):
        bess_factor = bess_factors[i]
        retail_y = retail_y1 * (1.02) ** (y - 1)
        dam_y = dam_y1 * bess_factor * (1.0 + dam_inflation) ** (y - 1)
        bm_y = bm_y1 * bess_factor * (1.0 + bm_inflation) ** (y - 1)
        rows.append({
            "project_year": y, "calendar_year": 2025 + y,
            "revenue_eur": retail_y + dam_y,
            "revenue_retail_eur": retail_y,
            "revenue_dam_eur": dam_y,
            "balancing_revenue_eur": bm_y,
            "bess_capacity_factor": bess_factor,
            "aggregator_fee_eur": 0.0,
        })
    return pd.DataFrame(rows)


def test_balancing_bars_track_bm_inflation_not_dam(tmp_path: Path):
    """With ``bm_inflation_pct=2`` and ``dam_inflation_pct=0`` the Year-5
    balancing bar heights must match ``balancing_revenue_eur[5]`` from
    the cashflow, not the Year-1 value scaled by the DAM ratio."""
    bm_infl = 0.02
    dam_infl = 0.00
    cf = _yearly_cf_dam_plus_balancing(bm_inflation=bm_infl, dam_inflation=dam_infl)
    econ = {
        "retail_inflation_pct": 0.0,
        "dam_inflation_pct": dam_infl * 100.0,
        "bm_inflation_pct": bm_infl * 100.0,
        "currency_format": "auto",
    }
    fig = _capture_fig(cf, _y1_kpis_dam_plus_balancing(), econ, tmp_path)

    sums, line_vals = _stack_sums_and_net_line(fig)
    for x, expected in line_vals.items():
        assert abs(sums[x] - expected) < 1.0, (
            f"Year {x}: stack sum {sums[x]} != net line {expected}"
        )

    # Independently verify Year-5 balancing-bar total equals the
    # cashflow's balancing_revenue_eur[5] (within rounding).  We
    # identify balancing bars by their canonical colors.
    ax = fig.axes[0]
    from matplotlib.colors import to_hex
    bm_colors = {
        financial_color(lbl).lower()
        for lbl in ("FCR", "aFRR-up", "aFRR-dn", "mFRR-up", "mFRR-dn")
    }
    expected_bm5 = float(
        cf.loc[cf["project_year"] == 5, "balancing_revenue_eur"].iloc[0]
    )
    year5_x = int(cf.loc[cf["project_year"] == 5, "calendar_year"].iloc[0])
    bm_bar_sum = 0.0
    for patch in ax.patches:
        x = round(patch.get_x() + patch.get_width() / 2.0)
        if x != year5_x:
            continue
        fc = patch.get_facecolor()
        c_hex = to_hex(fc).lower()
        if c_hex in bm_colors:
            bm_bar_sum += patch.get_height()
    assert abs(bm_bar_sum - expected_bm5) < 1.0, (
        f"Year-5 balancing bars sum to {bm_bar_sum} but cashflow says {expected_bm5}"
    )
