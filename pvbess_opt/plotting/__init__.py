"""Plotting subpackage for the PV+BESS optimizer.

All figures use the IEEE matplotlib preset and are exported as PDF.

Three families of plots:

* **Energy plots** — stacked daily / monthly / yearly views of the
  Year-1 dispatch (these come from ``daily.py``, ``monthly.py``, and
  ``yearly.py``).
* **Financial plots** — multi-year cash-flow plots + tornado diagrams
  + rolling-horizon Monte Carlo distribution (``financial.py``,
  ``uncertainty.py``).
* **Input-uncertainty plots** — analytical P10/P90 envelope on a
  representative week, monthly boxplots, and DAM intraday × seasonal
  heatmap (``inputs_uncertainty.py``).
"""

from .daily import plot_daily_combined, plot_daily_supply, plot_daily_surplus
from .financial import (
    plot_cumulative_cashflow,
    plot_irr_tornado,
    plot_monthly_cashflow_year1,
    plot_npv_tornado,
    plot_npv_waterfall,
    plot_payback,
    plot_yearly_cashflow_bars,
)
from .inputs_uncertainty import (
    plot_dam_intraday_heatmap,
    plot_input_forecast_band,
    plot_input_seasonal_boxplot,
)
from .monthly import (
    plot_monthly_combined,
    plot_monthly_supply,
    plot_monthly_surplus,
)
from .style import (
    apply_ieee_style,
    set_project_mode_label,
    set_scenario_label,
    set_show_titles,
)
from .uncertainty import (
    plot_foresight_gap_comparison,
    plot_rolling_horizon_distribution,
)
from .yearly import (
    plot_lifetime_summary,
    plot_yearly_combined,
    plot_yearly_supply,
    plot_yearly_surplus,
)

__all__ = [
    "apply_ieee_style",
    "set_project_mode_label",
    "set_scenario_label",
    "set_show_titles",
    "plot_daily_supply",
    "plot_daily_surplus",
    "plot_daily_combined",
    "plot_monthly_supply",
    "plot_monthly_surplus",
    "plot_monthly_combined",
    "plot_yearly_supply",
    "plot_yearly_surplus",
    "plot_yearly_combined",
    "plot_lifetime_summary",
    "plot_cumulative_cashflow",
    "plot_yearly_cashflow_bars",
    "plot_npv_waterfall",
    "plot_payback",
    "plot_monthly_cashflow_year1",
    "plot_npv_tornado",
    "plot_irr_tornado",
    "plot_rolling_horizon_distribution",
    "plot_foresight_gap_comparison",
    "plot_input_forecast_band",
    "plot_input_seasonal_boxplot",
    "plot_dam_intraday_heatmap",
]
