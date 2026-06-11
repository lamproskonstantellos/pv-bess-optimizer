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
decomposition differs.  ``baseload`` is reserved in the structure enum
but not implemented — it needs shortfall-pricing rules (see the design
note).

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

__all__ = [
    "PPA_SETTLEMENTS",
    "PPA_STRUCTURES",
    "PpaConfig",
    "resolve_ppa_config",
]

# ``baseload`` is reserved for the designed-but-not-implemented shaped
# profile (docs/ppa_design.md); the loader rejects it with guidance.
PPA_STRUCTURES: tuple[str, ...] = ("pay_as_produced",)
PPA_SETTLEMENTS: tuple[str, ...] = ("physical", "cfd")


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

    @property
    def active(self) -> bool:
        """True when the contract binds the Year-1 dispatch / revenue."""
        return (
            self.ppa_enabled
            and self.ppa_structure == "pay_as_produced"
            and self.ppa_volume_share_pct > 0.0
            and self.ppa_term_years >= 1
        )

    @property
    def share_frac(self) -> float:
        """Covered share of PV export in [0, 1] (0 when inactive)."""
        if not self.active:
            return 0.0
        return max(0.0, min(1.0, float(self.ppa_volume_share_pct) / 100.0))


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
        elif fld.name in ("ppa_structure", "ppa_settlement"):
            kwargs[fld.name] = str(value).strip().lower()
        else:
            kwargs[fld.name] = float(value)
    return PpaConfig(**kwargs)
