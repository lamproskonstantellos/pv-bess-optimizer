"""Resample a mixed-resolution ``timeseries`` sheet to a single timestep.

Use this when your raw inputs have different native resolutions
(e.g. 1-h demand + 15-min DAM prices).  The MILP requires a single
regular timestep, and :func:`pvbess_opt.io.detect_timestep_minutes` raises
when it sees mixed step sizes.

Columns fall into two kinds, resampled in opposite ways:

* **Flows** — energy columns (``load_kwh``, ``pv_kwh``) accumulate over
  their interval, so the *total* must be conserved across a resolution
  change. Upsampling (e.g. 60→15 min) splits each native value equally
  across the finer sub-intervals (1-h kWh → 4 × kWh/4). Downsampling
  (e.g. 15→60 min) **sums** the sub-intervals, so
  ``[10, 20, 30, 40] kWh`` at 15 min → ``100 kWh`` at 60 min.
* **Stocks** — price columns (``dam_price_eur_per_mwh``,
  ``retail_price_eur_per_mwh``) are instantaneous levels, not totals.
  Upsampling forward-fills (prices are piecewise-constant over their
  native period); downsampling takes the **mean** over the coarser
  interval.

The resampler:

* detects the native resolution of each numeric column;
* resamples flows and stocks per the rules above;
* writes the result to a new workbook (or overwrites the source with
  ``--in-place``) keeping the ``project`` / ``economic`` sheets
  untouched.

Usage::

    # Upsample 60→15 min: each hourly kWh is split into 4 equal quarters.
    python scripts/resample_timeseries.py input.xlsx --target-minutes 15
    # Downsample 15→60 min: four 15-min kWh values are summed per hour.
    python scripts/resample_timeseries.py input.xlsx --target-minutes 60 \\
        --out resampled.xlsx
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


_PRICE_COLS = ("dam_price_eur_per_mwh", "retail_price_eur_per_mwh")
_ENERGY_COLS = ("load_kwh", "pv_kwh")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resample a mixed-resolution timeseries to a single "
                    "regular timestep."
    )
    parser.add_argument("src", type=Path, help="Input workbook.")
    parser.add_argument(
        "--target-minutes", type=int, required=True,
        help="Target timestep in minutes (e.g. 15, 30, 60).",
    )
    out = parser.add_mutually_exclusive_group()
    out.add_argument("--out", type=Path, default=None,
                     help="Destination workbook path.")
    out.add_argument("--in-place", action="store_true",
                     help="Overwrite the source workbook.")
    return parser.parse_args(argv)


def _detect_native_minutes(series: pd.Series, idx: pd.DatetimeIndex) -> int:
    """Return the modal step size (minutes) of the non-null values in series."""
    notnull = series.notna()
    if not notnull.any():
        return 0
    masked_idx = idx[notnull]
    if len(masked_idx) < 2:
        return 0
    diffs = pd.Series(masked_idx).diff().dropna()
    return int(diffs.mode().iloc[0].total_seconds() // 60)


def _resample_column(
    series: pd.Series, idx: pd.DatetimeIndex, target_minutes: int, kind: str,
) -> pd.Series:
    """Resample one column to ``target_minutes`` using the right aggregation.

    ``kind`` selects the conservation rule:

    * ``"energy"`` — a *flow*; the total is conserved. Upsampling splits
      each native value equally across the finer sub-intervals;
      downsampling sums the sub-intervals.
    * ``"price"`` — a *stock* (instantaneous level). Upsampling
      forward-fills; downsampling takes the mean over the coarser bin.

    Other columns pass through unchanged when they already match the
    target step.
    """
    src_minutes = _detect_native_minutes(series, idx)
    if src_minutes == 0 or src_minutes == target_minutes:
        return series

    resampled = series.copy()
    resampled.index = idx
    # target finer than source ⇒ upsampling; coarser ⇒ downsampling.
    upsampling = target_minutes < src_minutes

    if kind == "price":
        if upsampling:
            target_idx = pd.date_range(
                idx.min(), idx.max(), freq=f"{target_minutes}min",
            )
            return resampled.reindex(target_idx, method="ffill")
        # Downsample a stock: average the native levels in each bin.
        return resampled.resample(f"{target_minutes}min").mean()
    if kind == "energy":
        if upsampling:
            # Split the source value equally across the sub-intervals so
            # the total energy is conserved.
            ratio = src_minutes / target_minutes
            if ratio <= 0:
                return series
            target_idx = pd.date_range(
                idx.min(), idx.max(), freq=f"{target_minutes}min",
            )
            return resampled.reindex(target_idx, method="ffill") / ratio
        # Downsample a flow: sum the sub-intervals so the total is conserved.
        return resampled.resample(f"{target_minutes}min").sum()
    return series


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    src: Path = args.src.resolve()
    if not src.exists():
        print(f"ERROR: source workbook not found: {src}", file=sys.stderr)
        return 2

    target = int(args.target_minutes)
    if target <= 0:
        print(f"ERROR: --target-minutes must be positive (got {target}).",
              file=sys.stderr)
        return 2

    if args.in_place:
        dst = src
    elif args.out is not None:
        dst = args.out.resolve()
    else:
        dst = src.parent / f"{src.stem}_resampled_{target}min{src.suffix}"

    sheets = pd.ExcelFile(src).sheet_names
    ts = pd.read_excel(src, sheet_name="timeseries", parse_dates=["timestamp"])
    ts = ts.sort_values("timestamp").reset_index(drop=True)
    idx = pd.DatetimeIndex(ts["timestamp"])

    out = pd.DataFrame()
    out_idx = pd.date_range(idx.min(), idx.max(), freq=f"{target}min")
    out["timestamp"] = out_idx

    for col in ts.columns:
        if col == "timestamp":
            continue
        kind = "price" if col in _PRICE_COLS else (
            "energy" if col in _ENERGY_COLS else "passthrough"
        )
        if kind == "passthrough":
            # Drop unrecognised numeric columns (the schema only
            # tolerates the four data columns).
            continue
        col_series = ts.set_index("timestamp")[col]
        resampled = _resample_column(col_series, idx, target, kind)
        out[col] = resampled.reindex(out_idx).values

    # Carry the other sheets through unchanged.
    other_sheets = {
        name: pd.read_excel(src, sheet_name=name)
        for name in sheets
        if name != "timeseries"
    }

    dst.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(dst, engine="openpyxl") as writer:
        out.to_excel(writer, sheet_name="timeseries", index=False)
        for name, df in other_sheets.items():
            df.to_excel(writer, sheet_name=name, index=False)

    print(
        f"Resampled {src} -> {dst} (target {target} min; "
        f"{len(out)} rows; columns: {[c for c in out.columns if c != 'timestamp']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
