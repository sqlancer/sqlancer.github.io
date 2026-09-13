"""Builds a record for every paper, from the best source available for each.

A paper with a supplied PDF is read in full. A paper without one still gets a
record, built from its abstract and the sentences a citation index recorded --
fewer sources, the same shape -- so nothing downstream has to ask whether a
paper is one kind or the other.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from ..impact import config, taxonomy
from ..impact.cache import Caches
from ..impact.collectors import fulltext
from ..impact.http import Fetcher
from . import checks, extract, mentions, store


def _document_from_metadata(paper: dict) -> dict:
    """A stand-in document for a paper whose full text we do not have.

    The abstract and the citation sentences, joined into something the same
    inventory can run over. Offsets and pages are meaningless here and are
    recorded as such rather than invented.
    """
    parts: List[str] = []
    abstract = (paper.get("abstract") or "").strip()
    if abstract:
        parts.append(f"Abstract\n\n{abstract}")
    seen = set()
    for entry in (paper.get("relationships") or {}).values():
        for item in (entry.get("evidence") or []):
            excerpt = (item.get("excerpt") or "").strip()
            if excerpt and excerpt not in seen:
                seen.add(excerpt)
                parts.append(excerpt)
    text = "\n\n".join(parts)
    return {"text": text, "pages": [], "page_count": 0, "sections": [],
            "references": [], "references_start": None, "has_outline": False}


def source_for(paper: dict, fetcher: Fetcher) -> Tuple[dict, str, Optional[str]]:
    """The best document for this paper: ``(document, route, url)``."""
    local = fulltext.local_pdf(paper.get("doi"), paper.get("arxiv_id"),
                               paper.get("s2_paper_id"))
    if local:
        raw = _read(fetcher, local)
        if raw:
            document = extract.assemble(raw)
            if document.get("text"):
                return document, "supplied_pdf", paper.get("url")

    for url in fulltext.usenix_pdf_urls(paper):
        raw = _read(fetcher, url)
        if raw:
            document = extract.assemble(raw)
            if document.get("text"):
                return document, "usenix", url

    if paper.get("arxiv_id"):
        url = f"https://arxiv.org/pdf/{paper['arxiv_id']}"
        raw = _read(fetcher, url)
        if raw:
            document = extract.assemble(raw)
            if document.get("text"):
                return document, "arxiv", url

    return _document_from_metadata(paper), "metadata", paper.get("url")


def _read(fetcher: Fetcher, url: str) -> Optional[bytes]:
    try:
        if url.startswith("file://"):
            import pathlib
            from urllib.parse import urlparse
            from urllib.request import url2pathname

            return pathlib.Path(url2pathname(urlparse(url).path)).read_bytes()
        raw, _ = fetcher.fetch_binary(url, max_age_days=365)
        return raw if raw and raw.lstrip().startswith(b"%PDF") else None
    except Exception:
        return None


def artifact_for(paper: dict) -> Optional[dict]:
    """What the pipeline already learned from this paper's repository."""
    artifacts = paper.get("artifacts") or []
    if not artifacts:
        return None
    first = artifacts[0]
    return {
        "url": first.get("url"),
        "markers": first.get("sqlancer_markers") or [],
        "evidence": first.get("marker_evidence") or [],
    }


def run(*, only: Optional[List[str]] = None, limit: Optional[int] = None,
        with_fulltext_only: bool = False) -> Dict[str, int]:
    papers = [p for p in config.load_json(config.DATA_FILES["papers"])["papers"]
              if not p.get("is_sqlancer_publication")]
    if only:
        wanted = set(only)
        papers = [p for p in papers if p["id"] in wanted]

    caches = Caches()
    fetcher = Fetcher(caches.artifacts, min_interval=0.5, max_retries=2,
                      timeout=45.0)
    tax = taxonomy.load()

    counts = {"written": 0, "unchanged": 0, "fulltext": 0, "metadata_only": 0,
              "mentions": 0}
    done = 0
    for paper in papers:
        document, route, url = source_for(paper, fetcher)
        if with_fulltext_only and route == "metadata":
            continue
        inventory = mentions.build(document, tax)
        findings = checks.run(inventory["mentions"])
        existing = store.load(paper["id"]) or {}
        record = store.build(paper, document, inventory, findings, route=route,
                             source_url=url, artifact=artifact_for(paper),
                             analysis=existing.get("analysis"))
        if store.save(record):
            counts["written"] += 1
        else:
            counts["unchanged"] += 1
        counts["fulltext" if route != "metadata" else "metadata_only"] += 1
        counts["mentions"] += len(inventory["mentions"])
        done += 1
        if limit and done >= limit:
            break
    return counts
