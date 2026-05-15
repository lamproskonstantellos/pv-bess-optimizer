"""IEEE matplotlib styling, PDF figure saving, and legend helpers.

All figures use the IEEE rcParams preset and are exported as PDF.  Plot
titles default to off (the figure caption in a paper plays that role) and
can be turned on via the ``show_titles`` key in the input workbook.
"""

from __future__ import annotations

import math
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.offsetbox import AnchoredText
from matplotlib.patches import Rectangle
from matplotlib.ticker import MaxNLocator
from matplotlib.transforms import Bbox, offset_copy

from ..config import IEEE_RCPARAMS, LEGEND_ORDER, assert_unique_colors

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

SHOW_TITLES: bool = False
SCENARIO_LABEL: str = ""
PROJECT_MODE_LABEL: str = ""  # "PV-only" | "BESS-only" | "Hybrid PV+BESS" | ""

# Validate the colour map once on import.
assert_unique_colors()


def apply_ieee_style() -> None:
    """Apply the IEEE matplotlib rcParams preset.  Idempotent."""
    mpl.rcParams.update(IEEE_RCPARAMS)


def set_show_titles(value: bool) -> None:
    """Enable or disable plot titles globally."""
    global SHOW_TITLES
    SHOW_TITLES = bool(value)


def show_titles() -> bool:
    """Return whether plot titles should be rendered."""
    return SHOW_TITLES


def set_scenario_label(label: str) -> None:
    """Set the scenario label injected into plot titles."""
    global SCENARIO_LABEL
    SCENARIO_LABEL = str(label or "").strip()


def get_scenario_label() -> str:
    """Return the currently configured scenario label."""
    return SCENARIO_LABEL


def set_project_mode_label(label: str) -> None:
    """Set the project-mode label injected into plot titles.

    One of ``"PV-only"`` / ``"BESS-only"`` / ``"Hybrid PV+BESS"`` /
    ``""`` (the empty default suppresses the annotation).
    """
    global PROJECT_MODE_LABEL
    PROJECT_MODE_LABEL = str(label or "").strip()


def get_project_mode_label() -> str:
    """Return the currently configured project-mode label."""
    return PROJECT_MODE_LABEL


# ---------------------------------------------------------------------------
# Figure saving (always PDF)
# ---------------------------------------------------------------------------


def save_figure(figpath: Path) -> Path:
    """Save the current figure as a PDF, honouring the IEEE preset."""
    figpath = Path(figpath)
    figpath.parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    out = figpath.with_suffix(".pdf")
    plt.savefig(out, format="pdf", bbox_inches="tight")
    plt.close()
    return out


def save_figure_daily(figpath: Path, date_str: str) -> Path:
    """Save a daily figure into a YYYY-MM subdirectory of its parent folder."""
    figpath = Path(figpath)
    month_folder = pd.to_datetime(date_str).strftime("%Y-%m")
    target = figpath.parent / month_folder / figpath.name
    return save_figure(target)


# ---------------------------------------------------------------------------
# Legends
# ---------------------------------------------------------------------------


def apply_legend(
    ax=None,
    *,
    max_rows: int = 2,
    custom_order: bool = False,
    plot_type: str = "daily",
) -> None:
    """Apply consistent legend styling, skipping plots with no series."""
    if ax is None:
        ax = plt.gca()

    handles, labels = ax.get_legend_handles_labels()
    if not labels:
        return

    if plot_type == "daily":
        y_offset = -0.20
    elif plot_type == "monthly":
        y_offset = -0.30
    elif plot_type == "yearly":
        y_offset = -0.25
    else:
        y_offset = -0.25

    if custom_order:
        ordered_handles, ordered_labels = [], []
        for desired in LEGEND_ORDER:
            if desired in labels:
                idx = labels.index(desired)
                ordered_handles.append(handles[idx])
                ordered_labels.append(labels[idx])
        for handle, label in zip(handles, labels):
            if label not in ordered_labels:
                ordered_handles.append(handle)
                ordered_labels.append(label)
        handles, labels = ordered_handles, ordered_labels

    num_entries = len(labels)
    if num_entries == 0:
        return
    ncol = max(1, int(np.ceil(num_entries / max_rows)))
    ax.legend(
        handles,
        labels,
        bbox_to_anchor=(0.5, y_offset),
        loc="upper center",
        ncol=ncol,
        frameon=True,
        framealpha=0.9,
    )


