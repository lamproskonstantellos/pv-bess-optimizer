"""Thin entry-point shim.

The real CLI lives in :func:`pvbess_opt.cli.main` and the programmatic
pipeline in :func:`pvbess_opt.pipeline.run`.  This shim is kept so
``python main.py inputs/input.xlsx`` continues to work; the installed
console script is ``pvbess`` (see ``[project.scripts]`` in pyproject).
"""

from __future__ import annotations

import sys

from pvbess_opt.cli import main

if __name__ == "__main__":
    sys.exit(main())
