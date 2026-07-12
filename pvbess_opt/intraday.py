"""Intraday (IDA) venue data model and two-stage re-dispatch driver.

The intraday auction is modelled as **two-stage sequential re-dispatch**
(``docs/intraday_design.md``): Stage 1 is the unchanged day-ahead solve;
Stage 2 re-solves the SAME model with the committed day-ahead net
position pinned as data (Eq. I1) and an intraday block added — per-step
IDA sells and buys bounded by a deviation cap (Eq. I2), maximising the
spread margin net of the venue fee (Eq. I3), with every trade mapping
to a physical flow change (Eq. I5).

This module contains the pure-Python pieces: the configuration
dataclass mirroring :class:`pvbess_opt.balancing.BalancingConfig`, the
Stage-1 position extractor, and the Stage-2 orchestration wrapper.  The
Pyomo intraday block itself lives in :mod:`pvbess_opt.optimization`.

When ``id_enabled == False`` (or the position columns are absent) the
MILP, KPIs and outputs are bit-identical to a build without the
feature.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "DA_POSITION_COLUMNS",
    "IntradayConfig",
    "extract_da_position",
    "redispatch_intraday",
    "resolve_intraday_config",
]


# The Stage-1 data columns pinned into the Stage-2 solve (Eq. I1).  The
# net position drives the settlement identity; the per-origin legs feed
# the linking constraints so the origin split (Eq. I4) stays exact.
DA_POSITION_COLUMNS: tuple[str, ...] = (
    "id_da_position_kwh",
    "id_da_pv_export_kwh",
    "id_da_bess_export_kwh",
    "id_da_grid_charge_kwh",
)


@dataclass(frozen=True, slots=True)
class IntradayConfig:
    """Typed view of the workbook ``intraday`` sheet.

    Defaults mirror ``pvbess_opt.io.INTRADAY_SHEET_DEFAULTS``; the
    loader validation lives in ``pvbess_opt.io``.
    """

    id_enabled: bool = False
    # Per-step bound on the traded intraday volume as a fraction of
    # p_grid_export_max_kw x dt (Eq. I2).
    id_max_deviation_frac_of_cap: float = 0.25
    # Physical-only IDA buys (Eq. I5); BESS charging from buys
    # additionally requires allow_bess_grid_charging.
    id_allow_purchases: bool = True
    # Venue trading fee on both buy and sell volume (Eq. E59).
    id_fee_eur_per_mwh: float = 0.0
    # Yearly indexation of the intraday margin in the cashflow.
    id_inflation_pct: float = 0.0


def resolve_intraday_config(raw: dict[str, Any]) -> IntradayConfig:
    """Build an :class:`IntradayConfig` from the workbook dict.

    Unknown keys are ignored (the workbook loader already warns on
    them); missing keys fall back to the dataclass defaults.  Booleans
    are coerced explicitly because workbook readers may deliver
    ``numpy`` scalars.
    """
    kwargs: dict[str, Any] = {}
    for fld in fields(IntradayConfig):
        if fld.name not in raw:
            continue
        value = raw[fld.name]
        if fld.name in ("id_enabled", "id_allow_purchases"):
            kwargs[fld.name] = bool(value)
        else:
            kwargs[fld.name] = float(value)
    return IntradayConfig(**kwargs)


def extract_da_position(res: pd.DataFrame) -> pd.DataFrame:
    """Return the committed day-ahead position columns (Eq. I1).

    ``g_DA_t = x_pg_t + x_bg_t - x_gb_t`` per step, extracted from the
    Stage-1 result frame together with its three per-origin legs (the
    linking constraints in the Stage-2 model need each leg, not just
    the net).  Missing flow columns (single-asset runs) count as zero.
    """
    import pandas as pd

    def _col(name: str) -> pd.Series:
        if name in res.columns:
            return res[name].astype(float).fillna(0.0)
        return pd.Series(0.0, index=res.index)

    pv_export = _col("pv_to_grid_kwh")
    bess_export = _col("bess_dis_grid_kwh")
    grid_charge = _col("bess_charge_grid_kwh")
    out = pd.DataFrame(index=res.index)
    out["id_da_pv_export_kwh"] = pv_export
    out["id_da_bess_export_kwh"] = bess_export
    out["id_da_grid_charge_kwh"] = grid_charge
    out["id_da_position_kwh"] = pv_export + bess_export - grid_charge
    return out


def redispatch_intraday(
    params: dict[str, Any],
    ts: pd.DataFrame,
    da_res: pd.DataFrame,
    *,
    solver_name: str = "highs",
    mip_gap: float = 0.001,
    time_limit_seconds: int = 1800,
    tee: bool = False,
) -> tuple[pd.DataFrame, str, pd.DataFrame]:
    """Run the Stage-2 intraday re-dispatch against ``da_res``.

    Pins the Stage-1 day-ahead position (Eq. I1) into the timeseries
    and re-solves the model — the intraday block attaches because the
    position columns are present.  Returns the ``run_scenario`` triple
    ``(res, resolved_solver, res_full)`` so callers can re-run the
    verification and KPI machinery on the Stage-2 frame.

    ``da_res`` should be the FULL-PRECISION Stage-1 frame (the
    ``res_full`` member of the ``run_scenario`` triple): the linking
    constraints pin the committed flows as equalities, and a round(4)
    frame injects per-step rounding noise the trades would have to
    absorb.

    Raises ``ValueError`` when the venue is disabled, the deviation
    fraction is zero (trading disabled — the caller skips the solve),
    or the IDA price column is missing.
    """
    # Deferred import: pvbess_opt.optimization imports this module for
    # the config resolver, so the solver entry point cannot be a
    # module-level import without a cycle.
    from .optimization import run_scenario

    cfg = resolve_intraday_config(params.get("intraday") or {})
    if not cfg.id_enabled:
        raise ValueError(
            "redispatch_intraday called with id_enabled = FALSE; the "
            "caller must gate the Stage-2 solve on the master switch."
        )
    if cfg.id_max_deviation_frac_of_cap <= 0.0:
        raise ValueError(
            "id_max_deviation_frac_of_cap = 0 disables intraday "
            "trading entirely: the Stage-2 result equals the committed "
            "day-ahead dispatch, so callers skip the re-solve instead "
            "of pinning every flow to a zero-slack equality."
        )
    if "ida_price_eur_per_mwh" not in ts.columns:
        raise ValueError(
            "redispatch_intraday requires the ida_price_eur_per_mwh "
            "timeseries column (enforced by the workbook loader)."
        )
    if len(da_res) != len(ts):
        raise ValueError(
            "Stage-1 result frame and timeseries length mismatch: "
            f"{len(da_res)} vs {len(ts)} steps."
        )

    ts_stage2 = ts.copy()
    position = extract_da_position(da_res)
    for col in DA_POSITION_COLUMNS:
        ts_stage2[col] = position[col].to_numpy(dtype=float)

    return run_scenario(
        params,
        ts_stage2,
        solver_name=solver_name,
        mip_gap=mip_gap,
        time_limit_seconds=time_limit_seconds,
        tee=tee,
        return_unrounded=True,
    )