# ---------------------------------------------------------------------------
# Universal value-annotation helper (round-3)
# ---------------------------------------------------------------------------


def annotate_value_safe(
    ax,
    x: float,
    y: float,
    text: str,
    *,
    transform=None,
    ha: str = "center",
    va: str = "center",
    fontsize: int = 7,
    color: str = "black",
    offset_points: tuple[float, float] = (0.0, 0.0),
    bbox_facecolor: str = "white",
    bbox_edgecolor: str = "grey",
    bbox_alpha: float = 0.85,
    bbox_pad: float = 0.2,
):
    """Place a bbox-wrapped value annotation at ``(x, y)``.

    The single entry point for every numeric annotation on a plot.
    Plotting modules MUST call this instead of ``ax.text(...)`` /
    ``ax.annotate(...)`` with an inline ``bbox=`` kwarg — universality
    tests enforce this.

    With ``transform=None`` (the default), ``(x, y)`` are data
    coordinates and ``offset_points`` may apply a Δ in points so the
    bbox sits cleanly above / beside the underlying mark.  Pass
    ``transform=ax.transAxes`` to anchor the bbox in axes-fraction
    coordinates (e.g. top-right summary boxes).
    """
    bbox_kwargs = {
        "facecolor": bbox_facecolor,
        "edgecolor": bbox_edgecolor,
        "alpha": bbox_alpha,
        "linewidth": 0.5,
        "boxstyle": f"round,pad={bbox_pad}",
    }
    if transform is None:
        if offset_points != (0.0, 0.0):
            tr = offset_copy(
                ax.transData, fig=ax.figure,
                x=offset_points[0], y=offset_points[1], units="points",
            )
        else:
            tr = ax.transData
    else:
        tr = transform
    return ax.text(
        x, y, text,
        transform=tr,
        ha=ha, va=va, fontsize=fontsize, color=color,
        bbox=bbox_kwargs,
    )


def expand_axes_for_annotations(ax, *, pad: float = 0.05) -> None:
    """Enlarge the current xlim / ylim by ``pad`` so bbox annotations
    placed near the data edges do not clip outside the frame.

    Idempotent enough for normal use: re-running with a small pad
    won't drift the axes.  Call at the END of a plotting function,
    after every annotation has been placed.
    """
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    dx = pad * (xmax - xmin) if xmax > xmin else pad
    dy = pad * (ymax - ymin) if ymax > ymin else pad
    ax.set_xlim(xmin - dx, xmax + dx)
    ax.set_ylim(ymin - dy, ymax + dy)


# ---------------------------------------------------------------------------
# Universal axes-margin helper
# ---------------------------------------------------------------------------

# Universal margin fractions — small enough to feel tight, large
# enough that no annotation, legend, or data point touches the
# axes frame.
UNIVERSAL_MARGIN_X_FRAC: float = 0.02
UNIVERSAL_MARGIN_Y_FRAC: float = 0.05
# Larger headroom for plots that anchor an annotation in the top
# corner (NPV waterfall total box, lifetime-cycles total box).
HEADROOM_Y_FRAC: float = 0.12


def apply_universal_margins(
    ax,
    *,
    x_frac: float = UNIVERSAL_MARGIN_X_FRAC,
    y_frac: float = UNIVERSAL_MARGIN_Y_FRAC,
    skip_x: bool = False,
    skip_y: bool = False,
) -> None:
    """Pad axes so data and annotations never touch the frame.

    Called as the LAST step in every plotting function before
    :func:`save_figure`.  Idempotent.

    Baseline-aware behaviour:

    * Y-axis — if the current y-min is at or above 0 (typical bar
      / stacked-bar plot with non-negative data), the floor is
      preserved at the data minimum. Only the top gets padded.
      Otherwise (line plot crossing zero, waterfall, NPV curve)
      both top and bottom are padded.
    * X-axis — if the plot contains bar artists, only the right
      side is padded (the leftmost bar sits at the left frame
      edge). Otherwise both sides padded symmetrically.

    Plots that put an annotation in the top-right corner (e.g.
    NPV waterfall, lifetime cycles total) should pass a larger
    ``y_frac`` (use :data:`HEADROOM_Y_FRAC` ≈ 0.12) so the
    annotation has its own breathing row above the data.
    """
    if not skip_y:
        ymin, ymax = ax.get_ylim()
        span_y = ymax - ymin
        if span_y > 0:
            new_ymin = ymin if ymin >= 0 else ymin - y_frac * span_y
            new_ymax = ymax + y_frac * span_y
            ax.set_ylim(new_ymin, new_ymax)
    if not skip_x:
        xmin, xmax = ax.get_xlim()
        span_x = xmax - xmin
        if span_x > 0:
            has_bars = any(
                hasattr(c, "patches") and len(c.patches) > 0
                for c in ax.containers
            ) or any(
                isinstance(p, Rectangle) and p.get_width() > 0
                for p in ax.patches
            )
            if has_bars:
                ax.set_xlim(xmin, xmax + x_frac * span_x)
            else:
                ax.set_xlim(xmin - x_frac * span_x, xmax + x_frac * span_x)


