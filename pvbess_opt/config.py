"""Constants shared across modules: plot labels, colors, IEEE style."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Plot labels and colors
# ---------------------------------------------------------------------------

ALL_LABELS: list[str] = [
    "Load (demand)",
    "PV→Load",
    "BESS→Load",
    "Import→Load",
    "PV→BESS (charge)",
    "Import→BESS (charge)",
    "PV→Grid (export)",
    "BESS→Grid (export)",
    "PV→Curtailment",
    "Export cap",
]

COLORS: dict[str, str] = {
    # Load (priority indicator)
    "Load (demand)": "#d62728",
    # PV-origin flows (warm gradient)
    "PV→Load": "#D2691E",
    "PV→BESS (charge)": "#DAA520",
    "PV→Grid (export)": "#C19A6B",
    "PV→Curtailment": "#3C3C3C",
    # BESS-origin flows (cool blue)
    "BESS→Load": "#1C5A8E",
    "BESS→Grid (export)": "#5B9BD5",
    # Grid-origin flows (slate)
    "Import→Load": "#607D8B",
    "Import→BESS (charge)": "#B0BEC5",
    # Annotations
    "Export cap": "#7f7f7f",
}

LEGEND_ORDER: list[str] = [
    "Load (demand)",
    "PV→Load",
    "BESS→Load",
    "PV→BESS (charge)",
    "PV→Grid (export)",
    "PV→Curtailment",
    "BESS→Grid (export)",
    "Import→Load",
    "Import→BESS (charge)",
    "Export cap",
]

# Stack alphas for area / bar plots
ALPHA_STACK_AREAS: float = 1.0
ALPHA_STACK_BARS: float = 1.0

# Default tick rotation
XTICK_ROT: int = 45

# ---------------------------------------------------------------------------
# IEEE matplotlib rcParams
# ---------------------------------------------------------------------------

IEEE_RCPARAMS: dict[str, object] = {
    # Font fallback chain so the IEEE preset works on Windows / macOS / Linux
    # without requiring a manual font install.  Matplotlib walks the list and
    # picks the first font it finds.
    "font.family": "serif",
    "font.serif": [
        "Times New Roman",
        "Times",
        "Liberation Serif",
        "Nimbus Roman",
        "DejaVu Serif",
        "serif",
    ],
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "font.size": 8,
    "axes.titlesize": 9,
    "axes.labelsize": 9,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "legend.fontsize": 7,
    "lines.linewidth": 1.0,
    "lines.markersize": 4,
    "axes.linewidth": 1.0,
    "axes.grid": True,
    "grid.linestyle": "--",
    "grid.linewidth": 0.8,
    "grid.alpha": 0.5,
    "legend.frameon": True,
    "legend.framealpha": 0.9,
    "legend.columnspacing": 1.0,
    "figure.figsize": (3.5, 2.5),
}

# Default IEEE figure size for PDF export.
PDF_FIGSIZE: tuple[float, float] = (3.5, 2.5)


def assert_unique_colors() -> None:
    """Sanity check: every label has a colour, no colour is reused."""
    missing = [lab for lab in ALL_LABELS if lab not in COLORS]
    if missing:
        raise ValueError(f"Missing colors for labels: {missing}")
    inverse: dict[str, list[str]] = {}
    for label, color in COLORS.items():
        inverse.setdefault(color.lower(), []).append(label)
    duplicates = {c: labs for c, labs in inverse.items() if len(labs) > 1}
    if duplicates:
        raise ValueError(f"Duplicate color assignments: {duplicates}")
