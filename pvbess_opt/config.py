"""Constants shared across modules: plot labels, colors, IEEE style."""

from __future__ import annotations

import logging
import math

# ---------------------------------------------------------------------------
# Project-level defaults
# ---------------------------------------------------------------------------

# Default share of p_grid_export_max_kw that is available for export, in
# percent (per hour-of-day).  Applied when the workbook omits the
# max_injection_profile sheet.  Matches the inverse of the historical
# 27 % regulatory curtailment used as the project's reference scenario
# (100 - 27 = 73).
DEFAULT_MAX_INJECTION_PCT_HOURLY: float = 73.0

# ---------------------------------------------------------------------------
# Plot labels and colors
# ---------------------------------------------------------------------------

ALL_LABELS: list[str] = [
    "Load (demand)",
    "PV generation",
    "PV→Load",
    "BESS→Load",
    "Import→Load",
    "PV→BESS (charge)",
    "Import→BESS (charge)",
    "PV→Grid (export)",
    "BESS→Grid (export)",
    "PV→Curtailment",
]

COLORS: dict[str, str] = {
    # Load (priority indicator)
    "Load (demand)": "#d62728",
    # PV reference line (merchant combined view)
    "PV generation": "#FFB300",
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
}

# ---------------------------------------------------------------------------
# Merchant revenue-plot label colours
# ---------------------------------------------------------------------------
#
# The daily / monthly / yearly merchant revenue plots draw labels of the
# form "PV→Grid (revenue)" / "BESS→Grid (revenue)" / "Import→BESS (cost)".
# These are a financial view of physical flows already present in the
# energy stack, so each revenue label shares the hex value of its energy
# counterpart — a reader who memorised "yellow = PV→Grid in the energy
# plot" reads "yellow = PV→Grid in the revenue plot" without a second
# look.
#
# Kept in a separate registry so ``COLORS`` itself stays free of
# duplicate hex values; ``label_color`` resolves through both
# registries.

MERCHANT_COLORS: dict[str, str] = {
    "PV→Grid (revenue)":   COLORS["PV→Grid (export)"],
    "BESS→Grid (revenue)": COLORS["BESS→Grid (export)"],
    "Import→BESS (cost)":  COLORS["Import→BESS (charge)"],
}


def label_color(label: str) -> str | None:
    """Return the canonical hex colour for any plot label.

    Looks up :data:`COLORS` (energy plots) first, then
    :data:`MERCHANT_COLORS` (financial-view variants of the same
    physical flow).  Returns ``None`` when neither registry knows
    the label, so call sites can fall back to matplotlib defaults
    where appropriate.
    """
    if label in COLORS:
        return COLORS[label]
    if label in MERCHANT_COLORS:
        return MERCHANT_COLORS[label]
    return None


