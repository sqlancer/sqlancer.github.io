"""The queue of things a collector saw and could not place.

Every collector reports candidates it cannot admit on its own: an issue in a
repository the registry does not know, a fork under an unrecognised
organisation, a provider file for a system with no entry. Until now that list
went into the run report and nowhere else, so each run rediscovered the same
items and no one ever saw them. Three real gaps were found by hand that this
list had already flagged and lost -- MatrixOne's nine bugs, Oxla's twelve, and
a TimescaleDB provider in CnosDB's fork.

So it is kept. Entries accumulate with the date they were first and last seen,
and leave in one of three ways: the record they point at is admitted, in which
case the item is dropped automatically; a later run classifies the candidate and
turns it down, which settles it just as firmly and moves it to ``dismissed``;
or a person rules that it should never be raised again, with a reason. The
second was missing for a long time, and it mattered -- a negative answer had
nowhere to go, so 347 decided candidates sat in the queue looking exactly like
work nobody had done. An item that simply stops appearing is *not* dropped: a
run that did not reach it looks exactly like a run that resolved it.
"""

from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional, Sequence, Set

from . import config
from .util import normalize_url, now, stable_digest, truncate

FILE = "needs_review.json"
SCHEMA_VERSION = "1.0.0"

DESCRIPTION = (
    "Candidates a collector could not place, kept between runs so they can be "
    "worked through. Each entry names what was seen and why it could not be "
    "admitted. An entry leaves when the record it points at enters the dataset, "
    "or when a person dismisses it with a reason."
)


def path():
    return config.DATA_DIR / FILE


def load() -> dict:
    """The queue as stored, or an empty one."""
    if not path().exists():
        return {"schema_version": SCHEMA_VERSION, "description": DESCRIPTION,
                "open": [], "dismissed": []}
    try:
        return config.load_json(path())
    except ValueError:
        return {"schema_version": SCHEMA_VERSION, "description": DESCRIPTION,
                "open": [], "dismissed": []}


def entry_id(item: dict) -> str:
    """A stable id for one item, so the same finding is one row over time."""
    url = normalize_url(item.get("url") or "") or (item.get("url") or "")
    return f"review:{stable_digest(url + '|' + (item.get('reason') or ''))}"


def _clean(item: dict, source: str, timestamp: str) -> dict:
    return {
        "id": entry_id(item),
        "kind": item.get("kind") or "unknown",
        "source": source,
        "url": item.get("url") or None,
        "title": truncate(item.get("title") or "", 300) or None,
        "reason": truncate(item.get("reason") or "", 500),
        "first_seen": timestamp,
        "last_seen": timestamp,
    }


def merge(queue: dict, items: Sequence[dict], *, source: str,
          timestamp: Optional[str] = None,
          admitted: Optional[Set[str]] = None) -> Dict[str, int]:
    """Fold this run's findings into the queue; returns what changed.

    ``admitted`` is the set of canonical URLs now in the dataset. An item
    pointing at one of them has been resolved by being collected, which is the
    only automatic way out.
    """
    timestamp = timestamp or now()
    admitted = admitted or set()
    dismissed = {row["id"] for row in queue.get("dismissed") or []}
    by_id = {row["id"]: row for row in queue.get("open") or []}

    counts = {"added": 0, "seen_again": 0, "resolved": 0}
    for item in items:
        row = _clean(item, source, timestamp)
        if row["id"] in dismissed:
            continue
        existing = by_id.get(row["id"])
        if existing is None:
            by_id[row["id"]] = row
            counts["added"] += 1
        else:
            existing["last_seen"] = timestamp
            existing["reason"] = row["reason"]
            existing["title"] = row["title"] or existing.get("title")
            counts["seen_again"] += 1

    for key, row in list(by_id.items()):
        canonical = normalize_url(row.get("url") or "")
        if canonical and canonical in admitted:
            del by_id[key]
            counts["resolved"] += 1

    queue["schema_version"] = SCHEMA_VERSION
    queue["description"] = DESCRIPTION
    queue["open"] = sorted(by_id.values(),
                           key=lambda row: (row["source"], row["id"]))
    queue.setdefault("dismissed", [])
    return counts


def save(queue: dict) -> bool:
    return config.write_json(path(), queue)


def summary(queue: dict) -> Dict[str, int]:
    """How many open items each collector is waiting on."""
    counts: Dict[str, int] = {}
    for row in queue.get("open") or []:
        counts[row["source"]] = counts.get(row["source"], 0) + 1
    return counts


def repository_of(url: Optional[str]) -> Optional[str]:
    """``owner/name`` for a GitHub URL, which is what a person decides about."""
    import re

    match = re.match(r"https?://github\.com/([^/]+/[^/]+)", url or "")
    return match.group(1) if match else None


def dismiss(queue: dict, ids: Iterable[str], reason: str, *,
            decided_by: Optional[str] = None,
            timestamp: Optional[str] = None) -> int:
    """Move items out of the queue for good, with the reason recorded."""
    timestamp = timestamp or now()
    wanted = set(ids)
    kept, moved = [], []
    for row in queue.get("open") or []:
        if row["id"] in wanted:
            moved.append({"id": row["id"], "url": row.get("url"),
                          "reason": reason, "decided_by": decided_by,
                          "decided_on": timestamp[:10]})
        else:
            kept.append(row)
    queue["open"] = kept
    queue.setdefault("dismissed", []).extend(moved)
    queue["dismissed"].sort(key=lambda row: row["id"])
    return len(moved)


def main() -> int:
    """Print the queue grouped so the decisions in it are visible.

    Grouped by reason and then by repository: 287 items across 52
    repositories is a list nobody reads, while 52 repositories with counts is
    a morning's work.
    """
    queue = load()
    rows = queue.get("open") or []
    print(f"{len(rows)} open, {len(queue.get('dismissed') or [])} dismissed")

    by_reason: Dict[str, List[dict]] = {}
    for row in rows:
        # Reasons name a specific repository or provider; group on the shape.
        import re

        key = re.sub(r"'[^']*'", "'...'", row["reason"])
        by_reason.setdefault(key, []).append(row)

    for reason in sorted(by_reason, key=lambda k: -len(by_reason[k])):
        group = by_reason[reason]
        print(f"\n{len(group)}  {reason}")
        repos: Dict[str, int] = {}
        loose: List[dict] = []
        for row in group:
            name = repository_of(row.get("url"))
            if name:
                repos[name] = repos.get(name, 0) + 1
            else:
                loose.append(row)
        for name, count in sorted(repos.items(), key=lambda kv: -kv[1])[:30]:
            print(f"     {count:>4}  {name}")
        if len(repos) > 30:
            print(f"     ... and {len(repos) - 30} more repositories")
        for row in loose[:10]:
            print(f"          {row.get('url') or row.get('title') or row['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
