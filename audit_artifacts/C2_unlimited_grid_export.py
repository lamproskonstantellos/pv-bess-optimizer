"""C.2 — Unlimited grid export verification.

The README claims:
* Empty cell, ``inf``, ``unlimited``, ``disabled``, ``none`` (case-
  insensitive) all map to the same "no cap" sentinel.
* A finite Big-M is substituted internally, derived from system
  capacity (pv_nameplate_kwp + bess_power_kw).
* A finite positive cap behaves exactly as before.
* The cap applies to grid_export_total = pv_to_grid + bess_dis_grid,
  not separately.

This script invokes the loader's grid-export parser on each token and
compares the resulting internal MILP bound.  It also confirms that a
workbook with a finite cap produces the same internal value (no
substitution).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from pathlib import Path
import tempfile

from pvbess_opt.io import (
    _parse_grid_export_max,
    _GRID_EXPORT_UNLIMITED_TOKENS,
    read_inputs,
    read_workbook,
    write_workbook,
)


def main():
    tokens = ["", "inf", "Inf", "INF", "infinity", "unlimited", "UNLIMITED",
              "disabled", "Disabled", "none", "NONE", None, float("nan")]
    print("=== _parse_grid_export_max(token) ===")
    for tok in tokens:
        try:
            v = _parse_grid_export_max(tok, default=12345.0)
            kind = "inf" if np.isinf(v) else f"{v}"
            print(f"  {tok!r:18s} → {kind}")
        except Exception as e:
            print(f"  {tok!r:18s} → ERROR: {e}")

    print(f"\n_GRID_EXPORT_UNLIMITED_TOKENS = {sorted(_GRID_EXPORT_UNLIMITED_TOKENS)}")

    print("\n=== Finite vs unlimited: confirm internal substitution ===")
    # Build a finite-cap workbook → check internal value preserved.
    typed_finite = read_workbook("inputs/input.xlsx")
    print(f"  Finite workbook p_grid_export_max_kw (raw): "
          f"{typed_finite['project']['p_grid_export_max_kw']}")

    params_finite, _ = read_inputs("inputs/input.xlsx")
    print(f"  Finite params p_grid_export_max_kw (MILP): "
          f"{params_finite['p_grid_export_max_kw']}")
    print(f"  Finite params grid_export_unlimited: "
          f"{params_finite['grid_export_unlimited']}")

    # Now write a "unlimited" variant and re-load.
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        typed_u = read_workbook("inputs/input.xlsx")
        typed_u["project"]["p_grid_export_max_kw"] = float("inf")
        # Need a flat ts in 'ts' for write_workbook.
        typed_u["ts"] = pd.read_excel("inputs/input.xlsx", sheet_name="timeseries")
        write_workbook(typed_u, tdp / "input_unlimited.xlsx")
        params_u, _ = read_inputs(tdp / "input_unlimited.xlsx")
        print(f"\n  Unlimited workbook p_grid_export_max_kw (MILP): "
              f"{params_u['p_grid_export_max_kw']}")
        print(f"  Unlimited workbook grid_export_unlimited: "
              f"{params_u['grid_export_unlimited']}")
        # Sanity check: big-M should derive from system capacity.
        pv_kwp = float(params_u["pv_nameplate_kwp"])
        bess_kw = float(params_u["bess_power_kw"])
        expected_big_m = max(2.0 * (pv_kwp + bess_kw), 1.0e6)
        print(f"  Expected internal big-M: max(2.0*(pv+bess)=2.0*({pv_kwp}+{bess_kw}), 1e6) = "
              f"{expected_big_m}")
        assert params_u["p_grid_export_max_kw"] == expected_big_m, (
            f"big-M mismatch: got {params_u['p_grid_export_max_kw']}, "
            f"expected {expected_big_m}"
        )
        print(f"  big-M derivation: MATCH ✓")


if __name__ == "__main__":
    main()