LEGEND_ORDER: list[str] = [
    "Load (demand)",
    "PV generation",
    "PV→Load",
    "BESS→Load",
    "PV→BESS (charge)",
    "PV→Grid (export)",
    "PV→Curtailment",
    "BESS→Grid (export)",
    "Import→Load",
    "Import→BESS (charge)",
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

# All hex values are picked from the Material Design palette (Google
# 2014 release, IEEE-print-safe) so the financial / lifecycle /
# uncertainty plots use a single coherent colour family.  Each entry
# is referenced by KEY everywhere downstream — no hex literal lives
# in a plotting module.
FINANCIAL_COLORS: dict[str, str] = {
    # Cashflow / NPV / payback stacks
    "revenue":        "#2E7D32",  # Material green 800
    "opex":           "#EF6C00",  # Material orange 800
    "capex":          "#C62828",  # Material red 800
    "devex":          "#8E44AD",  # Material purple
    "net":            "#1565C0",  # Material blue 800
    "discounted":     "#6A1B9A",  # Material purple 800
    "net_discounted": "#283593",  # Material indigo 800 (discounted net line)
    # Tornado halves (below / above base)
    "tornado_neg":    "#B71C1C",  # Material red 900
    "tornado_pos":    "#1B5E20",  # Material green 900
    # LCOE / LCOS summary
    "benchmark_band": "#BDBDBD",  # Material grey 400
    "lcoe_bar":       "#F57C00",  # Material orange 700
    "lcos_bar":       "#0277BD",  # Material light-blue 800
    "base_marker":    "#000000",  # pure black diamond
    # Revenue stack (load_from_pv intentionally same green as revenue)
    "load_from_pv":   "#2E7D32",
    "load_from_bess": "#00838F",  # Material cyan 800 — distinct from green/blue
    "export_from_pv":   "#42A5F5",  # Material blue 400
    "export_from_bess": "#0D47A1",  # Material blue 900
    "grid_charge_cost": "#D32F2F",  # Material red 700 (negative stack)
    "aggregator_fee":   "#AD1457",  # Material pink 800 (deduction tone)
    # Foreground net-revenue line — near-black (Material grey 900),
    # IEEE publication-style emphasis colour.  High contrast against
    # every saturated stack colour above; white-edged markers keep it
    # legible over the dark BESS-export blue.
    "net_revenue_line": "#212121",
    # Rolling-horizon Monte Carlo percentile markers + perfect
    # foresight benchmark.  Aliased to the cashflow palette keys so
    # the same colour reads identically across panels (red = lower
    # bound, blue = central, green = upper bound).
    "percentile_p10":    "#C62828",   # alias of capex (red)
    "percentile_p50":    "#1565C0",   # alias of net (blue)
    "percentile_p90":    "#2E7D32",   # alias of revenue (green)
    "perfect_foresight": "#212121",   # alias of net_revenue_line (charcoal)
}


# Per-source-set palette for the rolling-horizon comparison plots.
# Lives next to FINANCIAL_COLORS so the full palette is in one place;
# the keys match the workbook's ``uncertainty_compare_sources`` tokens.
UNCERTAINTY_SOURCE_COLORS: dict[str, str] = {
    "dam":  "#C62828",   # red — DAM noise only
    "pv":   "#EF6C00",   # amber — PV noise only
    "load": "#1565C0",   # blue — load noise only
    "all":  "#2E7D32",   # green — all three combined
}


def assert_unique_financial_colors() -> None:
    """Sanity check: financial colours must be mutually distinguishable.

    A handful of keys are intentionally aliased (e.g. ``load_from_pv`` reuses
    the ``revenue`` green) — those colliding pairs are whitelisted.  Any
    other duplicate hex value is treated as a configuration mistake.
    """
    allowed_aliases: set[frozenset[str]] = {
        frozenset({"revenue", "load_from_pv"}),
        frozenset({"capex", "percentile_p10"}),
        frozenset({"net", "percentile_p50"}),
        frozenset({"revenue", "percentile_p90"}),
        frozenset({"revenue", "load_from_pv", "percentile_p90"}),
        frozenset({"net_revenue_line", "perfect_foresight"}),
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


# ---------------------------------------------------------------------------
# Financial-plot canonical labels, colour bindings, and legend order
# ---------------------------------------------------------------------------
#
# Single source of truth for every label drawn on a financial /
# lifecycle plot.  The pattern mirrors ALL_LABELS / COLORS /
# LEGEND_ORDER above (used by the energy plots) so the two families
# stay symmetric.
#
# Plots emit literal label strings (e.g. ``label="Net cash-flow"``)
# and route the colour through :func:`financial_color` — typos are
# caught at plot time because ``financial_color`` raises for unknown
# labels.  Legend ordering is enforced by
# :func:`apply_financial_legend`, which drops handles whose label is
# not in :data:`FINANCIAL_LEGEND_ORDER`.

FINANCIAL_LABELS: tuple[str, ...] = (
    # Lines / markers
    "Net cash-flow",
    "Net cash-flow (discounted)",
    "Cumulative cash-flow",
    "Cumulative discounted cash-flow",
    "Cumulative NPV",
    "Net revenue",
    "Real-EUR net (deflated)",
    "Simple payback",
    "Discounted payback",
    # Bar / stack components
    "Revenue",
    "OPEX",
    "CAPEX",
    "DEVEX",
    # Revenue-stack subcomponents
    "Load from PV",
    "Load from BESS",
    "Export from PV",
    "Export from BESS",
    "Grid-charging cost",
    "Aggregator fee",
)


# Each canonical label binds to a FINANCIAL_COLORS key.  Multiple
# labels can share a key (e.g. the two cumulative-cashflow series
# both read off ``net`` vs ``discounted`` consistently).
FINANCIAL_LABEL_TO_COLOR_KEY: dict[str, str] = {
    "Net cash-flow":                    "net",
    "Net cash-flow (discounted)":       "net_discounted",
    "Cumulative cash-flow":             "net",
    "Cumulative discounted cash-flow":  "discounted",
    "Cumulative NPV":                   "discounted",
    "Net revenue":                      "net_revenue_line",
    "Real-EUR net (deflated)":          "net_revenue_line",
    "Simple payback":                   "net",
    "Discounted payback":               "discounted",
    "Revenue":                          "revenue",
    "OPEX":                             "opex",
    "CAPEX":                            "capex",
    "DEVEX":                            "devex",
    "Load from PV":                     "load_from_pv",
    "Load from BESS":                   "load_from_bess",
    "Export from PV":                   "export_from_pv",
    "Export from BESS":                 "export_from_bess",
    "Grid-charging cost":               "grid_charge_cost",
    "Aggregator fee":                   "aggregator_fee",
}


# Canonical legend order (left-to-right, top-to-bottom).  Plots
# render only the subset of labels they actually use, but the
# relative order is always this list.
FINANCIAL_LEGEND_ORDER: tuple[str, ...] = (
    # Lines first (headline series)
    "Net cash-flow",
    "Net cash-flow (discounted)",
    "Cumulative cash-flow",
    "Cumulative discounted cash-flow",
    "Cumulative NPV",
    "Net revenue",
    "Real-EUR net (deflated)",
    "Simple payback",
    "Discounted payback",
    # Then bars / stacks, positive flows first
    "Revenue",
    "Load from PV",
    "Load from BESS",
    "Export from PV",
    "Export from BESS",
    # Negative flows last
    "OPEX",
    "DEVEX",
    "CAPEX",
    "Grid-charging cost",
    "Aggregator fee",
)


def financial_color(label: str) -> str:
    """Return the hex colour for a canonical financial label.

    Raises ``ValueError`` if ``label`` is not in
    :data:`FINANCIAL_LABELS` — every label rendered must be
    canonical so that typos surface at plot time rather than
    silently producing an off-palette colour.
    """
    if label not in FINANCIAL_LABEL_TO_COLOR_KEY:
        raise ValueError(
            f"Financial label {label!r} is not canonical. "
            f"Add it to FINANCIAL_LABELS first."
        )
    color_key = FINANCIAL_LABEL_TO_COLOR_KEY[label]
    if color_key not in FINANCIAL_COLORS:
        raise ValueError(
            f"Color key {color_key!r} for label {label!r} is not "
            f"in FINANCIAL_COLORS."
        )
    return FINANCIAL_COLORS[color_key]


def _canonical_match_key(label: str) -> str | None:
    """Return the canonical legend-order key for ``label``, or None.

    Exact matches in :data:`FINANCIAL_LEGEND_ORDER` win first.  As a
    fallback we accept the ``"<canonical>: ..."`` pattern used by
    :func:`pvbess_opt.plotting.financial.plot_payback`, where the
    suffix carries a numeric annotation (e.g. ``"Simple payback: 5.0 yr"``)
    that should not break canonical ordering.
    """
    if label in FINANCIAL_LEGEND_ORDER:
        return label
    if ":" in label:
        prefix = label.split(":", 1)[0].strip()
        if prefix in FINANCIAL_LEGEND_ORDER:
            return prefix
    return None


def apply_financial_legend(ax, *, max_rows: int = 2, loc: str = "best") -> None:
    """Reorder the legend on ``ax`` to match :data:`FINANCIAL_LEGEND_ORDER`.

    Handles whose label is not in the canonical order (and does not
    map to a canonical key via :func:`_canonical_match_key`) are
    appended at the end and a warning is logged — same defensive
    behaviour as the energy plots' equivalent helper.
    """
    handles, labels = ax.get_legend_handles_labels()
    if not labels:
        return
    # Map canonical key -> list of (handle, label) — list because
    # multiple handles can share a canonical key (e.g. the LCOE/LCOS
    # benchmark bands when both rows share one ax).
    by_key: dict[str, list[tuple]] = {}
    extras: list[tuple] = []
    for h, lab in zip(handles, labels, strict=False):
        key = _canonical_match_key(lab)
        if key is None:
            extras.append((h, lab))
        else:
            by_key.setdefault(key, []).append((h, lab))

    ordered_handles: list = []
    ordered_labels: list = []
    for target in FINANCIAL_LEGEND_ORDER:
        for h, lab in by_key.get(target, ()):
            ordered_handles.append(h)
            ordered_labels.append(lab)
    for h, lab in extras:
        logging.getLogger(__name__).warning(
            "Non-canonical financial legend label rendered: %r. "
            "Add it to FINANCIAL_LABELS / FINANCIAL_LEGEND_ORDER.",
            lab,
        )
        ordered_handles.append(h)
        ordered_labels.append(lab)

    num_entries = len(ordered_labels)
    ncol = max(1, math.ceil(num_entries / max_rows))
    ax.legend(
        ordered_handles, ordered_labels,
        loc=loc, framealpha=0.9, ncol=ncol, fontsize=7,
    )


def assert_financial_label_color_coverage() -> None:
    """Fail at import time if labels and colours drift apart."""
    for label in FINANCIAL_LABELS:
        if label not in FINANCIAL_LABEL_TO_COLOR_KEY:
            raise ValueError(
                f"FINANCIAL_LABELS contains {label!r} but "
                "FINANCIAL_LABEL_TO_COLOR_KEY does not bind it."
            )
        ck = FINANCIAL_LABEL_TO_COLOR_KEY[label]
        if ck not in FINANCIAL_COLORS:
            raise ValueError(
                f"Color key {ck!r} (for label {label!r}) missing "
                "from FINANCIAL_COLORS."
            )
    canonical_set = set(FINANCIAL_LABELS)
    order_set = set(FINANCIAL_LEGEND_ORDER)
    if canonical_set != order_set:
        missing = canonical_set - order_set
        extra = order_set - canonical_set
        raise ValueError(
            f"FINANCIAL_LEGEND_ORDER drift: missing={missing}, extra={extra}"
        )


assert_financial_label_color_coverage()
