"""Shared plotting primitives: padded series, stacked bars/areas, line helpers."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..theme import ALPHA_STACK_AREAS, ALPHA_STACK_BARS, label_color
from .style import get_project_mode_label

__all__ = [
    "ZERO_THRESHOLD",
    "bar_stacked_bins",
    "edges_and_widths_monthly",
    "edges_and_widths_yearly",
    "fill_stacked_above",
    "line_if_nonzero",
    "line_masked_zeros",
    "month_aggregate",
    "pad_line_to_bins_end",
    "pad_right_to_end",
    "plot_stack_filtered",
    "pretty_date",
    "title_prefix",
    "year_aggregate",
]

ZERO_THRESHOLD: float = 1e-9


def pretty_date(date_str: str) -> str:
    """Return DD-MM-YYYY for plot titles."""
    return pd.to_datetime(date_str).strftime("%d-%m-%Y")


def pad_right_to_end(t, ys, end_ts):
    """Pad a time-series so step-post lines extend through `end_ts`.

    Returns (padded_t, [padded_y, ...]).  No-op if the series already runs
    through `end_ts`.
    """
    if len(t) == 0:
        return t, ys
    if pd.to_datetime(t.iloc[-1]) >= end_ts:
        return t, ys
    t_pad = pd.concat([pd.to_datetime(t), pd.Series([end_ts])], ignore_index=True)
    ys_pad = []
    for y in ys:
        arr = np.asarray(y, dtype=float)
        last = float(arr[-1]) if arr.size else 0.0
        ys_pad.append(np.append(arr, last))
    return t_pad, ys_pad


def edges_and_widths_monthly(g_dates: pd.Series):
    """For monthly bar plots: bin edges (one per day) and widths of 1 day."""
    left = pd.to_datetime(g_dates).dt.floor("D")
    width_days = np.ones(len(left), dtype=float)
    return left, width_days


def edges_and_widths_yearly(month_starts: pd.Series):
    """For yearly bar plots: bin edges (one per month) and per-month day count."""
    left = pd.to_datetime(month_starts)
    width_days = left.dt.days_in_month.astype(float).to_numpy()
    return left, width_days


def pad_line_to_bins_end(left: pd.Series, width_days, y):
    """Extend a step-post line to the end of the last bin."""
    left = pd.to_datetime(left)
    end = left.iloc[-1] + pd.to_timedelta(float(width_days[-1]), unit="D")
    t_pad = pd.concat([left, pd.Series([end])], ignore_index=True)
    arr = np.asarray(y, dtype=float)
    last = float(arr[-1]) if arr.size else 0.0
    y_pad = np.append(arr, last)
    return t_pad, y_pad


def plot_stack_filtered(ax, x, series, labels, *, step_post: bool = False):
    """Draw a stackplot, dropping any series that is identically zero.

    The keep-filter tests the ABSOLUTE sum: a negative stack (e.g. the
    grid-charging cost, or a CfD settlement leg) carries signal even
    though its signed sum is below zero — filtering on the signed sum
    silently dropped every negative segment from the revenue views.
    """
    keep = []
    for s, lab in zip(series, labels, strict=False):
        total = np.nansum(np.abs(np.asarray(s, dtype=float)))
        if total > ZERO_THRESHOLD:
            keep.append((s, lab))
    if not keep:
        return []
    kept_series = [s for s, _ in keep]
    kept_labels = [lab for _, lab in keep]
    kept_colors = [label_color(lab) for lab in kept_labels]
    if step_post:
        return ax.stackplot(x, *kept_series, labels=kept_labels, colors=kept_colors, step="post")
    return ax.stackplot(x, *kept_series, labels=kept_labels, colors=kept_colors)


def line_if_nonzero(ax, x, y, label, *, step_post: bool = False, **kwargs):
    """Plot `y` only if it has any nonzero entry."""
    if np.nansum(np.asarray(y)) <= ZERO_THRESHOLD:
        return
    if step_post:
        kwargs.setdefault("drawstyle", "steps-post")
    ax.plot(x, y, label=label, color=label_color(label), **kwargs)


def line_masked_zeros(ax, x, y, label, *, step_post: bool = False, **kwargs):
    """Plot `y` masking zero entries so the line breaks at zeros.

    Useful for series that are exactly zero outside their active window
    (e.g. PV generation at night). Skips the whole call if every value
    is zero — same all-zero behaviour as `line_if_nonzero` — but breaks
    the line at any individual zero so the flat-zero segments do not
    draw.
    """
    arr = np.asarray(y, dtype=float)
    if np.nansum(arr) <= ZERO_THRESHOLD:
        return
    masked = np.where(arr > ZERO_THRESHOLD, arr, np.nan)
    if step_post:
        kwargs.setdefault("drawstyle", "steps-post")
    ax.plot(x, masked, label=label, color=label_color(label), **kwargs)


def fill_stacked_above(ax, x, base, series, labels, *, step_post: bool = False):
    """Stacked filled areas drawn above a base curve."""
    cum = np.nan_to_num(np.asarray(base, dtype=float), nan=0.0)
    artists = []
    for s, lab in zip(series, labels, strict=False):
        s = np.nan_to_num(np.asarray(s, dtype=float), nan=0.0)
        if np.nansum(s) <= ZERO_THRESHOLD:
            continue
        y1 = cum
        y2 = cum + s
        artist = ax.fill_between(
            x,
            y1,
            y2,
            facecolor=label_color(lab),
            alpha=ALPHA_STACK_AREAS,
            label=lab,
            linewidth=0.0,
            step=("post" if step_post else None),
        )
        artists.append(artist)
        cum = y2
    return artists


def bar_stacked_bins(ax, left, width_days, series, labels, *, bottom=None):
    """Stacked bars that exactly fill each bin width.

    Keeps a series when its ABSOLUTE sum carries signal — negative
    stacks (grid-charging cost, CfD legs) must render; the previous
    signed-sum filter silently dropped them.
    """
    keep = []
    for s, lab in zip(series, labels, strict=False):
        arr = np.nan_to_num(np.asarray(s, dtype=float), nan=0.0)
        if np.nansum(np.abs(arr)) > ZERO_THRESHOLD:
            keep.append((arr, lab))
    if not keep:
        return []

    left = pd.to_datetime(left)
    bottoms = (
        np.zeros(len(left), dtype=float)
        if bottom is None
        else np.asarray(bottom, dtype=float).copy()
    )
    artists = []
    for arr, lab in keep:
        bars = ax.bar(
            left,
            arr,
            width=width_days,
            align="edge",
            bottom=bottoms,
            color=label_color(lab),
            label=lab,
            linewidth=0.0,
            alpha=ALPHA_STACK_BARS,
        )
        artists.append(bars)
        bottoms += arr
    return artists


def month_aggregate(res: pd.DataFrame, month: int) -> pd.DataFrame:
    """Daily totals for a given calendar month."""
    df = res[res["timestamp"].dt.month == month]
    if df.empty:
        return df
    g = df.groupby(df["timestamp"].dt.date).agg(
        {
            "load_kwh": "sum",
            "pv_kwh": "sum",
            "pv_to_load_kwh": "sum",
            "bess_dis_load_kwh": "sum",
            "grid_to_load_kwh": "sum",
            "pv_to_grid_kwh": "sum",
            "bess_dis_grid_kwh": "sum",
            "pv_to_bess_kwh": "sum",
            "bess_charge_grid_kwh": "sum",
            "pv_curtail_kwh": "sum",
        }
    ).reset_index().rename(columns={"timestamp": "date"})
    g["date"] = pd.to_datetime(g["date"])
    return g


def year_aggregate(res: pd.DataFrame, year: int) -> pd.DataFrame:
    """Monthly totals for a given calendar year."""
    df = res[pd.to_datetime(res["timestamp"]).dt.year == year].copy()
    if df.empty:
        return df
    grouped = df.groupby(pd.to_datetime(df["timestamp"]).dt.to_period("M")).agg(
        {
            "load_kwh": "sum",
            "pv_kwh": "sum",
            "pv_to_load_kwh": "sum",
            "bess_dis_load_kwh": "sum",
            "grid_to_load_kwh": "sum",
            "pv_to_grid_kwh": "sum",
            "bess_dis_grid_kwh": "sum",
            "pv_to_bess_kwh": "sum",
            "bess_charge_grid_kwh": "sum",
            "pv_curtail_kwh": "sum",
        }
    ).reset_index()
    grouped["month_start"] = grouped["timestamp"].dt.to_timestamp()
    grouped = grouped.sort_values("month_start").reset_index(drop=True)
    return grouped


def title_prefix(scenario_label: str) -> str:
    """Return ``' (<scenario>; <project mode>)'`` or ``''`` for plot titles.

    The project-mode segment is read from the
    :func:`pvbess_opt.plotting.style.get_project_mode_label`.  An empty
    value drops it from the prefix; the scenario label is shown alone
    in that case.
    """
    project_mode = get_project_mode_label()
    if scenario_label and project_mode:
        return f" ({scenario_label}; {project_mode})"
    if scenario_label:
        return f" ({scenario_label})"
    if project_mode:
        return f" ({project_mode})"
    return ""
