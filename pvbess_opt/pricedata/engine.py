"""Tier-1 repricing engine: frozen-dispatch factors and capture KPIs.

The reprice projection (``scenario_projection_mode = 'reprice'``) prices
the FROZEN Year-1 dispatch against each (scenario, year) curve and
derives per-stream escalation factors

    g_s[y] = R_s(dispatch_1, price_y) / R_s(dispatch_1, price_1)

which enter the existing per-year escalation machinery (Eq. E24) as
auto-generated replace-mode trajectories on the SPLIT stream taxonomy
(Eqs. E60/E61): ``revenue_dam_pv`` / ``revenue_dam_bess_export`` /
``expense_dam_bess_charge`` from the hourly curves, per-product
balancing capacity/activation from the store's annual table.  An input
swap into Eq. E24 — deliberately NOT a new projection equation.

Design points:

* The factor denominator is the DECK's year-1 curve, so ``g[1] == 1``
  by construction and the Year-1 cashflow stays anchored to the
  dispatch-KPI base (the Eq. E24 ``m_1 = 1`` contract).  A deck whose
  year-1 level departs from the workbook's own Year-1 prices is
  flagged — the scenario path is RELATIVE, the absolute Year-1 base
  always comes from the dispatch.
* A stream with zero Year-1 volume (no PV export, no grid charging)
  keeps a flat factor of 1.0 — inert, never a division by zero.
* Retail stays on its inflation index (self-consumption tariffs are
  not wholesale curves); intraday/imbalance scenario curves are out of
  scope by design (the store schema reserves the columns).

The same pass produces the per-year price-path / capture KPI table
(PV capture price and rate, realized BESS spread, per-product
balancing paths) that feeds the results workbook and the fan-chart
figures — the KPIs the Year-1-only price plots cannot show.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .adapters import build_parametric_deck, build_tyndp_deck
from .store import (
    BALANCING_PRODUCTS,
    PriceDataError,
    ScenarioDeck,
    load_scenario_store,
    stub_provider_error,
)

logger = logging.getLogger(__name__)

#: Auto-generated trajectory streams the reprice engine owns; a user
#: trajectory on any of these (or their aggregate aliases) conflicts
#: with an armed scenario engine.
REPRICE_PRICE_STREAMS: frozenset[str] = frozenset({
    "revenue_dam", "revenue_dam_pv", "revenue_dam_bess_export",
    "expense_dam_bess_charge",
    "balancing_capacity", "balancing_activation",
    *(f"balancing_capacity_{p}" for p in BALANCING_PRODUCTS),
    *(
        f"balancing_activation_{p}" for p in BALANCING_PRODUCTS
        if p != "fcr"
    ),
})


def build_scenario_deck(
    entry: dict[str, Any],
    *,
    base_dir: Path,
    ts: pd.DataFrame,
    n_steps: int,
    dt_minutes: int,
    n_years: int,
    start_year: int,
    engine_basis: str,
    engine_base_year: int,
    cpi_pct: float,
) -> ScenarioDeck:
    """Materialise one ``price_scenarios`` row into a loaded deck."""
    provider = str(entry["provider"]).strip().lower()
    store_dir = Path(str(entry["store_path"]))
    if not store_dir.is_absolute():
        store_dir = base_dir / store_dir
    name = str(entry["name"])
    vintage = str(entry.get("vintage") or "")
    weight_pct = float(entry.get("weight_pct", 0.0) or 0.0)
    if provider == "file":
        return load_scenario_store(
            store_dir,
            name=name, provider="file", vintage=vintage,
            weight_pct=weight_pct,
            n_steps=n_steps, dt_minutes=dt_minutes, n_years=n_years,
            start_year=start_year, engine_basis=engine_basis,
            engine_base_year=engine_base_year, cpi_pct=cpi_pct,
        )
    if provider == "parametric":
        if "dam_price_eur_per_mwh" not in ts.columns:
            raise PriceDataError(
                f"scenario {name!r}: the parametric provider "
                "derives its curves from the workbook "
                "dam_price_eur_per_mwh column, which is absent."
            )
        return build_parametric_deck(
            store_dir,
            name=name, vintage=vintage, weight_pct=weight_pct,
            year1_dam=ts["dam_price_eur_per_mwh"].to_numpy(dtype=float),
            pv_kwh=(
                ts["pv_kwh"].to_numpy(dtype=float)
                if "pv_kwh" in ts.columns else None
            ),
            year1_balancing=_year1_balancing_prices(ts),
            n_years=n_years,
            engine_basis=engine_basis,
        )
    if provider == "tyndp":
        return build_tyndp_deck(
            store_dir,
            name=name, vintage=vintage, weight_pct=weight_pct,
            n_steps=n_steps, dt_minutes=dt_minutes, n_years=n_years,
            start_year=start_year,
            engine_basis=engine_basis,
            engine_base_year=engine_base_year,
            cpi_pct=cpi_pct,
        )
    raise stub_provider_error(provider)


def _year1_balancing_prices(
    ts: pd.DataFrame,
) -> dict[str, tuple[float, float]]:
    """Year-1 per-product mean prices from the workbook columns."""
    out: dict[str, tuple[float, float]] = {}
    for product in BALANCING_PRODUCTS:
        cap_col = f"{product}_capacity_price_eur_per_mwh"
        act_col = f"{product}_activation_price_eur_per_mwh"
        cap = (
            float(ts[cap_col].astype(float).mean())
            if cap_col in ts.columns else 0.0
        )
        act = (
            float(ts[act_col].astype(float).mean())
            if act_col in ts.columns else 0.0
        )
        out[product] = (cap, act)
    return out


def _volume_kwh(res: pd.DataFrame, column: str, n_steps: int) -> np.ndarray:
    if column not in res.columns:
        return np.zeros(n_steps)
    return res[column].to_numpy(dtype=float)


def _revenue_eur(volume_kwh: np.ndarray, price: np.ndarray) -> float:
    return float((volume_kwh * price).sum() / 1000.0)


def _factor_series(
    revenues: list[float], *, stream: str, scenario: str,
) -> list[float]:
    """g[y] = R_y / R_1 with the zero-volume guard and the year-1 anchor."""
    base = revenues[0]
    if abs(base) < 1e-9:
        return [1.0] * len(revenues)
    series = [r / base for r in revenues]
    if not all(np.isfinite(v) for v in series):
        raise PriceDataError(
            f"scenario {scenario!r}: non-finite {stream} factor; check "
            "the store curves."
        )
    series[0] = 1.0
    return series


def derive_reprice_trajectories(
    deck: ScenarioDeck,
    res: pd.DataFrame,
    *,
    n_years: int,
) -> tuple[dict[str, dict[str, Any]], pd.DataFrame]:
    """Auto-trajectories + the per-year price-path/capture KPI table.

    Returns ``(trajectories, paths)``: replace-mode blocks for the
    split streams (Eqs. E60/E61), and one row per operating year with
    the factor and capture columns for the results workbook / figures.
    """
    year1 = deck.dam_curve(1)
    n_steps = len(year1)
    if len(res) != n_steps:
        raise PriceDataError(
            f"scenario {deck.name!r}: deck curves carry {n_steps} steps "
            f"but the Year-1 dispatch frame carries {len(res)}."
        )
    pv_export = _volume_kwh(res, "pv_to_grid_kwh", n_steps)
    bess_export = _volume_kwh(res, "bess_dis_grid_kwh", n_steps)
    bess_charge = _volume_kwh(res, "bess_charge_grid_kwh", n_steps)

    revenue_pv: list[float] = []
    revenue_bx: list[float] = []
    expense_bc: list[float] = []
    rows: list[dict[str, Any]] = []
    for y in range(1, n_years + 1):
        price_y = deck.dam_curve(y)
        r_pv = _revenue_eur(pv_export, price_y)
        r_bx = _revenue_eur(bess_export, price_y)
        c_bc = _revenue_eur(bess_charge, price_y)
        revenue_pv.append(r_pv)
        revenue_bx.append(r_bx)
        expense_bc.append(c_bc)
        mean_price = float(price_y.mean())
        pv_mwh = float(pv_export.sum() / 1000.0)
        dis_mwh = float(bess_export.sum() / 1000.0)
        ch_mwh = float(bess_charge.sum() / 1000.0)
        capture_price = r_pv / pv_mwh if pv_mwh > 1e-9 else float("nan")
        discharge_price = r_bx / dis_mwh if dis_mwh > 1e-9 else float("nan")
        charge_price = c_bc / ch_mwh if ch_mwh > 1e-9 else float("nan")
        rows.append({
            "project_year": y,
            "dam_mean_price_eur_per_mwh": mean_price,
            "pv_capture_price_eur_per_mwh": capture_price,
            "pv_capture_rate": (
                capture_price / mean_price
                if np.isfinite(capture_price) and abs(mean_price) > 1e-9
                else float("nan")
            ),
            "bess_discharge_price_eur_per_mwh": discharge_price,
            "bess_charge_price_eur_per_mwh": charge_price,
            "bess_realized_spread_eur_per_mwh": (
                discharge_price - charge_price
                if np.isfinite(discharge_price) and np.isfinite(charge_price)
                else float("nan")
            ),
        })

    trajectories: dict[str, dict[str, Any]] = {
        "revenue_dam_pv": {
            "mode": "replace",
            "values": _factor_series(
                revenue_pv, stream="revenue_dam_pv", scenario=deck.name,
            ),
        },
        "revenue_dam_bess_export": {
            "mode": "replace",
            "values": _factor_series(
                revenue_bx, stream="revenue_dam_bess_export",
                scenario=deck.name,
            ),
        },
        "expense_dam_bess_charge": {
            "mode": "replace",
            "values": _factor_series(
                expense_bc, stream="expense_dam_bess_charge",
                scenario=deck.name,
            ),
        },
    }
    paths = pd.DataFrame(rows)
    for stream, block in trajectories.items():
        paths[f"g_{stream}"] = block["values"]

    if deck.balancing is not None:
        bal = deck.balancing.set_index(["year", "product"]).sort_index()
        bal_years = bal.index.get_level_values("year").astype(int)
        bal_products = bal.index.get_level_values("product").astype(str)
        for product in BALANCING_PRODUCTS:
            # Hold-last horizon per PRODUCT: a product whose rows stop
            # earlier than another's holds its OWN last year — the
            # global max would silently drop the whole stream.  A
            # product entirely absent from the table is the documented
            # no-stream case (logged, not an error).
            product_years = bal_years[bal_products == product]
            if len(product_years) == 0:
                logger.info(
                    "[pricedata] scenario %r: balancing product %s is "
                    "absent from the store's annual table; the stream "
                    "keeps its workbook escalation.",
                    deck.name, product,
                )
                continue
            max_year = int(product_years.max())
            for kind, column in (
                ("capacity", "capacity_price_eur_per_mwh"),
                ("activation", "activation_price_eur_per_mwh"),
            ):
                if kind == "activation" and product == "fcr":
                    continue
                price_series = bal[column]
                prices: list[float] = []
                for y in range(1, n_years + 1):
                    value = price_series.get((min(y, max_year), product))
                    if value is None:
                        prices = []
                        break
                    prices.append(float(value))
                if not prices:
                    continue
                stream = f"balancing_{kind}_{product}"
                trajectories[stream] = {
                    "mode": "replace",
                    "values": _factor_series(
                        prices, stream=stream, scenario=deck.name,
                    ),
                }
                paths[f"{product}_{kind}_price_eur_per_mwh"] = prices

    return trajectories, paths


def deck_year1_level_check(
    deck: ScenarioDeck, ts: pd.DataFrame, *, tolerance_pct: float = 10.0,
) -> None:
    """Flag a deck whose year-1 level departs from the workbook prices.

    The reprice factors are RELATIVE (the Year-1 cashflow base always
    comes from the dispatch), so a level mismatch is not an error — but
    a store built for another zone/vintage usually shows up here first.
    """
    if "dam_price_eur_per_mwh" not in ts.columns:
        return
    workbook_mean = float(
        ts["dam_price_eur_per_mwh"].astype(float).mean(),
    )
    deck_mean = float(deck.dam_curve(1).mean())
    if abs(workbook_mean) < 1e-9:
        return
    drift_pct = 100.0 * abs(deck_mean - workbook_mean) / abs(workbook_mean)
    if drift_pct > tolerance_pct:
        logger.warning(
            "[pricedata] scenario %r: the deck's year-1 mean DAM price "
            "(%.2f EUR/MWh) departs from the workbook's Year-1 mean "
            "(%.2f EUR/MWh) by %.1f %%. The factors are relative — the "
            "Year-1 base stays the dispatch KPI base — but check the "
            "store's zone/vintage.",
            deck.name, deck_mean, workbook_mean, drift_pct,
        )


@dataclass
class ScenarioApplication:
    """What the armed engine did to one run (for outputs/provenance).

    ``paths`` is the APPLIED scenario's per-year price-path/capture
    table; ``fan`` maps every enabled scenario name to its table (the
    fan-chart input); ``summary_lines`` are the SUMMARY.md digest
    lines.  Both tables stay TIER-1 (frozen-dispatch) even under
    ``resolve`` mode, so the fan compares every scenario on the same
    footing; the Tier-2 factors surface in ``resolve_delta`` (the
    Tier-2 − Tier-1 diagnostic at the support years) and
    ``resolve_support`` (the raw per-support-year re-solve table).
    """

    applied: str
    mode: str
    paths: pd.DataFrame
    fan: dict[str, pd.DataFrame]
    weights: dict[str, float]
    summary_lines: list[str]
    resolve_delta: pd.DataFrame | None = None
    resolve_support: pd.DataFrame | None = None
    #: The applied scenario's merged auto-trajectory block (including
    #: the Tier-2 overrides under resolve mode) — the weighted
    #: ensemble reuses it verbatim for the applied member.
    applied_trajectories: dict[str, dict[str, Any]] | None = None


def apply_price_scenarios(
    econ: dict[str, Any],
    ts: pd.DataFrame,
    res: pd.DataFrame,
    *,
    base_dir: Path,
    params: dict[str, Any] | None = None,
    solver_opts: dict[str, Any] | None = None,
    kpis: dict[str, Any] | None = None,
) -> ScenarioApplication | None:
    """Arm the price-scenario layer on one run (the engine entry point).

    Reads the merged ``scenario_engine`` keys plus the parsed
    ``price_scenarios`` list from ``econ``; returns None (untouched
    econ) when disarmed.  When armed the applied scenario's
    auto-trajectories are merged into ``econ['trajectories']`` IN
    PLACE — everything downstream (cashflow, LCOE/LCOS OPEX,
    sensitivity, debt) then flows through the existing Eq. E24
    machinery unchanged.  ``reprice`` derives every factor from the
    frozen Year-1 dispatch; ``resolve`` additionally re-solves the
    MILP at the support years and overrides the three DAM streams
    with the degradation-normalised Tier-2 factors
    (:mod:`pvbess_opt.pricedata.resolve`) — it therefore needs the
    dispatch ``params`` (and reads the Year-1 discharge throughput
    from ``kpis`` for the pooled cycle-fade model).

    The applied scenario of a single run is ``debt_sizing_scenario``
    when named (the bankable path), else the first enabled row; the
    weighted ensemble across ALL rows runs afterwards in the same
    pipeline run (:func:`pvbess_opt.pricedata.ensemble.run_price_scenario_ensemble`).
    """
    if not bool(econ.get("price_scenarios_enabled", False)):
        return None
    scenarios = econ.get("price_scenarios") or []
    if not scenarios:
        logger.warning(
            "[pricedata] price_scenarios_enabled = TRUE but the "
            "price_scenarios sheet is disabled or empty; the scenario "
            "layer stays inert."
        )
        return None
    mode = str(
        econ.get("scenario_projection_mode", "reprice") or "reprice",
    ).strip().lower()
    if mode == "trajectory_only":
        logger.info(
            "[pricedata] scenario_projection_mode='trajectory_only': "
            "no auto-trajectories are generated; the declared "
            "trajectories sheet (refined stream taxonomy, Eqs. E60/E61) "
            "carries the price paths."
        )
        return None
    if mode == "resolve" and params is None:
        raise PriceDataError(
            "scenario_projection_mode='resolve' re-solves the dispatch "
            "at the support years and needs the run's dispatch params; "
            "the pipeline threads them automatically — a programmatic "
            "caller must pass params=."
        )

    n_years = int(econ.get("project_lifecycle_years", 0) or 0)
    if n_years <= 0:
        raise PriceDataError(
            "price scenarios need project_lifecycle_years >= 1."
        )
    # Default the start year to the SAME schema value the rest of the
    # pipeline uses (io.PROJECT_SHEET_DEFAULTS): a blank/zero cell must not
    # collapse to calendar year 0, which drives the real→nominal / TYNDP
    # basis bridge (Eq. G-basis) to ~0 and silently zeroes projected
    # prices.  Lazy import keeps pricedata decoupled from the heavy io
    # module and sidesteps any import cycle.
    from pvbess_opt.io import PROJECT_SHEET_DEFAULTS, SCENARIO_ENGINE_SHEET_DEFAULTS

    start_year = int(
        econ.get("project_start_year")
        or PROJECT_SHEET_DEFAULTS["project_start_year"]
    )
    if start_year <= 0:
        raise PriceDataError(
            "price scenarios need a positive project_start_year (got "
            f"{start_year!r}); set project_start_year on the project sheet."
        )
    dt_minutes = _infer_dt_minutes(ts)
    engine_basis = str(econ.get("price_basis", "nominal") or "nominal")
    engine_base_year = int(econ.get("price_base_year", 0) or 0)
    # Default a MISSING cpi_pct to the schema value (2.0), not 0.0: a bare
    # `econ.get('cpi_pct', 0.0) or 0.0` diverges from the schema default and
    # would deflate a real-/TYNDP-basis store at 0 % CPI for any caller that
    # bypasses read_economic_params.  An explicit 0.0 is preserved (a valid
    # "no inflation" choice), so `... or SCHEMA` is deliberately NOT used.
    _cpi = econ.get("cpi_pct")
    cpi_pct = float(
        SCENARIO_ENGINE_SHEET_DEFAULTS["cpi_pct"] if _cpi is None else _cpi
    )

    requested = str(econ.get("debt_sizing_scenario", "") or "").strip()
    names = [str(entry["name"]) for entry in scenarios]
    if requested and requested not in names:
        raise PriceDataError(
            f"debt_sizing_scenario {requested!r} does not match any "
            f"enabled price scenario ({', '.join(names)})."
        )
    applied_name = requested or names[0]

    fan: dict[str, pd.DataFrame] = {}
    weights: dict[str, float] = {}
    applied_trajectories: dict[str, dict[str, Any]] | None = None
    applied_paths: pd.DataFrame | None = None
    applied_deck: ScenarioDeck | None = None
    for entry in scenarios:
        deck = build_scenario_deck(
            entry,
            base_dir=base_dir, ts=ts, n_steps=len(ts),
            dt_minutes=dt_minutes, n_years=n_years,
            start_year=start_year, engine_basis=engine_basis,
            engine_base_year=engine_base_year, cpi_pct=cpi_pct,
        )
        deck_year1_level_check(deck, ts)
        trajectories, paths = derive_reprice_trajectories(
            deck, res, n_years=n_years,
        )
        fan[deck.name] = paths
        weights[deck.name] = deck.weight_pct
        if deck.name == applied_name:
            applied_trajectories = trajectories
            applied_paths = paths
            applied_deck = deck
    assert applied_trajectories is not None and applied_paths is not None
    assert applied_deck is not None

    resolve_delta: pd.DataFrame | None = None
    resolve_support: pd.DataFrame | None = None
    if mode == "resolve":
        # Lazy import: resolve.py itself imports the revenue helpers
        # from this module, so a top-level import would be circular.
        from .resolve import (
            build_resolve_delta,
            derive_resolve_trajectories,
            parse_support_years,
        )

        assert params is not None  # guarded above
        support_years = parse_support_years(
            str(econ.get("scenario_resolve_years", "") or ""), n_years,
        )
        resolution = int(
            econ.get("scenario_resolve_resolution", 60) or 60,
        )
        # The pooled cycle-fade model inside factors_for_year reads the
        # Year-1 discharge throughput — the same derated KPI number the
        # cashflow's replacement resolver consumes.
        econ["_resolve_year1_discharge_mwh"] = float(
            (kpis or {}).get("bess_total_discharge_mwh", 0.0) or 0.0
        )
        tier2, resolve_support = derive_resolve_trajectories(
            applied_deck, params, ts, econ,
            n_years=n_years, support_years=support_years,
            resolution_minutes=resolution,
            interp=str(
                econ.get("scenario_interp", "loglinear") or "loglinear",
            ),
            solver_opts=solver_opts,
        )
        resolve_delta = build_resolve_delta(
            applied_trajectories, tier2, support_years,
        )
        # The re-solves refine the three DAM streams only; balancing
        # paths from the store's annual table ride along from Tier-1.
        applied_trajectories = {**applied_trajectories, **tier2}

    econ["trajectories"] = merge_auto_trajectories(
        econ.get("trajectories"), applied_trajectories,
        scenario=applied_name,
    )
    # The armed marker: economics gates the support-reference rule
    # (support_ref_follows_scenario) on it, so a disarmed run with
    # user-declared price trajectories keeps its historical series.
    econ["_price_scenario_applied"] = applied_name
    streams = ", ".join(sorted(applied_trajectories))
    logger.info(
        "[pricedata] price-scenario engine armed (mode %r): "
        "scenario %r projected the Year-1 dispatch into "
        "auto-trajectories for %s; %d enabled scenario(s) feed the "
        "price-path figures and the in-run weighted ensemble.",
        mode, applied_name, streams, len(scenarios),
    )
    final_year = applied_paths.iloc[-1]
    summary_lines = [
        f"- Price scenarios: `{mode}` on `{applied_name}` "
        f"({len(scenarios)} enabled scenario(s))",
        f"- Year-{n_years} PV capture rate: "
        f"{final_year['pv_capture_rate']:.3f} "
        f"(capture {final_year['pv_capture_price_eur_per_mwh']:.2f} "
        f"vs baseload {final_year['dam_mean_price_eur_per_mwh']:.2f} "
        "EUR/MWh)",
    ]
    if resolve_delta is not None and not resolve_delta.empty:
        summary_lines.append(
            f"- Tier-2 re-solves at year(s) "
            f"{', '.join(str(y) for y in support_years)} "
            f"({resolution} min grid); max |g2 - g1| = "
            f"{float(resolve_delta['delta'].abs().max()):.4f}"
        )
    return ScenarioApplication(
        applied=applied_name,
        mode=mode,
        paths=applied_paths,
        fan=fan,
        weights=weights,
        summary_lines=summary_lines,
        resolve_delta=resolve_delta,
        resolve_support=resolve_support,
        applied_trajectories=applied_trajectories,
    )


def _infer_dt_minutes(ts: pd.DataFrame) -> int:
    """Model cadence from the frame length (whole non-leap year)."""
    n_steps = len(ts)
    if n_steps == 0 or n_steps % 365 != 0:
        raise PriceDataError(
            f"price scenarios need a whole non-leap-year timeseries; "
            f"got {n_steps} steps."
        )
    steps_per_day = n_steps // 365
    if (24 * 60) % steps_per_day != 0:
        raise PriceDataError(
            f"{steps_per_day} steps/day does not divide the day into "
            "whole minutes."
        )
    return (24 * 60) // steps_per_day


def merge_auto_trajectories(
    existing: dict[str, dict[str, Any]] | None,
    generated: dict[str, dict[str, Any]],
    *,
    scenario: str,
) -> dict[str, dict[str, Any]]:
    """Merge engine trajectories over the user block (price streams owned).

    A user-declared trajectory on any price stream conflicts with an
    armed scenario engine (double specification of the same path) and
    raises; non-price streams (opex family, retail) pass through.
    """
    merged = dict(existing or {})
    conflicts = sorted(REPRICE_PRICE_STREAMS & set(merged))
    if conflicts:
        raise PriceDataError(
            f"scenario {scenario!r}: the workbook already declares "
            f"trajectories for {', '.join(conflicts)}, which the armed "
            "price-scenario engine generates itself; remove the manual "
            "stream(s) or disable price_scenarios_enabled."
        )
    merged.update(generated)
    return merged
