"""Generates the inline SVG charts shown on the impact page.

Written by hand rather than through a plotting library for three reasons: the
output is a small, readable, diffable SVG that a reviewer can check against the
records; it needs no build-time dependency on GitHub Pages; and being inline
rather than an ``<img>`` it inherits the page's CSS, so the charts follow the
site's light and dark palettes instead of baking one in.

Colours come from the validated palette in the impact stylesheet and are
referenced as CSS custom properties. Every chart also has a table beside it on
the page, which is the required relief for the palette's two light-surface
colours that sit below 3:1 contrast.
"""

from __future__ import annotations

import html
from typing import Dict, List, Optional, Sequence

from . import config, pages
from .util import truncate

# Chart geometry. One place, so every chart on the page shares proportions.
BAR_HEIGHT = 22
BAR_GAP = 8
LABEL_WIDTH = 150
VALUE_WIDTH = 52
CHART_PAD = 12
COLUMN_MIN_WIDTH = 26
COLUMN_GAP = 10
GROUP_GAP = 16
PLOT_HEIGHT = 210

# Categorical slots, matching the CSS custom properties in impact.css.
SERIES_VARS = [f"--impact-series-{index}" for index in range(1, 9)]


def _escape(text: str) -> str:
    return html.escape(str(text), quote=True)


