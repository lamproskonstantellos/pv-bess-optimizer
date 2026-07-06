"""House date-axis style shared by the uncertainty-plot family.

Every date axis under ``06_uncertainty_plots/`` renders ``DD-MM-YYYY``
ticks via :func:`apply_house_date_axis` so the family is visually
consistent — rotated ``XTICK_ROT`` right-anchored, exactly like the
energy plots' daily / monthly date axes.
"""

from __future__ import annotations

from matplotlib.dates import AutoDateLocator, DateFormatter

from pvbess_opt.theme import XTICK_ROT

DATE_FMT = "%d-%m-%Y"  # house style: DD-MM-YYYY


def apply_house_date_axis(ax) -> None:
    """Apply the project's house x-axis date style: DD-MM-YYYY ticks,
    rotated right-anchored like every other dense axis in the report."""
    ax.xaxis.set_major_locator(AutoDateLocator())
    ax.xaxis.set_major_formatter(DateFormatter(DATE_FMT))
    for label in ax.get_xticklabels():
        label.set_rotation(XTICK_ROT)
        label.set_horizontalalignment("right")
