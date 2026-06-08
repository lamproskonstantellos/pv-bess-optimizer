"""Input-uncertainty visualization plots.

Three figures help readers reason about the rolling-horizon forecast
model:

* :func:`plot_input_forecast_band` — one representative week, mean line
  plus P10/P90 envelope from log-normal noise.
* :func:`plot_input_seasonal_boxplot` — monthly distribution of
  DAM / PV / load.
* :func:`plot_dam_intraday_heatmap` — DAM by hour-of-day vs day-of-year.

The forecast envelope is derived analytically from the log-normal
noise parameters used by ``add_forecast_noise`` (no Monte Carlo
needed):

    P10/P90 = mean × exp(-σ²/2 ± Φ⁻¹(0.90) · σ)
"""

from __future__ import annotations

import math
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ..theme import COLORS, FINANCIAL_COLORS
from ._dates import apply_house_date_axis
from .style import (
    apply_universal_margins,
    save_figure,
    show_titles,
)
from .style import (
    empty_placeholder as _placeholder,
)

_Z90 = 1.2816  # Phi^{-1}(0.90)

# House legend placement for the whole 06_uncertainty_plots/ family.
LEGEND_LOC = "upper right"
LEGEND_KWARGS = dict(loc=LEGEND_LOC, framealpha=0.85, fontsize=9)

__all__ = [
    "LEGEND_KWARGS",
    "LEGEND_LOC",
    "plot_dam_intraday_heatmap",
    "plot_input_forecast_band",
    "plot_input_seasonal_boxplot",
    "plot_uncertainty_coverage_by_horizon",
    "plot_uncertainty_crps_timeline",
    "plot_uncertainty_pit_histogram",
    "plot_uncertainty_residual_qq",
]




def _lognormal_band(actual: np.ndarray, sigma: float) -> tuple[np.ndarray, np.ndarray]:
    """Analytical P10/P90 of a log-normal-noised actual with E[X]=1."""
    if sigma <= 0.0:
        return actual.copy(), actual.copy()
    mu = -0.5 * sigma * sigma
    factor_low = float(np.exp(mu - _Z90 * sigma))
    factor_high = float(np.exp(mu + _Z90 * sigma))
    return actual * factor_low, actual * factor_high


def plot_input_forecast_band(
    ts: pd.DataFrame,
    out_path: Path,
    *,
    week_start_doy: int = 165,
    sigma_dam: float = 0.20,
    sigma_pv: float = 0.12,
    sigma_load: float = 0.05,
) -> Path:
    """Three-panel plot for one representative week with P10/P90 envelope.

    week_start_doy: day-of-year at which the 7-day window starts.
    """
    out_path = Path(out_path)
    timestamps = pd.to_datetime(ts["timestamp"])
    doy = timestamps.dt.dayofyear
    mask = (doy >= week_start_doy) & (doy < week_start_doy + 7)
    sub = ts.loc[mask].reset_index(drop=True)
    if sub.empty:
        return _placeholder(out_path, "Forecast band: no data.")

    panels = [
        ("dam_price_eur_per_mwh", "DAM (EUR/MWh)", sigma_dam,
         FINANCIAL_COLORS["net"]),
        ("pv_kwh", "PV (kWh / step)", sigma_pv, COLORS["PV to load"]),
    ]
    if "load_kwh" in sub.columns:
        panels.append((
            "load_kwh", "Load (kWh / step)", sigma_load,
            FINANCIAL_COLORS["revenue"],
        ))

    _fig, axes = plt.subplots(len(panels), 1, figsize=(7, 6.5), sharex=True)
    if len(panels) == 1:
        axes = [axes]

    t = sub["timestamp"]
    for ax, (col, ylabel, sigma, color) in zip(axes, panels, strict=False):
        actual = sub[col].to_numpy(dtype=float)
        # Sign-aware band on DAM (preserves negative-price sign).
        if col == "dam_price_eur_per_mwh":
            sign = np.where(actual < 0, -1.0, 1.0)
            magnitude = np.abs(actual)
            mag_low, mag_high = _lognormal_band(magnitude, sigma)
            low, high = sign * mag_low, sign * mag_high
        else:
            low, high = _lognormal_band(np.maximum(actual, 0.0), sigma)
        ax.fill_between(t, low, high, color=color, alpha=0.20,
                        label=f"P10–P90 (σ={sigma:.2f})")
        ax.plot(t, actual, color=color, linewidth=1.0, label="Actual")
        ax.set_ylabel(ylabel)
        ax.legend(**LEGEND_KWARGS)
        ax.grid(True, linestyle="--", alpha=0.5)
        apply_house_date_axis(ax)

    axes[-1].set_xlabel("Timestamp")
    if show_titles():
        axes[0].set_title(
            f"Forecast envelope, week starting DOY {week_start_doy}"
        )
    for ax in axes:
        apply_universal_margins(ax)
    return save_figure(out_path)


