"""scripts/resample_timeseries.py produces a workbook the loader accepts.

The script's pandas read_excel -> to_excel passthrough of the parameter
sheets mis-typed numeric 0/1 cells in the mixed-type kv value columns as
Excel BOOLEANS, so it printed success and the produced workbook was then
rejected at the very next read_inputs with a boolean-in-numeric-field
error (deferred-ledger item 8's 'loud-at-next-read' drift).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_resampled_workbook_loads_cleanly(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import resample_timeseries
    finally:
        sys.path.pop(0)

    from pvbess_opt.io import read_inputs, read_workbook, write_workbook

    typed = read_workbook(ROOT / "inputs" / "input.xlsx")
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)  # 1 day @ 15min
    src = tmp_path / "day.xlsx"
    write_workbook(typed, src)

    dst = tmp_path / "day_60min.xlsx"
    rc = resample_timeseries.main([
        str(src), "--target-minutes", "60", "--out", str(dst),
    ])
    assert rc == 0
    # The acceptance: the produced workbook parses end-to-end.
    params, ts = read_inputs(dst)
    assert params["dt_minutes"] == 60
    assert len(ts) == 24
    # Energy conserved through the downsample.
    src_pv = float(typed["ts"]["pv_kwh"].sum())
    assert abs(float(ts["pv_kwh"].sum()) - src_pv) < 1e-6 * max(src_pv, 1.0)
