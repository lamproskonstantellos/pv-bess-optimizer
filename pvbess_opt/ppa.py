"""PPA (power purchase agreement) contract configuration.

Design note: ``docs/ppa_design.md`` maps the PLEXOS / Gridcog contract
concepts onto the engine implemented across this package.  One
structure is implemented — **pay-as-produced** on a configurable share
of the PV export — with two settlement decompositions:

* ``physical`` (sleeved): the covered volume is paid the strike and
  never touches the DAM.
* ``cfd`` (virtual / financial): all PV export sells at DAM and the
  covered volume adds a two-way contract-for-difference leg
  (``strike − DAM``, negative when the DAM exceeds the strike).

Both settlements total ``share × export × strike`` on the covered
volume, so the dispatch incentive is identical (the MILP prices PV
export at ``(1 − s)·DAM + s·strike``) and only the revenue
decomposition differs.

A second structure — **baseload** — settles a contracted flat band
``ppa_baseload_mw`` financially against the plant's total export
(Eqs. P9-P11): every in-term step exchanges the fixed volume at
``strike − DAM`` (shortfall implicitly bought at spot, excess sold at
spot — the two forms are identical under symmetric spot settlement:
``Q·strike + (delivered − Q)·DAM = delivered·DAM + Q·(strike − DAM)``).
v1 is cfd-only: with that identity a physical sleeved variant totals
the same, and the financial decomposition reuses the existing PPA
columns end-to-end.  The fixed-volume leg contains no decision
variables, so dispatch is provably unchanged (Eq. P11) — firming
becomes a dispatch incentive only under asymmetric imbalance pricing,
which is recorded as future work in the design note.

This module owns the parsed configuration; the consumers are
:func:`pvbess_opt.kpis.add_economic_columns` (per-step EUR columns),
:func:`pvbess_opt.optimization.build_model` (objective),
:func:`pvbess_opt.economics.build_yearly_cashflow` (multi-year stream
with its own ``ppa_inflation_pct`` indexation and the post-term
reversion), and the availability derate list.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

import pandas as pd

__all__ = [
    "SUPPORT_REF_PERIODS",
    "SUPPORT_SCHEMES",
    "PpaConfig",
    "compute_support_settlement",
    "negative_price_mask",
    "resolve_ppa_config",
]

# The ppa_structure / ppa_settlement / ppa_negative_price_rule enums are
# validated centrally in ``io._ALLOWED_VALUES`` (the input-schema
# authority); no separate copy is kept here to avoid a silent drift trap.
#: Reference-period support settlement (Eqs. E55-E57 in
#: docs/economics_design.md): none (off), the Greek DAPEEP one-way
#: sliding Feed-in-Premium, or a two-way CfD.
SUPPORT_SCHEMES: tuple[str, ...] = ("none", "sliding_fip", "cfd_two_way")
SUPPORT_REF_PERIODS: tuple[str, ...] = ("monthly", "hourly")


def negative_price_mask(dam_series: pd.Series) -> pd.Series:
    """Per-step negative-DAM indicator ``m_t = 1{pi_DAM,t < 0}`` (Eq. P6).

    STRICT inequality — a zero price is not a negative hour.  Shared
    classifier: the PPA suspension clause (Eqs. P7/P8) consumes it
    today and reference-period support settlements (negative-hour
    eligibility) are expected to reuse it, so the definition lives in
    exactly one place.
    """
    return dam_series.astype(float) < 0.0


@dataclass(frozen=True, slots=True)
class PpaConfig:
    """Parsed ``ppa`` workbook section.

    Field names mirror the workbook keys verbatim; validation is
    performed by :func:`pvbess_opt.io._validate_ppa_config` at load
    time.  ``ppa_term_years`` counts operating years 1..term under
    contract; beyond the term the covered volume reverts to the DAM.
    """

    ppa_enabled: bool = False
    ppa_structure: str = "pay_as_produced"
    ppa_settlement: str = "physical"
    ppa_price_eur_per_mwh: float = 65.0
    ppa_volume_share_pct: float = 100.0
    ppa_term_years: int = 10
    ppa_inflation_pct: float = 0.0
    ppa_negative_price_rule: str = "none"
    ppa_baseload_mw: float = 0.0

    @property
    def active(self) -> bool:
        """True when the contract binds the Year-1 dispatch / revenue."""
        if not (self.ppa_enabled and self.ppa_term_years >= 1):
            return False
        if self.ppa_structure == "pay_as_produced":
            return self.ppa_volume_share_pct > 0.0
        if self.ppa_structure == "baseload":
            return self.ppa_baseload_mw > 0.0
        return False

    @property
    def share_frac(self) -> float:
        """Covered share of PV export in [0, 1] (0 when inactive).

        The baseload band is an ABSOLUTE volume, not a share — it
        reports 0 here so no share-based algebra ever touches it.
        """
        if not self.active or self.ppa_structure != "pay_as_produced":
            return 0.0
        return max(0.0, min(1.0, float(self.ppa_volume_share_pct) / 100.0))

    @property
    def reshapes_dispatch_price(self) -> bool:
        """True when the contract changes the MILP's PV export price.

        Only the pay-as-produced structure blends the strike into the
        per-step price (Eqs. P4/P8).  The baseload leg contains no
        decision variables (Eq. P11: an additive constant in the
        objective), so dispatch stays merchant-optimal and the price
        rebuild is skipped.
        """
        return self.active and self.ppa_structure == "pay_as_produced"

    @property
    def suspension_active(self) -> bool:
        """True when the negative-hour clause pauses the contract.

        With the clause on, every step with DAM < 0 settles at spot for
        the covered volume (physical: no-pay; cfd: the difference leg
        is suspended while the market leg keeps selling) — Eqs. P6-P8.
        """
        return self.active and self.ppa_negative_price_rule == "suspend"


def resolve_ppa_config(raw: dict[str, Any] | None) -> PpaConfig:
    """Build a :class:`PpaConfig` from the workbook dict.

    Missing keys fall back to the dataclass defaults; unknown keys are
    ignored (the workbook loader already warns on them).  Mirrors
    :func:`pvbess_opt.balancing.resolve_balancing_config`.
    """
    raw = raw or {}
    kwargs: dict[str, Any] = {}
    for fld in fields(PpaConfig):
        if fld.name not in raw:
            continue
        value = raw[fld.name]
        if fld.name == "ppa_enabled":
            kwargs[fld.name] = bool(value)
        elif fld.name == "ppa_term_years":
            kwargs[fld.name] = int(value)
        elif fld.name in (
            "ppa_structure", "ppa_settlement", "ppa_negative_price_rule",
        ):
            kwargs[fld.name] = str(value).strip().lower()
        else:
            kwargs[fld.name] = float(value)
    return PpaConfig(**kwargs)


def compute_support_settlement(
    res: pd.DataFrame,
    *,
    scheme: str,
    strike_eur_per_mwh: float,
    ref_period: str = "monthly",
    suspend_negative: bool = False,
) -> dict[str, Any]:
    """Reference-period support settlement on PV export (Eqs. E55-E57).

    The plant still sells at the DAM — the premium is a settlement
    OVERLAY on the eligible PV-export volume, mirroring the CfD-leg
    philosophy (dispatch is never modified).  Per month ``m`` the
    volume-weighted reference price over eligible steps is

        P_ref_m = sum(p_t * e_t) / sum(e_t)              (Eq. E55)

    and the premium settles as ``E_m * max(K - P_ref_m, 0)`` under
    ``sliding_fip`` (the Greek DAPEEP convention; ``K`` is the
    reference tariff, Timi Anaforas) or ``E_m * (K - P_ref_m)`` under
    ``cfd_two_way`` (Eq. E56).  ``suspend_negative`` removes the
    negative-DAM steps from BOTH the volume and the reference-price
    weighting (Eq. E57; the strict ``p < 0`` classifier is
    :func:`negative_price_mask`, shared with the PPA suspension
    clause).  ``ref_period='hourly'`` degenerates to the per-step CfD
    algebra ``sum(e_t * prem(K - p_t))`` for cross-checks.

    Returns the Year-1 settlement EUR, the eligible export MWh, and
    the per-calendar-month detail (eligible MWh, reference price,
    settlement) the cashflow projection consumes.
    """
    scheme = str(scheme or "none").strip().lower()
    if scheme not in SUPPORT_SCHEMES:
        raise ValueError(
            f"unknown support_scheme {scheme!r}; expected one of "
            f"{SUPPORT_SCHEMES}."
        )
    ref_period = str(ref_period or "monthly").strip().lower()
    if ref_period not in SUPPORT_REF_PERIODS:
        raise ValueError(
            f"unknown support_ref_period {ref_period!r}; expected one "
            f"of {SUPPORT_REF_PERIODS}."
        )
    for col in ("timestamp", "pv_to_grid_kwh", "dam_price_eur_per_mwh"):
        if col not in res.columns:
            raise ValueError(
                f"compute_support_settlement requires the {col!r} "
                "column on the dispatch frame."
            )
    strike = float(strike_eur_per_mwh)
    prices = res["dam_price_eur_per_mwh"].astype(float)
    export_mwh = res["pv_to_grid_kwh"].astype(float) / 1000.0
    eligible = (
        ~negative_price_mask(prices) if suspend_negative
        else pd.Series(True, index=res.index)
    )
    e = export_mwh.where(eligible, 0.0)
    months = pd.to_datetime(res["timestamp"]).dt.month

    monthly_e = [0.0] * 12
    monthly_ref = [0.0] * 12
    monthly_settlement = [0.0] * 12

    def _prem(diff: float) -> float:
        return max(diff, 0.0) if scheme == "sliding_fip" else diff

    total = 0.0
    for m in range(1, 13):
        mask = months == m
        e_m = float(e[mask].sum())
        monthly_e[m - 1] = e_m
        if e_m <= 1e-12:
            continue
        p_ref = float((prices[mask] * e[mask]).sum()) / e_m
        monthly_ref[m - 1] = p_ref
        if ref_period == "hourly":
            r_m = float(
                (e[mask] * (strike - prices[mask]).apply(_prem)).sum()
            )
        else:
            r_m = e_m * _prem(strike - p_ref)
        monthly_settlement[m - 1] = r_m
        total += r_m
    return {
        "support_settlement_eur": float(total),
        "support_eligible_export_mwh": float(sum(monthly_e)),
        "support_monthly_eligible_mwh": monthly_e,
        "support_monthly_ref_price_eur_per_mwh": monthly_ref,
        "support_monthly_settlement_eur": monthly_settlement,
    }
