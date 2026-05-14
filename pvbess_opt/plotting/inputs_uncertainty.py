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

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ..config import COLORS, FINANCIAL_COLORS
from .style import apply_universal_margins, save_figure, show_titles

_Z90 = 1.2816  # Phi^{-1}(0.90)


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
        return save_figure(out_path)

    panels = [
        ("dam_price_eur_per_mwh", "DAM (EUR/MWh)", sigma_dam,
         FINANCIAL_COLORS["net"]),
        ("pv_kwh", "PV (kWh / step)", sigma_pv, COLORS["PV→Load"]),
    ]
    if "load_kwh" in sub.columns:
        panels.append((
            "load_kwh", "Load (kWh / step)", sigma_load,
            FINANCIAL_COLORS["revenue"],
        ))

    fig, axes = plt.subplots(len(panels), 1, figsize=(7, 6.5), sharex=True)
    if len(panels) == 1:
        axes = [axes]

    t = sub["timestamp"]
    for ax, (col, ylabel, sigma, color) in zip(axes, panels):
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
        ax.legend(loc="best", fontsize=7, framealpha=0.9)
        ax.grid(True, linestyle="--", alpha=0.5)

    axes[-1].set_xlabel("Timestamp")
    plt.setp(axes[-1].get_xticklabels(), rotation=30, ha="right")
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

    fig, axes = plt.subplots(n_panels, 1, figsize=(7, 2.2 * n_panels))
    if n_panels == 1:
        axes = [axes]

    panels = [
        ("dam_price_eur_per_mwh", "DAM (EUR/MWh)", FINANCIAL_COLORS["net"]),
        ("pv_kwh",                "PV (kWh / step)", COLORS["PV→Load"]),
    ]
    if has_load:
        panels.append((
            "load_kwh", "Load (kWh / step)", FINANCIAL_COLORS["revenue"],
        ))

    for ax, (col, ylabel, color) in zip(axes, panels):
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

    # 24×365 grid; aggregate by mean within each (doy, hod) cell so
    # 15-min DAM (constant per hour) collapses cleanly.
    grid = np.full((24, 365), np.nan)
    for h, d, p in zip(hod, doy, dam):
        if 1 <= d <= 365:
            r, c = h, d - 1
            cur = grid[r, c]
            grid[r, c] = p if np.isnan(cur) else 0.5 * (cur + p)

    plt.figure(figsize=(7, 4))
    ax = plt.gca()
    finite = grid[np.isfinite(grid)]
    if finite.size == 0:
        return save_figure(out_path)
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
