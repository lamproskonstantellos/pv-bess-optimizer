"""PV timeseries generator correctness tests.

Six invariants the generator and the case-study fixture
(``inputs/input.xlsx``) must satisfy:

1. PV is **exactly zero** outside the 06:00-18:00 daylight window —
   no Gaussian-noise bleed at 03:00 / 04:00 / 21:00, etc.
2. Annual yield equals ``pv_nameplate_kwp * specific_production_kwh_per_kwp``
   exactly (the generator normalises the year).
3. Doubling ``pv_nameplate_kwp`` doubles the realised PV everywhere.
4. Doubling ``specific_production_kwh_per_kwp`` doubles the yearly
   total (linear scaling).
5. ``pv_nameplate_kwp = 0`` produces an all-zero PV array.
6. The case-study ``inputs/input.xlsx`` ships with the post-fix
   profile — no night-time PV, target specific production matched.

A small leftover-artifact audit runs at the end to ensure the v0.7
noise-bleed pattern (``np.maximum(pv + rng.normal(...), 0)`` without a
daylight gate) does not creep back into ``scripts/`` or ``tests/``.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts.build_input_xlsx import _build_pv_kwh, build_timeseries

ROOT = Path(__file__).resolve().parent.parent


def _gen(
    *,
    pv_nameplate_kwp: float = 4500.0,
    specific_production_kwh_per_kwp: float = 1500.0,
    target_minutes: int = 60,
    seed: int = 42,
) -> pd.DataFrame:
    return build_timeseries(
        2026,
        target_minutes=target_minutes,
        pv_nameplate_kwp=pv_nameplate_kwp,
        specific_production_kwh_per_kwp=specific_production_kwh_per_kwp,
        seed=seed,
    )


# ---------------------------------------------------------------------------
# Invariant 1 — strict zero outside the daylight window
# ---------------------------------------------------------------------------


def test_pv_is_strictly_zero_outside_daylight_hourly():
    ts = _gen(target_minutes=60)
    hours = pd.to_datetime(ts["timestamp"]).dt.hour.to_numpy()
    pv = ts["pv_kwh"].to_numpy(dtype=float)
    night_mask = (hours < 6) | (hours > 18)
    assert pv[night_mask].max() == 0.0, (
        f"night-time PV must be exactly 0; got max "
        f"{pv[night_mask].max():.6g} kWh at "
        f"hour {hours[night_mask][np.argmax(pv[night_mask])]}"
    )
    assert pv[night_mask].sum() == 0.0


def test_pv_is_strictly_zero_outside_daylight_quarter_hour():
    ts = _gen(target_minutes=15)
    timestamps = pd.to_datetime(ts["timestamp"])
    hours = timestamps.dt.hour.to_numpy()
    minutes = timestamps.dt.minute.to_numpy()
    pv = ts["pv_kwh"].to_numpy(dtype=float)
    # Daylight window is [06:00, 18:00] inclusive on the hour boundary.
    h_decimal = hours + minutes / 60.0
    night_mask = (h_decimal < 6.0) | (h_decimal > 18.0)
    assert pv[night_mask].max() == 0.0
    assert pv[night_mask].sum() == 0.0


# ---------------------------------------------------------------------------
# Invariant 2 — annual yield matches nameplate × specific production
# ---------------------------------------------------------------------------


def test_annual_yield_matches_specific_production_target():
    pv_kwp = 8000.0  # match the user's 8 MW reference
    sp = 1571.0      # 12 568 / 8
    ts = _gen(
        pv_nameplate_kwp=pv_kwp,
        specific_production_kwh_per_kwp=sp,
    )
    target_kwh = pv_kwp * sp
    realised_kwh = float(ts["pv_kwh"].sum())
    # Exact normalisation is performed in the generator; a tiny rounding
    # tolerance covers the 4-digit `np.round` applied after.
    assert realised_kwh == pytest.approx(target_kwh, rel=2e-4), (
        f"annual yield {realised_kwh:.1f} kWh does not match target "
        f"{target_kwh:.1f} kWh (= {pv_kwp:.0f} kWp × {sp:.0f} kWh/kWp)"
    )


def test_annual_yield_matches_for_default_case_study():
    ts = _gen(pv_nameplate_kwp=4500.0, specific_production_kwh_per_kwp=1500.0)
    target_kwh = 4500.0 * 1500.0
    assert float(ts["pv_kwh"].sum()) == pytest.approx(target_kwh, rel=2e-4)


# ---------------------------------------------------------------------------
# Invariant 3 — linear scaling in pv_nameplate_kwp
# ---------------------------------------------------------------------------


def test_doubling_nameplate_doubles_realised_pv():
    seed = 123
    ts_a = _gen(
        pv_nameplate_kwp=2000.0,
        specific_production_kwh_per_kwp=1500.0,
        seed=seed,
    )
    ts_b = _gen(
        pv_nameplate_kwp=4000.0,
        specific_production_kwh_per_kwp=1500.0,
        seed=seed,
    )
    a_total = float(ts_a["pv_kwh"].sum())
    b_total = float(ts_b["pv_kwh"].sum())
    assert b_total == pytest.approx(2.0 * a_total, rel=1e-6)


# ---------------------------------------------------------------------------
# Invariant 4 — linear scaling in specific_production_kwh_per_kwp
# ---------------------------------------------------------------------------


def test_doubling_specific_production_doubles_realised_pv():
    seed = 123
    ts_a = _gen(
        pv_nameplate_kwp=3000.0,
        specific_production_kwh_per_kwp=1200.0,
        seed=seed,
    )
    ts_b = _gen(
        pv_nameplate_kwp=3000.0,
        specific_production_kwh_per_kwp=2400.0,
        seed=seed,
    )
    a_total = float(ts_a["pv_kwh"].sum())
    b_total = float(ts_b["pv_kwh"].sum())
    assert b_total == pytest.approx(2.0 * a_total, rel=1e-6)


# ---------------------------------------------------------------------------
# Invariant 5 — pv_nameplate_kwp = 0 produces an all-zero PV array
# ---------------------------------------------------------------------------


def test_zero_nameplate_yields_all_zero_pv():
    ts = _gen(pv_nameplate_kwp=0.0, specific_production_kwh_per_kwp=1500.0)
    assert float(ts["pv_kwh"].sum()) == 0.0
    assert float(ts["pv_kwh"].max()) == 0.0


def test_build_pv_kwh_handles_zero_nameplate_directly():
    rng = np.random.default_rng(0)
    out = _build_pv_kwh(
        n_steps=24,
        n_steps_per_day=24,
        dt_hours=1.0,
        pv_nameplate_kwp=0.0,
        specific_production_kwh_per_kwp=1500.0,
        rng=rng,
    )
    assert out.shape == (24,)
    assert (out == 0.0).all()


# ---------------------------------------------------------------------------
# Invariant 6 — case-study workbook ships with the post-fix profile
# ---------------------------------------------------------------------------


def test_repo_input_xlsx_pv_zero_at_night():
    ts = pd.read_excel(ROOT / "inputs" / "input.xlsx", sheet_name="timeseries")
    timestamps = pd.to_datetime(ts["timestamp"])
    h_decimal = timestamps.dt.hour + timestamps.dt.minute / 60.0
    night_mask = (h_decimal < 6.0) | (h_decimal > 18.0)
    pv = ts["pv_kwh"].to_numpy(dtype=float)
    assert pv[night_mask].max() == 0.0, (
        "inputs/input.xlsx still has night-time PV — re-run "
        "scripts/build_input_xlsx.py to regenerate the fixture."
    )


def test_repo_input_xlsx_pv_specific_production_matches_workbook():
    ts = pd.read_excel(ROOT / "inputs" / "input.xlsx", sheet_name="timeseries")
    pv_sheet = pd.read_excel(ROOT / "inputs" / "input.xlsx", sheet_name="pv")
    keys = pv_sheet["key"].astype(str).tolist()
    values = pv_sheet["value"].tolist()
    pv_dict = dict(zip(keys, values))
    pv_kwp = float(pv_dict["pv_nameplate_kwp"])
    sp_target = float(pv_dict["specific_production_kwh_per_kwp"])
    annual_total_kwh = float(ts["pv_kwh"].sum())
    realised_sp = annual_total_kwh / pv_kwp
    assert realised_sp == pytest.approx(sp_target, rel=2e-4), (
        f"realised specific production {realised_sp:.1f} kWh/kWp does "
        f"not match workbook target {sp_target:.1f} kWh/kWp"
    )


# ---------------------------------------------------------------------------
# Leftover-artifact audit — no v0.7 noise-bleed pattern in the tree
# ---------------------------------------------------------------------------


_SELF_FILE = Path(__file__).resolve()


def _scan_files() -> list[Path]:
    """Yield every .py file under scripts/ + tests/ + main.py except this audit."""
    out: list[Path] = []
    for sub in ("scripts", "tests"):
        for path in (ROOT / sub).rglob("*.py"):
            if path.resolve() == _SELF_FILE:
                continue
            out.append(path)
    out.append(ROOT / "main.py")
    return out


_BAD_PATTERN = re.compile(
    r"np\.maximum\(\s*pv\s*\+\s*rng\.normal",
)


def test_no_v07_noise_bleed_pattern_remains():
    """The v0.7/v0.8 PR introduced ``pv = np.maximum(pv + rng.normal(...), 0)``
    in four places.  All four should have been replaced by the
    daylight-gated ``np.where(daylight, np.maximum(...), 0)`` form.
    Catch any regressions before they hit ``inputs/input.xlsx``.
    """
    hits: list[str] = []
    for path in _scan_files():
        text = path.read_text(encoding="utf-8")
        if _BAD_PATTERN.search(text):
            for i, line in enumerate(text.splitlines(), start=1):
                if _BAD_PATTERN.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{i}: {line.rstrip()}")
    assert not hits, (
        "v0.7 noise-bleed pattern found (it leaks PV into the night) — "
        "use daylight-gated noise instead:\n" + "\n".join(hits)
    )
