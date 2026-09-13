"""The roster of people who run SQLancer campaigns, and their bug reports.

Author search is the one route into reports that never say "SQLancer". A term
search cannot find them, and neither can a label; but a report filed by someone
who spends their time testing database systems with SQLancer, carrying a
SQLancer-shaped reproducer, in a database system's own tracker, is a report the
dataset should at least look at.

That route needs a handle, and this is where the handles come from. The TEST
lab's bug list is the largest record of who filed what, and it is inconsistent:
some entries name a GitHub handle, some a display name, and one -- ``ShuxinLi``
-- names something that looks like a handle and is not. Roughly four hundred
entries were unreachable for that reason alone.

So the roster is resolved rather than guessed. For a reporter with no usable
handle, the entries themselves say which issues they filed; fetching a sample
and reading each issue's author gives the handle, and the answer is only
accepted when every sample agrees. A person who reports on the SQLite forum or
a Jira has no GitHub profile to find, and stays in the roster without one.

The roster grants no bug anything. It decides whose issues are worth fetching;
whether an issue is a SQLancer bug is settled afterwards by the same attribution
rules every other candidate goes through.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .. import config, taxonomy
from ..github import GitHub
from ..http import Fetcher
from ..util import content_hash, now, slugify
from . import lab_members

COLLECTOR = "people"
COLLECTOR_VERSION = "1.0.0"

ISSUE_URL = re.compile(
    r"^https://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)", re.IGNORECASE)

# How many of a person's issues to read before deciding their handle, and how
# far the samples may disagree. A single sample is not evidence: an entry can
# record who found a bug while someone else filed it.
RESOLVE_SAMPLES = 8
RESOLVE_MIN_SAMPLES = 3

# Bot and service accounts that file issues on people's behalf.
NOT_A_PERSON = re.compile(r"(\[bot\]|-bot$|^bot-|^dependabot|^github-actions)",
                          re.IGNORECASE)


def _lab_entries(gh: GitHub) -> List[dict]:
    raw, _ = gh.raw_file(lab_members.BUGS_OWNER, lab_members.BUGS_REPO,
                         lab_members.BUGS_PATH, lab_members.BUGS_REF,
                         max_age_days=7)
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except ValueError:
        return []
    return entries if isinstance(entries, list) else []


def _looks_like_a_handle(name: str) -> bool:
    """Whether a ``reported_by`` value could be a GitHub login.

    A login has no spaces. That is a weak test and it is meant to be: a value
    that passes it is still resolved against real issues before being trusted.
    """
    return bool(name) and " " not in name and len(name) <= 39


def resolve_handle(gh: GitHub, urls: Sequence[str]
                   ) -> Tuple[Optional[str], List[str]]:
    """The GitHub login that filed these issues, and the ones that showed it.

    Returns ``(None, [])`` unless enough issues could be read and every one of
    them names the same author -- a reporter whose samples disagree is left for
    a person to sort out rather than resolved to whichever login won.
    """
    logins: Counter = Counter()
    evidence: Dict[str, str] = {}
    for url in urls:
        if len(logins) and sum(logins.values()) >= RESOLVE_SAMPLES:
            break
        match = ISSUE_URL.match(url or "")
        if not match:
            continue
        try:
            issue, _ = gh.issue(match.group(1), match.group(2),
                                int(match.group(3)), max_age_days=365)
        except Exception:
            continue
        login = ((issue or {}).get("user") or {}).get("login")
        if not login or NOT_A_PERSON.search(login):
            continue
        logins[login] += 1
        evidence.setdefault(login, url)

    total = sum(logins.values())
    if total < RESOLVE_MIN_SAMPLES or len(logins) != 1:
        return None, []
    login = next(iter(logins))
    return login, [evidence[login]]


def _evidence(source_url: str, note: str, excerpt: Optional[str],
              timestamp: str) -> dict:
    entry = {
        "source_url": source_url,
        "source_type": "github_issue" if "/issues/" in source_url else "website",
        "note": note,
        "retrieved_at": timestamp,
        "first_seen": timestamp,
        "last_verified": timestamp,
    }
    if excerpt:
        entry["excerpt"] = excerpt
        entry["excerpt_is_verbatim"] = True
        entry["content_sha256"] = content_hash(excerpt)
    return entry


def discover(gh: GitHub, timestamp: Optional[str] = None,
             fetcher: Optional[Fetcher] = None) -> List[dict]:
    """Build roster records for everyone the lab's bug list credits.

    Each record links the person to their GitHub profile and says how that link
    was established, so a wrong handle is visible rather than buried.
    """
    timestamp = timestamp or now()
    tax = taxonomy.load()

    entries = _lab_entries(gh)
    urls_by_name: Dict[str, List[str]] = defaultdict(list)
    counts: Counter = Counter()
    for entry in entries:
        name = (entry.get("reported_by") or "").strip()
        if not name:
            continue
        counts[name] += 1
        url = entry.get("url") or ""
        if ISSUE_URL.match(url):
            urls_by_name[name].append(url)

    # The taxonomy's contributor roster maps names to handles directly, which
    # settles anyone whose entries are too few to resolve from issues.
    known: Dict[str, str] = {}
    for entry in tax.project_contributors:
        if entry.get("name") and entry.get("github"):
            known[entry["name"].strip().lower()] = entry["github"]

    records: List[dict] = []
    for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        handle, samples = resolve_handle(gh, urls_by_name.get(name, []))
        if not handle:
            handle = known.get(name.strip().lower())
        evidence = [
            _evidence(
                f"https://github.com/{lab_members.BUGS_OWNER}/"
                f"{lab_members.BUGS_REPO}/blob/{lab_members.BUGS_REF}/"
                f"{lab_members.BUGS_PATH}",
                f"The TEST lab's bug list credits {count} report(s) to "
                f"“{name}”.",
                None, timestamp),
        ]
        if handle:
            for url in samples:
                evidence.append(_evidence(
                    url,
                    f"This issue, recorded against “{name}”, was "
                    f"filed on GitHub by {handle}.",
                    None, timestamp))
        elif _looks_like_a_handle(name):
            # The list already records something handle-shaped. Unverified is
            # not the same as wrong, and searching it costs one query.
            handle = name

        records.append({
            "id": f"person:{slugify(handle or name)}",
            "name": name,
            "github": handle,
            "handle_is_verified": bool(samples),
            "reports_on_lab_list": count,
            "role": ("Project contributor"
                     if tax.is_project_contributor(handle or name, [])
                     else "Bug reporter on the TEST lab's list"),
            "active": True,
            "evidence": evidence,
        })
    # The lab's bug list only names people who have already filed something on
    # it. The people page names the rest, and since a report is now admitted on
    # the strength of roster membership, someone missing from the roster has
    # their bugs missed entirely rather than merely uncounted.
    seen = {(record.get("github") or "").lower() for record in records}
    for handle in lab_members.from_people_page(fetcher or _default_fetcher()):
        if handle.lower() in seen:
            continue
        seen.add(handle.lower())
        records.append({
            "id": f"person:{slugify(handle)}",
            "name": handle,
            "github": handle,
            "handle_is_verified": True,
            "reports_on_lab_list": 0,
            "role": ("Project contributor"
                     if tax.is_project_contributor(handle, [])
                     else "TEST lab member"),
            "active": True,
            "evidence": [_evidence(
                lab_members.PEOPLE_URL,
                f"{handle} is linked as a member on the TEST lab's people page.",
                None, timestamp)],
        })

    return _merge_by_handle(records)


def _default_fetcher() -> Fetcher:
    from ..cache import Caches

    return Fetcher(Caches().http, min_interval=0.5, max_retries=2, timeout=20.0)


def _merge_by_handle(records: Sequence[dict]) -> List[dict]:
    """Fold together entries that resolved to the same person.

    The lab's list spells the same reporter more than one way -- a handle in one
    entry and a display name in another -- and both resolve to one GitHub
    account. Left apart they would be two roster entries with the same id, and
    the same person searched twice.
    """
    merged: Dict[str, dict] = {}
    out: List[dict] = []
    for record in records:
        key = record["id"]
        first = merged.get(key)
        if first is None:
            merged[key] = record
            out.append(record)
            continue
        first["reports_on_lab_list"] += record["reports_on_lab_list"]
        first["handle_is_verified"] = (first["handle_is_verified"]
                                       or record["handle_is_verified"])
        aliases = set(first.get("also_recorded_as") or [])
        aliases.add(record["name"])
        aliases.discard(first["name"])
        first["also_recorded_as"] = sorted(aliases)
        seen = {item["source_url"] for item in first["evidence"]}
        first["evidence"] += [item for item in record["evidence"]
                              if item["source_url"] not in seen]
    return out


def roster() -> List[dict]:
    """The committed roster."""
    path = config.DATA_FILES.get("people")
    if not path or not path.exists():
        return []
    return config.load_json(path).get("people", [])


def handles(gh: Optional[GitHub] = None,
            fetcher: Optional[Fetcher] = None) -> List[str]:
    """Every handle worth searching: the roster, plus the derived sources.

    The roster is the durable part; ``lab_members`` still contributes the people
    page and the taxonomy's own contributor list, which name people who have not
    filed anything on the lab's list yet.
    """
    from ..util import dedupe_preserving_order

    found = [entry["github"] for entry in roster()
             if entry.get("github") and entry.get("active", True)]
    if gh is not None:
        try:
            found += lab_members.handles(gh, fetcher)
        except Exception:
            pass
    return dedupe_preserving_order(found)


def identities(gh: Optional[GitHub] = None,
               fetcher: Optional[Fetcher] = None) -> List[str]:
    """Every name a roster member's reports might be filed under.

    Handles alone are not enough to say who found a bug. A GitHub issue records
    a handle, but the SQLite forum and a Jira record a person's display name,
    and the lab's own list uses both -- so matching on handles marked 220 of
    SQLite's bugs as ours and the remaining 22, filed on the forum by the same
    people, as somebody else's.
    """
    from ..util import dedupe_preserving_order

    found: List[str] = []
    for entry in roster():
        if not entry.get("active", True):
            continue
        found.append(entry.get("github") or "")
        found.append(entry.get("name") or "")
        found.extend(entry.get("also_recorded_as") or [])
    found += handles(gh, fetcher)
    return dedupe_preserving_order([name for name in found if name])


DECISIONS_FILE = "people_decisions.json"


def decisions() -> dict:
    """Human rulings the roster cannot make for itself.

    Being on the lab's people page is evidence of being in the lab, which is
    not the same question as belonging to the SQLancer project: a lab lists
    everyone working in it, including people whose work is something else
    entirely. Only someone who knows the work can tell the two apart, so the
    answer is recorded rather than inferred.
    """
    from .. import config

    path = config.DATA_DIR / DECISIONS_FILE
    if not path.exists():
        return {"counts_as_external": []}
    try:
        return config.load_json(path)
    except ValueError:
        return {"counts_as_external": []}


def counted_as_external(ruling: Optional[dict] = None) -> Set[str]:
    """Every name whose reports a ruling puts outside the project."""
    ruling = decisions() if ruling is None else ruling
    names: Set[str] = set()
    for entry in ruling.get("counts_as_external") or []:
        for name in [entry.get("github")] + list(entry.get("also_recorded_as") or []):
            if name:
                names.add(name.strip().lower())
    return names


def search_queries(people: Sequence[str]) -> List[str]:
    """One unqualified issue search per person.

    Deliberately without a search term. ``lab_members.search_queries`` asks for
    a person's issues *that mention SQLancer*, which cannot reach a report whose
    text never names it -- and those are most of them, because a bug report is
    written about the bug. Every issue returned here still has to earn its
    attribution afterwards.
    """
    return [f"author:{handle} type:issue" for handle in people if handle]
