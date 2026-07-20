"""Multi-scenario price decks (``<column>__<deck>`` variant columns).

A deck swaps the Year-1 price timeseries per scenario BEFORE the MILP
re-solve — the structural complement to the per-year trajectories
(which reshape years 2+ analytically).  Locked properties:

1. Variant columns are inert pass-through in a normal run and are
   stripped before any per-scenario workbook is written.
2. A deck-selecting scenario equals a standalone run whose canonical
   columns carry the deck prices (HiGHS parity).
3. Misconfiguration fails BEFORE any solver time: unknown deck names
   in ``run_scenario_batch``, unknown base names left of ``__`` at
   load.
4. Deck-free batches keep the comparison frame bit-identical (no
   ``price_deck`` column).
5. Partial decks fall back to base columns (INFO), and a deck-supplied
   balancing price column survives the scalar balancing fallback.
6. YAML ``price_decks:`` files merge as variant columns with row-count
   and column-name validation.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from pvbess_opt.io import read_workbook, write_workbook
from pvbess_opt.scenarios import (
    _apply_price_deck,
    _apply_scenario_overrides,
    _parse_scenarios_sheet,
    _strip_price_deck_variants,
    run_scenario_batch,
    validate_scenario_overrides,
)

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "inputs" / "input.xlsx"


def _highs_available() -> bool:
    try:
        import highspy  # noqa: F401
    except ImportError:
        return False
    return True


def _short_typed(tmp_path: Path, *, with_deck_column: bool = True) -> dict:
    """Shipped workbook sliced to one day, optionally with a high deck."""
    typed = read_workbook(SHIPPED)
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)
    if with_deck_column:
        typed["ts"]["dam_price_eur_per_mwh__high"] = (
            typed["ts"]["dam_price_eur_per_mwh"].astype(float) * 1.5
        )
    short = tmp_path / "short.xlsx"
    write_workbook(typed, short)
    return read_workbook(short)


_SOLVER_OPTS = {
    "solver_name": "highs", "mip_gap": 0.05,
    "time_limit_seconds": 180, "tee": False,
}


# ---------------------------------------------------------------------------
# 1. Inert variants + stripping
# ---------------------------------------------------------------------------


def test_variant_column_is_inert_at_load(tmp_path):
    base = _short_typed(tmp_path / "a", with_deck_column=False)
    with_deck = _short_typed(tmp_path / "b", with_deck_column=True)
    assert "dam_price_eur_per_mwh__high" in with_deck["ts"].columns
    pd.testing.assert_series_equal(
        base["ts"]["dam_price_eur_per_mwh"],
        with_deck["ts"]["dam_price_eur_per_mwh"],
    )


def test_apply_overrides_strips_variants_without_deck(tmp_path):
    typed = _short_typed(tmp_path)
    out = _apply_scenario_overrides(typed, {"name": "plain"})
    assert not [c for c in out["ts"].columns if "__" in str(c)]
    pd.testing.assert_series_equal(
        typed["ts"]["dam_price_eur_per_mwh"],
        out["ts"]["dam_price_eur_per_mwh"],
    )


def test_strip_variants_preserves_other_columns(tmp_path):
    typed = _short_typed(tmp_path)
    stripped = _strip_price_deck_variants(typed["ts"])
    assert "dam_price_eur_per_mwh" in stripped.columns
    assert "dam_price_eur_per_mwh__high" not in stripped.columns


def test_unknown_base_name_rejected(tmp_path):
    typed = read_workbook(SHIPPED)
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)
    typed["ts"]["load_kwh__high"] = 1.0  # load is not a price column
    bad = tmp_path / "bad.xlsx"
    write_workbook(typed, bad)
    with pytest.raises(ValueError, match="unknown price-deck base"):
        read_workbook(bad)


# ---------------------------------------------------------------------------
# 2+3. Deck resolution, fail-fast
# ---------------------------------------------------------------------------


def test_apply_price_deck_copies_and_strips(tmp_path):
    typed = _short_typed(tmp_path)
    high = typed["ts"]["dam_price_eur_per_mwh__high"].copy()
    out = _apply_scenario_overrides(
        typed, {"name": "deck", "price_deck": "high"},
    )
    assert not [c for c in out["ts"].columns if "__" in str(c)]
    assert out["ts"]["dam_price_eur_per_mwh"].tolist() == high.tolist()


def test_unknown_deck_fails_fast_before_any_solve(tmp_path, monkeypatch):
    import pvbess_opt.scenarios as scn_mod

    typed = _short_typed(tmp_path)

    def _boom(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("solver was invoked before deck validation")

    monkeypatch.setattr(scn_mod, "evaluate_scenario", _boom)
    with pytest.raises(ValueError, match="matches no"):
        run_scenario_batch(
            typed,
            [{"name": "bad", "price_deck": "low"}],
            solver_opts=_SOLVER_OPTS,
        )


def test_price_deck_value_validated():
    with pytest.raises(ValueError, match="non-empty deck name"):
        validate_scenario_overrides({"name": "x", "price_deck": ""})


def test_partial_deck_falls_back_with_info(caplog):
    ts = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=4, freq="15min"),
        "dam_price_eur_per_mwh": [50.0, 60.0, 70.0, 80.0],
        "retail_price_eur_per_mwh": [150.0] * 4,
        "dam_price_eur_per_mwh__high": [90.0, 100.0, 110.0, 120.0],
    })
    with caplog.at_level("INFO"):
        out = _apply_price_deck(ts, "high", scenario_name="partial")
    # DAM takes the deck values; retail keeps base (INFO logged).
    assert out["dam_price_eur_per_mwh"].tolist() == [90.0, 100.0, 110.0, 120.0]
    assert out["retail_price_eur_per_mwh"].tolist() == [150.0] * 4
    assert any(
        "retail_price_eur_per_mwh" in r.message for r in caplog.records
    )


# ---------------------------------------------------------------------------
# 4. Comparison-frame bit-identity
# ---------------------------------------------------------------------------


def _fake_rows(monkeypatch):
    import pvbess_opt.scenarios as scn_mod
    from pvbess_opt.scenarios import _COMPARISON_COLUMNS

    def _fake_eval(base_typed, scn, *, solver_opts, base_dir=None):
        row = {c: 0.0 for c in _COMPARISON_COLUMNS}
        row["name"] = scn.get("name")
        row["price_deck"] = str(scn.get("price_deck") or "")
        return row

    monkeypatch.setattr(scn_mod, "evaluate_scenario", _fake_eval)


def test_no_deck_batch_has_no_price_deck_column(tmp_path, monkeypatch):
    from pvbess_opt.scenarios import _COMPARISON_COLUMNS

    _fake_rows(monkeypatch)
    typed = _short_typed(tmp_path, with_deck_column=False)
    comparison = run_scenario_batch(
        typed, [{"name": "a"}, {"name": "b"}], solver_opts=_SOLVER_OPTS,
    )
    assert list(comparison.columns) == list(_COMPARISON_COLUMNS)


def test_deck_batch_carries_price_deck_column(tmp_path, monkeypatch):
    _fake_rows(monkeypatch)
    typed = _short_typed(tmp_path)
    comparison = run_scenario_batch(
        typed,
        [{"name": "central"}, {"name": "high", "price_deck": "high"}],
        solver_opts=_SOLVER_OPTS,
    )
    assert list(comparison.columns)[1] == "price_deck"
    assert comparison.loc[1, "price_deck"] == "high"


# ---------------------------------------------------------------------------
# 5. Balancing fallback interaction
# ---------------------------------------------------------------------------


def test_deck_balancing_column_survives_scalar_fallback(tmp_path):
    typed = read_workbook(SHIPPED)
    typed["ts"] = typed["ts"].iloc[:96].reset_index(drop=True)
    typed["balancing"]["balancing_enabled"] = True
    typed["ts"]["fcr_capacity_price_eur_per_mwh__high"] = 123.0
    short = tmp_path / "bal.xlsx"
    write_workbook(typed, short)
    base = read_workbook(short)

    out = _apply_scenario_overrides(
        base, {"name": "deck", "price_deck": "high"},
    )
    materialized = tmp_path / "scn.xlsx"
    write_workbook(out, materialized)
    reread = read_workbook(materialized)
    # The deck-resolved canonical column wins over the scalar default
    # applied by _apply_balancing_timeseries_fallback on the re-read.
    assert (
        reread["ts"]["fcr_capacity_price_eur_per_mwh"] == 123.0
    ).all()


# ---------------------------------------------------------------------------
# 6. YAML surface + scenarios sheet parsing
# ---------------------------------------------------------------------------


def _yaml_config(tmp_path: Path, deck_cols: dict) -> Path:
    typed = read_workbook(SHIPPED)
    ts = typed["ts"].iloc[:96].reset_index(drop=True)
    ts.to_csv(tmp_path / "ts.csv", index=False)
    deck = pd.DataFrame(deck_cols)
    deck.to_csv(tmp_path / "high.csv", index=False)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "timeseries_path: ts.csv\n"
        "price_decks:\n"
        "  high: high.csv\n"
    )
    return cfg


def test_yaml_price_decks_merge_as_variants(tmp_path):
    from pvbess_opt.io_read import load_structured_config

    cfg = _yaml_config(
        tmp_path, {"dam_price_eur_per_mwh": [200.0] * 96},
    )
    typed = load_structured_config(cfg)
    col = typed["ts"]["dam_price_eur_per_mwh__high"]
    assert (col == 200.0).all()


def test_yaml_price_deck_row_count_mismatch_raises(tmp_path):
    from pvbess_opt.io_read import load_structured_config

    cfg = _yaml_config(
        tmp_path, {"dam_price_eur_per_mwh": [200.0] * 50},
    )
    with pytest.raises(ValueError, match="rows"):
        load_structured_config(cfg)


def test_yaml_price_deck_unknown_column_raises(tmp_path):
    from pvbess_opt.io_read import load_structured_config

    cfg = _yaml_config(tmp_path, {"load_kwh": [1.0] * 96})
    with pytest.raises(ValueError, match="not a recognised price column"):
        load_structured_config(cfg)


def test_scenarios_sheet_price_deck_target_parses():
    df = pd.DataFrame(
        [
            ("TRUE", "High DAM", None, "price_deck", "high"),
        ],
        columns=["enabled", "name", "inherits", "target", "value"],
    )
    enabled, scenarios = _parse_scenarios_sheet(df)
    assert enabled is True
    assert scenarios == [{"name": "High DAM", "price_deck": "high"}]


# ---------------------------------------------------------------------------
# 7. Solve parity (deck scenario == standalone with deck prices)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _highs_available(), reason="HiGHS solver not installed")
def test_deck_scenario_matches_standalone_run(tmp_path):
    from pvbess_opt.scenarios import evaluate_scenario

    typed = _short_typed(tmp_path)

    # Standalone: canonical DAM column carries the high prices.
    standalone = read_workbook(SHIPPED)
    standalone["ts"] = standalone["ts"].iloc[:96].reset_index(drop=True)
    standalone["ts"]["dam_price_eur_per_mwh"] = (
        standalone["ts"]["dam_price_eur_per_mwh"].astype(float) * 1.5
    )
    alone = tmp_path / "alone.xlsx"
    write_workbook(standalone, alone)

    row_deck = evaluate_scenario(
        typed, {"name": "high", "price_deck": "high"},
        solver_opts=_SOLVER_OPTS,
    )
    row_alone = evaluate_scenario(
        read_workbook(alone), {"name": "alone"}, solver_opts=_SOLVER_OPTS,
    )
    assert row_deck["npv_eur"] == pytest.approx(
        row_alone["npv_eur"], rel=1e-9, abs=1e-6,
    )
    assert row_deck["profit_total_eur"] == pytest.approx(
        row_alone["profit_total_eur"], rel=1e-9, abs=1e-6,
    )


# ---------------------------------------------------------------------------
# 8. Per-deck comparison bars
# ---------------------------------------------------------------------------


def test_comparison_bars_label_decks(tmp_path, monkeypatch):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from pvbess_opt.plotting import scenarios as plot_mod

    captured: dict = {}

    def _save(path):
        captured["fig"] = plt.gcf()
        return path

    monkeypatch.setattr(plot_mod, "save_figure", _save)
    comparison = pd.DataFrame({
        "name": ["Merchant", "Merchant"],
        "price_deck": ["", "high"],
        "npv_eur": [1.0e6, 2.0e6],
        "irr_pct": [8.0, 11.0],
    })
    plot_mod.plot_scenario_comparison_bars(
        comparison, tmp_path / "bars.pdf",
    )
    labels = [
        t.get_text() for t in captured["fig"].axes[0].get_xticklabels()
    ]
    plt.close("all")
    assert labels == ["Merchant", "Merchant [high]"]
