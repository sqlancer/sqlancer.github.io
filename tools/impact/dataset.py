"""Loading, merging and writing the authoritative impact records.

The merge rules here are what make the weekly run idempotent. A collector
produces candidate records; ``Dataset.merge`` decides, per record, whether it is
new, whether it genuinely changed, or whether it is the same record seen again.
Fields that would otherwise churn on every run -- ``last_verified`` above all --
are only refreshed when something else about the record actually changed, so a
run with no new evidence rewrites no bytes and opens no pull request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import config
from .util import normalize_url


# Record collections keyed by the list field inside each data file.
LIST_FIELD = {
    "bugs": "bugs",
    "papers": "papers",
    "adoption": "adoption",
    "resources": "resources",
    "dbms": "dbms",
    "people": "people",
}

# Sort keys giving each file a stable, human-meaningful order.
SORT_KEY = {
    "bugs": lambda r: (r.get("dbms") or "", r.get("reported_date") or "",
                       r.get("title") or "", r["id"]),
    "papers": lambda r: (-(r.get("year") or 0), (r.get("title") or "").lower(), r["id"]),
    "adoption": lambda r: (r.get("dbms") or "", r.get("relationship") or "", r["id"]),
    "resources": lambda r: (-(r.get("year") or 0), (r.get("title") or "").lower(), r["id"]),
    "people": lambda r: (-(r.get("reports_on_lab_list") or 0),
                         (r.get("name") or "").lower(), r["id"]),
    "dbms": lambda r: r["id"],
}

# Fields that describe when a record was last looked at rather than what it
# says. Differences here alone never count as a change.
_VOLATILE = {"last_verified", "retrieved_at", "stored_at", "inspected_at",
             "classified_at", "fetched_at"}


def _strip_volatile(value: Any) -> Any:
    """Deep copy with bookkeeping timestamps removed, for change detection."""
    if isinstance(value, dict):
        return {k: _strip_volatile(v) for k, v in sorted(value.items())
                if k not in _VOLATILE}
    if isinstance(value, list):
        return [_strip_volatile(v) for v in value]
    return value


def substantively_equal(left: dict, right: dict) -> bool:
    return _strip_volatile(left) == _strip_volatile(right)


def _touch_last_verified(record: Any, timestamp: str) -> None:
    """Refresh every ``last_verified`` inside a record that did change."""
    if isinstance(record, dict):
        for key, value in record.items():
            if key == "last_verified" and isinstance(value, str):
                record[key] = timestamp
            else:
                _touch_last_verified(value, timestamp)
    elif isinstance(record, list):
        for item in record:
            _touch_last_verified(item, timestamp)


def _preserve_first_seen(new: Any, old: Any) -> None:
    """Carry ``first_seen`` over from the stored record.

    When a record is updated, the date it entered the dataset must not move.
    """
    if isinstance(new, dict) and isinstance(old, dict):
        if "first_seen" in new and isinstance(old.get("first_seen"), str):
            new["first_seen"] = old["first_seen"]
        for key, value in new.items():
            if key in old:
                _preserve_first_seen(value, old[key])
    elif isinstance(new, list) and isinstance(old, list):
        # Evidence lists are matched by source_url so re-ordering is harmless.
        by_url = {}
        for item in old:
            if isinstance(item, dict) and item.get("source_url"):
                by_url[normalize_url(item["source_url"])] = item
        for item in new:
            if isinstance(item, dict) and item.get("source_url"):
                counterpart = by_url.get(normalize_url(item["source_url"]))
                if counterpart is not None:
                    _preserve_first_seen(item, counterpart)


@dataclass
class MergeReport:
    """What a merge changed, used to build the pull-request description."""

    added: List[dict] = field(default_factory=list)
    updated: List[Tuple[dict, dict]] = field(default_factory=list)
    unchanged: int = 0
    rejected: List[dict] = field(default_factory=list)
    needs_review: List[dict] = field(default_factory=list)
    removed: List[dict] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)

    def extend(self, other: "MergeReport") -> None:
        self.added.extend(other.added)
        self.updated.extend(other.updated)
        self.unchanged += other.unchanged
        self.rejected.extend(other.rejected)
        self.needs_review.extend(other.needs_review)
        self.removed.extend(other.removed)


class Dataset:
    """In-memory view of every authoritative file, with change tracking."""

    def __init__(self, files: Optional[Dict[str, Any]] = None):
        self.files = dict(files or config.DATA_FILES)
        self.data: Dict[str, dict] = {}
        for name, path in self.files.items():
            if path.exists():
                self.data[name] = config.load_json(path)
        self.reports: Dict[str, MergeReport] = {}

    # -- accessors --------------------------------------------------------

    def records(self, name: str) -> List[dict]:
        payload = self.data.get(name)
        if payload is None:
            return []
        return payload.get(LIST_FIELD[name], [])

    def index(self, name: str) -> Dict[str, dict]:
        return {record["id"]: record for record in self.records(name)}

    def url_index(self, name: str, url_field: str = "primary_url") -> Dict[str, dict]:
        """Secondary index on a canonical URL, used to catch id-scheme drift.

        URLs shared by more than one record are omitted: an upstream ticket can
        cover two separately curated reports, and matching on it would silently
        merge them.
        """
        seen: Dict[str, List[dict]] = {}
        for record in self.records(name):
            url = normalize_url(record.get(url_field))
            if url:
                seen.setdefault(url, []).append(record)
        return {url: records[0] for url, records in seen.items() if len(records) == 1}

    def ensure(self, name: str, header: Dict[str, Any]) -> dict:
        payload = self.data.get(name)
        if payload is None:
            payload = dict(header)
            payload[LIST_FIELD[name]] = []
            self.data[name] = payload
        return payload

    # -- merging ----------------------------------------------------------

    def merge(self, name: str, candidates: Iterable[dict], *, timestamp: str,
              rejected: Optional[Iterable[dict]] = None,
              needs_review: Optional[Iterable[dict]] = None,
              authoritative: bool = False) -> MergeReport:
        """Fold ``candidates`` into the stored records for ``name``.

        Deduplication is by stable id first and by canonical URL second, so a
        record keeps its identity even if the id scheme is ever revised.

        With ``authoritative``, ``candidates`` is the complete set the source
        now yields, and stored records missing from it are removed. Only pass it
        for a collector that re-derives everything, such as a full paper
        reconciliation -- an incremental run has not looked at the records it
        would be deleting. Removals are reported, never silent, because a record
        leaving the dataset is exactly as reviewable as one arriving.
        """
        report = MergeReport(rejected=list(rejected or []),
                             needs_review=list(needs_review or []))
        candidates_list = list(candidates)
        seen_ids = set()
        payload = self.data.get(name)
        if payload is None:
            raise KeyError(f"no data file loaded for {name!r}")
        records = payload.setdefault(LIST_FIELD[name], [])
        by_id = {record["id"]: record for record in records}
        by_url = self.url_index(name)

        for candidate in candidates_list:
            existing = by_id.get(candidate["id"])
            if existing is None:
                url = normalize_url(candidate.get("primary_url"))
                if url:
                    # pop, so two candidates sharing a URL cannot both claim
                    # the same stored record.
                    existing = by_url.pop(url, None)
            if existing is None:
                report.added.append(candidate)
                records.append(candidate)
                by_id[candidate["id"]] = candidate
                seen_ids.add(candidate["id"])
                continue

            seen_ids.add(existing["id"])
            merged = dict(candidate)
            merged["id"] = existing["id"]  # never renumber a published record
            _preserve_first_seen(merged, existing)
            if substantively_equal(merged, existing):
                report.unchanged += 1
                continue
            _touch_last_verified(merged, timestamp)
            before = dict(existing)
            existing.clear()
            existing.update(merged)
            report.updated.append((before, existing))

        # An empty candidate list under `authoritative` almost always means the
        # collector failed rather than that everything genuinely went away, so
        # it never deletes the dataset.
        if authoritative and candidates_list:
            keep = {record["id"] for record in candidates_list}
            survivors = []
            for record in records:
                if record["id"] in keep or record["id"] in seen_ids:
                    survivors.append(record)
                else:
                    report.removed.append(record)
            if report.removed:
                records[:] = survivors

        records.sort(key=SORT_KEY[name])
        previous = self.reports.get(name)
        if previous is not None:
            previous.extend(report)
            self.reports[name] = previous
        else:
            self.reports[name] = report
        return report

    # -- persistence ------------------------------------------------------

    def save(self, names: Optional[Iterable[str]] = None) -> Dict[str, bool]:
        """Write files back; returns which ones changed on disk."""
        changed: Dict[str, bool] = {}
        for name in (names or self.data):
            payload = self.data.get(name)
            if payload is None:
                continue
            changed[name] = config.write_json(self.files[name], payload)
        return changed
