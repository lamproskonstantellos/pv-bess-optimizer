"""IEEE matplotlib styling, PDF figure saving, and legend helpers.

All figures use the IEEE rcParams preset and are exported as PDF.  Plot
titles default to off (the figure caption in a paper plays that role) and
can be turned on via the ``show_titles`` key in the input workbook.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Rectangle
from matplotlib.ticker import MaxNLocator
from matplotlib.transforms import offset_copy

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


# ---------------------------------------------------------------------------
# Universal axes-margin helper
# ---------------------------------------------------------------------------

# Universal margin fractions — small enough to feel tight, large
# enough that no annotation, legend, or data point touches the
# axes frame.
UNIVERSAL_MARGIN_X_FRAC: float = 0.02
UNIVERSAL_MARGIN_Y_FRAC: float = 0.05


def apply_universal_margins(
    ax,
    *,
    x_frac: float = UNIVERSAL_MARGIN_X_FRAC,
    y_frac: float = UNIVERSAL_MARGIN_Y_FRAC,
    skip_x: bool = False,
    skip_y: bool = False,
) -> None:
    """Pad axes so data and annotations never touch the frame.

    Called as the last step before :func:`save_figure`.  Idempotent.

    Baseline-aware behaviour:

    * Y-axis — if the current y-min is at or above 0 (typical bar
      / stacked-bar plot with non-negative data), the floor is
      preserved at the data minimum. Only the top gets padded.
      Otherwise (line plot crossing zero, waterfall, NPV curve)
      both top and bottom are padded.
    * X-axis — if the plot contains bar artists, only the right
      side is padded (the leftmost bar sits at the left frame
      edge). Otherwise both sides padded symmetrically.

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
# Fine tick density helper
# ---------------------------------------------------------------------------


def apply_fine_ticks(
    ax,
    *,
    nbins: int = 10,
    axis: str = "y",
) -> None:
    """Use a denser tick locator for plots that benefit from finer
    granularity (currency, energy).

    Picks steps from the standard ``[1, 2, 5, 10]`` family — the
    same family matplotlib uses for default ticks, just at a higher
    bin count.  For a y-range of 20M, ~10 bins yields a 2M step.
    For 10M, a 1M step.  For 5M, a 0.5M step.

    Call as the **last** axis-mutating step in a plotting function,
    after data is drawn and :func:`apply_universal_margins` has set
    the final limits.

    Parameters
    ----------
    axis : ``"y"`` (default) or ``"x"``.  Tornado plots whose value
        axis is horizontal pass ``axis="x"``.
    """
    locator = MaxNLocator(nbins=nbins, steps=[1, 2, 5, 10])
    if axis == "y":
        ax.yaxis.set_major_locator(locator)
    elif axis == "x":
        ax.xaxis.set_major_locator(locator)
    else:
        raise ValueError(f"axis must be 'x' or 'y', got {axis!r}")
