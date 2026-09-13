"""Imports database-system bugs from the NUS TEST lab's bug list.

The lab publishes a structured list at https://nus-test.github.io/bugs/, backed
by a JSON file in its site repository. It is a rich source of historical
SQLancer findings -- but it is a list of the lab's bugs, not SQLancer's, and it
includes plenty found with tools that have nothing to do with SQLancer. So none
of it is imported on the strength of being there.

Each candidate is taken back to its own bug report and put through exactly the
same attribution rules as any other GitHub discovery: a statement that SQLancer
or one of its oracles found it, or nothing. Reports whose page cannot be read,
or that the rules cannot settle, are surfaced for a human rather than guessed
at.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Sequence, Tuple

from .. import config, taxonomy
from ..classify import (Classifier, excluded_names, finder_names,
                        resolve_finder, resolve_technique, technique_names)
from ..github import GitHub
from ..http import Fetcher
from ..repos import canonical_issue_url
from ..util import (content_hash, github_issue_ref, normalize_url, now,
                    parse_date, slugify, stable_digest, truncate,
                    verbatim_excerpt, year_of)
from .github_bugs import deterministic_attribution, reproducer_excerpt
from .sqlancer_bugs import build_alias_map

COLLECTOR = "nus_test"
COLLECTOR_VERSION = "1.0.0"

OWNER, REPO, REF = "nus-test", "nus-test.github.io", "main"
DATA_PATH = "data/bugs.json"
DATA_URL = f"https://github.com/{OWNER}/{REPO}/blob/{REF}/{DATA_PATH}"
PAGE_URL = "https://nus-test.github.io/bugs/"

# The lab tracks several domains; only database systems are in scope here.
DBMS_DOMAIN = "dbms"

# Upstream resolution values, mapped onto the schema's status vocabulary.
RESOLUTION_STATUS = {
    "fixed": ("fixed", True),
    "confirmed": ("verified", True),
    "verified": ("verified", True),
    "open": ("open", True),
    "duplicate": ("closed_duplicate", False),
    "wontfix": ("closed_not_a_bug", False),
    "not a bug": ("closed_not_a_bug", False),
    "invalid": ("closed_not_a_bug", False),
}

MAX_PAGE_CHARS = 24_000
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)


def _status_of(entry: dict) -> Tuple[str, bool]:
    resolution = (entry.get("resolution") or "").strip().lower()
    if resolution in RESOLUTION_STATUS:
        return RESOLUTION_STATUS[resolution]
    state = (entry.get("state") or "").strip().lower()
    if state == "open":
        return "open", True
    return "unknown", False


def _strip_html(html: str) -> str:
    """Plain text of a fetched page, for the attribution rules to read.

    Tags are removed rather than parsed: the rules only need the prose, and the
    excerpt they store is checked against this same text, so the stored quote
    always matches what was actually inspected.
    """
    text = SCRIPT_RE.sub(" ", html)
    text = TAG_RE.sub(" ", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return truncate(text.strip(), MAX_PAGE_CHARS)


def load_index(gh: GitHub) -> List[dict]:
    """The lab's bug list, restricted to database systems."""
    raw, _ = gh.raw_file(OWNER, REPO, DATA_PATH, REF, max_age_days=3)
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except ValueError:
        return []
    return [entry for entry in entries
            if (entry.get("domain") or "").lower() == DBMS_DOMAIN]


def emailed(entries: Sequence[dict]) -> List[dict]:
    """Entries the list records without a link.

    Not every project takes bug reports in a public tracker. Umbra's are sent
    by email, so the lab's own list is the whole public record of them -- 55
    bugs that a route requiring a URL cannot see at all.
    """
    return [entry for entry in entries if not entry.get("url")]


