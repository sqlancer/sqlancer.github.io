"""Where a project's issues live now, when they were filed somewhere else.

Projects move organisation. DuckDB was ``cwida/duckdb`` before it was
``duckdb/duckdb``, and GitHub redirects the old links, so both forms reach the
same issue and both look canonical to a URL normaliser. That is enough to store
one bug twice: the project's curated list keeps the address it was filed under,
an issue search finds the address it lives at now, and nothing connects them --
76 DuckDB bugs were counted twice this way.

The registry already records the organisations a project has used, because the
bug search needs them. The same knowledge settles identity: an issue URL is
rewritten to the owner the project is at today, before anything derives a record
id from it.
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

from . import config
from .util import normalize_url

ISSUE_URL = re.compile(
    r"^(https://github\.com/)([\w.-]+)/([\w.-]+)(/(?:issues|pull)/\d+.*)$",
    re.IGNORECASE)

_ALIASES: Optional[Dict[Tuple[str, str], str]] = None


def _aliases() -> Dict[Tuple[str, str], str]:
    """``(former owner, repo) -> current owner`` for every registered system."""
    global _ALIASES
    if _ALIASES is not None:
        return _ALIASES
    mapping: Dict[Tuple[str, str], str] = {}
    try:
        registry = config.load_json(config.DATA_FILES["dbms"])["dbms"]
    except Exception:
        registry = []
    for entry in registry:
        repository = entry.get("repository") or ""
        parts = repository.rstrip("/").split("/")
        if len(parts) < 2:
            continue
        owner, name = parts[-2], parts[-1]
        for former in entry.get("github_owners") or []:
            if former.lower() != owner.lower():
                mapping[(former.lower(), name.lower())] = owner
    _ALIASES = mapping
    return mapping


def forget_aliases() -> None:
    """Drop the cached map, for tests that change the registry."""
    global _ALIASES
    _ALIASES = None


def canonical_issue_url(url: Optional[str]) -> Optional[str]:
    """``url`` with a former organisation rewritten to the current one.

    Anything that is not a GitHub issue or pull request under a known former
    owner comes back normalised but otherwise untouched.
    """
    canonical = normalize_url(url)
    if not canonical:
        return canonical
    match = ISSUE_URL.match(canonical)
    if not match:
        return canonical
    prefix, owner, name, rest = match.groups()
    current = _aliases().get((owner.lower(), name.lower()))
    if not current or current.lower() == owner.lower():
        return canonical
    return f"{prefix}{current}/{name}{rest}"
