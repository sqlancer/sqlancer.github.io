"""Imports SQLancer bug reports from MariaDB's public Jira.

MariaDB does not take bug reports on GitHub, so the repository search that
reaches every other system finds nothing for it: its 14 records all arrived
through curated lists. Its Jira is open, needs no credentials, and answers a
JQL query over the REST API, which makes it the one route to reports that name
SQLancer in MariaDB's own tracker.

Attribution is the shared one. Nothing here decides whether a report counts --
``deterministic_attribution`` does, on the same evidence it requires of a GitHub
issue -- so a Jira report earns its place exactly as an issue does. The only
translation is the issue dict the rules read: a Jira issue type of Bug is passed
where a GitHub defect label would be, because it is the same signal from the
same kind of source, applied by the project itself.
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

COLLECTOR = "mariadb_jira"
COLLECTOR_VERSION = "1.0.0"

DBMS_ID = "mariadb"
API = "https://jira.mariadb.org/rest/api/2/search"
BROWSE = "https://jira.mariadb.org/browse/"
FIELDS = "summary,description,status,resolution,created,reporter,issuetype"
PAGE_SIZE = 100
MAX_PAGES = 5

# Jira's ``~`` operator tokenises its argument and matches any of the words, so
# a multi-word technique name returns hundreds of unrelated issues -- searching
# for the NoREC paper's title matched 195. Only single tokens are searched, and
# a report describing a technique in words almost always names it too.
SINGLE_TOKEN = re.compile(r"^[A-Za-z][\w+.-]*$")

RESOLUTION_STATUS = {
    "fixed": ("fixed", True),
    "done": ("fixed", True),
    "duplicate": ("closed_duplicate", False),
    "not a bug": ("closed_not_a_bug", False),
    "won't fix": ("closed_not_a_bug", False),
    "wont fix": ("closed_not_a_bug", False),
    "cannot reproduce": ("closed_not_a_bug", False),
    "incomplete": ("closed_not_a_bug", False),
}

# An unresolved issue is open whatever workflow state it sits in.
OPEN_STATUSES = {"open", "confirmed", "in progress", "in review", "in testing",
                 "stalled", "needs feedback"}


def search_terms(tax: taxonomy.Taxonomy) -> List[str]:
    """Single-token names worth issuing a JQL query for."""
    terms = []
    for name in tax.search_terms():
        if SINGLE_TOKEN.match(name or ""):
            terms.append(name)
    seen, ordered = set(), []
    for term in terms:
        if term.lower() not in seen:
            seen.add(term.lower())
            ordered.append(term)
    return ordered


def _escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace('"', '\\"')


def search(fetcher: Fetcher, term: str, *, max_pages: int = MAX_PAGES
           ) -> List[dict]:
    """Every issue whose text matches ``term``."""
    issues: List[dict] = []
    for page in range(max_pages):
        query = urllib.parse.quote(f'text ~ "{_escape(term)}" ORDER BY created ASC')
        url = (f"{API}?jql={query}&startAt={page * PAGE_SIZE}"
               f"&maxResults={PAGE_SIZE}&fields={FIELDS}")
        try:
            entry, _ = fetcher.fetch(url, max_age_days=7)
        except Exception:
            return issues
        if entry.get("status") != 200:
            return issues
        try:
            payload = json.loads(entry["body"])
        except ValueError:
            return issues
        batch = payload.get("issues") or []
        issues.extend(batch)
        if len(issues) >= int(payload.get("total") or 0) or not batch:
            return issues
    return issues


def status_of(fields: dict) -> Tuple[str, bool]:
    """The report's state, from its resolution first and its workflow second."""
    resolution = ((fields.get("resolution") or {}).get("name") or "").strip().lower()
    if resolution in RESOLUTION_STATUS:
        return RESOLUTION_STATUS[resolution]
    if resolution:
        # An unrecognised resolution closed the issue, but not visibly as a
        # fix; saying "unknown" is more honest than guessing either way.
        return "unknown", False
    status = ((fields.get("status") or {}).get("name") or "").strip().lower()
    if status in OPEN_STATUSES:
        return "open", True
    return "unknown", False


def as_issue(item: dict) -> dict:
    """The shape ``deterministic_attribution`` reads, from a Jira issue.

    The issue type is passed where a GitHub label would be. Both are a
    statement by the project's own triage that this is a defect, which is what
    the rule is asking; nothing is invented that the tracker does not say.
    """
    fields = item.get("fields") or {}
    kind = (fields.get("issuetype") or {}).get("name") or ""
    return {
        "title": fields.get("summary") or "",
        "body": fields.get("description") or "",
        "labels": [{"name": kind}] if kind else [],
        "html_url": f"{BROWSE}{item.get('key', '')}",
    }


def source_text(item: dict) -> str:
    fields = item.get("fields") or {}
    return "\n".join(part for part in (fields.get("summary"),
                                       fields.get("description")) if part)


def build_record(item: dict, *, rule: str, technique: Optional[str],
                 finder: str, excerpt: str, text: str, timestamp: str,
                 confidence: str = "high") -> dict:
    """Assemble a bug record from one Jira issue."""
    fields = item.get("fields") or {}
    key = item.get("key") or ""
    url = f"{BROWSE}{key}"
    status, is_true_positive = status_of(fields)
    created = (fields.get("created") or "")[:10] or None
    reporter = fields.get("reporter") or {}

    return {
        "id": f"bug:{DBMS_ID}:{stable_digest(normalize_url(url))}",
        "dbms": DBMS_ID,
        "title": truncate(fields.get("summary") or key or "(untitled report)", 500),
        "reported_date": parse_date(created),
        "reported_year": year_of(parse_date(created)),
        "status": status,
        "status_is_true_positive": is_true_positive,
        "finder": finder,
        "technique": technique,
        "symptom": "unknown",
        "reporter": (reporter.get("displayName") or reporter.get("name")
                     or None),
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
                "note": (f"{key} in MariaDB's own Jira, filed as a "
                         f"{(fields.get('issuetype') or {}).get('name') or 'report'}."),
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
    """Return ``(records, rejected, needs_review)`` from MariaDB's Jira."""
    timestamp = timestamp or now()
    tax = tax or taxonomy.load()
    if fetcher is None:
        from ..cache import Caches
        fetcher = Fetcher(Caches().http, min_interval=1.0, max_retries=2,
                          timeout=30.0)
    terms = list(terms) if terms is not None else search_terms(tax)

    records: List[dict] = []
    rejected: List[dict] = []
    needs_review: List[dict] = []
    seen = set()

    for term in terms:
        for item in search(fetcher, term):
            key = item.get("key")
            if not key or key in seen:
                continue
            seen.add(key)
            text = source_text(item)
            if not text:
                continue
            issue = as_issue(item)
            found = deterministic_attribution(text, tax, issue)
            if found is None:
                rejected.append({
                    "kind": "bug",
                    "url": issue["html_url"],
                    "reason": "no SQLancer tool or technique attribution",
                    "title": issue["title"],
                })
                continue
            rule, technique, finder, excerpt = found
            if not verbatim_excerpt(text, excerpt):
                needs_review.append({
                    "kind": "bug",
                    "url": issue["html_url"],
                    "reason": "excerpt did not match the report text verbatim",
                })
                continue
            records.append(build_record(
                item, rule=rule, technique=technique, finder=finder,
                excerpt=excerpt, text=text, timestamp=timestamp))
    return records, rejected, needs_review