def _fetch_report(gh: GitHub, fetcher: Fetcher, url: str
                  ) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(text, source_type)`` for a bug report, or ``(None, None)``.

    GitHub issues go through the API so the thread's ``updated_at`` can drive
    the cache; everything else is a plain conditional fetch.
    """
    reference = github_issue_ref(url)
    if reference:
        owner, repo, kind, number = reference
        try:
            issue, _ = gh.issue(owner, repo, number, max_age_days=30)
        except RuntimeError:
            return None, None
        if not issue:
            return None, None
        body = "\n\n".join(part for part in
                           [issue.get("title") or "", issue.get("body") or ""]
                           if part)
        return truncate(body, MAX_PAGE_CHARS), (
            "github_pull_request" if kind == "pull_request" else "github_issue")

    try:
        entry, _ = fetcher.fetch(url, max_age_days=90)
    except Exception:
        # Dead links and unreachable trackers are common in a list this old;
        # one of them must not end the run.
        return None, None
    if entry.get("status") != 200 or not entry.get("body"):
        return None, None
    source_type = "forum_post" if "forum" in url.lower() else "issue_tracker"
    return _strip_html(entry["body"]), source_type


def collect(gh: GitHub, classifier: Optional[Classifier] = None, *,
            fetcher: Optional[Fetcher] = None, timestamp: Optional[str] = None,
            max_reports: int = 400, entries: Optional[List[dict]] = None
            ) -> Tuple[List[dict], List[dict], List[dict]]:
    """Return ``(records, rejected, needs_review)``.

    ``max_reports`` bounds how many reports one run will read. The raw cache
    makes an already-inspected report free, so successive runs work through the
    backlog without any single run being expensive.
    """
    timestamp = timestamp or now()
    tax = taxonomy.load()
    alias_map = build_alias_map()
    if fetcher is None:
        from ..cache import Caches
        # Short timeout and few retries: this walks a long list of external
        # trackers, some of which no longer resolve.
        fetcher = Fetcher(Caches().http, min_interval=0.5, max_retries=2,
                          timeout=15.0, initial_backoff=1.0, max_backoff=8.0)

    index = entries if entries is not None else load_index(gh)
    records: List[dict] = []
    rejected: List[dict] = []
    needs_review: List[dict] = []
    inspected = 0

    for entry in index:
        url = entry.get("url")
        canonical = normalize_url(url) if url else None
        if not canonical:
            # Handled by ``emailed_records``: there is no report to fetch, so
            # the rules that read one cannot apply.
            continue

        dbms_id = alias_map.get((entry.get("system") or "").strip().lower())
        if dbms_id is None:
            needs_review.append({
                "kind": "bug", "url": url,
                "reason": (f"database system {entry.get('system')!r} is not in "
                           f"the registry"),
            })
            continue

        if inspected >= max_reports:
            needs_review.append({
                "kind": "bug", "url": url,
                "reason": "not inspected yet; run budget reached",
            })
            continue

        text, source_type = _fetch_report(gh, fetcher, url)
        inspected += 1
        if not text:
            needs_review.append({
                "kind": "bug", "url": url,
                "reason": "bug report could not be fetched",
            })
            continue

        attribution = deterministic_attribution(text, tax)
        classifier_record = None
        confidence = "high"

        if attribution is None:
            # The lab's own list says this report came from one of its members,
            # and its reproducer carries SQLancer's generated-schema convention.
            # Neither is enough alone -- the list records who reported a bug but
            # not which tool found it, and the convention says only that a
            # SQLancer-family generator produced the SQL. Together they place
            # the report in a SQLancer campaign, which is what the policy's
            # campaign clause asks for. Many reports never mention the tool at
            # all, so without this the whole of a system like Dolt is invisible.
            signature = reproducer_excerpt(text)
            if (signature and entry.get("reported_by")
                    and not tax.find_excluded_techniques(text)):
                attribution = ("campaign_evidence", None, "sqlancer", signature)
                confidence = "medium"

        if attribution is None:
            if not (tax.mentions_finder(text) or tax.find_techniques(text)):
                rejected.append({
                    "kind": "bug", "url": url,
                    "reason": ("TEST lab bug with no SQLancer tool or technique "
                               "named in the report"),
                })
                continue
            # Ask before giving up: the answer may be cached, and a
            # cached answer needs no key. Checking availability first
            # made a warm cache useless offline.
            result = None if classifier is None else classifier.classify(
                "bug_attribution", source_id=f"nus-test:{canonical}",
                source_text=text, source_label=f"bug report {url}",
                technique_names=technique_names(tax),
                finder_names=finder_names(tax),
                excluded_names=excluded_names(tax))
            if result is None:
                needs_review.append({
                    "kind": "bug", "url": url, "title": entry.get("title"),
                    "reason": ("mentions SQLancer but does not state it found "
                               "the bug; needs semantic classification"),
                })
                continue
            if not result.is_positive:
                rejected.append({
                    "kind": "bug", "url": url, "answer": result.answer,
                    "reason": result.reason or "classifier did not confirm",
                })
                continue
            technique = resolve_technique(tax, result.extra.get("technique"))
            finder = resolve_finder(tax, result.extra.get("finder")) or "sqlancer"
            attribution = (
                "technique_attribution" if technique else "explicit_tool_statement",
                technique, finder, result.excerpts[0])
            classifier_record = result.classifier_record()
            confidence = "medium"

        rule, technique, finder, excerpt = attribution
        if not verbatim_excerpt(text, excerpt):
            continue

        reported_date = parse_date(entry.get("created_at"))
        status, is_true_positive = _status_of(entry)

        evidence = [{
            "source_url": url,
            "source_type": source_type or "issue_tracker",
            "excerpt": excerpt,
            "excerpt_is_verbatim": True,
            "note": ("The report states which tool or oracle found the bug."
                     if rule != "campaign_evidence" else
                     f"Reported by {entry.get('reported_by')} of the TEST lab, "
                     f"with a reproducer using SQLancer's generated-schema "
                     f"convention (tables t0, t1, ...; columns c0, c1, ...)."),
            "content_sha256": content_hash(text),
            "retrieved_at": timestamp,
            "first_seen": timestamp,
            "last_verified": timestamp,
        }, {
            "source_url": PAGE_URL,
            "source_type": "website",
            "excerpt": None,
            "note": (f"Listed on the NUS TEST lab bug page, reported by "
                     f"{entry.get('reported_by') or 'a lab member'}."),
            "retrieved_at": timestamp,
            "first_seen": timestamp,
            "last_verified": timestamp,
        }]

        record = {
            "id": f"bug:{dbms_id}:{stable_digest(canonical)}",
            "dbms": dbms_id,
            "title": truncate(entry.get("title") or "(untitled report)", 500),
            "reported_date": reported_date,
            "reported_year": year_of(reported_date),
            "status": status,
            "status_is_true_positive": is_true_positive,
            "finder": finder,
            "technique": technique,
            "symptom": "unknown",
            "reporter": entry.get("reported_by"),
            "links": {"report": url},
            "primary_url": canonical,
            "attribution": {
                "rule": rule,
                "confidence": confidence,
                "evidence": evidence,
            },
            "provenance": {
                "collector": COLLECTOR,
                "collector_version": COLLECTOR_VERSION,
                "policy_version": config.policy_version(),
                "first_seen": timestamp,
                "last_verified": timestamp,
                "source_url": DATA_URL,
                "source_type": "website",
                "content_sha256": content_hash(text),
            },
        }
        if classifier_record:
            record["attribution"]["classifier"] = classifier_record
        records.append(record)

    return records, rejected, needs_review


def emailed_records(entries: Sequence[dict], alias_map: Dict[str, str], *,
                    timestamp: Optional[str] = None
                    ) -> Tuple[List[dict], List[dict]]:
    """Records for bugs the lab reported privately, from the list alone.

    These are admitted under ``curated_primary_source``: the lab's bug list is
    the record its own members keep of what they found, which is the same
    standing as the SQLancer project's bug repository. Confidence is medium
    rather than high because the report itself is not public, so unlike a
    tracker entry it cannot be re-read to check what it says.

    No ``primary_url`` is set. There is no public report to point at, and
    pointing every one of them at the list page would make 55 bugs look like
    one record duplicated.
    """
    timestamp = timestamp or now()
    records: List[dict] = []
    needs_review: List[dict] = []

    for entry in emailed(entries):
        system = (entry.get("system") or "").strip()
        dbms_id = alias_map.get(system.lower())
        if dbms_id is None:
            needs_review.append({
                "kind": "bug", "url": PAGE_URL,
                "reason": (f"database system {system!r} is not in the "
                           f"registry, and this report has no link"),
                "title": entry.get("title"),
            })
            continue

        title = (entry.get("title") or "").strip()
        reported_date = parse_date(entry.get("created_at"))
        status, is_true_positive = _status_of(entry)
        reporter = entry.get("reported_by")
        # The list is the source, so its own row is the evidence. The title is
        # quoted verbatim because that is what the page shows.
        fingerprint = "|".join([dbms_id, title, entry.get("created_at") or ""])

        records.append({
            "id": f"bug:{dbms_id}:{stable_digest(fingerprint)}",
            "dbms": dbms_id,
            "title": truncate(title or "(untitled report)", 500),
            "reported_date": reported_date,
            "reported_year": year_of(reported_date),
            "status": status,
            "status_is_true_positive": is_true_positive,
            "finder": "sqlancer",
            "technique": None,
            "symptom": "unknown",
            "reporter": reporter,
            "links": {"record": PAGE_URL},
            "attribution": {
                "rule": "curated_primary_source",
                "confidence": "medium",
                "evidence": [{
                    "source_url": PAGE_URL,
                    "source_type": "website",
                    "excerpt": truncate(title, 500) or None,
                    "excerpt_is_verbatim": bool(title),
                    "note": (f"Listed on the NUS TEST lab bug page as a "
                             f"{system} bug reported by "
                             f"{reporter or 'a lab member'}. The report was "
                             f"sent to the developers directly, so the list is "
                             f"its only public record."),
                    "content_sha256": content_hash(fingerprint),
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
                "source_url": PAGE_URL,
                "source_type": "website",
                "content_sha256": content_hash(fingerprint),
            },
        })
    return records, needs_review
