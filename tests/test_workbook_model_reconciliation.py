"""Workbook <-> model schema reconciliation locks.

Direction 1 (workbook -> model): every key the workbook can carry is
consumed by the model.  ``settlement_minutes`` was the one dead key —
documented "informational", read into ``params``, and consumed by
nothing — so it has been removed from the schema; the loader now warns
and ignores it like any unknown key, and the polish script migrates old
workbooks by dropping the row.

Direction 2 (model -> workbook): every tunable the model reads is
parameterizable.  ``bess_eol_soh_pct`` (the SOH threshold that drives
the automatic replacement when ``bess_replacement_year`` is 'auto') is
a workbook key validated to (0, 100] and consumed by the replacement
resolver (``pvbess_opt.lifetime.resolve_bess_replacement_year``).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pvbess_opt.io import (
    BALANCING_SHEET_DEFAULTS,
    BESS_SHEET_DEFAULTS,
    ECONOMICS_SHEET_DEFAULTS,
    PROJECT_SHEET_DEFAULTS,
    PV_SHEET_DEFAULTS,
    SIMULATION_SHEET_DEFAULTS,
    read_inputs,
    read_workbook,
    validate_workbook_params,
    write_workbook,
)
from pvbess_opt.pipeline import _build_degradation_report


def _typed(tmp_overrides: dict | None = None) -> dict:
    n = 24
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "pv_kwh": [100.0] * n,
        "load_kwh": [50.0] * n,
        "dam_price_eur_per_mwh": [80.0] * n,
    })
    typed = {
        "ts": ts,
        "project": dict(PROJECT_SHEET_DEFAULTS),
        "pv": dict(PV_SHEET_DEFAULTS, pv_nameplate_kwp=1000.0),
        "bess": dict(
            BESS_SHEET_DEFAULTS, bess_power_kw=500.0, bess_capacity_kwh=2000.0,
        ),
        "economics": dict(ECONOMICS_SHEET_DEFAULTS),
        "simulation": dict(SIMULATION_SHEET_DEFAULTS),
        "balancing": dict(BALANCING_SHEET_DEFAULTS),
        "max_injection_profile": np.full(24, 100.0, dtype=float),
    }
    for section, kv in (tmp_overrides or {}).items():
        typed[section].update(kv)
    return typed


def test_settlement_minutes_removed_from_schema():
    assert "settlement_minutes" not in PROJECT_SHEET_DEFAULTS


def test_settlement_minutes_absent_from_params(tmp_path):
    xlsx = tmp_path / "wb.xlsx"
    write_workbook(_typed(), xlsx)
    params, _ts = read_inputs(xlsx)
    assert "settlement_minutes" not in params


def test_legacy_settlement_minutes_row_is_warned_and_ignored(tmp_path, caplog):
    """A pre-migration workbook still loads; the stale key is ignored."""
    xlsx = tmp_path / "legacy.xlsx"
    write_workbook(_typed(), xlsx)
    # Inject the legacy row back into the project sheet.
    from openpyxl import load_workbook

    wb = load_workbook(xlsx)
    ws = wb["project"]
    ws.append(["settlement_minutes", 15, "int", "legacy row"])
    wb.save(xlsx)
    with caplog.at_level("WARNING", logger="pvbess_opt.io"):
        typed = read_workbook(xlsx)
    assert "settlement_minutes" not in typed["project"]
    assert any(
        "settlement_minutes" in rec.getMessage() for rec in caplog.records
    )


def test_shipped_workbook_has_no_settlement_minutes_row():
    from pathlib import Path

    repo_xlsx = Path(__file__).resolve().parent.parent / "inputs" / "input.xlsx"
    df = pd.read_excel(repo_xlsx, sheet_name="project")
    assert "settlement_minutes" not in set(df["key"].astype(str))


def test_shipped_workbook_carries_bess_eol_row():
    from pathlib import Path

    repo_xlsx = Path(__file__).resolve().parent.parent / "inputs" / "input.xlsx"
    df = pd.read_excel(repo_xlsx, sheet_name="bess").set_index("key")
    # v1.0.0 case study: SOH-triggered replacement at 70 %.
    assert float(df.loc["bess_eol_soh_pct", "value"]) == 70.0


def test_bess_eol_soh_pct_round_trips_and_validates(tmp_path):
    xlsx = tmp_path / "eol.xlsx"
    write_workbook(_typed({"bess": {"bess_eol_soh_pct": 70.0}}), xlsx)
    typed = read_workbook(xlsx)
    assert typed["bess"]["bess_eol_soh_pct"] == 70.0

    bad = _typed({"bess": {"bess_eol_soh_pct": 0.0}})
    with pytest.raises(ValueError, match="bess_eol_soh_pct"):
        validate_workbook_params(bad, dt_minutes=60)


def test_bess_eol_threshold_drives_the_degradation_diagnostic():
    """A higher EoL threshold forces an earlier diagnostic replacement."""
    n = 96
    soc = pd.Series(
        10_000.0 + 8_000.0 * np.sin(np.linspace(0.0, 12 * np.pi, n)),
    ).clip(lower=2_000.0, upper=19_000.0)
    res = pd.DataFrame({"soc_kwh": soc})
    params = {
        "bess_capacity_kwh": 20_000.0,
        "soc_min_frac": 0.1,
        "soc_max_frac": 0.95,
    }
    base_econ = {
        "bess_degradation_pct_per_cycle": 0.0,
        "bess_degradation_annual_pct": 5.0,
        "project_lifecycle_years": 20,
        "project_start_year": 2026,
        "bess_replacement_year": "auto",
    }

    def _report_with_threshold(eol_pct: float):
        # Mirror the pipeline: resolve the auto sentinel once, store the
        # effective year, then build the report from it.
        from pvbess_opt.lifetime import resolve_bess_replacement_year

        econ = dict(base_econ, bess_eol_soh_pct=eol_pct)
        effective, _source, _second = resolve_bess_replacement_year(
            econ, year1_discharge_mwh=0.0, capacity_mwh=20.0,
        )
        econ["bess_replacement_year_effective"] = effective
        return _build_degradation_report(
            res, params, econ, kpis={"bess_total_discharge_mwh": 0.0},
        )

    low = _report_with_threshold(60.0)
    high = _report_with_threshold(90.0)
    first_low = int(low.loc[low["replacement"], "project_year"].min())
    first_high = int(high.loc[high["replacement"], "project_year"].min())
    # 5 %/yr calendar fade: SOH hits 90 % within ~3 years but needs ~10
    # years to reach 60 %.
    assert first_high < first_low
