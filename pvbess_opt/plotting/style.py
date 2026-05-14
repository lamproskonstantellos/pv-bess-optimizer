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


def apply_universal_margins(
    ax,
    *,
    x_frac: float = UNIVERSAL_MARGIN_X_FRAC,
    y_frac: float = UNIVERSAL_MARGIN_Y_FRAC,
    skip_x: bool = False,
    skip_y: bool = False,
) -> None:
    """Pad axes limits so data and annotations never touch the frame.

    Called as the last step in every plotting function before
    :func:`save_figure`.  Adds a small symmetric padding (2% in x,
    5% in y by default) to the current axes limits.

    skip_x: pass True for plots with a fixed x-domain that must not
        extend (e.g. monthly plots whose x-axis spans exactly
        Jan 1 → Feb 1, or tornado plots that already apply their own
        outward padding).
    skip_y: same for fixed y-domains.
    """
    if not skip_x:
        xmin, xmax = ax.get_xlim()
        span = xmax - xmin
        if span > 0:
            ax.set_xlim(xmin - x_frac * span, xmax + x_frac * span)
    if not skip_y:
        ymin, ymax = ax.get_ylim()
        span = ymax - ymin
        if span > 0:
            ax.set_ylim(ymin - y_frac * span, ymax + y_frac * span)
