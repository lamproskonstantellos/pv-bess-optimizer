# Test-suite overview

The `tests/` directory is the project's executable specification: each
file locks one behaviour or contract and is named for what it verifies.
To understand an invariant the code must uphold, read the test that
guards it.

## Running

```bash
pip install -r requirements/dev.txt
pytest                # fast lane (default)
pytest -m slow        # opt-in real-scale workbook lane (minutes wall-clock)
```

The fast lane runs on every pull request; the slow lane runs on pushes to
the default branch and on the nightly schedule.

## Conventions

* Test modules are named `test_<area>.py`. Helpers shared across modules
  live in `tests/_*.py` (for example `tests/_pv_helpers.py` and
  `tests/_balancing_helpers.py`) and `tests/conftest.py` holds the shared
  fixtures.
* Tests that solve the real-scale workbook are marked `slow` and excluded
  from the default lane.
* Repository-hygiene invariants — no stale version/annotation tokens in
  evergreen surfaces, required tokens and files present, and the shipped
  input workbook loading without legacy warnings — are locked by
  `tests/test_repo_hygiene.py`.
* The domain design documents under `docs/` (see `docs/README.md`) are
  machine-checked where they state code contracts:
  `tests/test_logic_spec_conformance.py` parses the constraint and
  invariant headings out of `docs/self_consumption_design.md` and the
  verification-appendix symbols out of
  `docs/balancing_market_design.md` and asserts each one on a freshly
  built model; `tests/test_input_surface_parity.py` locks the
  workbook / YAML / scenario-target configuration surfaces to be exact
  mirrors.
