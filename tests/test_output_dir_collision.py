"""Same-second output-directory collisions must never overwrite a run.

The run directories are stamped to whole seconds
(``<input>_<scenario>_<YYYYmmdd_HHMMSS>``), so two runs starting within
the same second previously shared ONE directory: the second batch
silently overwrote the first's ``scenario_comparison.xlsx`` and plots.
``pvbess_opt.io.unique_output_dir`` bumps the name with a logged
``_2`` / ``_3`` suffix instead; all three run surfaces (single run,
scenario batch, sizing sweep) route their directory through it.
"""

from __future__ import annotations

import logging

from pvbess_opt.io import unique_output_dir


def test_free_name_is_returned_verbatim(tmp_path):
    target = tmp_path / "input_scenarios_20260101_120000"
    assert unique_output_dir(target) == target


def test_collision_bumps_to_numbered_sibling(tmp_path, caplog):
    target = tmp_path / "input_scenarios_20260101_120000"
    target.mkdir()
    with caplog.at_level(logging.INFO, logger="pvbess_opt.io"):
        bumped = unique_output_dir(target)
    assert bumped == tmp_path / "input_scenarios_20260101_120000_2"
    assert any("already exists" in r.getMessage() for r in caplog.records)
    bumped.mkdir()
    assert unique_output_dir(target) == (
        tmp_path / "input_scenarios_20260101_120000_3"
    )


def test_all_three_run_surfaces_route_through_the_helper():
    """Wiring lock: the single-run layout, the scenario batch and the
    sizing sweep all build their output directory via unique_output_dir
    (a helper nobody calls is not a fix)."""
    import inspect

    from pvbess_opt import pipeline, scenarios, sizing

    assert "unique_output_dir(" in inspect.getsource(pipeline._run_one)
    assert "unique_output_dir(" in inspect.getsource(scenarios.run_scenarios)
    assert "unique_output_dir(" in inspect.getsource(sizing.run_sizing)
