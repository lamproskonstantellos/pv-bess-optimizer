"""Monte Carlo realisation tests for the balancing market."""

from __future__ import annotations

from pvbess_opt.optimization import run_scenario
from pvbess_opt.rolling_horizon import monte_carlo_balancing
from tests._balancing_helpers import _balancing_on


def test_monte_carlo_returns_quantiles_in_order(short_params, short_ts):
    p_on = _balancing_on(short_params)
    res, _ = run_scenario(p_on, short_ts)
    mc = monte_carlo_balancing(res, p_on, n_scenarios=100, seed=1729)
    p10 = mc["bm_total_balancing_revenue_p10_eur"]
    p50 = mc["bm_total_balancing_revenue_p50_eur"]
    p90 = mc["bm_total_balancing_revenue_p90_eur"]
    assert p10 <= p50 <= p90


def test_monte_carlo_is_reproducible(short_params, short_ts):
    p_on = _balancing_on(short_params)
    res, _ = run_scenario(p_on, short_ts)
    mc1 = monte_carlo_balancing(res, p_on, n_scenarios=80, seed=42)
    mc2 = monte_carlo_balancing(res, p_on, n_scenarios=80, seed=42)
    assert (
        mc1["bm_total_balancing_revenue_p50_eur"]
        == mc2["bm_total_balancing_revenue_p50_eur"]
    )
    assert mc1["bm_mc_total_realised_eur"] == mc2["bm_mc_total_realised_eur"]


def test_soc_constrained_fraction_is_bounded(short_params, short_ts):
    p_on = _balancing_on(short_params)
    res, _ = run_scenario(p_on, short_ts)
    mc = monte_carlo_balancing(res, p_on, n_scenarios=50, seed=7)
    fraction = mc["bm_soc_constrained_scenarios_pct"]
    assert 0.0 <= fraction <= 100.0


def test_monte_carlo_off_returns_empty_dict(short_params, short_ts):
    """With ``balancing_enabled=False`` the MC helper short-circuits
    and returns no balancing keys."""
    # Build a dispatch frame from a ON-run, then flip the flag in the
    # params we pass to monte_carlo_balancing; the helper looks at the
    # gate independently.
    p_on = _balancing_on(short_params)
    res, _ = run_scenario(p_on, short_ts)
    p_off = dict(p_on)
    p_off["balancing"] = dict(p_on["balancing"], balancing_enabled=False)
    mc = monte_carlo_balancing(res, p_off, n_scenarios=10)
    assert mc == {}
