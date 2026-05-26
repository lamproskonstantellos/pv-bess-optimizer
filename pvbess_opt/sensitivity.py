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

from .constants import (
    DEFAULT_SENSITIVITY_DELTA_PCT,
    DEFAULT_SENSITIVITY_DISCOUNT_RATE_DELTA_PP,
)
from .economics import build_yearly_cashflow, compute_financial_kpis

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

    Carries both the absolute driver values (``*_value``) and the
    resulting metric outcomes (``*_outcome``) so a tornado plot can
    annotate each bar end with the driver value that produced it.
    """

    name: str               # e.g. "CAPEX" — the variable identifier
    driver_type: str         # e.g. "capex" — keys the numeric formatter
    base_value: float        # base case absolute driver value
    low_value: float         # absolute driver value at the low end
    high_value: float        # absolute driver value at the high end
    low_outcome: float       # IRR or NPV at the low driver end
    high_outcome: float      # IRR or NPV at the high driver end
    sensitivity_pct: float   # the +/- magnitude used (e.g. 20.0)


# Maps the ``variable`` column to a ``driver_type`` understood by the
# tornado numeric formatter.
_DRIVER_TYPE_BY_VARIABLE: dict[str, str] = {
    "CAPEX": "capex",
    "OPEX": "opex",
    "Revenue": "revenue",
    "DiscountRate": "discount_rate",
}


def variables_for_npv_sensitivity(
    econ: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return the four canonical NPV-sensitivity variables."""
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
    components = ["revenue_eur", "opex_eur", "capex_eur"]
    if "devex_eur" in df.columns:
        components.append("devex_eur")
    if "balancing_revenue_eur" in df.columns:
        components.append("balancing_revenue_eur")
    df["net_cashflow_eur"] = sum(df[c].astype(float) for c in components)
    df["discounted_cf_eur"] = (
        df["net_cashflow_eur"] * df["discount_factor"].astype(float)
    )
    df["cumulative_cf_eur"] = df["net_cashflow_eur"].cumsum()
    df["cumulative_dcf_eur"] = df["discounted_cf_eur"].cumsum()
    return df


def _scale_capex(yearly_cf: pd.DataFrame, factor: float) -> pd.DataFrame:
    """Scale CAPEX and DEVEX by the same factor.

    The CAPEX driver represents the full Year-0 outlay — per-asset CAPEX
    plus per-asset DEVEX plus the site-wide lump sum — so a single factor
    scales the whole ``capex_eur`` / ``devex_eur`` Year-0 rows together.
    """
    df = yearly_cf.copy()
    df["capex_eur"] = df["capex_eur"].astype(float) * float(factor)
    if "devex_eur" in df.columns:
        df["devex_eur"] = df["devex_eur"].astype(float) * float(factor)
    return _recompute_net(df)


def _scale_opex(yearly_cf: pd.DataFrame, factor: float) -> pd.DataFrame:
    df = yearly_cf.copy()
    df["opex_eur"] = df["opex_eur"].astype(float) * float(factor)
    return _recompute_net(df)


def _scale_revenue(yearly_cf: pd.DataFrame, factor: float) -> pd.DataFrame:
    """Scale every revenue stream by the same factor.

    The Revenue driver sweeps the project's Year-1+ income holistically:
    retail + DAM net revenue (``revenue_eur`` and its per-stream
    breakdowns), the aggregator-fee deduction that scales with gross
    revenue, and balancing capacity + activation revenue.  Scaling only
    ``revenue_eur`` would keep balancing constant as an offset and
    yield a "Revenue +10 %" scenario that's actually weaker than the
    base case once balancing dominates — the bug fixed in this commit.
    """
    df = yearly_cf.copy()
    df["revenue_eur"] = df["revenue_eur"].astype(float) * float(factor)
    for col in (
        "revenue_retail_eur",
        "revenue_dam_eur",
        "aggregator_fee_eur",
        "balancing_capacity_revenue_eur",
        "balancing_activation_revenue_eur",
        "balancing_revenue_eur",
    ):
        if col in df.columns:
            df[col] = df[col].astype(float) * float(factor)
    return _recompute_net(df)


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

    The CAPEX driver scales the whole Year-0 outlay — per-asset CAPEX,
    per-asset DEVEX, and the site-wide lump sum (``site_capex_eur`` /
    ``site_devex_eur``) all live inside the ``capex_eur`` / ``devex_eur``
    columns — so a +/-X % CAPEX scenario moves the lump sum too.
    """
    variables = variables_for_npv_sensitivity(econ)
    rows: list[dict[str, Any]] = []

    base_yearly_cf = build_yearly_cashflow(year1_kpis, econ, capacities)
    # The CAPEX driver is the full Year-0 outlay: CAPEX + DEVEX + site
    # lump sum.
    base_capex_total = float(base_yearly_cf["capex_eur"].sum())
    if "devex_eur" in base_yearly_cf.columns:
        base_capex_total += float(base_yearly_cf["devex_eur"].sum())
    base_opex_total = float(
        base_yearly_cf.loc[
            base_yearly_cf["project_year"] >= 1, "opex_eur"
        ].sum()
    )
    after_y0_mask = base_yearly_cf["project_year"] >= 1
    base_revenue_total = float(
        base_yearly_cf.loc[after_y0_mask, "revenue_eur"].sum()
    )
    if "balancing_revenue_eur" in base_yearly_cf.columns:
        base_revenue_total += float(
            base_yearly_cf.loc[after_y0_mask, "balancing_revenue_eur"].sum()
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
                low_outcome=float(by_scen.loc["low", metric]),  # type: ignore[arg-type]
                high_outcome=float(by_scen.loc["high", metric]),  # type: ignore[arg-type]
                sensitivity_pct=float(sens_pct),
            )
        except (TypeError, ValueError):
            continue
        if np.isnan(record.base_value) or np.isnan(record.low_value):
            continue
        out[str(label)] = record
    return out
