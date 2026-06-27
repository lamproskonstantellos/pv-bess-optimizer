"""The figure-format switch makes the single styler emit PNG / SVG / PDF.

Locks the DRY export hook used by ``scripts/export_readme_figures.py``:
one switch (``set_figure_format``) drives the one saver (``save_figure``),
so the README gallery can be rendered as PNG without forking any plotting
code.  Default stays PDF (the paper report), so every other plot test is
unaffected.
"""

from __future__ import annotations

import matplotlib.pyplot as plt
import pytest

from pvbess_opt.plotting.style import (
    get_figure_format,
    save_figure,
    set_figure_format,
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_default_format_is_pdf(tmp_path):
    assert get_figure_format() == "pdf"
    plt.figure()
    plt.plot([0, 1], [0, 1])
    out = save_figure(tmp_path / "fig")
    assert out.suffix == ".pdf"
    assert out.exists()


def test_png_format_forces_png_suffix_and_bytes(tmp_path):
    try:
        set_figure_format("png")
        assert get_figure_format() == "png"
        plt.figure()
        plt.plot([0, 1], [0, 1])
        # Pass a .pdf path on purpose — the suffix must be forced to .png.
        out = save_figure(tmp_path / "fig.pdf")
        assert out.suffix == ".png"
        assert out.exists()
        assert out.read_bytes()[:8] == _PNG_MAGIC
    finally:
        set_figure_format("pdf")
    assert get_figure_format() == "pdf"


def test_invalid_format_raises():
    with pytest.raises(ValueError, match="figure format must be one of"):
        set_figure_format("jpeg")
    assert get_figure_format() == "pdf"
