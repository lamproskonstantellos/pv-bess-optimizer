# Contributing

## Package layout

The `pvbess_opt/` package keeps a **flat module layout** (≤ 12 top-level
modules).  This is a deliberate ceiling: re-evaluate subpackaging into
`solve/` / `finance/` / `uncertainty/` / `plotting/` only when the module
count crosses **12**.

Current modules (11 + plotting subpackage):

```
pvbess_opt/
├── __init__.py
├── config.py
├── io.py
├── optimization.py
├── kpis.py
├── lifetime.py
├── economics.py
├── sensitivity.py
├── rolling_horizon.py
├── availability.py
├── max_injection.py
└── plotting/
```

If the count crosses 12, future PRs should subpackage by responsibility:
`solve/` (optimization, lifetime, max_injection), `finance/` (economics,
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

## Tests

Run the full suite:

```bash
pip install -r requirements/dev.txt
python -m pyflakes pvbess_opt/ main.py tests/ scripts/
python -m pycodestyle --max-line-length=100 --select=E9,W6,E501 pvbess_opt/ main.py tests/ scripts/
python -m pytest tests/ -v
```

CI runs the same three commands across Python 3.11 / 3.12 on
Ubuntu / macOS / Windows.

## Naming conventions

Everything lowercase snake_case.  No camelCase.  No PascalCase except
class names.  No abbreviations like `eta_ch`/`eta_dis` — use the full
form `efficiency_charge` / `efficiency_discharge`.

## Plot style

All figures use the IEEE matplotlib preset (`apply_ieee_style`) and are
exported as PDF.  Plot titles default to off; toggle with `show_titles`
in the `project` sheet.
