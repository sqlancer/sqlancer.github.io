"""Imports SQLancer bug reports from Gitee.

Several database projects take their bug reports on gitee.com rather than on
GitHub, so the GitHub search that reaches every other tracker cannot see them
at all. openGauss is the case that showed why this matters: its tracker holds
a report titled "TLP 等价验证出现结果内容不一致" over a schema beginning
``CREATE UNLOGGED TABLE t0(c0 boolean PRIMARY KEY UNIQUE)``, which is a
SQLancer oracle finding a bug and was invisible to every route the pipeline
had.

Attribution is the shared rule, unchanged. What the reports are written in is
not: the sentence patterns are English, so a Chinese report clears the rules
only through the two signals that are language-independent -- the tool's own
name, and SQLancer's generated-schema fingerprint in the reproducer.

Searching is by tool and oracle name only. The Chinese phrases for the
symptoms these bugs produce -- 等价验证, 结果不一致, 模糊测试 -- return
several hundred issues each from MindSpore, OpenHarmony and the kernel, and
not one of them earns an attribution.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Dict, List, Optional, Sequence, Tuple

from .. import config, taxonomy
from ..http import Fetcher
from ..util import (content_hash, normalize_url, now, parse_date, stable_digest,
                    truncate, verbatim_excerpt, year_of)
from .github_bugs import deterministic_attribution

COLLECTOR = "gitee_bugs"
COLLECTOR_VERSION = "1.0.0"

API = "https://gitee.com/api/v5"
SEARCH = f"{API}/search/issues"
PAGE_SIZE = 100
MAX_PAGES = 3

SINGLE_TOKEN = re.compile(r"^[A-Za-z][\w+.-]*$")

# Searches that came back non-200 or raised, so an empty result set can be
# told apart from a throttled one.
FAILED_SEARCHES: List[Tuple[str, Optional[str], object]] = []

# Gitee blocks by address, not by endpoint: a burst of requests took the whole
# API to 403 for hours, individual issue fetches included. Once several
# searches in a row have failed there is nothing to be gained by continuing,
# and something to lose.
MAX_CONSECUTIVE_FAILURES = 3

# Gitee's workflow states. "closed" means the work was completed; "rejected"
# is the project saying this was not a defect.
STATE_TO_STATUS = {
    "closed": ("fixed", True),
    "open": ("open", True),
    "progressing": ("open", True),
    "rejected": ("closed_not_a_bug", False),
}


def repository_index() -> Dict[str, str]:
    """Map each registered project's Gitee repositories to its registry id."""
    index: Dict[str, str] = {}
    for entry in config.load_json(config.DATA_FILES["dbms"])["dbms"]:
        for name in entry.get("gitee_repositories") or []:
            index[name.strip().lower()] = entry["id"]
    return index


def search_terms(tax: taxonomy.Taxonomy) -> List[str]:
    """Single-token tool and oracle names worth searching for."""
    seen, ordered = set(), []
    for name in tax.search_terms():
        if SINGLE_TOKEN.match(name or "") and name.lower() not in seen:
            seen.add(name.lower())
            ordered.append(name)
    return ordered


def search(fetcher: Fetcher, term: str, *, repository: Optional[str] = None,
           max_pages: int = MAX_PAGES) -> List[dict]:
    """Every issue matching ``term``, in one repository or across Gitee.

    Scoped to a repository by default, for the same reason the GitHub search
    is: an unscoped query returns a page at a time out of far more matches
    than it will page through, so a real report sits behind hundreds of
    unrelated ones and is never reached. Searching "TLP" across Gitee returned
    three hundred issues from throwaway repositories and not the openGauss one
    this collector exists for.
    """
    issues: List[dict] = []
    scope = ""
    if repository and "/" in repository:
        owner, _ = repository.split("/", 1)
        # Gitee wants the full owner/name in ``repo``, not the bare name.
        scope = (f"&owner={urllib.parse.quote(owner)}"
                 f"&repo={urllib.parse.quote(repository)}")
    for page in range(1, max_pages + 1):
        url = (f"{SEARCH}?q={urllib.parse.quote(term)}"
               f"&page={page}&per_page={PAGE_SIZE}{scope}")
        try:
            entry, _ = fetcher.fetch(url, max_age_days=7)
        except Exception as error:
            FAILED_SEARCHES.append((term, repository, str(error)))
            return issues
        if entry.get("status") != 200:
            # Gitee throttles hard, and a throttled run returning nothing is
            # indistinguishable from a tracker with nothing in it. Recorded so
            # the caller can tell the two apart.
            FAILED_SEARCHES.append((term, repository, entry.get("status")))
            return issues
        try:
            batch = json.loads(entry["body"])
        except ValueError:
            return issues
        if not isinstance(batch, list) or not batch:
            return issues
        issues.extend(batch)
        if len(batch) < PAGE_SIZE:
            return issues
    return issues


def _blocked() -> bool:
    """Whether the last few searches all failed, meaning we are blocked."""
    recent = FAILED_SEARCHES[-MAX_CONSECUTIVE_FAILURES:]
    return (len(recent) == MAX_CONSECUTIVE_FAILURES
            and all(status == 403 for _, _, status in recent))


