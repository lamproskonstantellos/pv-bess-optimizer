"""One-at-a-time sensitivity analysis for the multi-year financial model.

Industry-standard tornado analysis varies one driver at a time by a
fixed +/- delta and records the change in NPV / IRR / payback.  This
module wraps :func:`pvbess_opt.economics.build_yearly_cashflow` and
:func:`pvbess_opt.economics.compute_financial_kpis` so callers can run
the analysis with a single function call after the base run.

The four default drivers — total CAPEX, total annual OPEX, Year-1
revenue, discount rate — are the four most impactful for any
PV + BESS project per Lazard's *Levelized Cost of Storage* and
NREL's *Annual Technology Baseline*.

Sign conventions
----------------

* ``delta_npv_eur`` is signed: ``high - base`` for the high scenario
  and ``low - base`` for the low scenario.
* ``delta_irr_pp`` and ``delta_payback_years`` are also signed.
* The DataFrame includes a synthetic ``base`` row per variable so the
  tornado plots can centre cleanly without having to look up the base
  KPI dictionary separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .availability import availability_factor
from .constants import (
    DEFAULT_SENSITIVITY_DELTA_PCT,
    DEFAULT_SENSITIVITY_DISCOUNT_RATE_DELTA_PP,
    DEFAULT_SENSITIVITY_TAX_RATE_DELTA_PP,
)
from .economics import (
    TAX_LAYER_COLUMNS,
    _contract_phase,
    build_yearly_cashflow,
    compute_financial_kpis,
)

__all__ = [
    "DriverSensitivity",
    "build_driver_sensitivities",
    "run_sensitivity_analysis",
    "variables_for_irr_sensitivity",
    "variables_for_npv_sensitivity",
]


@dataclass
class DriverSensitivity:
    """One tornado driver's base / low / high state.

    Carries the absolute driver values (``*_value``) so a tornado plot
    can annotate each bar end with the driver value that produced it;
    the metric outcomes travel separately in the plot's low/high
    arrays.
    """

    name: str               # e.g. "CAPEX" — the variable identifier
    driver_type: str         # e.g. "capex" — keys the numeric formatter
    base_value: float        # base case absolute driver value
    low_value: float         # absolute driver value at the low end
    high_value: float        # absolute driver value at the high end
    sensitivity_pct: float   # the +/- magnitude used (e.g. 20.0)


# Maps the ``variable`` column to a ``driver_type`` understood by the
# tornado numeric formatter.
_DRIVER_TYPE_BY_VARIABLE: dict[str, str] = {
    "CAPEX": "capex",
    "OPEX": "opex",
    "Revenue": "revenue",
    "DiscountRate": "discount_rate",
    "PpaPrice": "ppa_price",
    # Same EUR/MWh strike semantics as the PPA driver.
    "SupportStrike": "ppa_price",
    # Absolute percentage-point semantics like the discount rate.
    "TaxRate": "discount_rate",
}


def variables_for_npv_sensitivity(
    econ: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return the canonical NPV-sensitivity variables.

    Four always (CAPEX, OPEX, Revenue, DiscountRate) plus the optional
    PPA-price driver when a PPA contract is enabled.
    """
    capex_d = float(
        econ.get("sensitivity_capex_delta_pct", DEFAULT_SENSITIVITY_DELTA_PCT)
    ) / 100.0
    opex_d = float(
        econ.get("sensitivity_opex_delta_pct", DEFAULT_SENSITIVITY_DELTA_PCT)
    ) / 100.0
    rev_d = float(
        econ.get("sensitivity_revenue_delta_pct", DEFAULT_SENSITIVITY_DELTA_PCT)
    ) / 100.0
    rate_d = float(econ.get(
        "sensitivity_discount_rate_delta_pp",
        DEFAULT_SENSITIVITY_DISCOUNT_RATE_DELTA_PP,
    ))
    raw = [
        {"name": "CAPEX", "kind": "relative", "delta": capex_d,
         "label": "Total CAPEX"},
        {"name": "OPEX", "kind": "relative", "delta": opex_d,
         "label": "Total annual OPEX"},
        {"name": "Revenue", "kind": "relative", "delta": rev_d,
         "label": "Year-1 revenue base"},
        {"name": "DiscountRate", "kind": "absolute", "delta": rate_d,
         "label": "Discount rate"},
    ]
    # Optional PPA-strike driver — only meaningful with an enabled
    # contract (the run loop additionally requires a non-zero Year-1
    # PPA stream before recording the rows).
    if bool(econ.get("ppa_enabled", False)):
        ppa_d = float(
            econ.get(
                "sensitivity_ppa_price_delta_pct",
                DEFAULT_SENSITIVITY_DELTA_PCT,
            )
        ) / 100.0
        raw.append(
            {"name": "PpaPrice", "kind": "relative", "delta": ppa_d,
             "label": "PPA price"},
        )
    # Support-scheme strike driver (Eqs. E55-E57): the cashflow
    # rebuilds the settlement from the strike-independent Year-1
    # monthly detail, so a full rebuild at the perturbed strike is
    # EXACT (including the sliding one-way clamp).  Shares the PPA
    # delta knob — both are EUR/MWh strike perturbations.
    if str(
        econ.get("support_scheme", "none") or "none"
    ).strip().lower() in ("sliding_fip", "cfd_two_way"):
        sup_d = float(
            econ.get(
                "sensitivity_ppa_price_delta_pct",
                DEFAULT_SENSITIVITY_DELTA_PCT,
            )
        ) / 100.0
        raw.append(
            {"name": "SupportStrike", "kind": "relative", "delta": sup_d,
             "label": "Support strike"},
        )
    # TaxRate driver (Eqs. E34-E38 downstream): active only while the
    # tax layer is on.  Taxes are NONLINEAR (taxable-base clamp, loss
    # carry-forward), so each leg is a full cashflow + tax-layer
    # rebuild and the driver reports POST-TAX deltas in dedicated
    # columns — the pre-tax metric columns stay NaN on its rows, so
    # the pre-tax tornado layouts skip it.
    if float(econ.get("corporate_tax_rate_pct", 0.0) or 0.0) > 0.0:
        tax_d = float(
            econ.get(
                "sensitivity_tax_rate_delta_pp",
                DEFAULT_SENSITIVITY_TAX_RATE_DELTA_PP,
            )
        )
        raw.append(
            {"name": "TaxRate", "kind": "absolute", "delta": tax_d,
             "label": "Corporate tax rate"},
        )
    return [v for v in raw if float(v["delta"]) > 0.0]  # type: ignore[arg-type]


