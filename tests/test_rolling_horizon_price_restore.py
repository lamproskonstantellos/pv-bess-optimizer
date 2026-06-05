"""Regression test for the actuals-restore path in ``rolling_horizon_dispatch``.

Asserts that every price column listed in
:data:`pvbess_opt.rolling_horizon.PRICE_COLUMNS` is restored from the
noise-free input when ``evaluate_with_actuals=True``, regardless of
which columns :func:`add_forecast_noise` happens to touch today.

The test monkeypatches ``add_forecast_noise`` to inject a constant
multiplicative perturbation on a balancing capacity-price column.
Without the canonical restore the noise would survive into the
realised KPIs; with it, balancing revenue must equal the noise-free
value.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt import rolling_horizon as rh_module
from pvbess_opt.rolling_horizon import (
    PRICE_COLUMNS,
    rolling_horizon_dispatch,
)
from tests._balancing_helpers import _balancing_on


def _solver_available() -> bool:
    try:
        from pyomo.opt import SolverFactory
        return bool(SolverFactory("highs").available(exception_flag=False))
    except Exception:  # pragma: no cover - env guard
        return False


pytestmark = pytest.mark.skipif(
    not _solver_available(),
    reason="HiGHS solver not available",
)


def test_price_columns_contract():
    """PRICE_COLUMNS must include DAM, retail, and every balancing price."""
    expected_min = {
        "dam_price_eur_per_mwh",
        "retail_price_eur_per_mwh",
        "fcr_capacity_price_eur_per_mwh",
        "afrr_up_capacity_price_eur_per_mwh",
        "afrr_dn_capacity_price_eur_per_mwh",
        "mfrr_up_capacity_price_eur_per_mwh",
        "mfrr_dn_capacity_price_eur_per_mwh",
        "afrr_up_activation_price_eur_per_mwh",
        "afrr_dn_activation_price_eur_per_mwh",
        "mfrr_up_activation_price_eur_per_mwh",
        "mfrr_dn_activation_price_eur_per_mwh",
    }
    assert expected_min.issubset(set(PRICE_COLUMNS))


def _ts_with_balancing(short_ts: pd.DataFrame) -> pd.DataFrame:
    """Add canonical balancing capacity-price columns to ``short_ts``."""
    ts = short_ts.copy()
    n = len(ts)
    ts["fcr_capacity_price_eur_per_mwh"] = 12.0
    ts["afrr_up_capacity_price_eur_per_mwh"] = 18.0
    ts["afrr_dn_capacity_price_eur_per_mwh"] = 15.0
    ts["mfrr_up_capacity_price_eur_per_mwh"] = 6.0
    ts["mfrr_dn_capacity_price_eur_per_mwh"] = 5.0
    ts["afrr_up_activation_price_eur_per_mwh"] = 220.0
    ts["afrr_dn_activation_price_eur_per_mwh"] = 25.0
    ts["mfrr_up_activation_price_eur_per_mwh"] = 180.0
    ts["mfrr_dn_activation_price_eur_per_mwh"] = 20.0
    assert n == len(ts)
    return ts


def test_actuals_restore_strips_balancing_price_noise(
    short_params, short_ts, monkeypatch,
):
    """Noise injected on a balancing capacity price must not survive."""
    real_add_noise = rh_module.add_forecast_noise

    def noisy_add_forecast_noise(ts, **kwargs):
        """Wrap the real noiser; also multiply FCR capacity by 2 in forecast."""
        noised = real_add_noise(ts, **kwargs)
        out = noised.copy()
        commit_steps = int(kwargs.get("commit_steps", 0))
        col = "fcr_capacity_price_eur_per_mwh"
        if col in out.columns:
            arr = out[col].to_numpy(dtype=float).copy()
            arr[commit_steps:] = arr[commit_steps:] * 2.0
            out[col] = arr
        return out

    monkeypatch.setattr(
        rh_module, "add_forecast_noise", noisy_add_forecast_noise,
    )

    ts = _ts_with_balancing(short_ts)
    p_on = _balancing_on(short_params)
    # Smaller windows so the noise actually hits multiple solves.
    _full, kpis_noised = rolling_horizon_dispatch(
        p_on, ts,
        window_hours=12, commit_hours=6,
        forecast_seed=42,
        evaluate_with_actuals=True,
    )

    # Re-run with no noise as the reference.
    _full2, kpis_clean = rolling_horizon_dispatch(
        p_on, ts,
        window_hours=12, commit_hours=6,
        forecast_seed=None,
        evaluate_with_actuals=True,
    )

    # The FCR capacity-revenue KPI is computed by ``compute_kpis``
    # from the per-step capacity price column.  Restoring the
    # noise-free price means the noised run reports the SAME FCR
    # capacity revenue as the noise-free run when there is no
    # foresight benefit -- i.e. the realised settlement is
    # noise-free.  Allow a small tolerance for SOC-driven
    # commit-window divergence.
    fcr_noised = float(kpis_noised.get("bm_fcr_capacity_revenue_eur", 0.0) or 0.0)
    fcr_clean = float(kpis_clean.get("bm_fcr_capacity_revenue_eur", 0.0) or 0.0)
    # Without the restore the noised run would report ~2x the clean
    # value (the doubled capacity-price column had survived into the
    # actuals frame).  With the restore the dispatch decisions may
    # differ slightly because of the noise, but the settlement is
    # done against the noise-free price column.
    assert fcr_noised == pytest.approx(fcr_clean, rel=0.5), (
        f"FCR revenue diverged catastrophically: "
        f"noised={fcr_noised}, clean={fcr_clean}"
    )
    # Stronger guard: the doubled-price scenario would push noised
    # at least 50% above clean if the noise leaked through.  Within
    # 50% tolerance must hold.
    if fcr_clean > 0.0:
        ratio = fcr_noised / fcr_clean
        assert ratio < 1.5, (
            f"FCR revenue leaked balancing noise: ratio={ratio:.3f}"
        )


def test_actuals_restore_overwrites_every_listed_price_column(
    short_params, short_ts, monkeypatch,
):
    """All PRICE_COLUMNS entries are restored to ts values in evaluate-actuals."""
    real_add_noise = rh_module.add_forecast_noise
    sentinel_value = -99_999.0

    def overwriter(ts, **kwargs):
        """Replace every price column with a sentinel in the forecast frame."""
        noised = real_add_noise(ts, **kwargs)
        out = noised.copy()
        commit_steps = int(kwargs.get("commit_steps", 0))
        for col in PRICE_COLUMNS:
            if col not in out.columns:
                continue
            arr = out[col].to_numpy(dtype=float).copy()
            arr[commit_steps:] = sentinel_value
            out[col] = arr
        return out

    monkeypatch.setattr(rh_module, "add_forecast_noise", overwriter)

    ts = _ts_with_balancing(short_ts)
    p_on = _balancing_on(short_params)
    full, _kpis = rolling_horizon_dispatch(
        p_on, ts,
        window_hours=12, commit_hours=6,
        forecast_seed=99,
        evaluate_with_actuals=True,
    )

    # Every PRICE_COLUMN present in ``ts`` must be restored to the ts
    # value in the returned ``full`` frame -- the sentinel must not
    # appear anywhere.
    for col in PRICE_COLUMNS:
        if col not in ts.columns:
            continue
        assert col in full.columns, f"{col} dropped from output"
        n = len(full)
        np.testing.assert_array_almost_equal(
            full[col].to_numpy(dtype=float),
            ts[col].iloc[:n].to_numpy(dtype=float),
            decimal=6,
            err_msg=f"{col} not restored to ts values",
        )
