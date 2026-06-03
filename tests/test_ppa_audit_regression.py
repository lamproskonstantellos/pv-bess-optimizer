"""Off-state regression guard against the committed audit baselines.

The JSON evidence files under ``scripts/audit_runs/results`` were
generated before the PPA / zero-feed-in features existed, so they are the
oracle for "both features off".  This guard re-runs each combination at
its recorded resolution and asserts that every KPI in the baseline
reproduces bit-identically; the only permitted difference is the set of
new, zero-valued PPA KPI keys (plus the project-revenue roll-up), which
is contract C1.

The fast lane covers the sub-sampled combinations (a few seconds each);
the full-year combinations run in the slow lane.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.audit_runs._common import (
    load_canonical_workbook,
    override_config,
    run_pipeline,
)

_RESULTS_DIR = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "audit_runs" / "results"
)

# The exact set of KPI keys the PPA feature adds on top of a pre-feature
# run.  Five PPA roll-ups, one canonical aggregate, one project total.
_EXPECTED_ADDED_KEYS = {
    "ppa_premium_total_eur",
    "ppa_premium_pv_eur",
    "ppa_premium_bess_eur",
    "ppa_contracted_mwh",
    "ppa_merchant_mwh",
    "revenue_ppa_premium_eur",
    "project_revenue_total_eur",
}
_PPA_ZERO_KEYS = _EXPECTED_ADDED_KEYS - {"project_revenue_total_eur"}


def _all_baseline_jsons() -> list[Path]:
    return sorted(_RESULTS_DIR.glob("*.json"))


def _subsampled_baseline_jsons() -> list[Path]:
    out: list[Path] = []
    for path in _all_baseline_jsons():
        payload = json.loads(path.read_text())
        if payload.get("subsample_steps_applied"):
            out.append(path)
    return out


def _rerun_and_compare(json_path: Path) -> None:
    baseline = json.loads(json_path.read_text())
    combo = baseline["combination"]
    base_params, ts = load_canonical_workbook()
    params = override_config(
        base_params,
        mode=combo["mode"],
        asset_config=combo["asset_config"],
        balancing_enabled=combo["balancing_enabled"],
    )
    # PPA and zero_feed_in are both off (defaults) — exactly the state the
    # committed baseline was generated in.
    result = run_pipeline(
        params, ts,
        mc_scenarios=2,  # MC does not enter the deterministic KPI dict
        subsample_steps=baseline.get("subsample_steps_applied"),
    )
    new_kpis = result["kpis"]
    old_kpis = baseline["kpis"]

    # Every pre-feature KPI must reproduce bit-identically.
    for key, value in old_kpis.items():
        assert key in new_kpis, f"{json_path.name}: missing KPI {key!r}"
        assert new_kpis[key] == value, (
            f"{json_path.name}: KPI {key!r} drifted "
            f"{value!r} -> {new_kpis[key]!r}"
        )

    # The only additions are the new PPA keys (and the roll-up).
    added = set(new_kpis) - set(old_kpis)
    assert added == _EXPECTED_ADDED_KEYS, (
        f"{json_path.name}: unexpected KPI key delta {added!r}"
    )

    # Every PPA KPI is exactly zero with the feature off.
    for key in _PPA_ZERO_KEYS:
        assert new_kpis[key] == 0.0, f"{json_path.name}: {key} != 0"

    # The roll-up equals profit + balancing when the PPA is off.
    assert new_kpis["project_revenue_total_eur"] == pytest.approx(
        new_kpis["profit_total_eur"]
        + new_kpis.get("bm_total_balancing_revenue_eur", 0.0),
        abs=0.01,
    )

    # Invariants stay within tolerance.
    for name, entry in result["invariants"].items():
        assert entry["within_tolerance"], f"{json_path.name}: {name} violated"


@pytest.mark.parametrize(
    "json_path", _subsampled_baseline_jsons(), ids=lambda p: p.stem,
)
def test_audit_baseline_unchanged_fast(json_path: Path) -> None:
    _rerun_and_compare(json_path)


@pytest.mark.slow
@pytest.mark.parametrize(
    "json_path", _all_baseline_jsons(), ids=lambda p: p.stem,
)
def test_audit_baseline_unchanged_full(json_path: Path) -> None:
    _rerun_and_compare(json_path)