def variables_for_irr_sensitivity(
    econ: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return the three IRR-sensitivity drivers (no discount rate)."""
    return [
        v for v in variables_for_npv_sensitivity(econ)
        if v["name"] != "DiscountRate"
    ]


# ---------------------------------------------------------------------------
# Cashflow rebuild helpers
# ---------------------------------------------------------------------------


def _recompute_net(df: pd.DataFrame) -> pd.DataFrame:
    """Refresh net / discounted / cumulative columns after a column edit.

    ``net_cashflow_eur`` must mirror the build-time formula in
    :func:`pvbess_opt.economics.build_yearly_cashflow`, which folds
    ``balancing_revenue_eur`` into the net alongside ``revenue_eur``,
    ``opex_eur``, ``capex_eur`` and ``devex_eur``.  Dropping balancing
    here would make every perturbed scenario strip balancing revenue
    while the base KPI still includes it — the symptom that surfaced
    in the IRR / NPV tornadoes after the balancing block landed.
    """
    # Tax-layer columns (Eqs. E34-E38) are DROPPED from perturbed
    # frames: taxes are nonlinear (the taxable-base clamp and the loss
    # carry-forward), so scaled copies would be silently stale.  The
    # pre-tax tornado never reads them; post-tax metrics come from
    # full rebuilds only (the DiscountRate-driver path regenerates
    # them correctly through build_yearly_cashflow).
    df = df.drop(
        columns=[c for c in TAX_LAYER_COLUMNS if c in df.columns],
    )
    components = ["revenue_eur", "opex_eur", "capex_eur"]
    if "devex_eur" in df.columns:
        components.append("devex_eur")
    if "balancing_revenue_eur" in df.columns:
        components.append("balancing_revenue_eur")
    if "balancing_aggregator_fee_eur" in df.columns:
        components.append("balancing_aggregator_fee_eur")
    if "route_to_market_fee_eur" in df.columns:
        components.append("route_to_market_fee_eur")
    if "optimizer_fee_eur" in df.columns:
        components.append("optimizer_fee_eur")
    if "grid_charging_fee_eur" in df.columns:
        components.append("grid_charging_fee_eur")
    if "imbalance_cost_eur" in df.columns:
        components.append("imbalance_cost_eur")
    if "toll_revenue_eur" in df.columns:
        components.append("toll_revenue_eur")
    if "optimizer_floor_topup_eur" in df.columns:
        components.append("optimizer_floor_topup_eur")
    if "state_support_eur" in df.columns:
        components.append("state_support_eur")
    if "state_support_clawback_eur" in df.columns:
        components.append("state_support_clawback_eur")
    if "capacity_market_revenue_eur" in df.columns:
        components.append("capacity_market_revenue_eur")
    if "revenue_levy_eur" in df.columns:
        components.append("revenue_levy_eur")
    if "curtailment_compensation_eur" in df.columns:
        components.append("curtailment_compensation_eur")
    if "augmentation_capex_eur" in df.columns:
        components.append("augmentation_capex_eur")
    if "go_revenue_eur" in df.columns:
        components.append("go_revenue_eur")
    if "support_settlement_eur" in df.columns:
        components.append("support_settlement_eur")
    if "intraday_revenue_eur" in df.columns:
        components.append("intraday_revenue_eur")
    if "intraday_fee_eur" in df.columns:
        components.append("intraday_fee_eur")
    if "ppa_revenue_eur" in df.columns:
        components.append("ppa_revenue_eur")
    # bess_market_revenue_eur (Eq. E25a) is deliberately NOT a net
    # component: it is the informational netting base for the
    # contracted BESS structures, composed of streams already summed
    # above (mirrors the monthly frame's aggregator_fee_eur, which is
    # informational there for the same reason).
    df["net_cashflow_eur"] = sum(df[c].astype(float) for c in components)
    df["discounted_cf_eur"] = (
        df["net_cashflow_eur"] * df["discount_factor"].astype(float)
    )
    df["cumulative_cf_eur"] = df["net_cashflow_eur"].cumsum()
    df["cumulative_dcf_eur"] = df["discounted_cf_eur"].cumsum()
    return df


def _scale_capex(yearly_cf: pd.DataFrame, factor: float) -> pd.DataFrame:
    """Scale CAPEX and DEVEX by the same factor.

    Scales EVERY ``capex_eur`` / ``devex_eur`` row: the Year-0 outlay
    (per-asset CAPEX + per-asset DEVEX + the site-wide lump sum) and any
    BESS replacement CAPEX in its scheduled year — the replacement is a
    percentage of the same unit cost, so a +/-X % CAPEX world moves it
    by the same factor.  Augmentation events (Eq. E51) are priced off
    the same unit cost too (``capex_bess_eur_per_kwh`` on the declining
    curve), so their column scales with the driver; the Revenue driver
    leaves it untouched (an investment outflow has no price component).
    The driver VALUE reported on the tornado is the Year-0 outlay only
    (see ``run_sensitivity_analysis``).
    """
    df = yearly_cf.copy()
    df["capex_eur"] = df["capex_eur"].astype(float) * float(factor)
    if "devex_eur" in df.columns:
        df["devex_eur"] = df["devex_eur"].astype(float) * float(factor)
    if "augmentation_capex_eur" in df.columns:
        df["augmentation_capex_eur"] = (
            df["augmentation_capex_eur"].astype(float) * float(factor)
        )
    return _recompute_net(df)


def _scale_opex(yearly_cf: pd.DataFrame, factor: float) -> pd.DataFrame:
    df = yearly_cf.copy()
    df["opex_eur"] = df["opex_eur"].astype(float) * float(factor)
    return _recompute_net(df)


def _scale_revenue(
    yearly_cf: pd.DataFrame, factor: float,
    econ: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Scale every revenue stream by the same factor, then rederive the fee.

    The Revenue driver sweeps the project's Year-1+ income holistically:
    retail + DAM net revenue (``revenue_eur`` and its per-stream
    breakdowns), the aggregator-fee deduction that scales with gross
    revenue, and balancing capacity + activation revenue.

    Decision table vs the per-year trajectory vectors (Eq. E24): the
    uniform driver scaling perturbs the price LEVEL and commutes with a
    trajectory's per-year SHAPE, so trajectory-shaped revenue columns
    and ``optimizer_fee_eur`` scale with the driver, while
    ``route_to_market_fee_eur`` and ``grid_charging_fee_eur`` stay
    untouched (volume-based regulated charges, no price component;
    locked by tests/test_trajectory_application.py and
    tests/test_grid_charging_fee.py).

    The perturbed frame is reconstructed so it satisfies the same
    gross/net identity the original cashflow does:

    1. Recover the TRUE per-year gross directly from the base frame.
       :func:`pvbess_opt.economics.build_yearly_cashflow` stores
       ``revenue_eur == gross`` in years where the gross is <= 0 (the
       aggregator fee is clamped to zero) and
       ``revenue_eur == (1 - frac) * gross`` where the gross is > 0, so
       in BOTH regimes ``revenue_eur + |aggregator_fee_eur| == gross``.
       Recovering the gross this way — rather than inverting the net
       with a single ``net / (1 - frac)`` — is essential: a constant
       inversion over-inflates the fee-clamped years by ``1 / (1 - frac)``
       whenever a positive-gross year makes ``frac`` non-zero, which
       breaks the ``_scale_revenue(cf, 1.0)`` no-op on mixed-sign
       cashflows.
    2. Scale the gross by ``factor`` and rederive ``aggregator_fee_eur``,
       ``revenue_eur`` and the per-stream nets using the SAME
       aggregator-fee fraction the base cashflow used (recovered by
       :func:`_infer_aggregator_fee_frac`, so no econ dict has to be
       threaded through).  The fee is clamped at a non-negative-gross
       deduction (as the base build does) and re-split across the
       retail/DAM streams in proportion to their gross, so
       ``revenue_retail_eur + revenue_dam_eur == revenue_eur`` holds
       even in the negative-gross regime where the clamp fires.

    Recovering the gross from the base frame keeps the gross/net identity
    ``revenue_eur + |aggregator_fee_eur| == factor * gross_base`` exact
    across the sign flip, and the explicit fee rederivation keeps the two
    columns in sync against any future non-uniformly-scaled term (a fixed
    surcharge, a balancing-bundled fee variant, ...).
    """
    df = yearly_cf.copy()
    frac = _infer_aggregator_fee_frac(df)

    # Step 1 — recover the true per-year gross from the base frame.  This is
    # correct in both the fee-applied (revenue_eur == (1-frac)*gross) and the
    # fee-free / clamped (revenue_eur == gross, fee == 0) years.
    revenue_base = df["revenue_eur"].astype(float)
    if "aggregator_fee_eur" in df.columns:
        fee_base_abs = df["aggregator_fee_eur"].astype(float).abs()
    else:
        fee_base_abs = pd.Series(0.0, index=df.index)
    gross_base = revenue_base + fee_base_abs
    # Per-year (1 - frac): 1.0 in the fee-free / clamped years, (1 - frac)
    # where the fee applied.  Guard the zero-gross division exactly as the
    # per-stream split below (gross_base ~ 0 -> treat the year as fee-free).
    nonzero_base = gross_base.abs() > 1e-12
    one_minus_f_year = (
        revenue_base / gross_base.where(nonzero_base, 1.0)
    ).where(nonzero_base, 1.0)

    # Balancing and PPA revenue streams carry no energy-aggregator fee, so
    # they scale by the driver directly.  The optional balancing-aggregator
    # (BSP) fee is proportional to gross balancing revenue, so it scales by
    # the same factor and stays in sync with balancing_revenue_eur.  The
    # optimizer revenue share is proportional to the (price-driven) BESS
    # trading margin, so it scales with the revenue driver too.  The
    # route-to-market fee does NOT scale: it is EUR/MWh on exported VOLUME,
    # and the revenue driver perturbs prices, not energy.
    for col in (
        "balancing_capacity_revenue_eur",
        "balancing_activation_revenue_eur",
        "balancing_revenue_eur",
        "balancing_aggregator_fee_eur",
        "optimizer_fee_eur",
        "ppa_revenue_eur",
        # Informational netting base of the contracted BESS structures
        # (Eq. E25a): price-proportional, so it scales with the driver;
        # it is NOT part of net_cashflow_eur (see _recompute_net).
        "bess_market_revenue_eur",
        # Imbalance settlement (Eq. E28): a price-spread times volume —
        # price-proportional, so it scales with the Revenue driver
        # (same rationale as the balancing columns).
        "imbalance_cost_eur",
        # Revenue levy (Eq. E33): its base is a uniform-scaling sum of
        # price-driven market streams and factor > 0 preserves the
        # zero-turnover clamp (max(f*base, 0) == f*max(base, 0)), so
        # the constant scale is exact.
        "revenue_levy_eur",
        # Curtailment compensation (Eq. E49): the compensated volume
        # is paid at an administered price that regimes typically link
        # to the market value of the curtailed energy — classified
        # price-linked, so it scales with the Revenue driver.
        "curtailment_compensation_eur",
        # GO revenue (Eq. E54): certificate prices move with the
        # renewables market, so the driver scales it.
        "go_revenue_eur",
        # Intraday margin (Eq. E58): a price SPREAD times traded
        # volume — price-proportional like the imbalance settlement,
        # so it scales with the Revenue driver.  Its venue fee does
        # NOT (see below).
        "intraday_revenue_eur",
    ):
        if col in df.columns:
            df[col] = df[col].astype(float) * float(factor)
    # intraday_fee_eur (Eq. E59) does NOT scale with the Revenue
    # driver: it is EUR/MWh on traded VOLUME — the exact
    # route_to_market_fee_eur rationale (the driver perturbs prices,
    # not energy).
    # support_settlement_eur (Eqs. E55-E57) does NOT scale with the
    # Revenue driver either: the strike leg is an administered tariff
    # and only the reference leg co-moves with prices — a constant
    # scale would be wrong on the mixed column, so the dedicated
    # SupportStrike driver perturbs the strike via a full rebuild
    # instead (the optimizer-fee vs route-to-market-fee precedent).
    # toll_revenue_eur (Eq. E29) does NOT scale with the Revenue driver:
    # it is a fixed contractual EUR/MW payment — the driver perturbs
    # market prices, which a toll is by construction insulated from
    # (the same no-scale rationale as route_to_market_fee_eur and
    # grid_charging_fee_eur above).  capacity_market_revenue_eur
    # (Eq. E32) does NOT scale either: an administratively set capacity
    # price, not an energy price (the route_to_market_fee_eur
    # precedent) — it still joins the E31a netting base below at its
    # UN-scaled value.

    # Optimizer floor+share (Eq. E30): the fee/top-up pair is PIECEWISE
    # in the margin, so a constant scale is wrong once the floor is
    # enabled — the tornado would miss the kink at M_y = Floor.  With
    # ``econ`` threaded, both columns are recomputed from the SCALED
    # margin base against the UN-scaled floor level (the floor is
    # contractual, not price-linked).  The legacy ``econ=None`` path is
    # exact for the plain share (max(f*M, 0) == f*max(M, 0) for f > 0),
    # which is why the constant scale above remains the default.
    if (
        econ is not None
        and bool(econ.get("optimizer_floor_enabled", False))
        and "optimizer_fee_eur" in df.columns
        and "bess_market_revenue_eur" in df.columns
    ):
        share = max(0.0, min(1.0, float(
            econ.get("optimizer_revenue_share_pct", 0.0) or 0.0
        ) / 100.0))
        floor_rate = max(0.0, float(
            econ.get("optimizer_floor_eur_per_kw_year", 0.0) or 0.0
        ))
        bess_kw = max(0.0, float(econ.get("bess_power_kw", 0.0) or 0.0))
        floor_level = floor_rate * bess_kw * availability_factor(
            float(econ.get("unavailability_pct", 0.0) or 0.0)
        )
        raw_from = econ.get("optimizer_term_year_from", 1)
        term_from = int(1 if raw_from is None else raw_from)
        raw_to = econ.get("optimizer_term_year_to", 0)
        term_to = int(0 if raw_to is None else raw_to)
        basis = str(
            econ.get("optimizer_margin_basis", "dam") or "dam"
        ).strip().lower()
        # The scaled margin base: bess_market_revenue_eur (E25a) was
        # scaled by the driver above.  Under the 'dam' basis the
        # balancing components are subtracted back out (cent-level;
        # the E25a column is the only stored decomposition).
        margin = df["bess_market_revenue_eur"].astype(float).copy()
        if basis != "dam_plus_balancing":
            for bal_col in (
                "balancing_capacity_revenue_eur",
                "balancing_activation_revenue_eur",
                "balancing_aggregator_fee_eur",
            ):
                if bal_col in df.columns:
                    margin = margin - df[bal_col].astype(float)
        years = df["project_year"].astype(int)
        n_years = int(years.max())
        in_term = years.map(
            lambda y: y >= 1 and _contract_phase(
                y, term_from, term_to, n_years,
            )
        )
        fee = (
            -share * (margin - floor_level).clip(lower=0.0) + 0.0
        ).where(in_term, 0.0)
        topup = (
            (floor_level - margin).clip(lower=0.0) + 0.0
        ).where(in_term, 0.0)
        df["optimizer_fee_eur"] = fee
        if "optimizer_floor_topup_eur" in df.columns:
            df["optimizer_floor_topup_eur"] = topup

    # State support (Eqs. E31/E31a): the gross support does NOT scale
    # (fixed EUR/MW), and the two-way netting is recomputed from the
    # SCALED market-revenue base against the UN-scaled indexed
    # threshold — the netting is revenue-stabilising, so the Revenue
    # tornado bars visibly narrow as the share rises (documented in
    # docs/uncertainty_design.md).  Linear (no clamp), so the recompute
    # is exact.
    if (
        econ is not None
        and "state_support_clawback_eur" in df.columns
        and "bess_market_revenue_eur" in df.columns
    ):
        ss_share = max(0.0, min(1.0, float(econ.get(
            "state_support_clawback_share_pct", 0.0,
        ) or 0.0) / 100.0))
        ss_rate = max(0.0, float(
            econ.get("state_support_eur_per_mw_year", 0.0) or 0.0
        ))
        if ss_share > 0.0 and ss_rate > 0.0:
            ss_theta = max(0.0, float(econ.get(
                "state_support_clawback_threshold_eur_per_mw_year", 0.0,
            ) or 0.0))
            ss_bess_kw = max(0.0, float(
                econ.get("bess_power_kw", 0.0) or 0.0
            ))
            ss_infl = float(
                econ.get("state_support_indexation_pct", 0.0) or 0.0
            ) / 100.0
            raw_ss_from = econ.get("state_support_year_from", 1)
            ss_from = int(1 if raw_ss_from is None else raw_ss_from)
            raw_ss_to = econ.get("state_support_year_to", 0)
            ss_to = int(0 if raw_ss_to is None else raw_ss_to)
            years_ss = df["project_year"].astype(int)
            n_years_ss = int(years_ss.max())
            in_window = years_ss.map(
                lambda y: y >= 1 and _contract_phase(
                    y, ss_from, ss_to, n_years_ss,
                )
            )
            base = df["bess_market_revenue_eur"].astype(float)
            if "capacity_market_revenue_eur" in df.columns:
                base = base + df["capacity_market_revenue_eur"].astype(
                    float,
                )
            theta_y = (
                ss_theta * (ss_bess_kw / 1000.0)
                * (1.0 + ss_infl) ** (years_ss - 1)
            )
            df["state_support_clawback_eur"] = (
                -ss_share * (base - theta_y) + 0.0
            ).where(in_window, 0.0)

    # Step 2 — scale the gross and rederive the fee with the SAME frac and the
    # SAME non-negative-gross clamp the base build applies (economics.py:
    # 399-405): the aggregator fee is a non-negative deduction; BSPs do not
    # rebate negative-gross dispatches.  Without the clamp the perturbed
    # cashflow would flip the fee sign whenever the perturbed gross turns
    # negative, which the base build never does.
    gross = float(factor) * gross_base
    fee = -frac * gross.clip(lower=0.0)
    df["aggregator_fee_eur"] = fee
    df["revenue_eur"] = gross + fee

    # Re-split the (possibly clamped) fee across the retail/DAM streams in
    # proportion to their gross contribution so the per-stream net columns
    # still sum to revenue_eur once the gross<0 clamp has zeroed the fee --
    # mirrors build_yearly_cashflow's per-stream fee split (same 1e-12
    # zero-gross threshold).
    has_streams = (
        "revenue_retail_eur" in df.columns
        and "revenue_dam_eur" in df.columns
    )
    if has_streams:
        # Recover each stream's per-year gross from its base net the same way
        # as the total (net / one_minus_f_year), then scale by the driver.
        retail_gross = float(factor) * (
            df["revenue_retail_eur"].astype(float) / one_minus_f_year
        )
        dam_gross = gross - retail_gross
        nonzero = gross.abs() > 1e-12
        retail_share = (retail_gross / gross.where(nonzero, 1.0)).where(
            nonzero, 0.0
        )
        retail_fee = fee * retail_share
        dam_fee = fee - retail_fee
        df["revenue_retail_eur"] = retail_gross + retail_fee
        df["revenue_dam_eur"] = dam_gross + dam_fee

    return _recompute_net(df)


def _infer_aggregator_fee_frac(df: pd.DataFrame) -> float:
    """Recover the aggregator-fee fraction the base cashflow used.

    Uses the algebraic identity
    ``aggregator_fee_eur = -aggregator_fee_frac * (revenue_eur +
    |aggregator_fee_eur|)`` over any year where the fee is non-zero,
    so the perturbation stays in sync with the base cashflow without
    having to thread the ``econ`` dict through the sensitivity helpers.
    Returns 0 when ``aggregator_fee_eur`` is missing or identically zero.
    """
    if "aggregator_fee_eur" not in df.columns:
        return 0.0
    revenue_net = df.get("revenue_eur", pd.Series(0.0, index=df.index)).astype(float)
    fee = df["aggregator_fee_eur"].astype(float)
    # Look at any year where the fee is non-trivial; avoid Year-0 (CAPEX
    # row, fee is zero by construction).
    mask = fee.abs() > 1e-6
    if not bool(mask.any()):
        return 0.0
    fee_abs = float(fee[mask].abs().iloc[0])
    rev = float(revenue_net[mask].iloc[0])
    denom = rev + fee_abs
    if abs(denom) <= 1e-12:
        return 0.0
    return max(0.0, min(1.0, fee_abs / denom))


def _rebuild_with_discount_rate(
    year1_kpis: dict[str, Any],
    econ: dict[str, Any],
    capacities: dict[str, float],
    new_rate_pct: float,
) -> pd.DataFrame:
    """Rebuild ``yearly_cf`` from scratch with an alternative discount rate."""
    perturbed = dict(econ)
    perturbed["discount_rate_pct"] = float(new_rate_pct)
    return build_yearly_cashflow(year1_kpis, perturbed, capacities)


# ---------------------------------------------------------------------------
# Public orchestrator
# ---------------------------------------------------------------------------


def run_sensitivity_analysis(
    year1_kpis: dict[str, Any],
    econ: dict[str, Any],
    capacities: dict[str, float],
    base_kpis: dict[str, float],
) -> pd.DataFrame:
    """Run the four-driver tornado sensitivity around the base case.

    The CAPEX driver perturbs every ``capex_eur`` / ``devex_eur`` row —
    the Year-0 outlay (per-asset CAPEX, per-asset DEVEX, the site-wide
    lump sum) AND the scheduled BESS replacement CAPEX, which is a
    percentage of the same unit cost.  The driver VALUE recorded for the
    tornado annotations is the Year-0 outlay only, so the EUR labels
    agree with the Year-0 stack in the other charts and with the
    ``initial_investment_eur`` KPI.
    """
    variables = variables_for_npv_sensitivity(econ)
    rows: list[dict[str, Any]] = []

    base_yearly_cf = build_yearly_cashflow(year1_kpis, econ, capacities)
    # The CAPEX driver VALUE shown on the tornado is the Year-0 outlay
    # (per-asset CAPEX + DEVEX + site lump sum) so the EUR labels agree
    # with the Year-0 stack in the other financial charts and with the
    # initial_investment_eur KPI.  The +/-delta perturbation itself
    # still scales every capex_eur row — the replacement CAPEX is a
    # percentage of the same unit cost, so a +/-X % CAPEX world moves
    # it by the same factor (see _scale_capex).
    _y0_mask = base_yearly_cf["project_year"] == 0
    base_capex_total = float(base_yearly_cf.loc[_y0_mask, "capex_eur"].sum())
    if "devex_eur" in base_yearly_cf.columns:
        base_capex_total += float(
            base_yearly_cf.loc[_y0_mask, "devex_eur"].sum()
        )
    # Same convention for the OPEX / Revenue drivers: the perturbation
    # scales EVERY year of the stream, but the recorded driver VALUE is
    # the Year-1 figure so the EUR labels agree with the row labels
    # ("Total annual OPEX", "Year-1 revenue base") and with the yearly
    # cashflow chart.
    _y1_mask = base_yearly_cf["project_year"] == 1
    base_opex_year1 = float(base_yearly_cf.loc[_y1_mask, "opex_eur"].sum())
    base_revenue_year1 = float(
        base_yearly_cf.loc[_y1_mask, "revenue_eur"].sum()
    )
    if "balancing_revenue_eur" in base_yearly_cf.columns:
        base_revenue_year1 += float(
            base_yearly_cf.loc[_y1_mask, "balancing_revenue_eur"].sum()
        )
    if "ppa_revenue_eur" in base_yearly_cf.columns:
        base_revenue_year1 += float(
            base_yearly_cf.loc[_y1_mask, "ppa_revenue_eur"].sum()
        )
    base_rate = float(econ.get("discount_rate_pct", 7.0))

    base_npv = float(base_kpis.get("npv_eur", float("nan")))
    base_irr = float(base_kpis.get("irr_pct", float("nan")))
    base_payback_simple = float(base_kpis.get("simple_payback_years", float("nan")))

    def _record(
        variable: str, label: str, scenario: str,
        delta_value: float, value: float,
        kpis: dict[str, float] | None,
    ) -> None:
        if kpis is None:
            npv = irr = payback = float("nan")
        else:
            npv = float(kpis.get("npv_eur", float("nan")))
            irr = float(kpis.get("irr_pct", float("nan")))
            payback = float(kpis.get("simple_payback_years", float("nan")))
        d_npv = (
            float("nan") if (np.isnan(npv) or np.isnan(base_npv))
            else npv - base_npv
        )
        d_irr = (
            float("nan") if (np.isnan(irr) or np.isnan(base_irr))
            else irr - base_irr
        )
        d_payback = (
            float("nan") if (np.isnan(payback) or np.isnan(base_payback_simple))
            else payback - base_payback_simple
        )
        rows.append(
            {
                "variable": variable,
                "label": label,
                "scenario": scenario,
                "delta_value": float(delta_value),
                "value": float(value),
                "npv_eur": float(round(npv, 2)) if not np.isnan(npv) else npv,
                "irr_pct": (
                    float(round(irr, 4)) if not np.isnan(irr) else irr
                ),
                "payback_years": (
                    float(round(payback, 4)) if not np.isnan(payback) else payback
                ),
                "delta_npv_eur": (
                    float(round(d_npv, 2)) if not np.isnan(d_npv) else d_npv
                ),
                "delta_irr_pp": (
                    float(round(d_irr, 4)) if not np.isnan(d_irr) else d_irr
                ),
                "delta_payback_years": (
                    float(round(d_payback, 4))
                    if not np.isnan(d_payback) else d_payback
                ),
            }
        )

    for var in variables:
        name = str(var["name"])
        label = str(var["label"])
        delta = float(var["delta"])

        if name == "CAPEX":
            base_value = base_capex_total
            # "low"/"high" name the signed driver perturbation, so the
            # absolute value at each scenario must track delta_value:
            # low => -delta, high => +delta.
            low_value = base_capex_total * (1.0 - delta)
            high_value = base_capex_total * (1.0 + delta)
            low_kpis = compute_financial_kpis(
                _scale_capex(base_yearly_cf, 1.0 - delta), econ,
            )
            high_kpis = compute_financial_kpis(
                _scale_capex(base_yearly_cf, 1.0 + delta), econ,
            )
            _record(name, label, "base", 0.0, base_value, base_kpis)
            _record(name, label, "low", -delta, low_value, low_kpis)
            _record(name, label, "high", +delta, high_value, high_kpis)
            continue

        if name == "OPEX":
            base_value = base_opex_year1
            low_value = base_opex_year1 * (1.0 - delta)
            high_value = base_opex_year1 * (1.0 + delta)
            low_kpis = compute_financial_kpis(
                _scale_opex(base_yearly_cf, 1.0 - delta), econ,
            )
            high_kpis = compute_financial_kpis(
                _scale_opex(base_yearly_cf, 1.0 + delta), econ,
            )
            _record(name, label, "base", 0.0, base_value, base_kpis)
            _record(name, label, "low", -delta, low_value, low_kpis)
            _record(name, label, "high", +delta, high_value, high_kpis)
            continue

        if name == "Revenue":
            base_value = base_revenue_year1
            low_value = base_revenue_year1 * (1.0 - delta)
            high_value = base_revenue_year1 * (1.0 + delta)
            low_kpis = compute_financial_kpis(
                _scale_revenue(base_yearly_cf, 1.0 - delta, econ), econ,
            )
            high_kpis = compute_financial_kpis(
                _scale_revenue(base_yearly_cf, 1.0 + delta, econ), econ,
            )
            _record(name, label, "base", 0.0, base_value, base_kpis)
            _record(name, label, "low", -delta, low_value, low_kpis)
            _record(name, label, "high", +delta, high_value, high_kpis)
            continue

        if name == "PpaPrice":
            # Exact strike rescaling of the Year-1 bases: the contract
            # leg is linear in the strike (physical: covered x strike;
            # cfd: covered x (strike - DAM), whose strike part is
            # covered x strike).  The cashflow is rebuilt from the
            # rescaled KPI bases so the term / reversion / escalation
            # arithmetic stays exact.
            base_strike = float(econ.get("ppa_price_eur_per_mwh", 0.0) or 0.0)
            settlement = str(
                econ.get("ppa_settlement", "physical") or "physical"
            ).strip().lower()
            rev1_ppa = float(
                year1_kpis.get("revenue_pv_ppa_eur", 0.0) or 0.0
            )
            covered_dam = float(
                year1_kpis.get("ppa_covered_dam_value_eur", 0.0) or 0.0
            )
            strike_value = (
                rev1_ppa + covered_dam if settlement == "cfd" else rev1_ppa
            )
            if base_strike <= 0.0 or abs(strike_value) < 1e-9:
                continue  # no meaningful PPA stream to perturb

            def _ppa_kpis_at(
                factor: float,
                *,
                _strike_value: float = strike_value,
                _covered_dam: float = covered_dam,
                _settlement: str = settlement,
                _rev1_ppa: float = rev1_ppa,
            ) -> dict[str, Any]:
                rescaled = dict(year1_kpis)
                new_leg = _strike_value * factor - (
                    _covered_dam if _settlement == "cfd" else 0.0
                )
                rescaled["revenue_pv_ppa_eur"] = new_leg
                rescaled["profit_total_eur"] = float(
                    year1_kpis.get("profit_total_eur", 0.0) or 0.0
                ) + (new_leg - _rev1_ppa)
                return rescaled

            low_kpis = compute_financial_kpis(
                build_yearly_cashflow(
                    _ppa_kpis_at(1.0 - delta), econ, capacities,
                ),
                econ,
            )
            high_kpis = compute_financial_kpis(
                build_yearly_cashflow(
                    _ppa_kpis_at(1.0 + delta), econ, capacities,
                ),
                econ,
            )
            _record(name, label, "base", 0.0, base_strike, base_kpis)
            _record(
                name, label, "low", -delta,
                base_strike * (1.0 - delta), low_kpis,
            )
            _record(
                name, label, "high", +delta,
                base_strike * (1.0 + delta), high_kpis,
            )
            continue

        if name == "SupportStrike":
            base_strike = float(
                econ.get("support_strike_eur_per_mwh", 0.0) or 0.0
            )
            if base_strike <= 0.0:
                continue
            low_kpis = compute_financial_kpis(
                build_yearly_cashflow(
                    year1_kpis,
                    {**econ, "support_strike_eur_per_mwh":
                     base_strike * (1.0 - delta)},
                    capacities,
                ),
                econ,
            )
            high_kpis = compute_financial_kpis(
                build_yearly_cashflow(
                    year1_kpis,
                    {**econ, "support_strike_eur_per_mwh":
                     base_strike * (1.0 + delta)},
                    capacities,
                ),
                econ,
            )
            _record(name, label, "base", 0.0, base_strike, base_kpis)
            _record(
                name, label, "low", -delta,
                base_strike * (1.0 - delta), low_kpis,
            )
            _record(
                name, label, "high", +delta,
                base_strike * (1.0 + delta), high_kpis,
            )
            continue

        if name == "TaxRate":
            base_rate = float(
                econ.get("corporate_tax_rate_pct", 0.0) or 0.0
            )
            if base_rate <= 0.0:
                continue
            low_rate = max(base_rate - delta, 0.0)
            high_rate = min(base_rate + delta, 100.0)

            def _post_tax_kpis_at(rate_pct: float) -> dict[str, float]:
                econ_r = {**econ, "corporate_tax_rate_pct": rate_pct}
                return compute_financial_kpis(
                    build_yearly_cashflow(year1_kpis, econ_r, capacities),
                    econ_r,
                )

            base_npv_pt = float(
                base_kpis.get("npv_post_tax_eur", float("nan"))
            )
            base_irr_pt = float(
                base_kpis.get("irr_post_tax_pct", float("nan"))
            )

            def _post_tax_fields(
                k: dict[str, float],
                *,
                _base_npv_pt: float = base_npv_pt,
                _base_irr_pt: float = base_irr_pt,
            ) -> dict[str, float]:
                npv_pt = float(k.get("npv_post_tax_eur", float("nan")))
                irr_pt = float(k.get("irr_post_tax_pct", float("nan")))
                d_npv = (
                    float("nan")
                    if (np.isnan(npv_pt) or np.isnan(_base_npv_pt))
                    else npv_pt - _base_npv_pt
                )
                d_irr = (
                    float("nan")
                    if (np.isnan(irr_pt) or np.isnan(_base_irr_pt))
                    else irr_pt - _base_irr_pt
                )
                return {
                    "npv_post_tax_eur": npv_pt,
                    "irr_post_tax_pct": irr_pt,
                    "delta_npv_post_tax_eur": d_npv,
                    "delta_irr_post_tax_pp": d_irr,
                }

            # Pre-tax metric columns stay NaN (kpis=None) — the driver
            # moves only the post-tax family by construction.
            _record(name, label, "base", 0.0, base_rate, None)
            rows[-1].update(_post_tax_fields(base_kpis))
            _record(name, label, "low", -delta, low_rate, None)
            rows[-1].update(_post_tax_fields(_post_tax_kpis_at(low_rate)))
            _record(name, label, "high", +delta, high_rate, None)
            rows[-1].update(_post_tax_fields(_post_tax_kpis_at(high_rate)))
            continue

        if name == "DiscountRate":
            base_value = base_rate
            low_value = max(base_rate - delta, -99.0)
            high_value = base_rate + delta
            try:
                low_cf = _rebuild_with_discount_rate(
                    year1_kpis, econ, capacities, low_value,
                )
                low_kpis = compute_financial_kpis(
                    low_cf, {**econ, "discount_rate_pct": low_value},
                )
            except (ValueError, ArithmeticError, KeyError, TypeError):
                low_kpis = None  # type: ignore[assignment]
            try:
                high_cf = _rebuild_with_discount_rate(
                    year1_kpis, econ, capacities, high_value,
                )
                high_kpis = compute_financial_kpis(
                    high_cf, {**econ, "discount_rate_pct": high_value},
                )
            except (ValueError, ArithmeticError, KeyError, TypeError):
                high_kpis = None  # type: ignore[assignment]
            _record(name, label, "base", 0.0, base_value, base_kpis)
            _record(name, label, "low", -delta, low_value, low_kpis)
            _record(name, label, "high", +delta, high_value, high_kpis)
            continue

    return pd.DataFrame(rows)


def build_driver_sensitivities(
    sens_df: pd.DataFrame, metric: str,
) -> dict[str, DriverSensitivity]:
    """Build ``{label: DriverSensitivity}`` from a sensitivity frame.

    ``metric`` is the outcome column (``"irr_pct"`` or ``"npv_eur"``).
    Returns an empty dict when the frame lacks the driver-value
    metadata (``value`` / ``delta_value``) or any required scenario row
    — callers then fall back to the metadata-free tornado layout.
    """
    if sens_df is None or sens_df.empty:
        return {}
    required = {"variable", "label", "scenario", "value", "delta_value", metric}
    if not required.issubset(sens_df.columns):
        return {}

    out: dict[str, DriverSensitivity] = {}
    for label, grp in sens_df.groupby("label"):
        by_scen = grp.drop_duplicates("scenario").set_index("scenario")
        if not {"base", "low", "high"}.issubset(by_scen.index):
            continue
        variable = str(grp["variable"].iloc[0])
        driver_type = _DRIVER_TYPE_BY_VARIABLE.get(variable, variable.lower())
        # Relative drivers store delta_value as a fraction (0.20); the
        # discount-rate driver stores it directly in percentage points.
        # pandas .loc returns a broad Scalar type; the columns are
        # numeric by construction (built by run_sensitivity_analysis).
        delta = abs(float(by_scen.loc["low", "delta_value"]))  # type: ignore[arg-type]
        sens_pct = delta if driver_type == "discount_rate" else delta * 100.0
        try:
            record = DriverSensitivity(
                name=variable,
                driver_type=driver_type,
                base_value=float(by_scen.loc["base", "value"]),  # type: ignore[arg-type]
                low_value=float(by_scen.loc["low", "value"]),  # type: ignore[arg-type]
                high_value=float(by_scen.loc["high", "value"]),  # type: ignore[arg-type]
                sensitivity_pct=float(sens_pct),
            )
        except (TypeError, ValueError):
            continue
        if np.isnan(record.base_value) or np.isnan(record.low_value):
            continue
        out[str(label)] = record
    return out
