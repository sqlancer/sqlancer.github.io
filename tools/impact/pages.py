"""Generates one page per database system under ``/impact/dbms/``.

The impact page shows a bar per system and a row per project. Both raise the
same question -- *which* bugs, and what is the evidence that this project uses
SQLancer -- and neither has room to answer it. A page per system does, and it
gives the bar and the project name somewhere to link to.

The generated files are deliberately thin: front matter and one include. All
rendering lives in ``_includes/impact/dbms-detail.html`` and all numbers come
from ``stats.json``, so a change to how a system is presented is one edit rather
than sixty, and the generated files stay reviewable in a diff.
"""

from __future__ import annotations

import pathlib
from typing import Dict, List, Optional

from . import config

PAGE_DIR = config.REPO_ROOT / "_pages" / "impact" / "dbms"
PERMALINK_BASE = "/impact/dbms/"

YEAR_PAGE_DIR = config.REPO_ROOT / "_pages" / "impact" / "bugs"
YEAR_PERMALINK_BASE = "/impact/bugs/"

TALK_PAGE_DIR = config.REPO_ROOT / "_pages" / "impact" / "talks"
TALK_PERMALINK_BASE = "/impact/talks/"

YEAR_TEMPLATE = """---
permalink: {permalink}
title: "Bugs reported in {year}"
excerpt: "{excerpt}"
bug_year: {year}
author_profile: false
sitemap: true
---

{{% include impact/year-detail.html year=page.bug_year %}}
"""

TALK_TEMPLATE = """---
permalink: {permalink}
title: "{title}"
excerpt: "{excerpt}"
talk_id: "{talk_id}"
author_profile: false
sitemap: true
---

{{% include impact/talk-detail.html id=page.talk_id %}}
"""

TEMPLATE = """---
permalink: {permalink}
title: "{title}"
excerpt: "{excerpt}"
dbms: {dbms_id}
author_profile: false
sitemap: true
---

{{% include impact/dbms-detail.html id=page.dbms %}}
"""


def _escape(text: str) -> str:
    """Quote a value for a double-quoted YAML scalar."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def wanted(stats: dict) -> List[dict]:
    """Systems that get a page: anything with a bug, adoption, or support.

    A system with nothing recorded against it would be an empty page, and an
    empty page is worse than no link.
    """
    return [row for row in stats["dbms"]["rows"]
            if row.get("bugs") or row.get("supported")
            or row.get("adoption_relationships")
            or row.get("planned_adoption")]


def excerpt_for(row: dict) -> str:
    """One sentence saying what this system's page holds."""
    parts = []
    if row.get("bugs"):
        parts.append(f"{row['bugs']} bugs SQLancer found in "
                     f"{row['name']}, each with its evidence")
    if row.get("adoption_relationships"):
        parts.append("how the project uses SQLancer")
    elif row.get("planned_adoption"):
        parts.append("the project's proposal to adopt SQLancer")
    if not parts:
        parts.append(f"what SQLancer records about {row['name']}")
    # Not ``capitalize``: it lowercases everything after the first letter,
    # which turns DuckDB into duckdb.
    sentence = "; ".join(parts)
    return sentence[:1].upper() + sentence[1:] + "."


def render(row: dict) -> str:
    return TEMPLATE.format(
        permalink=f"{PERMALINK_BASE}{row['id']}/",
        title=_escape(f"{row['name']} and SQLancer"),
        excerpt=_escape(excerpt_for(row)),
        dbms_id=row["id"])


def write_all(stats: Optional[dict] = None,
              directory: Optional[pathlib.Path] = None) -> Dict[str, str]:
    """Write a page per system; returns what happened to each, by id.

    Pages for systems that are no longer in the dataset are removed, so a
    system dropped from the records does not leave a page behind claiming
    numbers nothing produces any more.
    """
    if stats is None:
        stats = config.load_json(config.DATA_FILES["stats"])
    directory = directory or PAGE_DIR
    directory.mkdir(parents=True, exist_ok=True)

    outcome: Dict[str, str] = {}
    keep = set()
    for row in wanted(stats):
        path = directory / f"{row['id']}.html"
        keep.add(path.name)
        markup = render(row)
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        if existing == markup:
            outcome[row["id"]] = "unchanged"
            continue
        path.write_text(markup, encoding="utf-8")
        outcome[row["id"]] = "written" if existing is None else "updated"
    for path in directory.glob("*.html"):
        if path.name not in keep:
            path.unlink()
            outcome[path.stem] = "removed"
    return outcome


