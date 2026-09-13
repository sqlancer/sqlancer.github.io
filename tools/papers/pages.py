"""Generates one page per paper under ``/impact/papers/``.

The impact page lists a paper with a sentence on what it did with SQLancer, and
that is all it should carry: the lists run to two hundred entries, and a reader
scanning them wants the connection, not the paper. Everything else the record
holds -- the classifications with their reasoning, the sentences each rests on,
the resolved references, the artifact -- has nowhere to go on that page and is
the reason the record exists.

So each paper gets a page. As with the database systems, the generated files
are front matter and one include: the rendering lives in
``_includes/impact/paper-detail.html`` and every figure comes from the record,
so presentation is one edit rather than a hundred and eighty.
"""

from __future__ import annotations

import json
import pathlib
from typing import Dict, List, Optional

from tools.impact import config

PAGE_DIR = config.REPO_ROOT / "_pages" / "impact" / "papers"
RECORD_DIR = config.REPO_ROOT / "_data" / "papers"
PERMALINK_BASE = "/impact/papers/"

TEMPLATE = """---
permalink: {permalink}
title: "{title}"
excerpt: "{excerpt}"
paper_key: {key}
author_profile: false
sitemap: true
---

{{% include impact/paper-detail.html key=page.paper_key %}}
"""


def _escape(text: str) -> str:
    """Quote a value for a double-quoted YAML scalar."""
    return (str(text).replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", " ").strip())


def records() -> List[dict]:
    """Every stored paper record, with the key its page is named for."""
    found = []
    for path in sorted(RECORD_DIR.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        record["_key"] = path.stem
        found.append(record)
    return found


def excerpt_for(record: dict) -> str:
    """One sentence for the page's own metadata.

    The relationship narrative if the paper has been analysed, because that is
    what distinguishes this page from the paper's own abstract; the title
    otherwise.
    """
    analysis = record.get("analysis") or {}
    narrative = (analysis.get("narrative") or "").strip()
    if narrative:
        sentence = narrative.split(". ")[0].rstrip(".")
        return sentence[:280] + "."
    title = (record.get("paper") or {}).get("title") or "A paper citing SQLancer"
    return f"What {title} records about SQLancer."


def render(record: dict) -> str:
    paper = record.get("paper") or {}
    title = paper.get("title") or record["_key"]
    return TEMPLATE.format(
        permalink=f"{PERMALINK_BASE}{record['_key']}/",
        title=_escape(title),
        excerpt=_escape(excerpt_for(record)),
        key=record["_key"])


def write_all(directory: Optional[pathlib.Path] = None,
              found: Optional[List[dict]] = None) -> Dict[str, str]:
    """Write a page per paper record; returns what happened to each."""
    directory = directory or PAGE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    found = records() if found is None else found

    outcome: Dict[str, str] = {}
    keep = set()
    for record in found:
        path = directory / f"{record['_key']}.html"
        keep.add(path.name)
        markup = render(record)
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        if existing == markup:
            outcome[record["_key"]] = "unchanged"
            continue
        path.write_text(markup, encoding="utf-8")
        outcome[record["_key"]] = "written" if existing is None else "updated"
    # A record that leaves the dataset must not leave a page behind claiming
    # figures nothing produces any more.
    for path in directory.glob("*.html"):
        if path.name not in keep:
            path.unlink()
            outcome[path.stem] = "removed"
    return outcome


def main() -> int:
    outcome = write_all()
    counts: Dict[str, int] = {}
    for state in outcome.values():
        counts[state] = counts.get(state, 0) + 1
    summary = ", ".join(f"{count} {state}"
                        for state, count in sorted(counts.items()))
    print(f"paper pages: {len(outcome)} papers ({summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