def _nice_max(value: int) -> int:
    """Round an axis maximum up to a readable step."""
    if value <= 0:
        return 1
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000):
        if value <= step * 4:
            return -(-value // step) * step
    return -(-value // 5000) * 5000


def _svg_open(width: int, height: int, title: str, description: str) -> List[str]:
    return [
        # max-width pins the chart to its natural size so a chart with few
        # categories is not stretched across the full column.
        f'<svg class="impact-chart" viewBox="0 0 {width} {height}" '
        f'width="100%" height="auto" style="max-width:{width}px" role="img" '
        f'preserveAspectRatio="xMinYMin meet" '
        f'aria-label="{_escape(title)}" xmlns="http://www.w3.org/2000/svg">',
        f"  <title>{_escape(title)}</title>",
        f"  <desc>{_escape(description)}</desc>",
    ]


def horizontal_bars(series: Sequence[dict], *, title: str, value_label: str,
                    max_rows: int = 40, color_var: str = "--impact-sequential"
                    ) -> str:
    """Ranked magnitude chart. One hue, because the job is size, not identity."""
    rows = list(series)[:max_rows]
    if not rows:
        return _empty(title)
    maximum = max(row["count"] for row in rows) or 1
    plot_width = 420
    width = LABEL_WIDTH + plot_width + VALUE_WIDTH + CHART_PAD * 2
    height = CHART_PAD * 2 + len(rows) * (BAR_HEIGHT + BAR_GAP) - BAR_GAP

    parts = _svg_open(width, height, title,
                      f"{value_label} for {len(rows)} categories, "
                      f"largest {maximum}.")
    y = CHART_PAD
    for row in rows:
        bar_width = max(2, round(row["count"] / maximum * plot_width))
        label = truncate(str(row["label"]), 24)
        parts.append(
            f'  <text class="impact-chart__label" x="{LABEL_WIDTH - 8}" '
            f'y="{y + BAR_HEIGHT / 2}" text-anchor="end" '
            f'dominant-baseline="central">{_escape(label)}</text>')
        parts.append(
            f'  <rect class="impact-chart__bar" x="{LABEL_WIDTH}" y="{y}" '
            f'width="{bar_width}" height="{BAR_HEIGHT}" rx="4" '
            f'fill="var({color_var})"><title>'
            f'{_escape(row["label"])}: {row["count"]}</title></rect>')
        parts.append(
            f'  <text class="impact-chart__value" x="{LABEL_WIDTH + bar_width + 8}" '
            f'y="{y + BAR_HEIGHT / 2}" dominant-baseline="central">'
            f'{row["count"]}</text>')
        y += BAR_HEIGHT + BAR_GAP
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def columns(series: Sequence[dict], *, title: str, value_label: str,
            color_var: str = "--impact-sequential",
            link_base: Optional[str] = None) -> str:
    """Single-series column chart for a value over time.

    With ``link_base`` each column links to ``link_base<key>/``. A bar labelled
    2026 invites the question of what is in it, and the page it links to is
    where that is answered.
    """
    rows = list(series)
    if not rows:
        return _empty(title)
    maximum = _nice_max(max(row["count"] for row in rows))
    left = 44
    column_width = max(COLUMN_MIN_WIDTH, min(56, 640 // max(len(rows), 1)))
    width = left + len(rows) * (column_width + COLUMN_GAP) + CHART_PAD
    height = PLOT_HEIGHT + 54

    parts = _svg_open(width, height, title,
                      f"{value_label} across {len(rows)} periods, "
                      f"peak {max(row['count'] for row in rows)}.")
    parts.extend(_y_axis(left, width, maximum))
    x = left + COLUMN_GAP / 2
    for row in rows:
        bar_height = max(2, round(row["count"] / maximum * PLOT_HEIGHT))
        y = CHART_PAD + PLOT_HEIGHT - bar_height
        href = (f"{link_base}{row['key']}/"
                if link_base and row.get("key") else None)
        if href:
            # One link around the column, its value and its label, so the whole
            # column is one target rather than three adjacent ones.
            parts.append(f'  <a href="{_escape(href)}" '
                         f'aria-label="{_escape(row["label"])}: '
                         f'{row["count"]}">')
        parts.append(
            f'  <rect class="impact-chart__bar" x="{x}" y="{y}" '
            f'width="{column_width}" height="{bar_height}" rx="4" '
            f'fill="var({color_var})"><title>'
            f'{_escape(row["label"])}: {row["count"]}</title></rect>')
        parts.append(
            f'  <text class="impact-chart__value" x="{x + column_width / 2}" '
            f'y="{y - 6}" text-anchor="middle">{row["count"]}</text>')
        parts.append(
            f'  <text class="impact-chart__label" x="{x + column_width / 2}" '
            f'y="{CHART_PAD + PLOT_HEIGHT + 18}" text-anchor="middle">'
            f'{_escape(row["label"])}</text>')
        if href:
            parts.append("  </a>")
        x += column_width + COLUMN_GAP
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def grouped_columns(categories: Sequence[str], groups: Sequence[dict], *,
                    title: str, value_label: str) -> str:
    """Grouped columns for a small number of series that are the subject.

    ``groups`` is a list of ``{"label": ..., "values": [...]}``; the value list
    is parallel to ``categories``. Capped at the categorical slots the palette
    validates for adjacent forms.
    """
    if not categories or not groups:
        return _empty(title)
    groups = list(groups)[:4]
    maximum = _nice_max(max((max(g["values"]) if g["values"] else 0)
                            for g in groups))
    left = 44
    bar_width = 14
    group_width = len(groups) * bar_width + (len(groups) - 1) * 2
    step = group_width + GROUP_GAP
    width = left + len(categories) * step + CHART_PAD
    height = PLOT_HEIGHT + 54

    parts = _svg_open(width, height, title,
                      f"{value_label}: {len(groups)} series across "
                      f"{len(categories)} years.")
    parts.extend(_y_axis(left, width, maximum))
    x = left + GROUP_GAP / 2
    for index, category in enumerate(categories):
        bar_x = x
        for slot, group in enumerate(groups):
            value = group["values"][index] if index < len(group["values"]) else 0
            bar_height = max(0, round(value / maximum * PLOT_HEIGHT))
            y = CHART_PAD + PLOT_HEIGHT - bar_height
            if bar_height > 0:
                parts.append(
                    f'  <rect class="impact-chart__bar" x="{bar_x}" y="{y}" '
                    f'width="{bar_width}" height="{bar_height}" rx="4" '
                    f'fill="var({SERIES_VARS[slot]})"><title>'
                    f'{_escape(group["label"])} {_escape(category)}: {value}'
                    f'</title></rect>')
                parts.append(
                    f'  <text class="impact-chart__value impact-chart__value--tiny" '
                    f'x="{bar_x + bar_width / 2}" y="{y - 4}" '
                    f'text-anchor="middle">{value}</text>')
            # 2px surface gap between adjacent fills.
            bar_x += bar_width + 2
        parts.append(
            f'  <text class="impact-chart__label" x="{x + group_width / 2}" '
            f'y="{CHART_PAD + PLOT_HEIGHT + 18}" text-anchor="middle">'
            f'{_escape(category)}</text>')
        x += step
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def stacked_horizontal_bars(rows: Sequence[dict], *, title: str,
                            value_label: str, max_rows: int = 40,
                            link_base: Optional[str] = None) -> str:
    """Ranked bars split into segments, for magnitude plus composition.

    Used where the total and its make-up matter equally: how many bugs a system
    has, and how many of those someone outside the project found.

    With ``link_base`` each row becomes a link to ``link_base<key>/`` -- the
    obvious thing to do with a bar labelled DuckDB is to click it, and the
    detail page is what it should reach. The href is written root-relative
    rather than through Liquid's ``relative_url``, which the SVG cannot reach;
    that is correct as long as the site has no ``baseurl``, and it has none.
    """
    rows = [row for row in rows if row["count"] > 0][:max_rows]
    if not rows:
        return _empty(title)
    maximum = max(row["count"] for row in rows) or 1
    plot_width = 400
    width = LABEL_WIDTH + plot_width + VALUE_WIDTH + CHART_PAD * 2
    height = CHART_PAD * 2 + len(rows) * (BAR_HEIGHT + BAR_GAP) - BAR_GAP

    parts = _svg_open(width, height, title,
                      f"{value_label} for {len(rows)} categories, "
                      f"largest {maximum}, each split by who found them.")
    y = CHART_PAD
    for row in rows:
        x = float(LABEL_WIDTH)
        label = truncate(str(row["label"]), 24)
        href = (f"{link_base}{row['key']}/"
                if link_base and row.get("key") else None)
        if href:
            # One link around the label and every segment of its bar, so the
            # whole row is one target rather than several adjacent ones.
            parts.append(f'  <a href="{_escape(href)}" '
                         f'aria-label="{_escape(row["label"])}: '
                         f'{row["count"]} bugs">')
        parts.append(
            f'  <text class="impact-chart__label" x="{LABEL_WIDTH - 8}" '
            f'y="{y + BAR_HEIGHT / 2}" text-anchor="end" '
            f'dominant-baseline="central">{_escape(label)}</text>')
        for slot, segment in enumerate(row["segments"]):
            if segment["count"] <= 0:
                continue
            span = segment["count"] / maximum * plot_width
            # 2px surface gap between adjacent fills, per the mark spec.
            drawn = max(2.0, span - 2)
            parts.append(
                f'  <rect class="impact-chart__bar" x="{x:.1f}" y="{y}" '
                f'width="{drawn:.1f}" height="{BAR_HEIGHT}" rx="4" '
                f'fill="var({SERIES_VARS[slot]})"><title>'
                f'{_escape(row["label"])} &#8212; {_escape(segment["label"])}: '
                f'{segment["count"]}</title></rect>')
            x += span
        parts.append(
            f'  <text class="impact-chart__value" x="{x + 8:.1f}" '
            f'y="{y + BAR_HEIGHT / 2}" dominant-baseline="central">'
            f'{row["count"]}</text>')
        if href:
            parts.append("  </a>")
        y += BAR_HEIGHT + BAR_GAP
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def stacked_bar(series: Sequence[dict], *, title: str, value_label: str) -> str:
    """Single horizontal stacked bar for a part-to-whole breakdown."""
    rows = [row for row in series if row["count"] > 0][:4]
    if not rows:
        return _empty(title)
    total = sum(row["count"] for row in rows)
    width = 660
    bar_top = 16
    bar_height = 34
    height = bar_top + bar_height + 26

    parts = _svg_open(width, height, title,
                      f"{value_label}: {total} in {len(rows)} states.")
    x = 0.0
    for slot, row in enumerate(rows):
        segment = row["count"] / total * width
        # 2px surface gap between segments, per the mark spec.
        drawn = max(2.0, segment - 2)
        parts.append(
            f'  <rect class="impact-chart__bar" x="{x:.1f}" y="{bar_top}" '
            f'width="{drawn:.1f}" height="{bar_height}" rx="4" '
            f'fill="var({SERIES_VARS[slot]})"><title>'
            f'{_escape(row["label"])}: {row["count"]}</title></rect>')
        if segment > 90:
            parts.append(
                f'  <text class="impact-chart__value" x="{x + drawn / 2:.1f}" '
                f'y="{bar_top + bar_height + 16}" text-anchor="middle">'
                f'{_escape(row["label"])} {row["count"]}</text>')
        x += segment
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _y_axis(left: int, width: int, maximum: int) -> List[str]:
    """Recessive gridlines plus a baseline, at four evenly spaced ticks."""
    parts = []
    for step in range(5):
        value = round(maximum * step / 4)
        y = CHART_PAD + PLOT_HEIGHT - (PLOT_HEIGHT * step / 4)
        parts.append(
            f'  <line class="impact-chart__grid" x1="{left}" y1="{y:.1f}" '
            f'x2="{width - CHART_PAD}" y2="{y:.1f}" />')
        parts.append(
            f'  <text class="impact-chart__tick" x="{left - 8}" y="{y:.1f}" '
            f'text-anchor="end" dominant-baseline="central">{value}</text>')
    parts.append(
        f'  <line class="impact-chart__axis" x1="{left}" '
        f'y1="{CHART_PAD + PLOT_HEIGHT}" x2="{width - CHART_PAD}" '
        f'y2="{CHART_PAD + PLOT_HEIGHT}" />')
    return parts


def _empty(title: str) -> str:
    return (f'<svg class="impact-chart impact-chart--empty" viewBox="0 0 400 60" '
            f'width="100%" height="60" role="img" '
            f'aria-label="{_escape(title)}: no data yet" '
            f'xmlns="http://www.w3.org/2000/svg">\n'
            f'  <title>{_escape(title)}</title>\n'
            f'  <text class="impact-chart__label" x="200" y="30" '
            f'text-anchor="middle">No records yet.</text>\n'
            f'</svg>\n')


PLOTS = {}


def build_all(stats: dict) -> Dict[str, str]:
    """Render every chart from the derived statistics."""
    from .stats import RELATIONSHIP_LABELS

    charts: Dict[str, str] = {}

    charts["bugs-by-dbms"] = stacked_horizontal_bars(
        stats["bugs"]["by_dbms_and_affiliation"],
        title="Bugs found by SQLancer, by database system and who found them",
        value_label="Bugs per database system",
        link_base=pages.PERMALINK_BASE)

    charts["bugs-by-reporter"] = stacked_bar(
        stats["bugs"]["by_reporter_affiliation"],
        title="Who found the bugs SQLancer is credited with",
        value_label="Bug reports by reporter affiliation")

    charts["bugs-by-year"] = columns(
        stats["bugs"]["by_year"],
        title="Bugs found by SQLancer, by year reported",
        value_label="Bugs reported per year",
        link_base=pages.YEAR_PERMALINK_BASE)

    charts["bugs-by-technique"] = horizontal_bars(
        stats["bugs"]["by_technique"],
        title="Bugs found by SQLancer, by technique",
        value_label="Bugs per technique")

    charts["bugs-by-status"] = stacked_bar(
        stats["bugs"]["by_status"],
        title="Status of the bugs SQLancer found",
        value_label="Bug reports by status")

    charts["papers-by-year"] = columns(
        stats["papers"]["by_year"],
        title="Papers citing SQLancer, by publication year",
        value_label="Papers citing SQLancer per year")

    years = [str(year) for year in stats["papers"]["years"]]
    yearly = stats["papers"]["by_relationship_year"]
    # Three adjacent series, within the palette's validated limit. Reuse and
    # extension share one, because they answer one question and a few papers
    # do both; recognition keeps its own, being the signal that SQLancer is the
    # reference point in its field, which the other two do not capture.
    charts["papers-relationships-by-year"] = grouped_columns(
        years,
        [{"label": RELATIONSHIP_LABELS[key],
          "values": [entry["count"] for entry in yearly.get(key, [])]}
         for key in ("reusing_or_extending", "compares_with",
                     "describes_as_state_of_the_art")],
        title="How papers relate to SQLancer, by publication year",
        value_label="Papers per relationship per year")

    return charts


def write_all(stats: Optional[dict] = None) -> Dict[str, bool]:
    """Write every chart to ``_includes/impact/plots``; returns what changed."""
    if stats is None:
        stats = config.load_json(config.DATA_FILES["stats"])
    charts = build_all(stats)
    config.PLOT_DIR.mkdir(parents=True, exist_ok=True)
    changed: Dict[str, bool] = {}
    for name, markup in charts.items():
        path = config.PLOT_DIR / f"{name}.svg"
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        if existing == markup:
            changed[name] = False
            continue
        path.write_text(markup, encoding="utf-8")
        changed[name] = True
    return changed


def main() -> int:
    changed = write_all()
    updated = [name for name, did in changed.items() if did]
    print(f"plots: {len(changed)} charts, "
          f"{len(updated) or 'no'} updated"
          + (f" ({', '.join(sorted(updated))})" if updated else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
