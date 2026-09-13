"""Imports SQLancer's own curated bug repository, github.com/sqlancer/bugs.

That repository is the project's primary record of what SQLancer found, so its
entries are admitted under the ``curated_primary_source`` clause of the
attribution policy. Nothing is inferred: the status, the oracle and the links
come from the file, and the technique is resolved through the taxonomy rather
than by pattern-matching in this module.

Every imported record carries two or three pieces of evidence -- the upstream
bug report itself, the repository's own statement of what it collects, and,
where the entry names a SQLancer oracle, the verbatim entry text showing it.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

from .. import config, taxonomy
from ..github import GitHub
from ..repos import canonical_issue_url
from ..util import (content_hash, normalize_url, now, parse_date, slugify,
                    stable_digest, truncate, verbatim_excerpt, year_of)

COLLECTOR = "sqlancer_bugs"
COLLECTOR_VERSION = "1.0.0"

OWNER, REPO, REF = "sqlancer", "bugs", "master"
BUGS_JSON_URL = f"https://github.com/{OWNER}/{REPO}/blob/{REF}/bugs.json"
README_URL = f"https://github.com/{OWNER}/{REPO}/blob/{REF}/README.md"

# The statement that makes this repository usable as attribution evidence.
README_CLAIM = "the repository stores a list of bugs found by SQLancer"

STATUS_MAP = {
    "fixed": ("fixed", True),
    "verified": ("verified", True),
    "open": ("open", True),
    "fixed (in documentation)": ("fixed_in_documentation", True),
    "closed (not a bug)": ("closed_not_a_bug", False),
    "closed (duplicate)": ("closed_duplicate", False),
}

# The upstream 'oracle' field mixes techniques with symptoms. Values that are
# not a technique describe how the bug manifested.
SYMPTOM_ONLY = {"error": "error", "crash": "crash", "hang": "hang"}

# Preference order when a record links several sources; the first present wins.
PRIMARY_LINK_ORDER = ("bugreport", "bugreports", "bugtracker", "email",
                      "email 2", "commit", "fix", "fix1", "fixed",
                      "documentation", "docs fix")


def _source_type(url: str) -> str:
    lowered = url.lower()
    if "github.com" in lowered and "/pull/" in lowered:
        return "github_pull_request"
    if "github.com" in lowered and "/issues/" in lowered:
        return "github_issue"
    if "github.com" in lowered or "gitlab" in lowered:
        return "commit"
    if "mailinglist" in lowered or "mailman" in lowered or "pipermail" in lowered:
        return "mailing_list"
    if "forum" in lowered:
        return "forum_post"
    if "sqlite.org" in lowered or "bugs." in lowered or "jira" in lowered:
        return "issue_tracker"
    return "other"


def record_spans(text: str) -> List[Tuple[int, int]]:
    """Byte spans of each top-level array element in ``text``.

    Used so that an evidence excerpt can be lifted verbatim out of the upstream
    file rather than re-serialised from the parsed structure.
    """
    decoder = json.JSONDecoder()
    spans: List[Tuple[int, int]] = []
    index = text.index("[") + 1
    length = len(text)
    while index < length:
        while index < length and text[index] in " \t\r\n,":
            index += 1
        if index >= length or text[index] == "]":
            break
        _, end = decoder.raw_decode(text, index)
        spans.append((index, end))
        index = end
    return spans


def _entry_excerpt(text: str, span: Tuple[int, int]) -> Optional[str]:
    """Verbatim slice of an entry from ``"date"`` through its ``"oracle"`` line.

    That window identifies the record and shows the oracle without dragging in
    the test case, which can run to hundreds of statements.
    """
    start, end = span
    chunk = text[start:end]
    date_at = chunk.find('"date"')
    oracle_at = chunk.find('"oracle"')
    if date_at < 0 or oracle_at < 0:
        return None
    line_end = chunk.find("\n", oracle_at)
    if line_end < 0:
        line_end = len(chunk)
    excerpt = chunk[date_at:line_end].rstrip().rstrip(",")
    return truncate(excerpt, 3900)


def _dbms_id(raw: str, alias_map: Dict[str, str]) -> str:
    return alias_map.get(raw.strip().lower(), slugify(raw))


def build_alias_map() -> Dict[str, str]:
    """Map every registered DBMS alias to its canonical registry id."""
    registry = config.load_json(config.DATA_FILES["dbms"])["dbms"]
    alias_map: Dict[str, str] = {}
    for entry in registry:
        for alias in [entry["id"], entry["name"], *entry.get("aliases", [])]:
            alias_map.setdefault(alias.strip().lower(), entry["id"])
    return alias_map


def _primary_url(links: Dict[str, str]) -> Optional[str]:
    for key in PRIMARY_LINK_ORDER:
        if links.get(key):
            return links[key]
    for value in links.values():
        if value:
            return value
    return None


def collect(gh: GitHub, *, timestamp: Optional[str] = None) -> List[dict]:
    """Return bug records imported from the curated repository."""
    timestamp = timestamp or now()
    tax = taxonomy.load()
    alias_map = build_alias_map()

    raw_text, entry = gh.raw_file(OWNER, REPO, "bugs.json", REF, max_age_days=1)
    if raw_text is None:
        raise RuntimeError("could not fetch sqlancer/bugs bugs.json")
    readme_text, _ = gh.raw_file(OWNER, REPO, "README.md", REF, max_age_days=30)

    entries = json.loads(raw_text)
    spans = record_spans(raw_text)
    if len(spans) != len(entries):  # formatting changed upstream
        spans = [None] * len(entries)

    readme_hash = content_hash(readme_text) if readme_text else None

    # Only quote the repository's self-description if it really says this.
    readme_excerpt = README_CLAIM if (
        readme_text and verbatim_excerpt(readme_text, README_CLAIM)) else None

    records: List[dict] = []
    for item, span in zip(entries, spans):
        # Hash this entry rather than the whole upstream file: an unrelated bug
        # being added upstream must not rewrite the hash of all 499 records.
        entry_text = raw_text[span[0]:span[1]] if span else json.dumps(item)
        entry_hash = content_hash(entry_text)
        dbms_id = _dbms_id(item["dbms"], alias_map)
        links = {k: v for k, v in (item.get("links") or {}).items() if v}
        primary = _primary_url(links)
        reported_date = parse_date(item.get("date"))
        status, is_true_positive = STATUS_MAP.get(
            item.get("status", ""), ("unknown", False))

        oracle = item.get("oracle") or ""
        technique = tax.technique_from_oracle_label(oracle)
        symptom = SYMPTOM_ONLY.get(oracle.strip().lower(),
                                   "logic" if technique else "unknown")

        # The title is part of the identity because a single upstream ticket
        # occasionally covers two separately curated reports; the URL alone
        # would collapse them into one record.
        identity = "|".join([
            canonical_issue_url(primary) or dbms_id,
            item.get("date") or "",
            item.get("title") or "",
        ])
        record_id = f"bug:{dbms_id}:{stable_digest(identity)}"

        evidence: List[dict] = []
        if primary:
            evidence.append({
                "source_url": primary,
                "source_type": _source_type(primary),
                "excerpt": None,
                "note": "Upstream bug report or fix for this issue.",
                "retrieved_at": timestamp,
                "first_seen": timestamp,
                "last_verified": timestamp,
            })
        evidence.append({
            "source_url": README_URL,
            "source_type": "curated_bug_repository",
            "excerpt": readme_excerpt,
            "excerpt_is_verbatim": True,
            "note": ("SQLancer's own curated bug repository, which is the "
                     "primary record of what the tool found."),
            "content_sha256": readme_hash,
            "retrieved_at": timestamp,
            "first_seen": timestamp,
            "last_verified": timestamp,
        })

        # The entry itself is quoted verbatim, so every record can be audited
        # against the upstream file without re-deriving it. When it names a
        # SQLancer oracle, that quote is also what upgrades the attribution
        # from "came from the curated list" to "the technique is on record".
        rule = "curated_primary_source"
        excerpt = _entry_excerpt(raw_text, span) if span else None
        if excerpt and verbatim_excerpt(raw_text, excerpt):
            note = "Entry for this report in the curated bugs.json."
            if technique:
                note = (f"Entry in bugs.json recording that the "
                        f"{tax.display_name(technique)} oracle found this bug.")
                rule = "technique_attribution"
            evidence.append({
                "source_url": BUGS_JSON_URL,
                "source_type": "curated_bug_repository",
                "excerpt": excerpt,
                "excerpt_is_verbatim": True,
                "note": note,
                "content_sha256": entry_hash,
                "retrieved_at": timestamp,
                "first_seen": timestamp,
                "last_verified": timestamp,
            })

        if not links:
            # A handful of early reports went through private security channels
            # and have no public URL. The curated entry is then the primary
            # source, so the record stays auditable rather than being dropped.
            links = {"curated_entry": BUGS_JSON_URL}

        record = {
            "id": record_id,
            "dbms": dbms_id,
            "title": item.get("title") or "(untitled report)",
            "reported_date": reported_date,
            "reported_year": year_of(reported_date),
            "status": status,
            "status_is_true_positive": is_true_positive,
            "finder": "sqlancer",
            "technique": technique,
            "symptom": symptom,
            "reporter": item.get("reporter"),
            "severity": item.get("severity"),
            "cve": item.get("cve"),
            "links": links,
            "attribution": {
                "rule": rule,
                "confidence": "high",
                "evidence": evidence,
            },
            "provenance": {
                "collector": COLLECTOR,
                "collector_version": COLLECTOR_VERSION,
                "policy_version": config.policy_version(),
                "first_seen": timestamp,
                "last_verified": timestamp,
                "source_url": BUGS_JSON_URL,
                "source_type": "curated_bug_repository",
                "content_sha256": entry_hash,
            },
        }
        if primary:
            record["primary_url"] = normalize_url(primary)
        records.append(_drop_nulls(record))
    return records


def _drop_nulls(value):
    """Strip ``None`` values the schemas do not allow, keeping nullable ones."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if item is None and key in ("content_sha256", "excerpt_is_verbatim",
                                        "note", "severity", "cve"):
                continue
            out[key] = _drop_nulls(item)
        return out
    if isinstance(value, list):
        return [_drop_nulls(item) for item in value]
    return value
