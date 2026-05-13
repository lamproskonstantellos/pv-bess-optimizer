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


# ---------------------------------------------------------------------------
# Financial-plot colour palette (v0.8 polish)
# ---------------------------------------------------------------------------

FINANCIAL_COLORS: dict[str, str] = {
    # Cashflow / NPV / payback stacks
    "revenue":        "#2E7D32",  # green
    "opex":           "#EF6C00",  # amber
    "capex":          "#C62828",  # red
    "devex":          "#8E44AD",  # purple
    "net":            "#1565C0",  # blue
    "discounted":     "#6A1B9A",  # dark purple
    # Tornado halves (below / above base)
    "tornado_neg":    "#B71C1C",  # red
    "tornado_pos":    "#1B5E20",  # green
    # LCOE / LCOS summary
    "benchmark_band": "#BDBDBD",  # light grey
    "lcoe_bar":       "#F57C00",  # warm orange
    "lcos_bar":       "#0277BD",  # cool blue
    "base_marker":    "#000000",  # black
    # Revenue stack (load_from_pv intentionally same green as revenue)
    "load_from_pv":   "#2E7D32",
    "load_from_bess": "#00838F",  # teal — distinct from green/blue
    "export_from_pv":   "#42A5F5",  # light blue
    "export_from_bess": "#0D47A1",  # dark blue
    "grid_charge_cost": "#D32F2F",  # red (negative stack)
}


def assert_unique_financial_colors() -> None:
    """Sanity check: financial colours must be mutually distinguishable.

    A handful of keys are intentionally aliased (e.g. ``load_from_pv`` reuses
    the ``revenue`` green) — those colliding pairs are whitelisted.  Any
    other duplicate hex value is treated as a configuration mistake.
    """
    allowed_aliases: set[frozenset[str]] = {
        frozenset({"revenue", "load_from_pv"}),
    }
    inverse: dict[str, list[str]] = {}
    for key, hex_value in FINANCIAL_COLORS.items():
        inverse.setdefault(hex_value.lower(), []).append(key)
    duplicates = {h: keys for h, keys in inverse.items() if len(keys) > 1}
    real_duplicates = {
        h: keys for h, keys in duplicates.items()
        if frozenset(keys) not in allowed_aliases
    }
    if real_duplicates:
        raise ValueError(
            f"Duplicate FINANCIAL_COLORS hex values: {real_duplicates}"
        )


assert_unique_financial_colors()
