"""Regression guard for the Sphinx documentation build.

Builds the HTML docs with intersphinx disabled (so the test never touches
the network) and asserts the build emits **zero** warnings.  This catches
docutils-level defects that ``sphinx-build -W`` would otherwise flag only
in the docs CI lane:

* malformed simple tables (a cell overflowing its column),
* section-title underlines shorter than the title,
* autodoc docstrings whose field lists carry unexpected indentation,
* ``html_static_path`` entries that point at a missing directory.

The test ``skip``s when Sphinx is not installed (the dev requirements do
not pull in the docs toolchain); it runs in the docs environment and on
any checkout that installed ``requirements/docs.txt``.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

pytest.importorskip("sphinx")

ROOT = Path(__file__).resolve().parent.parent
DOCS_SRC = ROOT / "docs" / "source"


# Sphinx's own napoleon extension emits a RemovedInSphinx11Warning (its
# internal autodoc-options mapping interface) once per documented object, so
# building the HTML inside pytest floods the suite with ~1400 identical
# third-party deprecations that are neither our code nor a correctness signal.
# Silence that one message here (the build's OWN docutils/autodoc warnings are
# still asserted below via warning_stream); every other warning stays visible.
@pytest.mark.filterwarnings(
    "ignore:The mapping interface for autodoc options:"
)
def test_docs_build_emits_no_warnings(tmp_path):
    """A clean Sphinx HTML build must produce no warnings or errors.

    Intersphinx is disabled via ``confoverrides`` so the build is
    hermetic — the only warnings that can surface are real docutils /
    autodoc defects in the project's own ``.rst`` files and docstrings.
    """
    if not DOCS_SRC.exists():
        pytest.skip("docs/source not present in this checkout")

    from sphinx.application import Sphinx

    warning_stream = io.StringIO()
    app = Sphinx(
        srcdir=str(DOCS_SRC),
        confdir=str(DOCS_SRC),
        outdir=str(tmp_path / "html"),
        doctreedir=str(tmp_path / "doctrees"),
        buildername="html",
        warning=warning_stream,
        warningiserror=False,
        freshenv=True,
        # Disable intersphinx so the build never reaches out to the
        # network; remote inventory fetches are an environment concern,
        # not a documentation defect.
        confoverrides={"intersphinx_mapping": {}},
    )
    app.build()

    warnings_text = warning_stream.getvalue().strip()
    # Defensive: drop any residual network/intersphinx noise so the
    # assertion only fires on genuine documentation defects.
    offending = [
        line
        for line in warnings_text.splitlines()
        if line.strip()
        and "objects.inv" not in line
        and "intersphinx" not in line.lower()
        and "inventory" not in line.lower()
    ]
    assert not offending, (
        "Sphinx build emitted warnings:\n" + "\n".join(offending)
    )