def plot_input_seasonal_boxplot(
    ts: pd.DataFrame, out_path: Path,
) -> Path:
    """Three-panel monthly boxplot of DAM, PV, load."""
    out_path = Path(out_path)
    timestamps = pd.to_datetime(ts["timestamp"])
    months = timestamps.dt.month
    has_load = "load_kwh" in ts.columns
    n_panels = 3 if has_load else 2

    _fig, axes = plt.subplots(n_panels, 1, figsize=(7, 2.2 * n_panels))
    if n_panels == 1:
        axes = [axes]

    panels = [
        ("dam_price_eur_per_mwh", "DAM (EUR/MWh)", FINANCIAL_COLORS["net"]),
        ("pv_kwh",                "PV (kWh / step)", COLORS["PV to load"]),
    ]
    if has_load:
        panels.append((
            "load_kwh", "Load (kWh / step)", FINANCIAL_COLORS["revenue"],
        ))

    for ax, (col, ylabel, color) in zip(axes, panels, strict=False):
        data = [ts.loc[months == m, col].to_numpy(dtype=float)
                for m in range(1, 13)]
        ax.boxplot(data, positions=range(1, 13), showfliers=False,
                   patch_artist=True,
                   boxprops=dict(facecolor=color, alpha=0.4))
        ax.set_xticks(range(1, 13))
        ax.set_ylabel(ylabel)
        ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    axes[-1].set_xlabel("Month")
    if show_titles():
        axes[0].set_title("Monthly distribution of inputs (Year 1)")
    for ax in axes:
        apply_universal_margins(ax, skip_x=True)
    return save_figure(out_path)


def plot_dam_intraday_heatmap(
    ts: pd.DataFrame, out_path: Path,
) -> Path:
    """Heatmap of DAM by hour-of-day (y) × day-of-year (x)."""
    out_path = Path(out_path)
    timestamps = pd.to_datetime(ts["timestamp"])
    doy = timestamps.dt.dayofyear.to_numpy()
    hod = timestamps.dt.hour.to_numpy()
    dam = ts["dam_price_eur_per_mwh"].to_numpy(dtype=float)

    # 24 × N grid; N is 366 in leap years so Feb-29 (doy 366) is not
    # silently dropped.  Aggregate by mean within each (doy, hod) cell so
    # 15-min DAM (constant per hour) collapses cleanly.
    n_days = int(doy.max()) if doy.size else 365
    n_days = max(n_days, 365)
    grid = np.full((24, n_days), np.nan)
    for h, d, p in zip(hod, doy, dam, strict=False):
        if 1 <= d <= n_days:
            r, c = h, d - 1
            cur = grid[r, c]
            grid[r, c] = p if np.isnan(cur) else 0.5 * (cur + p)

    finite = grid[np.isfinite(grid)]
    if finite.size == 0:
        return _placeholder(out_path, "DAM intraday heatmap: no data.")

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    vmin = float(np.nanpercentile(grid, 5))
    vmax = float(np.nanpercentile(grid, 95))
    if vmin == vmax:
        vmax = vmin + 1.0
    im = ax.imshow(
        grid, aspect="auto", origin="lower",
        cmap="coolwarm", interpolation="nearest",
        vmin=vmin, vmax=vmax,
    )
    ax.set_xlabel("Day of year")
    ax.set_ylabel("Hour of day")
    cbar = plt.colorbar(im, ax=ax, pad=0.02)
    cbar.set_label("DAM (EUR/MWh)")
    if show_titles():
        ax.set_title("DAM intraday × seasonal heatmap (Year 1)")
    apply_universal_margins(ax, skip_x=True, skip_y=True)
    return save_figure(out_path)


# ---------------------------------------------------------------------------
# Forecast-calibration diagnostics
# ---------------------------------------------------------------------------
#
# These compare a synthetic forecast (median = the input signal, per-step
# Gaussian width sigma_step = |signal| * sigma) against a seeded realised
# draw from the same log-normal noise model used by add_forecast_noise.
# They diagnose whether the band width is well-calibrated; well-calibrated
# forecasts give a flat PIT histogram, a diagonal residual Q-Q, ~0.80
# P10-P90 coverage, and a low, stable CRPS.

_SQRT2 = float(np.sqrt(2.0))
_INV_SQRT_PI = float(1.0 / np.sqrt(np.pi))
_erf_vec = np.vectorize(math.erf)


def _norm_cdf(x: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + _erf_vec(np.asarray(x, dtype=float) / _SQRT2))


