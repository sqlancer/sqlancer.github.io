"""Reads and writes the per-paper files in ``_data/papers``.

One file per paper, holding everything learned about it: what was read, the
document's shape, every mention with its context, the artifact, the advisory
checks, and -- once it has been done -- the analysis.

Extraction and analysis carry separate version stamps and are written by
separate steps. Re-running the prompt must never redo the PDF work, and
improving the extractor must never silently discard an analysis: that
separation is the reason the expensive half only happens once.
"""

from __future__ import annotations

import json
import pathlib
from typing import Dict, List, Optional

from ..impact import config
from ..impact.util import content_hash, now, slugify

SCHEMA_VERSION = "1.0.0"
DIRECTORY = config.DATA_DIR.parent / "papers"


def path_for(paper_id: str) -> pathlib.Path:
    """Where a paper's file lives. Named for the id, which outlives the title."""
    return DIRECTORY / f"{slugify(paper_id)}.json"


def load(paper_id: str) -> Optional[dict]:
    path = path_for(paper_id)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def save(record: dict) -> bool:
    """Write a record. Returns whether anything actually changed.

    Timestamps are excluded from the comparison, so a re-run that learns nothing
    new leaves the file -- and the diff a reviewer reads -- untouched.
    """
    path = path_for(record["paper"]["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(record, indent=2, ensure_ascii=False, sort_keys=False)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            existing = None
        if existing is not None and _comparable(existing) == _comparable(record):
            return False
    path.write_text(body + "\n", encoding="utf-8")
    return True


def _comparable(record: dict) -> str:
    copy = json.loads(json.dumps(record))
    copy.pop("generated_at", None)
    for source in copy.get("sources") or []:
        source.pop("retrieved_at", None)
    if copy.get("analysis"):
        copy["analysis"].pop("generated_at", None)
    return json.dumps(copy, sort_keys=True)


def build(paper: dict, document: dict, inventory: dict, findings: dict, *,
          route: str, source_url: Optional[str] = None,
          artifact: Optional[dict] = None,
          analysis: Optional[dict] = None) -> dict:
    """Assemble the record. Everything learned, in one self-describing file."""
    from . import extract, mentions as mentions_module

    text = document.get("text") or ""
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now(),
        "paper": {
            "id": paper["id"],
            "title": paper.get("title"),
            "authors": paper.get("authors") or [],
            "year": paper.get("year"),
            "venue": paper.get("venue"),
            "doi": paper.get("doi"),
            "arxiv_id": paper.get("arxiv_id"),
            "s2_paper_id": paper.get("s2_paper_id"),
            "url": paper.get("url"),
            "also_indexed_as": paper.get("also_indexed_as") or [],
        },
        "sources": [{
            "kind": "fulltext" if route != "metadata" else "metadata",
            "route": route,
            "url": source_url,
            "retrieved_at": now(),
            "chars": len(text),
            "content_sha256": content_hash(text) if text else None,
        }],
        "document": {
            "has_fulltext": route != "metadata",
            "page_count": document.get("page_count", 0),
            "has_outline": bool(document.get("has_outline")),
            "sections": [
                {"number": s.get("number"), "title": s.get("title"),
                 "start": s.get("start")}
                for s in (document.get("sections") or [])
            ],
        },
        "references": [
            {"number": r["number"], "text": r["text"][:400],
             "is_sqlancer_publication": r["number"] in {
                 s["number"] for s in inventory.get("sqlancer_references") or []},
             }
            for r in (document.get("references") or [])
        ],
        "sqlancer_references": inventory.get("sqlancer_references") or [],
        "mentions": inventory.get("mentions") or [],
        "artifact": artifact,
        "checks": findings,
        "analysis": analysis,
        "provenance": {
            "extractor_version": extract.EXTRACTOR_VERSION,
            "inventory_version": mentions_module.INVENTORY_VERSION,
            "checks_version": findings.get("checks_version"),
            "policy_version": config.policy_version(),
        },
    }


def all_records() -> List[dict]:
    if not DIRECTORY.is_dir():
        return []
    out = []
    for path in sorted(DIRECTORY.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                out.append(json.load(handle))
        except (OSError, ValueError):
            continue
    return out
