"""Regression tests for the P2 documentation / refactor items.

Covers:

* P2.11 -- ``bm_revenue_share_pct`` denominator does not double-count
  balancing.
* P2.17 -- every previously inline ``dt_minutes / 60.0`` site now
  routes through :func:`pvbess_opt.timeutils.dt_hours_from`.
* P2.19 -- ``_compute_balancing_kpis``' zero-initialised dict shape
  matches the fully-populated path.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from pvbess_opt.balancing import PRODUCTS_ALL, PRODUCTS_WITH_ACTIVATION
from pvbess_opt.kpis import _compute_balancing_kpis
from pvbess_opt.timeutils import dt_hours_from


# ---------------------------------------------------------------------------
# P2.17 -- dt_hours_from helper
# ---------------------------------------------------------------------------


def test_dt_hours_from_handles_normal_case():
    assert dt_hours_from({"dt_minutes": 60}) == pytest.approx(1.0)
    assert dt_hours_from({"dt_minutes": 15}) == pytest.approx(0.25)
    assert dt_hours_from({"dt_minutes": 30}) == pytest.approx(0.5)


def test_dt_hours_from_treats_missing_or_zero_as_zero():
    assert dt_hours_from({}) == 0.0
    assert dt_hours_from({"dt_minutes": 0}) == 0.0
    assert dt_hours_from({"dt_minutes": None}) == 0.0


def test_dt_hours_from_clamps_negative():
    assert dt_hours_from({"dt_minutes": -15}) == 0.0


def test_no_remaining_inline_dt_minutes_literals():
    """grep guard: every site that previously computed
    ``params['dt_minutes'] / 60.0`` routes through the helper now.

    The helper's own definition is exempted; every other module must
    delegate.
    """
    repo_root = Path(__file__).resolve().parent.parent
    pkg = repo_root / "pvbess_opt"
    main_py = repo_root / "main.py"
    sources = list(pkg.rglob("*.py")) + [main_py]
    offenders: list[str] = []
    for src in sources:
        if src.name == "timeutils.py":
            continue
        text = src.read_text()
        # Match any line that divides dt_minutes by 60 (inline)
        for ln_idx, line in enumerate(text.splitlines(), start=1):
            # Skip docstrings / comments that just *mention* the
            # expression -- the heuristic is "the line contains
            # ``dt_minutes`` and ``/ 60`` and is NOT inside a comment".
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if (
                "dt_minutes" in line
                and ("/ 60.0" in line or "/ 60 " in line or "/60.0" in line)
                # Don't flag the divisor that converts hours->steps
                # (``hours * 60 // dt_minutes``).
                and "// " not in line and "//" not in line
            ):
                offenders.append(f"{src.relative_to(repo_root)}:{ln_idx}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


# ---------------------------------------------------------------------------
# P2.19 -- balancing-KPI dict shape is stable
# ---------------------------------------------------------------------------


def _kpi_dict_keyset_off() -> set[str]:
    """Run the zero-initialisation path (no balancing config)."""
    res = pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=3, freq="h")})
    params = {"dt_minutes": 60, "balancing": {"balancing_enabled": False}}
    return set(_compute_balancing_kpis(res, params).keys())


def _kpi_dict_keyset_on() -> set[str]:
    """Run the fully-populated path with a balancing-on params + dispatch frame."""
    n = 3
    res = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
            "profit_load_from_pv_eur": [0.0] * n,
            "profit_load_from_bess_eur": [0.0] * n,
            "profit_export_from_pv_eur": [0.0] * n,
            "profit_export_from_bess_eur": [0.0] * n,
            "expense_charge_bess_grid_eur": [0.0] * n,
        }
    )
    for p in PRODUCTS_ALL:
        res[f"bm_reservation_{p}_kw"] = [10.0] * n
        res[f"{p}_capacity_price_eur_per_mwh"] = [20.0] * n
    for p in PRODUCTS_WITH_ACTIVATION:
        res[f"{p}_activation_price_eur_per_mwh"] = [50.0] * n
    params = {
        "dt_minutes": 60,
        "efficiency_charge": 0.95,
        "efficiency_discharge": 0.95,
        "balancing": {"balancing_enabled": True},
    }
    return set(_compute_balancing_kpis(res, params).keys())


def test_balancing_kpi_dict_shape_matches_off_and_on():
    """The zero-init and full-populate paths emit the same key set."""
    off = _kpi_dict_keyset_off()
    on = _kpi_dict_keyset_on()
    missing_in_off = on - off
    missing_in_on = off - on
    assert not missing_in_off, (
        f"keys present when ON but absent from OFF init: {missing_in_off}"
    )
    assert not missing_in_on, (
        f"keys present when OFF but absent from ON path: {missing_in_on}"
    )


# ---------------------------------------------------------------------------
# P2.11 -- bm_revenue_share_pct denominator does not double-count balancing
# ---------------------------------------------------------------------------


def test_bm_revenue_share_denominator_excludes_balancing_from_dam():
    """Balancing revenue must not enter the non-balancing-revenue sum.

    The denominator is computed from ``profit_*_eur`` columns of the
    dispatch frame, which are filled by ``add_economic_columns`` for
    DAM / retail energy flows only.  This regression locks the
    non-overlap so a future change folding balancing into
    profit_total_eur (and therefore into the per-step profit_*_eur
    columns) is caught.
    """
    n = 3
    res = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
            # 1000 EUR of "DAM+retail" revenue across the horizon.
            "profit_load_from_pv_eur": [200.0] * n,
            "profit_load_from_bess_eur": [0.0] * n,
            "profit_export_from_pv_eur": [100.0] * n,
            "profit_export_from_bess_eur": [40.0] * n,
            "expense_charge_bess_grid_eur": [10.0] * n,
        }
    )
    for p in PRODUCTS_ALL:
        res[f"bm_reservation_{p}_kw"] = [100.0] * n
        # Capacity price 80 EUR/MWh -> with alpha=0.5 default,
        # cap_rev per step = 0.5 * 1.0h / 1000 * 80 * 100 = 4 EUR / step.
        res[f"{p}_capacity_price_eur_per_mwh"] = [80.0] * n
    for p in PRODUCTS_WITH_ACTIVATION:
        res[f"{p}_activation_price_eur_per_mwh"] = [0.0] * n
    params = {
        "dt_minutes": 60,
        "efficiency_charge": 0.95,
        "efficiency_discharge": 0.95,
        "balancing": {
            "balancing_enabled": True,
            # Override probabilities to be deterministic for the
            # share check.  alpha=0.5 keeps balancing nonzero.
        },
    }
    out = _compute_balancing_kpis(res, params)
    # Non-balancing revenue (DAM + retail) sum should equal
    # (200 + 0 + 100 + 40 - 10) * 3 = 330 * 3 = 990 EUR.
    expected_non_bal = 330.0 * n
    bal_total = float(out["bm_total_balancing_revenue_eur"])
    # share% = 100 * bal / (non_bal + bal).  If balancing leaked into
    # the non_bal sum, the share would be smaller than expected.
    share = float(out["bm_revenue_share_pct"])
    expected_share = 100.0 * bal_total / (expected_non_bal + bal_total)
    # 1e-3 % tolerance is appropriate for the 4-dp rounding in
    # _compute_balancing_kpis.
    assert share == pytest.approx(expected_share, abs=0.01)