def _norm_pdf(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    return np.exp(-0.5 * x * x) / np.sqrt(2.0 * np.pi)


def _norm_ppf(p: np.ndarray) -> np.ndarray:
    """Inverse standard-normal CDF (Acklam's rational approximation)."""
    p = np.clip(np.asarray(p, dtype=float), 1e-12, 1.0 - 1e-12)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1.0 - 0.02425
    out = np.zeros_like(p)
    lo = p < plow
    hi = p > phigh
    mid = ~(lo | hi)
    if lo.any():
        q = np.sqrt(-2.0 * np.log(p[lo]))
        out[lo] = (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                  ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1.0)
    if hi.any():
        q = np.sqrt(-2.0 * np.log(1.0 - p[hi]))
        out[hi] = -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                   ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1.0)
    if mid.any():
        q = p[mid] - 0.5
        r = q * q
        out[mid] = (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
                   (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1.0)
    return out


def _diagnostic_panels(ts: pd.DataFrame, sigmas: dict[str, float]):
    """Return ``[(col, label, sigma, color)]`` for the present sources."""
    panels = [
        ("dam_price_eur_per_mwh", "DAM", sigmas["dam"], FINANCIAL_COLORS["net"]),
        ("pv_kwh", "PV", sigmas["pv"], COLORS["PV to load"]),
    ]
    if "load_kwh" in ts.columns:
        panels.append(("load_kwh", "Load", sigmas["load"], FINANCIAL_COLORS["revenue"]))
    return [p for p in panels if p[0] in ts.columns]


def _forecast_vs_realised(actual: np.ndarray, sigma: float, rng):
    """Synthetic (median, sigma_step, realised, valid-mask) for a source."""
    median = np.asarray(actual, dtype=float)
    sigma_step = np.abs(median) * float(sigma)
    if sigma > 0.0:
        mult = np.exp(sigma * rng.standard_normal(median.shape) - 0.5 * sigma * sigma)
    else:
        mult = np.ones_like(median)
    realised = median * mult
    valid = sigma_step > 1e-9
    return median, sigma_step, realised, valid


def plot_uncertainty_coverage_by_horizon(
    ts: pd.DataFrame,
    out_path: Path,
    *,
    sigma_dam: float = 0.20,
    sigma_pv: float = 0.12,
    sigma_load: float = 0.05,
    commit_steps: int = 96,
    base_seed: int = 42,
) -> Path:
    """Empirical P10–P90 coverage vs horizon hour, one line per source."""
    out_path = Path(out_path)
    sigmas = {"dam": sigma_dam, "pv": sigma_pv, "load": sigma_load}
    panels = _diagnostic_panels(ts, sigmas)
    if not panels or commit_steps < 1:
        return _placeholder(out_path, "Coverage-by-horizon: no data.")

    dt_h = 24.0 / commit_steps
    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    rng = np.random.default_rng(base_seed)
    for col, label, sigma, color in panels:
        actual = ts[col].to_numpy(dtype=float)
        median, _sig, realised, valid = _forecast_vs_realised(actual, sigma, rng)
        low, high = _lognormal_band(median, sigma)
        inside = (realised >= low) & (realised <= high) & valid
        horizon = (np.arange(len(actual)) % commit_steps)
        hours, cover = [], []
        for h in range(commit_steps):
            sel = (horizon == h) & valid
            if sel.any():
                hours.append(h * dt_h)
                cover.append(float(inside[sel].sum()) / float(sel.sum()))
        if hours:
            ax.plot(hours, cover, color=color, linewidth=1.2, label=label)
    ax.axhline(0.80, color="grey", linestyle="--", linewidth=1.0,
               label="Nominal P10–P90 = 0.80")
    ax.set_xlabel("Horizon (hours ahead)")
    ax.set_ylabel("Empirical coverage")
    ax.set_ylim(0.0, 1.0)
    if show_titles():
        ax.set_title("P10–P90 coverage by forecast horizon")
    ax.legend(**LEGEND_KWARGS)
    ax.grid(True, linestyle="--", alpha=0.5)
    apply_universal_margins(ax)
    return save_figure(out_path)


def plot_uncertainty_pit_histogram(
    ts: pd.DataFrame,
    out_path: Path,
    *,
    sigma_dam: float = 0.20,
    sigma_pv: float = 0.12,
    sigma_load: float = 0.05,
    base_seed: int = 42,
) -> Path:
    """Probability-integral-transform histogram per source (flat = calibrated)."""
    out_path = Path(out_path)
    sigmas = {"dam": sigma_dam, "pv": sigma_pv, "load": sigma_load}
    panels = _diagnostic_panels(ts, sigmas)
    if not panels:
        return _placeholder(out_path, "PIT histogram: no data.")

    _fig, axes = plt.subplots(len(panels), 1, figsize=(7, 2.2 * len(panels)))
    if len(panels) == 1:
        axes = [axes]
    rng = np.random.default_rng(base_seed)
    for ax, (col, label, sigma, color) in zip(axes, panels, strict=False):
        actual = ts[col].to_numpy(dtype=float)
        median, sigma_step, realised, valid = _forecast_vs_realised(actual, sigma, rng)
        z = (realised[valid] - median[valid]) / sigma_step[valid]
        pit = _norm_cdf(z)  # PIT = F_forecast(actual); uniform when calibrated
        ax.hist(pit, bins=20, range=(0.0, 1.0), color=color, alpha=0.6,
                edgecolor="black", linewidth=0.4,
                label=f"{label} (n={pit.size})")
        ideal = pit.size / 20.0 if pit.size else 0.0
        ax.axhline(ideal, color="grey", linestyle="--", linewidth=1.0)
        ax.set_ylabel(label)
        ax.legend(**LEGEND_KWARGS)
        ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    axes[-1].set_xlabel("PIT value")
    if show_titles():
        axes[0].set_title("Probability integral transform (flat ⇒ calibrated)")
    for ax in axes:
        apply_universal_margins(ax, skip_x=True)
    return save_figure(out_path)


def plot_uncertainty_crps_timeline(
    ts: pd.DataFrame,
    out_path: Path,
    *,
    sigma_dam: float = 0.20,
    sigma_pv: float = 0.12,
    sigma_load: float = 0.05,
    base_seed: int = 42,
) -> Path:
    """Step-wise CRPS timeline (Gaussian band approximation), one line per source."""
    out_path = Path(out_path)
    sigmas = {"dam": sigma_dam, "pv": sigma_pv, "load": sigma_load}
    panels = _diagnostic_panels(ts, sigmas)
    if not panels:
        return _placeholder(out_path, "CRPS timeline: no data.")

    t = pd.to_datetime(ts["timestamp"])
    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    rng = np.random.default_rng(base_seed)
    for col, label, sigma, color in panels:
        actual = ts[col].to_numpy(dtype=float)
        median, sigma_step, realised, valid = _forecast_vs_realised(actual, sigma, rng)
        crps = np.zeros_like(median)
        s = sigma_step[valid]
        z = (realised[valid] - median[valid]) / s
        crps[valid] = s * (
            z * (2.0 * _norm_cdf(z) - 1.0) + 2.0 * _norm_pdf(z) - _INV_SQRT_PI
        )
        ax.plot(t, crps, color=color, linewidth=0.9, label=label)
    ax.set_xlabel("Timestamp")
    ax.set_ylabel("CRPS")
    if show_titles():
        ax.set_title("Step-wise CRPS over the forecast band")
    ax.legend(**LEGEND_KWARGS)
    ax.grid(True, linestyle="--", alpha=0.5)
    apply_house_date_axis(ax)
    apply_universal_margins(ax)
    return save_figure(out_path)


def plot_uncertainty_residual_qq(
    ts: pd.DataFrame,
    out_path: Path,
    *,
    sigma_dam: float = 0.20,
    sigma_pv: float = 0.12,
    sigma_load: float = 0.05,
    base_seed: int = 42,
) -> Path:
    """Q-Q plot of normalised residuals vs standard normal, per source."""
    out_path = Path(out_path)
    sigmas = {"dam": sigma_dam, "pv": sigma_pv, "load": sigma_load}
    panels = _diagnostic_panels(ts, sigmas)
    if not panels:
        return _placeholder(out_path, "Residual Q-Q: no data.")

    _fig, axes = plt.subplots(len(panels), 1, figsize=(7, 2.4 * len(panels)))
    if len(panels) == 1:
        axes = [axes]
    rng = np.random.default_rng(base_seed)
    for ax, (col, label, sigma, color) in zip(axes, panels, strict=False):
        actual = ts[col].to_numpy(dtype=float)
        median, sigma_step, realised, valid = _forecast_vs_realised(actual, sigma, rng)
        resid = np.sort((realised[valid] - median[valid]) / sigma_step[valid])
        n = resid.size
        if n:
            theoretical = _norm_ppf((np.arange(1, n + 1) - 0.5) / n)
            ax.scatter(theoretical, resid, s=6, color=color, alpha=0.5,
                       label=f"{label} (n={n})")
            lim = float(max(abs(theoretical).max(), abs(resid).max(), 1.0))
            ax.plot([-lim, lim], [-lim, lim], color="grey", linestyle="--",
                    linewidth=1.0, label="Standard normal")
        ax.set_ylabel(label)
        ax.legend(**LEGEND_KWARGS)
        ax.grid(True, linestyle="--", alpha=0.5)
    axes[-1].set_xlabel("Theoretical normal quantile")
    if show_titles():
        axes[0].set_title("Normalised-residual Q-Q vs standard normal")
    for ax in axes:
        apply_universal_margins(ax)
    return save_figure(out_path)