# ---------------------------------------------------------------------------
# Corner-value annotation with deterministic nice-tick expansion (v5)
# ---------------------------------------------------------------------------

# Tunables
_OVERLAP_TOLERANCE_PX2: float = 4.0   # sub-pixel anti-aliasing slack
_SAFETY_HEADROOM_PX: float = 6.0      # extra pixels above measured need
_NICE_TICK_STEPS: list[float] = [1, 2, 2.5, 5, 10]  # matplotlib's default family


def anchor_corner_value(
    ax,
    *,
    text: str,
    loc: str = "upper right",
    fontsize: int = 8,
    borderaxespad: float = 0.5,
):
    """Place a value annotation at the upper-right corner with
    deterministic ymax expansion to a "nice" tick boundary if the
    data would overlap.

    Policy
    ------
    The annotation always lives in upper-right (predictable
    placement).  If the current axes don't have room, the y-axis
    upper limit is extended to the next clean tick value — not by
    arbitrary percentages, by matplotlib's own tick rules.  The
    data itself is never modified; only the frame grows.

    Falls back to ``fig.text`` above the axes only in pathological
    cases where even a generous expansion can't clear the corner.

    Parameters
    ----------
    loc : default ``"upper right"``.  Any other value bypasses the
        expansion logic and places the annotation at that location
        with no auto-adjustment (caller is being explicit).

    Returns
    -------
    The placed artist.  Usually :class:`AnchoredText` (Steps 1–4),
    occasionally :class:`~matplotlib.text.Text` (Step 5 fallback).
    """
    if loc != "upper right":
        return _place_anchored(ax, text, loc, fontsize, borderaxespad)

    # --- Step 1: trial placement, measure overlap ---
    pixel_overlap_y = _measure_vertical_overlap(
        ax, text, fontsize, borderaxespad,
    )
    if pixel_overlap_y <= _OVERLAP_TOLERANCE_PX2 ** 0.5:
        return _place_anchored(
            ax, text, "upper right", fontsize, borderaxespad,
        )

    # --- Step 2: compute required Δy in data coords ---
    needed_pixels = pixel_overlap_y + _SAFETY_HEADROOM_PX
    data_delta = _pixels_to_data_y(ax, needed_pixels)
    ymin, ymax = ax.get_ylim()
    target_ymax = ymax + data_delta

    # --- Step 3: snap to next nice tick boundary ---
    nice_ymax = _next_nice_tick_above(target_ymax, ymin)
    ax.set_ylim(ymin, nice_ymax)

    # --- Step 4: place permanently ---
    artist = _place_anchored(
        ax, text, "upper right", fontsize, borderaxespad,
    )

    # --- Step 5: defensive re-check (should always pass) ---
    ax.figure.canvas.draw()
    renderer = ax.figure.canvas.get_renderer()
    final_bbox = artist.get_window_extent(renderer=renderer)
    data_bboxes = _collect_data_bboxes(ax)
    final_overlap = _bbox_overlap_score(final_bbox, data_bboxes)
    if final_overlap > _OVERLAP_TOLERANCE_PX2:
        artist.remove()
        ax.set_ylim(ymin, ymax)
        return _place_outside_axes(ax, text, fontsize)
    return artist


