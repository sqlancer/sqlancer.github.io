"""Imports the bugs SQLancer's own source code records, one file per provider.

Every provider package carries a ``<Name>Bugs.java`` whose fields exist to
switch off behaviour that trips over a real defect in that system. Each field is
introduced by a comment linking the report. That makes the file a bug list the
project maintains as part of its working code -- it is edited when a bug is
found and when one is fixed -- so it is admitted under the same
``curated_primary_source`` clause as the project's own bug repository.

Forks are read as well as upstream. A database vendor that maintains its own
SQLancer fork keeps its provider's bug file there, and for a system with no
upstream provider that file is the only public record of what SQLancer found in
it: Oxla's twelve bugs exist nowhere else this pipeline can reach.

Where the linked tracker can be read, the report's real status is fetched, so a
fixed bug is not published as though its state were unknown. Where it cannot --
a private Jira, a tracker that refuses robots -- the status stays unknown and
the record says so rather than guessing.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .. import config
from ..github import GitHub
from ..util import (content_hash, normalize_url, now, stable_digest, truncate,
                    year_of)

COLLECTOR = "provider_bugs"
COLLECTOR_VERSION = "1.0.0"

UPSTREAM = ("sqlancer", "sqlancer", "main")
PROVIDER_ROOT = "src/sqlancer"

# A field whose only purpose is to record a bug, and the comment above it.
FIELD = re.compile(r"^\s*public\s+static\s+(?:final\s+)?boolean\s+(\w+)\s*=")
COMMENT = re.compile(r"^\s*(?://+!?|\*|/\*+)\s?(.*?)\s*$")
COMMENT_END = re.compile(r"\*/\s*$")
URL = re.compile(r"https?://[^\s\"'<>)\]]+")

# Trackers whose state can be read without credentials.
GITHUB_ISSUE = re.compile(
    r"github\.com/([\w.-]+)/([\w.-]+)/(?:issues|pull)/(\d+)", re.IGNORECASE)
MARIADB_JIRA = re.compile(r"jira\.mariadb\.org/browse/([A-Z]+-\d+)")

GITHUB_STATE = {
    ("closed", "completed"): ("fixed", True),
    ("closed", None): ("fixed", True),
    ("closed", "not_planned"): ("closed_not_a_bug", False),
    ("open", None): ("open", True),
}

# Jira resolutions that mean the report was not a defect. Everything else that
# is resolved is treated as fixed, and an unresolved issue as open.
JIRA_NOT_A_BUG = {"not a bug", "won't fix", "cannot reproduce", "duplicate",
                  "incomplete", "invalid"}


def provider_index() -> Dict[str, str]:
    """Map a provider directory to the registry id of the system it tests.

    The registry already records this for anything SQLancer supports upstream,
    as ``support.provider_path``; three of them differ from the id (postgres,
    sqlite3, yugabyte). A downstream-only provider has no such entry, so its
    directory name is matched against ids and aliases instead.
    """
    index: Dict[str, str] = {}
    for entry in config.load_json(config.DATA_FILES["dbms"])["dbms"]:
        path = (entry.get("support") or {}).get("provider_path")
        if path:
            index[path.rstrip("/").split("/")[-1].lower()] = entry["id"]
        index.setdefault(entry["id"].lower(), entry["id"])
        for alias in entry.get("aliases") or []:
            index.setdefault(alias.lower(), entry["id"])
    return index


def parse_entries(text: str) -> List[dict]:
    """Every recorded bug in one ``<Name>Bugs.java``.

    An entry is a field declaration plus the comment block directly above it,
    which is where the link and the description live. Comments not followed by
    a field -- the licence header, a note about the class -- belong to no entry
    and are dropped when the next field or a blank line arrives.
    """
    entries: List[dict] = []
    buffer: List[str] = []
    for line in (text or "").splitlines():
        comment = COMMENT.match(line)
        if comment is not None and not FIELD.match(line):
            body = comment.group(1)
            if not COMMENT_END.search(line) or body:
                buffer.append(body)
            continue
        field = FIELD.match(line)
        if field is not None:
            note = " ".join(part for part in buffer if part).strip()
            urls = [normalize_url(u.rstrip(".,;")) for u in URL.findall(note)]
            entries.append({
                "constant": field.group(1),
                "urls": [u for u in urls if u],
                "note": note,
                # The comment as written, for use as a verbatim excerpt.
                "excerpt": "\n".join(buffer).strip(),
            })
        buffer = []
    return [entry for entry in entries if entry["urls"]]


def bugs_files(gh: GitHub, owner: str, repo: str, ref: str) -> List[Tuple[str, str]]:
    """``(provider directory, path)`` for every bug file in a checkout."""
    result = gh.tree(owner, repo, ref)
    entries = result[0] if isinstance(result, tuple) else result
    found: List[Tuple[str, str]] = []
    for item in entries or []:
        path = item["path"] if isinstance(item, dict) else str(item)
        if not path.startswith(f"{PROVIDER_ROOT}/") or not path.endswith("Bugs.java"):
            continue
        parts = path.split("/")
        if len(parts) == 4:
            found.append((parts[2], path))
    return found


def github_issue(gh: GitHub, url: str) -> Optional[dict]:
    """The linked GitHub issue, or None when the URL is not one."""
    match = GITHUB_ISSUE.search(url)
    if not match:
        return None
    owner, repo, number = match.group(1), match.group(2), int(match.group(3))
    try:
        result = gh.issue(owner, repo, number)
    except Exception:
        return None
    issue = result[0] if isinstance(result, tuple) else result
    return issue or None


def _github_status(gh: GitHub, url: str) -> Optional[Tuple[str, bool]]:
    issue = github_issue(gh, url)
    if issue is None:
        return None
    state = issue.get("state")
    reason = issue.get("state_reason")
    return GITHUB_STATE.get((state, reason)) or GITHUB_STATE.get((state, None))


def _jira_status(fetcher, url: str) -> Optional[Tuple[str, bool]]:
    match = MARIADB_JIRA.search(url)
    if not match or fetcher is None:
        return None
    api = (f"https://jira.mariadb.org/rest/api/2/issue/{match.group(1)}"
           "?fields=resolution,status")
    try:
        entry, _ = fetcher.fetch(api, max_age_days=14)
    except Exception:
        return None
    if entry.get("status") != 200:
        return None
    import json

    try:
        fields = json.loads(entry["body"]).get("fields") or {}
    except ValueError:
        return None
    resolution = ((fields.get("resolution") or {}).get("name") or "").lower()
    if not resolution:
        return "open", True
    if resolution in JIRA_NOT_A_BUG:
        return "closed_not_a_bug", False
    return "fixed", True


# A tracker that answers 404 or 401 to everyone is not a link a reader can
# follow. Oxla's Jira is private, so twelve records pointed at a page that does
# not exist for anybody outside the company. A 403 is different -- bugs.mysql.com
# refuses this client but serves a browser -- so it is left alone.
UNREADABLE = (401, 404)


def is_publicly_readable(url: str, fetcher=None) -> bool:
    """Whether a reader following this link would reach the report.

    Only a definite "not for you" counts as unreadable. Anything else, an
    error included, leaves the link in place: failing to reach a tracker from
    here is not evidence that nobody can.
    """
    if fetcher is None:
        return True
    try:
        entry, _ = fetcher.fetch(url, max_age_days=30)
    except Exception:
        return True
    return entry.get("status") not in UNREADABLE


def resolve_status(gh: GitHub, url: str, fetcher=None) -> Tuple[str, bool]:
    """The report's real state where the tracker can be read, else unknown.

    An unknown state still counts as a bug: the field exists in SQLancer's
    source because the defect is real and had to be worked around. What is
    unknown is whether it has since been fixed, not whether it was a bug.
    """
    return (_github_status(gh, url) or _jira_status(fetcher, url)
            or ("unknown", True))


def build_record(entry: dict, dbms_id: str, *, url: str, source_url: str,
                 repository: str, timestamp: str,
                 status: Tuple[str, bool], readable: bool = True,
                 title: Optional[str] = None) -> dict:
    """Assemble a bug record from one field in a provider's bug file.

    ``readable`` says whether a reader can follow the linked tracker. When
    they cannot, the record carries no ``primary_url``: the public evidence is
    the bug file itself, and publishing a link into a private Jira as though it
    were the report gives a reader nothing to check.
    """
    state, is_true_positive = status
    excerpt = entry["excerpt"] or entry["note"]
    links = {"record": source_url}
    links["report" if readable else "tracker"] = url
    record = {
        "id": f"bug:{dbms_id}:{stable_digest(normalize_url(url))}",
        "dbms": dbms_id,
        "title": truncate(title or _title_for(entry), 500),
        "reported_date": None,
        "reported_year": None,
        "status": state,
        "status_is_true_positive": is_true_positive,
        "finder": "sqlancer",
        "technique": None,
        "symptom": "unknown",
        "reporter": None,
        "links": links,
        "attribution": {
            "rule": "curated_primary_source",
            "confidence": "high",
            "evidence": [{
                "source_url": source_url,
                "source_type": "github_repository",
                "excerpt": truncate(excerpt, 500),
                "excerpt_is_verbatim": True,
                "note": (f"{repository} records this bug in its {dbms_id} "
                         f"provider, as the field {entry['constant']} that "
                         f"works around it."
                         + ("" if readable else
                            f" The report itself is in the project's own "
                            f"tracker at {url}, which is not public.")),
                "content_sha256": content_hash(excerpt),
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
            "source_url": source_url,
            "source_type": "github_repository",
            "content_sha256": content_hash(excerpt),
        },
    }
    if readable:
        record["primary_url"] = normalize_url(url)
    return record


NUMBER_IN_NAME = re.compile(r"(\d{3,})")


def bug_url(entry: dict) -> str:
    """The URL this field is about, which is not always the first listed.

    A comment can cite more than one issue -- CockroachDB's file has an entry
    naming a closed bug and the underlying one it depends on -- and the field
    name says which is the subject. Taking the first URL recorded the wrong
    issue and titled it with a note to maintainers.
    """
    urls = entry["urls"]
    wanted = NUMBER_IN_NAME.findall(entry.get("constant") or "")
    for number in wanted:
        for url in urls:
            if re.search(rf"(?<!\d){re.escape(number)}(?!\d)", url):
                return url
    return urls[0]


def _title_for(entry: dict) -> str:
    """The description if the comment carries one, else the link itself.

    Upstream writes a bare URL above each field; the Oxla provider writes a
    sentence saying what goes wrong. Where there is a sentence it is the better
    title, and where there is not, inventing one would be writing evidence.
    """
    text = URL.sub("", entry["note"]).strip(" .:-")
    text = re.sub(r"^(?:See|see)\s*:?\s*", "", text).strip()
    return text or entry["urls"][0]


def collect(gh: GitHub, checkouts: Optional[Sequence[Tuple[str, str, str]]] = None,
            *, timestamp: Optional[str] = None, fetcher=None
            ) -> Tuple[List[dict], List[dict], List[dict]]:
    """Return ``(records, rejected, needs_review)`` from every bug file."""
    timestamp = timestamp or now()
    index = provider_index()
    checkouts = list(checkouts) if checkouts is not None else [UPSTREAM]

    records: List[dict] = []
    rejected: List[dict] = []
    needs_review: List[dict] = []
    seen: set = set()

    for owner, repo, ref in checkouts:
        repository = f"{owner}/{repo}"
        for directory, path in bugs_files(gh, owner, repo, ref):
            dbms_id = index.get(directory.lower())
            source_url = f"https://github.com/{repository}/blob/{ref}/{path}"
            if dbms_id is None:
                needs_review.append({
                    "kind": "bug",
                    "url": source_url,
                    "reason": (f"{repository} records bugs for a provider "
                               f"{directory!r} that is not a registered "
                               f"database system"),
                })
                continue
            result = gh.raw_file(owner, repo, path, ref)
            raw = result[0] if isinstance(result, tuple) else result
            for entry in parse_entries(raw or ""):
                url = bug_url(entry)
                key = normalize_url(url)
                if key in seen:
                    continue
                seen.add(key)
                records.append(build_record(
                    entry, dbms_id, url=url, source_url=source_url,
                    repository=repository, timestamp=timestamp,
                    status=resolve_status(gh, url, fetcher),
                    readable=is_publicly_readable(url, fetcher),
                    # The report's own title beats the comment above the
                    # field, which is sometimes a note to maintainers rather
                    # than a description of the bug.
                    title=(github_issue(gh, url) or {}).get("title")))
    return records, rejected, needs_review
