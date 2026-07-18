"""Multi-year, multi-scenario price layer (opt-in).

Canonical per-scenario stores (:mod:`pvbess_opt.pricedata.store`),
the parametric / TYNDP adapters (:mod:`pvbess_opt.pricedata.adapters`)
and — with the projection-engine phase — the year-by-year repricing of
the frozen Year-1 dispatch into per-stream escalation trajectories.
With ``price_scenarios_enabled = FALSE`` (the default) the layer is
inert.
"""

from .adapters import build_parametric_deck, build_tyndp_deck
from .engine import (
    ScenarioApplication,
    apply_price_scenarios,
    derive_reprice_trajectories,
)
from .store import (
    BALANCING_PRODUCTS,
    SCENARIO_PROVIDERS,
    PriceDataError,
    ScenarioDeck,
    load_scenario_store,
    stub_provider_error,
)

__all__ = [
    "BALANCING_PRODUCTS",
    "SCENARIO_PROVIDERS",
    "PriceDataError",
    "ScenarioApplication",
    "ScenarioDeck",
    "apply_price_scenarios",
    "build_parametric_deck",
    "build_tyndp_deck",
    "derive_reprice_trajectories",
    "load_scenario_store",
    "stub_provider_error",
]
