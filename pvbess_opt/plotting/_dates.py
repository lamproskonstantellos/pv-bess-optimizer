"""House date-axis style shared by the uncertainty-plot family.

Every date axis under ``06_uncertainty_plots/`` renders ``DD-MM-YYYY``
ticks via :func:`apply_house_date_axis` so the family is visually
consistent.
"""

from __future__ import annotations

from matplotlib.dates import AutoDateLocator, DateFormatter

DATE_FMT = "%d-%m-%Y"  # house style: DD-MM-YYYY


def apply_house_date_axis(ax) -> None:
    """Apply the project's house x-axis date style: DD-MM-YYYY ticks."""
    ax.xaxis.set_major_locator(AutoDateLocator())
    ax.xaxis.set_major_formatter(DateFormatter(DATE_FMT))
    for label in ax.get_xticklabels():
        # Keep horizontal; AutoDateLocator auto-thins if labels collide.
        label.set_rotation(0)
