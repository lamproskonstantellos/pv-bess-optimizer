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

from .balancing import (
    plot_balancing_mc_distribution,
    plot_balancing_reservation_profile,
)
from .bess_revenue import (
    plot_bess_capacity_vs_activation_split,
    plot_bess_revenue_by_month,
    plot_bess_revenue_waterfall,
)
from .daily import (
    plot_daily_combined,
    plot_daily_combined_merchant,
    plot_daily_combined_merchant_with_soc,
    plot_daily_combined_with_soc,
    plot_daily_dispatch,
    plot_daily_revenue,
    plot_daily_soc,
    plot_daily_supply,
    plot_daily_surplus,
)
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
    plot_uncertainty_coverage_by_horizon,
    plot_uncertainty_crps_timeline,
    plot_uncertainty_pit_histogram,
    plot_uncertainty_residual_qq,
)
from .lifecycle import (
    plot_lcoe_summary,
    plot_lcos_summary,
    plot_lifetime_cycles,
    plot_revenue_stack_yearly,
)
from .monthly import (
    plot_monthly_combined,
    plot_monthly_combined_merchant,
    plot_monthly_dispatch,
    plot_monthly_revenue,
    plot_monthly_soc,
    plot_monthly_supply,
    plot_monthly_surplus,
)
from .sizing import (
    plot_efficient_frontier,
    plot_npv_vs_capacity,
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
    plot_yearly_combined_merchant,
    plot_yearly_dispatch,
    plot_yearly_revenue,
    plot_yearly_soc,
    plot_yearly_supply,
    plot_yearly_surplus,
)

__all__ = [
    "apply_ieee_style",
    "plot_balancing_mc_distribution",
    "plot_balancing_reservation_profile",
    "plot_bess_capacity_vs_activation_split",
    "plot_bess_revenue_by_month",
    "plot_bess_revenue_waterfall",
    "plot_cumulative_cashflow",
    "plot_daily_combined",
    "plot_daily_combined_merchant",
    "plot_daily_combined_merchant_with_soc",
    "plot_daily_combined_with_soc",
    "plot_daily_dispatch",
    "plot_daily_revenue",
    "plot_daily_soc",
    "plot_daily_supply",
    "plot_daily_surplus",
    "plot_dam_intraday_heatmap",
    "plot_efficient_frontier",
    "plot_foresight_gap_comparison",
    "plot_input_forecast_band",
    "plot_input_seasonal_boxplot",
    "plot_irr_tornado",
    "plot_lcoe_summary",
    "plot_lcos_summary",
    "plot_lifetime_cycles",
    "plot_lifetime_summary",
    "plot_monthly_cashflow_year1",
    "plot_monthly_combined",
    "plot_monthly_combined_merchant",
    "plot_monthly_dispatch",
    "plot_monthly_revenue",
    "plot_monthly_soc",
    "plot_monthly_supply",
    "plot_monthly_surplus",
    "plot_npv_tornado",
    "plot_npv_vs_capacity",
    "plot_npv_waterfall",
    "plot_payback",
    "plot_revenue_stack_yearly",
    "plot_rolling_horizon_distribution",
    "plot_uncertainty_coverage_by_horizon",
    "plot_uncertainty_crps_timeline",
    "plot_uncertainty_pit_histogram",
    "plot_uncertainty_residual_qq",
    "plot_yearly_cashflow_bars",
    "plot_yearly_combined",
    "plot_yearly_combined_merchant",
    "plot_yearly_dispatch",
    "plot_yearly_revenue",
    "plot_yearly_soc",
    "plot_yearly_supply",
    "plot_yearly_surplus",
    "set_project_mode_label",
    "set_scenario_label",
    "set_show_titles",
]
