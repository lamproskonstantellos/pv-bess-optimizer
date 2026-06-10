"""Negative bar segments must STACK, never overlap, in every year.

In a BESS replacement year both OPEX and CAPEX are non-zero while DEVEX
is zero.  The CAPEX bar was previously drawn with ``bottom=devex`` (= 0
there), so it was painted from 0 over the OPEX bar at the same x — the
OPEX segment vanished inside the CAPEX block and the visible stack
understated the year's outflow.  Both bar charts now stack the negative
segments cumulatively (OPEX from 0, DEVEX below it, CAPEX below both),
so for every year the union of segment extents is contiguous and the
stack bottom equals opex + devex + capex.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
import pytest

import pvbess_opt.plotting.financial as fin_mod
from pvbess_opt.plotting.financial import (
    plot_npv_waterfall,
    plot_yearly_cashflow_bars,
)


def _yearly_cf_with_replacement() -> pd.DataFrame:
    """5-year cashflow with a year-3 BESS replacement (OPEX + CAPEX)."""
    rows = [{
        "project_year": 0, "calendar_year": 2025,
        "revenue_eur": 0.0, "opex_eur": 0.0,
        "devex_eur": -75_000.0, "capex_eur": -600_000.0,
        "net_cashflow_eur": -675_000.0,
        "discount_factor": 1.0, "discounted_cf_eur": -675_000.0,
    }]
    r = 0.07
    for y in range(1, 6):
        df_y = 1 / (1 + r) ** y
        capex_y = -150_000.0 if y == 3 else 0.0
        rev_y, opex_y = 150_000.0, -14_000.0
        net = rev_y + opex_y + capex_y
        rows.append({
            "project_year": y, "calendar_year": 2025 + y,
            "revenue_eur": rev_y, "opex_eur": opex_y,
            "devex_eur": 0.0, "capex_eur": capex_y,
            "net_cashflow_eur": net,
            "discount_factor": df_y, "discounted_cf_eur": net * df_y,
        })
    return pd.DataFrame(rows)


def _render(plot_fn, tmp_path: Path):
    """Render and return the live figure (bypass close-on-save)."""
    plt.close("all")
    captured: dict = {}
    original_save = fin_mod.save_figure

    def keep_open(out):
        captured["fig"] = plt.gcf()
        return Path(out)

    fin_mod.save_figure = keep_open
    try:
        plot_fn(_yearly_cf_with_replacement(), tmp_path / "plot.pdf")
    finally:
        fin_mod.save_figure = original_save
    return captured["fig"]


def _negative_segments_by_x(ax) -> dict[float, list[tuple[float, float]]]:
    """Map bar-centre x -> list of (top, bottom) extents of negative bars."""
    out: dict[float, list[tuple[float, float]]] = {}
    for container in getattr(ax, "containers", []):
        for patch in container:
            h = patch.get_height()
            if h >= -1e-9:  # only negative segments
                continue
            x = round(patch.get_x() + patch.get_width() / 2.0, 6)
            y0 = patch.get_y()           # segment top (bottom kwarg)
            out.setdefault(x, []).append((y0, y0 + h))
    return out


@pytest.mark.parametrize(
    "plot_fn,discounted",
    [(plot_yearly_cashflow_bars, False), (plot_npv_waterfall, True)],
)
def test_negative_bars_stack_without_overlap(plot_fn, discounted, tmp_path):
    cf = _yearly_cf_with_replacement()
    fig = _render(plot_fn, tmp_path)
    ax = fig.axes[0]
    segments = _negative_segments_by_x(ax)
    assert segments, "no negative bar segments rendered"

    factor = cf.set_index("calendar_year")["discount_factor"]
    expected_bottom = {
        int(row["calendar_year"]): (
            (row["opex_eur"] + row["devex_eur"] + row["capex_eur"])
            * (row["discount_factor"] if discounted else 1.0)
        )
        for _, row in cf.iterrows()
    }

    for x, segs in segments.items():
        # 1) no two negative segments at the same x may overlap.
        ordered = sorted(segs, key=lambda s: s[0], reverse=True)
        for (_top_a, bot_a), (top_b, _bot_b) in pairwise(ordered):
            assert top_b <= bot_a + 1e-6, (
                f"overlapping negative segments at x={x}: "
                f"{ordered} (segment starting {top_b} overlaps one "
                f"ending {bot_a})"
            )
        # 2) the stack reaches exactly opex + devex + capex for that year.
        year = round(x)
        stack_bottom = min(b for _t, b in segs)
        assert stack_bottom == pytest.approx(
            expected_bottom[year], rel=1e-9, abs=1e-6,
        ), f"stack bottom at {year} != opex+devex+capex"
    _ = factor  # silence unused in non-discounted branch


def test_replacement_year_opex_remains_visible(tmp_path):
    """The replacement year must show BOTH an OPEX and a CAPEX segment,
    with CAPEX starting exactly where OPEX ends."""
    fig = _render(plot_yearly_cashflow_bars, tmp_path)
    ax = fig.axes[0]
    segs = _negative_segments_by_x(ax)[2028.0]
    assert len(segs) == 2
    ordered = sorted(segs, key=lambda s: s[0], reverse=True)
    (opex_top, opex_bot), (capex_top, capex_bot) = ordered
    assert opex_top == pytest.approx(0.0, abs=1e-9)
    assert opex_bot == pytest.approx(-14_000.0, abs=1e-6)
    assert capex_top == pytest.approx(-14_000.0, abs=1e-6)
    assert capex_bot == pytest.approx(-164_000.0, abs=1e-6)
