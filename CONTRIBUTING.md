# Contributing

## Package layout

The `pvbess_opt/` package keeps a **flat module layout** plus two
subpackages (`plotting/` for the figure stack, `resource/` for the
PVGIS fetch).  Re-evaluate subpackaging into `solve/` / `finance/` /
`uncertainty/` if the flat layout becomes hard to navigate.

Current top-level modules (24 + the `plotting/` and `resource/`
subpackages):

```
pvbess_opt/
├── __init__.py
├── availability.py
├── balancing.py
├── cli.py
├── constants.py
├── degradation.py
├── economics.py
├── emissions.py
├── io.py
├── io_read.py
├── io_style.py
├── kpis.py
├── lifetime.py
├── max_injection.py
├── modes.py
├── optimization.py
├── pipeline.py
├── ppa.py
├── rolling_horizon.py
├── scenarios.py
├── sensitivity.py
├── sizing.py
├── theme.py
├── timeutils.py
├── plotting/
└── resource/
```

A future subpackaging would group by responsibility: `solve/`
(optimization, lifetime, max_injection), `finance/` (economics,
sensitivity, availability), `uncertainty/` (rolling_horizon),
`plotting/` (already a subpackage).

## Workbook schema

The input workbook carries nine core sheets (`timeseries`,
`project`, `pv`, `bess`, `economics`, `simulation`, `balancing`,
`ppa`, `max_injection_profile`) plus the optional per-source sub-cap
sheets (`max_injection_profile_pv` / `max_injection_profile_bess`) and
the `sizing` / `scenarios` sweep sheets.  See
`docs/source/users.guide/inputs.rst` for the full reference.

## Style

* Pure Python, runs on Python ≥ 3.11 across Linux, macOS, Windows.
* All file paths via `pathlib`.  No shell escapes.
* Lowercase snake_case for every variable, parameter, KPI key, and
  workbook key.
* All KPI keys returned by `compute_kpis` are lowercase.

## Tests and checks

Run the gates locally:

```bash
pip install -r requirements/dev.txt
ruff check .
mypy
vulture
python -m pytest tests/ -q
```

CI runs ruff, mypy, and vulture, then the fast-lane pytest across
Python 3.11 / 3.12, all on Ubuntu, plus a Sphinx docs build.  The slow
lane runs on pushes to the default branch and on the nightly schedule.

## Naming conventions

Everything lowercase snake_case.  No camelCase.  No PascalCase except
class names.  No abbreviations like `eta_ch`/`eta_dis`: use the full
form `efficiency_charge` / `efficiency_discharge`.

## Plot style

All figures use the IEEE matplotlib preset (`apply_ieee_style`) and are
exported as PDF.  Plot titles default to off; toggle with `show_titles`
in the `project` sheet.
