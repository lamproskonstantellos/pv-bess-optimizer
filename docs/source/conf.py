"""Sphinx configuration for the pv-bess-optimizer documentation.

Builds the docs site (Read-the-Docs theme) from ``docs/source/`` into
``docs/build/`` via::

    pip install -r requirements/docs.txt
    sphinx-build -b html docs/source docs/build
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

# Make the package importable for autodoc.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# -- Project information ----------------------------------------------------

project = "pv-bess-optimizer"
author = "Lampros Konstantellos"
copyright = f"{datetime.now().year}, {author}"

try:
    from pvbess_opt import __version__ as release  # type: ignore
except Exception:
    release = "0.8.1"
version = ".".join(release.split(".")[:2])

# -- General configuration --------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx.ext.mathjax",
    "sphinx.ext.todo",
]

templates_path = ["_templates"]
exclude_patterns: list[str] = []

source_suffix = {".rst": "restructuredtext"}
master_doc = "index"
language = "en"

# -- HTML output ------------------------------------------------------------

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
html_theme_options = {
    "navigation_depth": 3,
    "collapse_navigation": False,
    "sticky_navigation": True,
    "titles_only": False,
}

# -- Autodoc / Napoleon -----------------------------------------------------

autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
}
autodoc_typehints = "description"
napoleon_google_docstring = True
napoleon_numpy_docstring = True

# -- Intersphinx ------------------------------------------------------------

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pandas": ("https://pandas.pydata.org/docs/", None),
    "numpy": ("https://numpy.org/doc/stable/", None),
    "matplotlib": ("https://matplotlib.org/stable/", None),
    "pyomo": ("https://pyomo.readthedocs.io/en/stable/", None),
}
