"""Discovers bug reports written by the people who run SQLancer campaigns.

Searching by term has a ceiling: GitHub caps a search at 1000 results, and
"SQLancer" appears in far more issues than that. Searching by author does not
hit the same wall, and it reaches reports the term queries never returned.

The people who file these reports are the TEST lab, whose site lists its members
and whose bug list records the handles that filed each entry. Both are read
here, and every candidate then goes through exactly the same attribution rules
as any other GitHub discovery -- being a lab member's issue is a reason to look
at it, never a reason to count it.
"""

from __future__ import annotations

import json
import re
from typing import List, Optional, Sequence, Tuple

from ..github import GitHub
from ..http import Fetcher
from ..util import dedupe_preserving_order

COLLECTOR = "lab_members"
COLLECTOR_VERSION = "1.0.0"

PEOPLE_URL = "https://nus-test.github.io/people/"
BUGS_OWNER, BUGS_REPO, BUGS_REF = "nus-test", "nus-test.github.io", "main"
BUGS_PATH = "data/bugs.json"

GITHUB_HANDLE = re.compile(
    r"github\.com/([A-Za-z0-9][\w-]{1,38})(?![\w/-])", re.IGNORECASE)

# Accounts that appear on the page but are not people filing bug reports.
NOT_A_MEMBER = {
    "sqlancer", "nus-test", "github", "orgs", "features", "about", "pricing",
    "sponsors", "readme", "site", "security", "apps", "marketplace", "explore",
    "settings", "login", "join", "topics", "collections", "blog",
}


def from_people_page(fetcher: Fetcher) -> List[str]:
    """GitHub handles linked from the lab's people page."""
    try:
        entry, _ = fetcher.fetch(PEOPLE_URL, max_age_days=14)
    except Exception:
        return []
    if entry.get("status") != 200:
        return []
    handles = [match.group(1) for match in GITHUB_HANDLE.finditer(entry["body"])]
    return [h for h in dedupe_preserving_order(handles)
            if h.lower() not in NOT_A_MEMBER]


def from_bug_list(gh: GitHub) -> List[str]:
    """Handles that filed entries on the lab's bug list.

    Complements the people page: it names people who have left, and the page
    names people who have not filed anything yet.
    """
    raw, _ = gh.raw_file(BUGS_OWNER, BUGS_REPO, BUGS_PATH, BUGS_REF,
                         max_age_days=7)
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except ValueError:
        return []
    handles = []
    for entry in entries:
        reporter = (entry.get("reported_by") or "").strip()
        # The field holds a GitHub handle for some entries and a display name
        # for others; only a handle is usable as a search qualifier.
        if reporter and " " not in reporter and reporter.lower() not in NOT_A_MEMBER:
            handles.append(reporter)
    return dedupe_preserving_order(handles)


def handles(gh: GitHub, fetcher: Optional[Fetcher] = None) -> List[str]:
    """Every handle worth searching, from both sources."""
    if fetcher is None:
        from ..cache import Caches
        fetcher = Fetcher(Caches().http, min_interval=0.5, max_retries=2,
                          timeout=20.0)
    # The taxonomy's own contributor roster belongs here too: a Google Summer
    # of Code contributor files bugs from SQLancer campaigns just as a lab
    # member does, and their reports are found the same way.
    from .. import taxonomy

    roster = [entry.get("github") for entry
              in taxonomy.load().project_contributors if entry.get("github")]
    found = from_people_page(fetcher) + from_bug_list(gh) + roster
    return dedupe_preserving_order(found)


def search_queries(members: Sequence[str], terms: Sequence[str]) -> List[str]:
    """One issue search per member, covering every discovery term at once.

    A single OR query per person keeps this to one search each rather than one
    per person per term, which matters against a 30-searches-per-minute limit.
    """
    joined = " OR ".join(sorted({term for term in terms if term}))
    return [f"author:{handle} ({joined}) type:issue" for handle in members]