def _measure_vertical_overlap(
    ax, text: str, fontsize: int, borderaxespad: float,
) -> float:
    """Place a trial AnchoredText at upper-right, measure how many
    pixels of vertical extent it shares with data artists, then
    remove the trial.

    Returns 0 if the annotation sits cleanly above all data."""
    trial = _place_anchored(
        ax, text, "upper right", fontsize, borderaxespad,
    )
    try:
        ax.figure.canvas.draw()
        renderer = ax.figure.canvas.get_renderer()
        trial_bbox = trial.get_window_extent(renderer=renderer)
        data_bboxes = _collect_data_bboxes(ax)
        if ax.legend_ is not None:
            try:
                data_bboxes.append(
                    ax.legend_.get_window_extent(renderer=renderer)
                )
            except Exception:
                pass
        max_overlap = 0.0
        for b in data_bboxes:
            if b.x1 < trial_bbox.x0 or b.x0 > trial_bbox.x1:
                continue
            if b.y1 > trial_bbox.y0:
                overlap = b.y1 - trial_bbox.y0
                if overlap > max_overlap:
                    max_overlap = overlap
        return max_overlap
    finally:
        trial.remove()


def _pixels_to_data_y(ax, pixels: float) -> float:
    """Convert a pixel delta on the y-axis to a data-coordinate
    delta, using the current axes transform."""
    trans = ax.transData.inverted()
    y0_pix = ax.transData.transform((0, ax.get_ylim()[0]))[1]
    _, y0_data = trans.transform((0, y0_pix))
    _, y1_data = trans.transform((0, y0_pix + pixels))
    return abs(y1_data - y0_data)


def _next_nice_tick_above(target: float, ymin: float) -> float:
    """Return the smallest "nice" tick value >= ``target``, using
    matplotlib's own :class:`MaxNLocator` with the standard step
    family.

    This guarantees the new ymax aligns to a value that matplotlib
    would naturally pick as a tick, so the y-axis tick labels stay
    clean (e.g. 0, 2M, 4M, 6M, 8M, 10M — not 0, 2.2M, 4.4M).
    """
    if target <= ymin:
        return target
    locator = MaxNLocator(
        nbins="auto", steps=_NICE_TICK_STEPS, prune=None,
    )
    span = target - ymin
    ticks = locator.tick_values(ymin, ymin + span * 1.5)
    for t in ticks:
        if t >= target:
            return float(t)
    magnitude = 10 ** math.floor(math.log10(max(abs(target), 1.0)))
    return math.ceil(target / magnitude) * magnitude


def _place_anchored(
    ax, text: str, loc: str, fontsize: int, borderaxespad: float,
) -> AnchoredText:
    at = AnchoredText(
        text, loc=loc, pad=0.4, borderpad=borderaxespad,
        frameon=True, prop={"size": fontsize},
    )
    at.patch.set_boxstyle("round,pad=0.3")
    at.patch.set_facecolor("white")
    at.patch.set_alpha(0.9)
    at.patch.set_edgecolor("grey")
    at.patch.set_linewidth(0.5)
    ax.add_artist(at)
    return at


def _place_outside_axes(ax, text: str, fontsize: int):
    fig = ax.figure
    ax_pos = ax.get_position()
    x = ax_pos.x1
    y = ax_pos.y1 + 0.01
    txt = fig.text(
        x, y, text, ha="right", va="bottom", fontsize=fontsize,
        bbox={
            "facecolor": "white", "edgecolor": "grey",
            "alpha": 0.9, "linewidth": 0.5,
            "boxstyle": "round,pad=0.3",
        },
    )
    fig.subplots_adjust(top=min(0.92, fig.subplotpars.top))
    return txt


def _bbox_overlap_score(text_bbox: Bbox, data_bboxes) -> float:
    score = 0.0
    for b in data_bboxes:
        ix0 = max(text_bbox.x0, b.x0)
        iy0 = max(text_bbox.y0, b.y0)
        ix1 = min(text_bbox.x1, b.x1)
        iy1 = min(text_bbox.y1, b.y1)
        if ix1 > ix0 and iy1 > iy0:
            score += (ix1 - ix0) * (iy1 - iy0)
    return score


def _collect_data_bboxes(ax):
    fig = ax.figure
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    out = []
    for artist in ax.get_children():
        if isinstance(artist, AnchoredText):
            continue
        if artist is ax.legend_ or artist is ax.patch:
            continue
        try:
            bbox = artist.get_window_extent(renderer=renderer)
            if bbox.width > 0 and bbox.height > 0:
                out.append(bbox)
        except Exception:
            continue
    return out
