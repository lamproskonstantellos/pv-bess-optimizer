"""Lender-case analysis: deterministic production haircuts (Eq. E44).

``production_p90_factor_pct`` applies the lender convention of an
INTER-ANNUAL resource case: the P90 exceedance year delivers a fixed
fraction of the modelled P50 energy, so every PV-linked revenue line
of the yearly cashflow is scaled deterministically and the leverage
metrics (DSCR, equity IRR, debt capacity) are re-evaluated on the
haircut CFADS.  This is deliberately DISTINCT from the intra-year
forecast-noise Monte Carlo (``uncertainty_enabled``): that machinery
models dispatch realism against imperfect foresight within the year,
not resource risk between years — the scope split is documented in
both ``docs/economics_design.md`` (E44) and
``docs/uncertainty_design.md``.

No re-dispatch happens: the scaling is a documented cashflow-level
approximation (a real P90 irradiance year changes BESS arbitrage
volumes and curtailment nonlinearly).  The exact alternative —
re-solving the dispatch with a scaled PV profile through the scenario
engine — is recorded as future work, not silently promised.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from .economics import _leverage_kpis, size_debt
from .sensitivity import _infer_aggregator_fee_frac, _recompute_net

logger = logging.getLogger(__name__)

__all__ = ["apply_production_case", "build_lender_cases"]


def apply_production_case(
    yearly_cf: pd.DataFrame, factor: float,
) -> pd.DataFrame:
    """Scale the PV-production-linked streams of ``yearly_cf`` (Eq. E44).

    ``factor`` is the production ratio (0.92 = the case year delivers
    92 % of the modelled energy).  Per-column classification — each
    decision is deliberate:

    * SCALES with production: the retail/DAM revenue family (gross
      recovered from the base frame per the
      :func:`pvbess_opt.sensitivity._scale_revenue` identity
      ``revenue_eur + |aggregator_fee_eur| == gross``, then the
      aggregator fee rederived with the same fraction and
      non-negative-gross clamp, so the gross/net identity holds
      through the clamp); ``ppa_revenue_eur`` (the pay-as-produced
      volume IS PV energy); ``route_to_market_fee_eur`` (Eq. E13c:
      EUR/MWh on exported VOLUME, which falls with production — the
      opposite of the price-perturbing Revenue tornado driver, where
      it stays fixed); ``imbalance_cost_eur`` (Eq. E28: PV-curve
      volume times DAM prices).
    * DOES NOT scale: the balancing family (capacity AND activation —
      BESS reservation revenue, not PV-resource-driven; activation is
      partly PV-coupled in some configurations but is classified
      BESS-driven here, flagged in the docs); ``toll_revenue_eur`` and
      ``capacity_market_revenue_eur`` (fixed contractual /
      administrative EUR/MW payments); ``state_support_eur`` and its
      clawback, ``optimizer_fee_eur`` / ``optimizer_floor_topup_eur``
      (BESS-margin structures; their bases are dominated by BESS
      trading, and re-solving their piecewise kinks under a resource
      case is part of the recorded re-dispatch future work);
      ``grid_charging_fee_eur`` (grid-import volume, not PV);
      ``revenue_levy_eur`` (mixed market-turnover base — kept at the
      base value, which overstates the levy under a haircut and is
      therefore conservative for a lender case); OPEX / CAPEX / DEVEX;
      the informational ``bess_market_revenue_eur`` netting base.

    The tax-layer columns are dropped by the net recompute (the same
    stale-value guard as the sensitivity frames): lender DSCR reads
    the PRE-tax CFADS convention.
    """
    factor = float(factor)
    df = yearly_cf.copy()
    frac = _infer_aggregator_fee_frac(df)

    # Recover the true per-year gross from the base frame — exact in
    # both the fee-applied and the fee-free / clamped years (see the
    # _scale_revenue docstring for why inverting the net with a
    # constant 1/(1-frac) would be wrong on mixed-sign cashflows).
    revenue_base = df["revenue_eur"].astype(float)
    if "aggregator_fee_eur" in df.columns:
        fee_base_abs = df["aggregator_fee_eur"].astype(float).abs()
    else:
        fee_base_abs = pd.Series(0.0, index=df.index)
    gross_base = revenue_base + fee_base_abs
    nonzero_base = gross_base.abs() > 1e-12
    one_minus_f_year = (
        revenue_base / gross_base.where(nonzero_base, 1.0)
    ).where(nonzero_base, 1.0)

    gross = factor * gross_base
    fee = -frac * gross.clip(lower=0.0)
    df["aggregator_fee_eur"] = fee
    df["revenue_eur"] = gross + fee

    # Re-split the (possibly clamped) fee across the retail/DAM
    # streams in proportion to their gross, mirroring the base build.
    if "revenue_retail_eur" in df.columns and "revenue_dam_eur" in df.columns:
        retail_gross = factor * (
            df["revenue_retail_eur"].astype(float) / one_minus_f_year
        )
        dam_gross = gross - retail_gross
        nonzero = gross.abs() > 1e-12
        retail_share = (retail_gross / gross.where(nonzero, 1.0)).where(
            nonzero, 0.0,
        )
        retail_fee = fee * retail_share
        df["revenue_retail_eur"] = retail_gross + retail_fee
        df["revenue_dam_eur"] = dam_gross + (fee - retail_fee)

    for col in (
        "ppa_revenue_eur",
        "route_to_market_fee_eur",
        "imbalance_cost_eur",
    ):
        if col in df.columns:
            df[col] = df[col].astype(float) * factor

    return _recompute_net(df)


def build_lender_cases(
    yearly_cf: pd.DataFrame, econ: dict[str, Any],
    *,
    low_price_cf: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """The lender case table (Eq. E44): per-case leverage KPIs.

    Rows ``base`` (the run's own cashflow, factor 100 %) and ``p90``
    (the ``production_p90_factor_pct`` haircut; identical to base when
    the factor is 100), plus ``low_price`` when the deck cashflow is
    supplied (the pipeline passes it only when
    ``debt_sizing_case = 'low_price'`` already re-dispatched the deck
    — the table alone never triggers a solve; the price case keeps
    the full production, so its factor column reads 100).  Columns:
    ``min_dscr`` / ``avg_dscr`` / ``equity_irr_pct`` (from the E20
    schedule on the case CFADS — the debt amount is the run's
    resolved one, frozen under target-DSCR sizing, so the cases
    answer "same committed debt, worse year"), ``npv_eur`` (case
    discounted net) and ``debt_capacity_eur`` (the E41/E42 capacity
    the case CFADS could carry at the configured ``target_dscr``).
    LCOE / LCOS are deliberately EXCLUDED: they are Lazard cost
    figures, and scaling the energy denominator without the cost
    numerator would misstate them.
    """
    raw_f = econ.get("production_p90_factor_pct")
    factor_pct = 100.0 if raw_f is None else float(raw_f)
    cases: list[tuple[str, float, pd.DataFrame]] = [
        ("base", 100.0, yearly_cf),
        (
            "p90", factor_pct,
            yearly_cf if factor_pct == 100.0
            else apply_production_case(yearly_cf, factor_pct / 100.0),
        ),
    ]
    if low_price_cf is not None and not low_price_cf.empty:
        cases.append(("low_price", 100.0, low_price_cf))
    rows: list[dict[str, Any]] = []
    for case, f_pct, frame in cases:
        net = frame["net_cashflow_eur"].to_numpy(dtype=float)
        equity_irr_pct, min_dscr, avg_dscr = _leverage_kpis(net, econ)
        npv = float(frame["discounted_cf_eur"].sum())
        investment = -float(net[0]) if net.size >= 1 else 0.0
        capacity = size_debt(
            net[1:], econ, investment,
        ).debt_capacity_eur if investment > 0.0 else float("nan")
        rows.append({
            "case": case,
            "production_factor_pct": round(f_pct, 4),
            "min_dscr": round(min_dscr, 4),
            "avg_dscr": round(avg_dscr, 4),
            "equity_irr_pct": round(equity_irr_pct, 4),
            "npv_eur": round(npv, 2),
            "debt_capacity_eur": round(capacity, 2),
        })
    return pd.DataFrame(rows)
