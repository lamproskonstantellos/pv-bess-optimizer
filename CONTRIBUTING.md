# Contributing

## Package layout

The `pvbess_opt/` package keeps a **flat module layout**.  The historical
≤ 12-module ceiling is now **advisory** — the count has grown past it as
the model gained features; re-evaluate subpackaging into `solve/` /
`finance/` / `uncertainty/` / `plotting/` if the flat layout becomes hard
to navigate.

Current modules (14 + plotting subpackage):

```
pvbess_opt/
├── __init__.py
├── theme.py
├── constants.py
├── io.py
├── optimization.py
├── balancing.py
├── modes.py
├── kpis.py
├── lifetime.py
├── economics.py
├── sensitivity.py
├── rolling_horizon.py
├── availability.py
├── max_injection.py
├── timeutils.py
└── plotting/
```

A future subpackaging would group by responsibility: `solve/`
(optimization, lifetime, max_injection), `finance/` (economics,
sensitivity, availability), `uncertainty/` (rolling_horizon),
`plotting/` (already a subpackage).

## Workbook schema

The input workbook is split across **eight themed sheets**:
`timeseries`, `project`, `pv`, `bess`, `economics`, `simulation`,
`balancing`, `max_injection_profile`.  See
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
Python 3.11 / 3.12, all on Ubuntu.  The slow lane runs on pushes to the
default branch and on the nightly schedule.

## Naming conventions

Everything lowercase snake_case.  No camelCase.  No PascalCase except
class names.  No abbreviations like `eta_ch`/`eta_dis` — use the full
form `efficiency_charge` / `efficiency_discharge`.

## Plot style

All figures use the IEEE matplotlib preset (`apply_ieee_style`) and are
exported as PDF.  Plot titles default to off; toggle with `show_titles`
in the `project` sheet.
