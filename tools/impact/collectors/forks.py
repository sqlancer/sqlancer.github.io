"""Reads the fork list of the SQLancer repository.

Forks are the one place GitHub's code search cannot reach -- it excludes them
entirely -- so a database system that maintains its own SQLancer fork is
invisible to every other collector here. Several do, which makes this the
highest-yield adoption source of the lot.

A fork under a project's own organisation is a deliberate act by that project's
team, so it is admitted as adoption evidence. Whether the project went on to
modify SQLancer is checked rather than assumed: the fork is compared against
upstream, and the number of commits it is ahead by is recorded as part of the
evidence.

Forks also feed the artifact matching used for papers, since a renamed fork is
often a research artifact.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .. import config
from ..github import GitHub
from ..util import now, truncate

COLLECTOR = "forks"
COLLECTOR_VERSION = "1.0.0"

UPSTREAM_OWNER, UPSTREAM_REPO = "sqlancer", "sqlancer"
FORKS_URL = f"https://github.com/{UPSTREAM_OWNER}/{UPSTREAM_REPO}/forks"

MAX_PAGES = 8


def list_forks(gh: GitHub, *, max_age_days: int = 3) -> List[dict]:
    """Every fork of the SQLancer repository."""
    forks: List[dict] = []
    for page in range(1, MAX_PAGES + 1):
        data, _ = gh.get(f"/repos/{UPSTREAM_OWNER}/{UPSTREAM_REPO}/forks",
                         params={"per_page": 100, "page": page, "sort": "oldest"},
                         max_age_days=max_age_days)
        if not data:
            break
        forks.extend(data)
        if len(data) < 100:
            break
    return forks


def _owner_index() -> Dict[str, dict]:
    """Map every GitHub organisation a registered DBMS operates under to it."""
    registry = config.load_json(config.DATA_FILES["dbms"])["dbms"]
    index: Dict[str, dict] = {}
    for entry in registry:
        owners = list(entry.get("github_owners") or [])
        repository = entry.get("repository")
        if repository:
            parts = repository.rstrip("/").split("/")
            if len(parts) >= 2:
                owners.append(parts[-2])
        for owner in owners:
            index[owner.lower()] = entry
    return index


def commits_ahead(gh: GitHub, owner: str, repo: str,
                  branch: Optional[str]) -> Optional[int]:
    """How many commits a fork is ahead of upstream, or None if unknown.

    A fork nobody touched is a bookmark; one with commits on top is a project
    doing something with SQLancer. The distinction belongs in the evidence.
    """
    if not branch:
        return None
    data, _ = gh.get(
        f"/repos/{UPSTREAM_OWNER}/{UPSTREAM_REPO}/compare/"
        f"{UPSTREAM_OWNER}:main...{owner}:{branch}",
        max_age_days=14)
    if not isinstance(data, dict):
        return None
    ahead = data.get("ahead_by")
    return ahead if isinstance(ahead, int) else None


def adoption_records(gh: GitHub, forks: Optional[List[dict]] = None, *,
                     timestamp: Optional[str] = None
                     ) -> Tuple[List[dict], List[dict]]:
    """Return ``(records, needs_review)`` for forks under a DBMS project's org.

    Forks under an organisation this registry does not know are reported rather
    than guessed at: several are database systems SQLancer has no provider for,
    and adding one is a registry decision, not something to infer from a fork.
    """
    timestamp = timestamp or now()
    forks = list_forks(gh) if forks is None else forks
    owners = _owner_index()

    records: List[dict] = []
    needs_review: List[dict] = []

    for fork in forks:
        login = ((fork.get("owner") or {}).get("login") or "")
        entry = owners.get(login.lower())
        full_name = fork.get("full_name") or ""
        url = fork.get("html_url") or f"https://github.com/{full_name}"

        if entry is None:
            if (fork.get("owner") or {}).get("type") == "Organization":
                needs_review.append({
                    "kind": "adoption",
                    "url": url,
                    "reason": (f"organisation {login!r} maintains a SQLancer "
                               f"fork but is not a registered database system"),
                })
            continue

        owner, repo = full_name.split("/", 1)
        ahead = commits_ahead(gh, owner, repo, fork.get("default_branch"))
        pushed = (fork.get("pushed_at") or "")[:10]

        detail = f"The {entry['name']} project maintains its own SQLancer fork"
        if ahead:
            detail += f", {ahead} commit(s) ahead of upstream"
        if pushed:
            detail += f", last updated {pushed}"
        detail += "."

        records.append({
            "id": f"adoption:{entry['id']}:official_testing",
            "dbms": entry["id"],
            "relationship": "official_testing",
            "finder": "sqlancer",
            "summary": truncate(detail, 600),
            "since_year": None,
            "active": not fork.get("archived", False),
            "evidence": [{
                "source_url": url,
                "source_type": "github_repository",
                "excerpt": None,
                "note": truncate(
                    f"{full_name} is a fork of {UPSTREAM_OWNER}/"
                    f"{UPSTREAM_REPO} under the {entry['name']} project's own "
                    f"GitHub organisation"
                    + (f", {ahead} commit(s) ahead of upstream" if ahead else "")
                    + ".", 500),
                "retrieved_at": timestamp,
                "first_seen": timestamp,
                "last_verified": timestamp,
            }],
            "provenance": {
                "collector": COLLECTOR,
                "collector_version": COLLECTOR_VERSION,
                "policy_version": config.policy_version(),
                "first_seen": timestamp,
                "last_verified": timestamp,
                "source_url": FORKS_URL,
                "source_type": "github_repository",
            },
        })
        if entry.get("repository"):
            records[-1]["project_repository"] = entry["repository"]

    return records, needs_review


def artifact_candidates(forks: Optional[List[dict]] = None,
                        gh: Optional[GitHub] = None) -> List[str]:
    """Forks worth inspecting as research artifacts.

    A fork still called ``sqlancer`` is almost always a working copy; one that
    was renamed usually became something with a name of its own. Renaming is the
    cheap signal that separates the two, and it keeps this from returning all
    several hundred forks.
    """
    if forks is None:
        forks = list_forks(gh) if gh is not None else []
    candidates = []
    for fork in forks:
        name = (fork.get("name") or "").lower()
        full_name = fork.get("full_name") or ""
        if not full_name:
            continue
        if name not in ("sqlancer", "sqlancer-sqlancer"):
            candidates.append(full_name)
    return candidates