def as_issue(item: dict) -> dict:
    """The shape ``deterministic_attribution`` reads, from a Gitee issue."""
    return {
        "title": item.get("title") or "",
        "body": item.get("body") or "",
        "labels": [{"name": (label or {}).get("name", "")}
                   for label in (item.get("labels") or [])],
        "html_url": item.get("html_url") or "",
    }


def source_text(item: dict) -> str:
    return "\n".join(part for part in (item.get("title"), item.get("body"))
                     if part)


def status_of(item: dict) -> Tuple[str, bool]:
    state = (item.get("state") or "").strip().lower()
    return STATE_TO_STATUS.get(state, ("unknown", False))


def build_record(item: dict, dbms_id: str, *, rule: str,
                 technique: Optional[str], finder: str, excerpt: str,
                 text: str, timestamp: str, confidence: str = "high") -> dict:
    """Assemble a bug record from one Gitee issue."""
    url = item.get("html_url") or ""
    status, is_true_positive = status_of(item)
    created = (item.get("created_at") or "")[:10] or None
    repository = (item.get("repository") or {}).get("full_name") or "Gitee"

    return {
        "id": f"bug:{dbms_id}:{stable_digest(normalize_url(url))}",
        "dbms": dbms_id,
        "title": truncate(item.get("title") or "(untitled report)", 500),
        "reported_date": parse_date(created),
        "reported_year": year_of(parse_date(created)),
        "status": status,
        "status_is_true_positive": is_true_positive,
        "finder": finder,
        "technique": technique,
        "symptom": "unknown",
        "reporter": (item.get("user") or {}).get("login") or None,
        "links": {"report": url},
        "primary_url": normalize_url(url),
        "attribution": {
            "rule": rule,
            "confidence": confidence,
            "evidence": [{
                "source_url": url,
                "source_type": "issue_tracker",
                "excerpt": excerpt,
                "excerpt_is_verbatim": True,
                "note": (f"Issue {item.get('number')} in {repository} on "
                         f"Gitee, the project's own tracker."),
                "content_sha256": content_hash(text),
                "retrieved_at": timestamp,
                "first_seen": timestamp,
                "last_verified": timestamp,
            }],
        },
        "provenance": {
            "collector": COLLECTOR,
            "collector_version": COLLECTOR_VERSION,
            "policy_version": config.policy_version(),
            "first_seen": timestamp,
            "last_verified": timestamp,
            "source_url": url,
            "source_type": "issue_tracker",
            "content_sha256": content_hash(text),
        },
    }


def collect(fetcher: Optional[Fetcher] = None,
            tax: Optional[taxonomy.Taxonomy] = None, *,
            timestamp: Optional[str] = None,
            terms: Optional[Sequence[str]] = None
            ) -> Tuple[List[dict], List[dict], List[dict]]:
    """Return ``(records, rejected, needs_review)`` from Gitee."""
    timestamp = timestamp or now()
    tax = tax or taxonomy.load()
    if fetcher is None:
        from ..cache import Caches
        # Deliberately slow. Gitee's limit is not documented and it blocks by
        # address when crossed.
        fetcher = Fetcher(Caches().http, min_interval=3.0, max_retries=1,
                          timeout=30.0)
    terms = list(terms) if terms is not None else search_terms(tax)
    index = repository_index()

    records: List[dict] = []
    rejected: List[dict] = []
    needs_review: List[dict] = []
    seen = set()
    del FAILED_SEARCHES[:]

    # Only registered repositories are searched. Gitee hosts a great deal of
    # throwaway content that matches a three-letter acronym, and an unscoped
    # sweep put a thousand such issues into the review queue without reaching
    # a single real report. Finding database projects that host here is a
    # registry question, not something to infer from an acronym match.
    for repository, dbms_id in sorted(index.items()):
        for term in terms:
            if _blocked():
                break
            for item in search(fetcher, term, repository=repository):
                url = item.get("html_url") or ""
                if not url or url in seen:
                    continue
                seen.add(url)
                text = source_text(item)
                if not text:
                    continue
                found = deterministic_attribution(text, tax, as_issue(item))
                if found is None:
                    rejected.append({
                        "kind": "bug", "url": url,
                        "reason": "no SQLancer tool or technique attribution",
                        "title": item.get("title"),
                    })
                    continue
                rule, technique, finder, excerpt = found
                if not verbatim_excerpt(text, excerpt):
                    needs_review.append({
                        "kind": "bug", "url": url,
                        "reason": ("excerpt did not match the report text "
                                   "verbatim"),
                    })
                    continue
                records.append(build_record(
                    item, dbms_id, rule=rule, technique=technique,
                    finder=finder, excerpt=excerpt, text=text,
                    timestamp=timestamp))
    if FAILED_SEARCHES:
        needs_review.append({
            "kind": "bug",
            "url": SEARCH,
            "reason": (f"{len(FAILED_SEARCHES)} Gitee searches did not "
                       f"complete, so this run's result is a lower bound"),
        })
    return records, rejected, needs_review