def year_excerpt(row: dict) -> str:
    """One sentence saying what a year's page holds."""
    systems = row.get("systems") or 0
    return (f"{row['bugs']} bugs attributed to SQLancer and reported in "
            f"{row['year']}, across {systems} database "
            f"system{'' if systems == 1 else 's'}, each with its evidence.")


def render_year(row: dict) -> str:
    return YEAR_TEMPLATE.format(
        permalink=f"{YEAR_PERMALINK_BASE}{row['year']}/",
        year=row["year"],
        excerpt=_escape(year_excerpt(row)))


def write_years(stats: Optional[dict] = None,
                directory: Optional[pathlib.Path] = None) -> Dict[str, str]:
    """Write a page per year that has bugs; returns what happened to each."""
    if stats is None:
        stats = config.load_json(config.DATA_FILES["stats"])
    directory = directory or YEAR_PAGE_DIR
    directory.mkdir(parents=True, exist_ok=True)

    outcome: Dict[str, str] = {}
    keep = set()
    for row in stats["bugs"].get("years") or []:
        if not row.get("bugs"):
            continue
        path = directory / f"{row['year']}.html"
        keep.add(path.name)
        markup = render_year(row)
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        if existing == markup:
            outcome[str(row["year"])] = "unchanged"
            continue
        path.write_text(markup, encoding="utf-8")
        outcome[str(row["year"])] = "written" if existing is None else "updated"
    for path in directory.glob("*.html"):
        if path.name not in keep:
            path.unlink()
            outcome[path.stem] = "removed"
    return outcome


def talk_slug(talk: dict) -> str:
    """The page's name: the video id where there is one, else the record's."""
    return talk.get("video_id") or talk["id"].split(":")[-1]


def talk_excerpt(talk: dict) -> str:
    """One sentence saying what this talk's page holds."""
    count = len(talk.get("mentions") or [])
    speakers = ", ".join(talk.get("speakers") or []) or "a speaker"
    where = talk.get("event") or talk.get("publisher") or "a talk"
    return (f"{speakers} at {where}: the {count} "
            f"point{'' if count == 1 else 's'} where SQLancer comes up, each "
            "linked to the moment it was said.")


def render_talk(talk: dict) -> str:
    return TALK_TEMPLATE.format(
        permalink=f"{TALK_PERMALINK_BASE}{talk_slug(talk)}/",
        title=_escape(talk["title"]),
        excerpt=_escape(talk_excerpt(talk)),
        talk_id=_escape(talk["id"]))


def write_talks(talks: Optional[dict] = None,
                directory: Optional[pathlib.Path] = None) -> Dict[str, str]:
    """Write a page per talk; returns what happened to each, by slug."""
    if talks is None:
        from . import talks as talks_store
        talks = talks_store.load()
    directory = directory or TALK_PAGE_DIR
    directory.mkdir(parents=True, exist_ok=True)

    outcome: Dict[str, str] = {}
    keep = set()
    for talk in talks.get("talks") or []:
        slug = talk_slug(talk)
        path = directory / f"{slug}.html"
        keep.add(path.name)
        markup = render_talk(talk)
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        if existing == markup:
            outcome[slug] = "unchanged"
            continue
        path.write_text(markup, encoding="utf-8")
        outcome[slug] = "written" if existing is None else "updated"
    for path in directory.glob("*.html"):
        if path.name not in keep:
            path.unlink()
            outcome[path.stem] = "removed"
    return outcome


def _summarise(outcome: Dict[str, str]) -> str:
    counts: Dict[str, int] = {}
    for state in outcome.values():
        counts[state] = counts.get(state, 0) + 1
    return ", ".join(f"{count} {state}" for state, count in sorted(counts.items()))


def main() -> int:
    outcome = write_all()
    print(f"pages: {len(outcome)} database systems ({_summarise(outcome)})")
    years = write_years()
    print(f"pages: {len(years)} years ({_summarise(years)})")
    talks = write_talks()
    print(f"pages: {len(talks)} talks ({_summarise(talks)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
