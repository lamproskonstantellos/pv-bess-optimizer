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

from typing import Any

import numpy as np
import pandas as pd

from .economics import build_yearly_cashflow, compute_financial_kpis


def variables_for_npv_sensitivity(
    econ: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return the four canonical NPV-sensitivity variables."""
    capex_d = float(econ.get("sensitivity_capex_delta_pct", 10.0)) / 100.0
    opex_d = float(econ.get("sensitivity_opex_delta_pct", 10.0)) / 100.0
    rev_d = float(econ.get("sensitivity_revenue_delta_pct", 10.0)) / 100.0
    rate_d = float(econ.get("sensitivity_discount_rate_delta_pp", 2.0))
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
    return [v for v in raw if v["delta"] > 0.0]


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


def _scale_capex(yearly_cf: pd.DataFrame, factor: float) -> pd.DataFrame:
    df = yearly_cf.copy()
    df["capex_eur"] = df["capex_eur"].astype(float) * float(factor)
    df["net_cashflow_eur"] = (
        df["revenue_eur"].astype(float)
        + df["opex_eur"].astype(float)
        + df["capex_eur"].astype(float)
    )
    df["discounted_cf_eur"] = (
        df["net_cashflow_eur"] * df["discount_factor"].astype(float)
    )
    df["cumulative_cf_eur"] = df["net_cashflow_eur"].cumsum()
    df["cumulative_dcf_eur"] = df["discounted_cf_eur"].cumsum()
    return df


def _scale_opex(yearly_cf: pd.DataFrame, factor: float) -> pd.DataFrame:
    df = yearly_cf.copy()
    df["opex_eur"] = df["opex_eur"].astype(float) * float(factor)
    df["net_cashflow_eur"] = (
        df["revenue_eur"].astype(float)
        + df["opex_eur"].astype(float)
        + df["capex_eur"].astype(float)
    )
    df["discounted_cf_eur"] = (
        df["net_cashflow_eur"] * df["discount_factor"].astype(float)
    )
    df["cumulative_cf_eur"] = df["net_cashflow_eur"].cumsum()
    df["cumulative_dcf_eur"] = df["discounted_cf_eur"].cumsum()
    return df


def _scale_revenue(yearly_cf: pd.DataFrame, factor: float) -> pd.DataFrame:
    df = yearly_cf.copy()
    df["revenue_eur"] = df["revenue_eur"].astype(float) * float(factor)
    df["net_cashflow_eur"] = (
        df["revenue_eur"].astype(float)
        + df["opex_eur"].astype(float)
        + df["capex_eur"].astype(float)
    )
    df["discounted_cf_eur"] = (
        df["net_cashflow_eur"] * df["discount_factor"].astype(float)
    )
    df["cumulative_cf_eur"] = df["net_cashflow_eur"].cumsum()
    df["cumulative_dcf_eur"] = df["discounted_cf_eur"].cumsum()
    return df


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
    """Run the four-driver tornado sensitivity around the base case."""
    variables = variables_for_npv_sensitivity(econ)
    rows: list[dict[str, Any]] = []

    base_yearly_cf = build_yearly_cashflow(year1_kpis, econ, capacities)
    base_capex_total = float(base_yearly_cf["capex_eur"].sum())
    base_opex_total = float(
        base_yearly_cf.loc[
            base_yearly_cf["project_year"] >= 1, "opex_eur"
        ].sum()
    )
    base_revenue_total = float(
        base_yearly_cf.loc[
            base_yearly_cf["project_year"] >= 1, "revenue_eur"
        ].sum()
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
            low_value = base_capex_total * (1.0 + delta)
            high_value = base_capex_total * (1.0 - delta)
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
            base_value = base_opex_total
            low_value = base_opex_total * (1.0 - delta)
            high_value = base_opex_total * (1.0 + delta)
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
            base_value = base_revenue_total
            low_value = base_revenue_total * (1.0 - delta)
            high_value = base_revenue_total * (1.0 + delta)
            low_kpis = compute_financial_kpis(
                _scale_revenue(base_yearly_cf, 1.0 - delta), econ,
            )
            high_kpis = compute_financial_kpis(
                _scale_revenue(base_yearly_cf, 1.0 + delta), econ,
            )
            _record(name, label, "base", 0.0, base_value, base_kpis)
            _record(name, label, "low", -delta, low_value, low_kpis)
            _record(name, label, "high", +delta, high_value, high_kpis)
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
            except Exception:
                low_kpis = None
            try:
                high_cf = _rebuild_with_discount_rate(
                    year1_kpis, econ, capacities, high_value,
                )
                high_kpis = compute_financial_kpis(
                    high_cf, {**econ, "discount_rate_pct": high_value},
                )
            except Exception:
                high_kpis = None
            _record(name, label, "base", 0.0, base_value, base_kpis)
            _record(name, label, "low", -delta, low_value, low_kpis)
            _record(name, label, "high", +delta, high_value, high_kpis)
            continue

    return pd.DataFrame(rows)
