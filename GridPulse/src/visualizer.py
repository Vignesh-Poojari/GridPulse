"""
visualizer.py  --  Member 3 : Matplotlib figure builders

All ``plot_*`` functions take a matplotlib ``Figure`` (NOT pyplot), clear nothing
themselves except what they draw, and return the figure.  That makes them usable
both for the live canvas embedded in Tk and for headless PNG export.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from matplotlib.figure import Figure

from .analytics_engine import fmt_hour

PEAK_COLOR = "#e4572e"
OFFPEAK_COLOR = "#2e9e6b"
PRIMARY = "#2563eb"
GREY = "#9ca3af"
HOURS_X = np.arange(24)
PALETTE = ["#2563eb", "#e4572e", "#2e9e6b", "#f59e0b", "#8b5cf6",
           "#0ea5e9", "#ec4899", "#14b8a6", "#64748b", "#a16207"]


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def new_figure(figsize=(9.0, 5.2)) -> Figure:
    return Figure(figsize=figsize, dpi=100, facecolor="white", layout="constrained")


def _style(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)


def _hour_axis(ax):
    ax.set_xlim(-0.5, 23.5)
    ax.set_xticks(range(0, 24, 2))
    ax.set_xticklabels([f"{h:02d}" for h in range(0, 24, 2)])
    ax.set_xlabel("Hour of day")


def _spans(start: int, end: int):
    start, end = int(start), int(end)
    if start < end:
        return [(start - 0.5, end - 0.5)]
    return [(start - 0.5, 23.5), (-0.5, end - 0.5)]


def _shade(ax, tariff):
    for start, end, color, label in (
            (tariff.peak_start, tariff.peak_end, PEAK_COLOR, f"Peak window ({tariff.peak_label})"),
            (tariff.offpeak_start, tariff.offpeak_end, OFFPEAK_COLOR, f"Off-peak ({tariff.offpeak_label})")):
        if start == end:
            continue
        for i, (a, b) in enumerate(_spans(start, end)):
            ax.axvspan(a, b, color=color, alpha=0.12, lw=0, label=label if i == 0 else None)


def _cat_colors(categories):
    unique = sorted(set(categories))
    return {c: PALETTE[i % len(PALETTE)] for i, c in enumerate(unique)}


def plot_empty(fig: Figure, message: str = "Load data to see charts") -> Figure:
    ax = fig.add_subplot(111)
    ax.axis("off")
    ax.text(0.5, 0.5, message, ha="center", va="center", fontsize=14, color=GREY)
    return fig


# --------------------------------------------------------------------------- #
#  Chart 1 – 24-hour load curve
# --------------------------------------------------------------------------- #
def plot_load_curve(fig: Figure, a: dict) -> Figure:
    ax = fig.add_subplot(111)
    prof, t = a["profile"], a["tariff"]
    _shade(ax, t)
    ax.fill_between(HOURS_X, prof, color=PRIMARY, alpha=0.15)
    ax.plot(HOURS_X, prof, color=PRIMARY, lw=2.4, marker="o", ms=4, label="Average load (all days)")
    ax.axhline(a["avg_load"], color="#374151", ls="--", lw=1, label=f"Mean load {a['avg_load']:.2f} kW")
    ph = a["peak_hour"]
    ax.annotate(f"Peak {fmt_hour(ph)}\n{prof[ph]:.2f} kW", (ph, prof[ph]), xytext=(0, 18),
                textcoords="offset points", ha="center", fontsize=9, fontweight="bold",
                arrowprops=dict(arrowstyle="-", color="#374151"))
    ax.set_ylim(0, max(prof.max() * 1.3, 0.1))
    ax.set_ylabel("Load (kW)")
    ax.set_title("24-Hour Load Curve", fontweight="bold", loc="left")
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    _hour_axis(ax)
    _style(ax)
    return fig



# --------------------------------------------------------------------------- #
#  Chart 2 – appliance breakdown (bar + donut)
# --------------------------------------------------------------------------- #
def plot_appliance_breakdown(fig: Figure, a: dict, top_n: int = 12) -> Figure:
    names, cats, kwh = a["names"], a["categories"], np.asarray(a["appliance_kwh"], float)
    colors = _cat_colors(cats)
    gs = fig.add_gridspec(1, 2, width_ratios=[1.45, 1])
    ax = fig.add_subplot(gs[0, 0])

    order = np.argsort(kwh)[::-1]
    shown = order[:top_n]
    labels = [names[i] for i in shown]
    values = kwh[shown].tolist()
    bar_colors = [colors[cats[i]] for i in shown]
    if len(order) > top_n:
        labels.append("Others")
        values.append(float(kwh[order[top_n:]].sum()))
        bar_colors.append(GREY)
    total = kwh.sum() or 1.0
    y = np.arange(len(labels))[::-1]
    ax.barh(y, values, color=bar_colors, height=0.68)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9)
    for yi, v in zip(y, values):
        ax.text(v, yi, f"  {v:,.1f} kWh ({v / total * 100:.0f}%)", va="center", fontsize=8)
    ax.set_xlim(0, max(values) * 1.38 if values else 1)
    ax.set_xlabel("Energy in period (kWh)")
    ax.set_title("Appliance-wise consumption", fontweight="bold", loc="left")
    _style(ax)
    ax.grid(axis="y", alpha=0)
    ax.grid(axis="x", alpha=0.3)

    ax2 = fig.add_subplot(gs[0, 1])
    by_cat: dict[str, float] = {}
    for c, v in zip(cats, kwh):
        by_cat[c] = by_cat.get(c, 0.0) + float(v)
    items = sorted(by_cat.items(), key=lambda kv: kv[1], reverse=True)
    wedges, _t, autotexts = ax2.pie(
        [v for _, v in items], colors=[colors[c] for c, _ in items], startangle=90, counterclock=False,
        wedgeprops=dict(width=0.42, edgecolor="white"),
        autopct=lambda p: f"{p:.0f}%" if p >= 6 else "", pctdistance=0.79, textprops=dict(fontsize=8, color="white"))
    for t in autotexts:
        t.set_fontweight("bold")
    ax2.text(0, 0, f"{total:,.0f}\nkWh", ha="center", va="center", fontsize=12, fontweight="bold")
    ax2.legend(wedges, [f"{c}" for c, _ in items], loc="upper center", bbox_to_anchor=(0.5, 0.02),
               ncol=2, fontsize=8, frameon=False)
    ax2.set_title("By category", fontweight="bold")
    return fig


# --------------------------------------------------------------------------- #
#  Chart 3 – weekday vs weekend
# --------------------------------------------------------------------------- #
def plot_weekday_weekend(fig: Figure, a: dict) -> Figure:
    ax = fig.add_subplot(111)
    wd, we, t = a["weekday_profile"], a["weekend_profile"], a["tariff"]
    _shade(ax, t)
    w = 0.4
    has_wd, has_we = not np.isnan(wd).all(), not np.isnan(we).all()
    if has_wd:
        ax.bar(HOURS_X - w / 2, wd, w, color=PRIMARY, label=f"Weekday ({wd.sum():.1f} kWh/day, {a['n_weekday']} days)")
    if has_we:
        ax.bar(HOURS_X + w / 2, we, w, color="#f59e0b", label=f"Weekend ({we.sum():.1f} kWh/day, {a['n_weekend']} days)")
    if not has_we:
        ax.text(0.99, 0.95, "No weekend days in this dataset", transform=ax.transAxes,
                ha="right", va="top", fontsize=9, color=GREY)
    top = np.nanmax(np.concatenate([wd, we])) if (has_wd or has_we) else 1
    ax.set_ylim(0, max(top * 1.3, 0.1))
    ax.set_ylabel("Average load (kW)")
    ax.set_title("Weekday vs Weekend Usage", fontweight="bold", loc="left")
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    _hour_axis(ax)
    _style(ax)
    return fig


# --------------------------------------------------------------------------- #
#  Chart 4 – before vs after load-shift simulation
# --------------------------------------------------------------------------- #
def plot_shift_comparison(fig: Figure, before: dict, after: dict, savings: dict) -> Figure:
    ax = fig.add_subplot(111)
    t = before["tariff"]
    _shade(ax, t)
    ax.bar(HOURS_X, before["profile"], 0.75, color=GREY, alpha=0.7, label="Before (current habits)")
    ax.plot(HOURS_X, after["profile"], color=OFFPEAK_COLOR, lw=2.6, marker="o", ms=4, label="After (simulated)")
    ax.plot(HOURS_X, before["profile"], color="#4b5563", lw=1, ls=":")
    top = max(before["profile"].max(), after["profile"].max())
    ax.set_ylim(0, max(top * 1.45, 0.1))
    ax.set_ylabel("Average load (kW)")
    ax.set_title("Before vs After Load-Shift Simulation", fontweight="bold", loc="left")
    box = (f"PAR  {savings['par_before']:.2f} → {savings['par_after']:.2f}\n"
           f"Load factor  {savings['lf_before']:.2f} → {savings['lf_after']:.2f}\n"
           f"Peak-window share  {savings['peak_share_before'] * 100:.0f}% → {savings['peak_share_after'] * 100:.0f}%\n"
           f"Saving  ₹{savings['bill_month']:,.0f}/month  (₹{savings['bill_year']:,.0f}/year)")
    ax.text(0.99, 0.97, box, transform=ax.transAxes, ha="right", va="top", fontsize=9,
            bbox=dict(boxstyle="round,pad=0.5", fc="white", ec="#d1d5db"))
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    _hour_axis(ax)
    _style(ax)
    return fig


# --------------------------------------------------------------------------- #
#  Export
# --------------------------------------------------------------------------- #
CHART_TITLES = ("24-Hour Load Curve", "Appliance Breakdown", "Weekday vs Weekend", "Before vs After Shift")


def build_chart(fig: Figure, index: int, analysis: dict, sim: dict | None = None) -> Figure:
    """Dispatch to one of the four charts (index 0-3)."""
    if index == 0:
        return plot_load_curve(fig, analysis)
    if index == 1:
        return plot_appliance_breakdown(fig, analysis)
    if index == 2:
        return plot_weekday_weekend(fig, analysis)
    if sim is None:
        return plot_empty(fig, "Open the Peak-Shift Simulator tab and move a slider\nto create a before/after comparison")
    return plot_shift_comparison(fig, sim["before"], sim["after"], sim["savings"])


def export_all_charts(folder: str | Path, analysis: dict, sim: dict | None = None,
                      dpi: int = 200) -> list[Path]:
    """Save every chart as a high-resolution PNG; the simulation chart only if one exists."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stems = ("01_load_curve", "02_appliance_breakdown", "03_weekday_vs_weekend", "04_before_after_shift")
    written = []
    for i, stem in enumerate(stems):
        if i == 3 and sim is None:
            continue
        fig = new_figure((10, 5.8))
        build_chart(fig, i, analysis, sim)
        path = folder / f"{stem}.png"
        fig.savefig(path, dpi=dpi, facecolor="white")
        written.append(path)
    return written
