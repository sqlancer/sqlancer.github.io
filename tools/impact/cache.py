"""The three cache layers the pipeline relies on.

Caching here is strictly an optimisation. Every collector must produce the same
authoritative records with a cold cache as with a warm one; the tests assert
that. What the cache buys is that a weekly run does not refetch unchanged pages
and, more importantly, does not spend tokens reclassifying unchanged evidence.

Layers:

1. ``RawCache``      - fetched source material plus the validators (ETag,
                       Last-Modified, upstream ``updated_at``, content hash)
                       needed to decide whether it changed.
2. ``DerivedCache``  - deterministic extraction results, keyed by a stable
                       source id plus the hash of the source content.
3. ``ClassificationCache`` - answers from the semantic classifiers, keyed so
                       that a change to the evidence, the policy, the taxonomy,
                       the prompt version or the model forces a re-ask.

Entries are plain JSON files under ``.cache/impact`` so they diff readably and
can be committed where they are useful as reproducible collector state.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import config
from .util import now, sha256_hex


def _key_path(root: Path, key: str, suffix: str = ".json") -> Path:
    """Shard entries into 256 buckets so directories stay listable."""
    digest = sha256_hex(key)
    return root / digest[:2] / (digest + suffix)


class JsonStore:
    """Content-addressed JSON store with a human-readable key recorded inside."""

    def __init__(self, root: Path):
        self.root = root
        self.hits = 0
        self.misses = 0
        self.writes = 0

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        path = _key_path(self.root, key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                entry = json.load(handle)
        except (OSError, ValueError):
            self.misses += 1
            return None
        if entry.get("key") != key:  # digest collision or stale layout
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def put(self, key: str, value: Dict[str, Any]) -> None:
        path = _key_path(self.root, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"key": key, "stored_at": now(), "value": value}
        text = json.dumps(entry, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        if path.exists():
            with open(path, "r", encoding="utf-8") as handle:
                if handle.read() == text:
                    return
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
        self.writes += 1

    def stats(self) -> Dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "writes": self.writes}


class RawCache(JsonStore):
    """Layer 1: fetched source material and its change validators."""

    def store(self, key: str, *, body: str, url: str, status: int = 200,
              etag: Optional[str] = None, last_modified: Optional[str] = None,
              upstream_updated_at: Optional[str] = None,
              extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        value = {
            "url": url,
            "status": status,
            "body": body,
            "content_sha256": "sha256:" + sha256_hex(body),
            "etag": etag,
            "last_modified": last_modified,
            "upstream_updated_at": upstream_updated_at,
            "fetched_at": now(),
        }
        if extra:
            value.update(extra)
        self.put(key, value)
        return value

    def is_fresh(self, key: str, *, upstream_updated_at: Optional[str] = None,
                 max_age_days: Optional[int] = None) -> bool:
        """Whether the cached copy can be reused without contacting the source.

        An upstream modification timestamp, when the API offers one, is the
        cheapest possible check: no request at all is needed if it is unchanged.
        """
        entry = self.get(key)
        if entry is None:
            return False
        value = entry["value"]
        if upstream_updated_at is not None:
            return value.get("upstream_updated_at") == upstream_updated_at
        if max_age_days is None:
            return True
        fetched = value.get("fetched_at")
        if not fetched:
            return False
        age = time.time() - time.mktime(time.strptime(fetched, "%Y-%m-%dT%H:%M:%SZ"))
        return age < max_age_days * 86400


class DerivedCache(JsonStore):
    """Layer 2: deterministic extraction keyed by source id + content hash."""

    def compute(self, source_id: str, source_hash: str, extractor_version: str,
                producer: Callable[[], Any]) -> Any:
        key = f"{source_id}|{source_hash}|{extractor_version}"
        entry = self.get(key)
        if entry is not None:
            return entry["value"]["result"]
        result = producer()
        self.put(key, {"source_id": source_id, "source_hash": source_hash,
                       "extractor_version": extractor_version, "result": result})
        return result


class ClassificationCache(JsonStore):
    """Layer 3: semantic classification answers, including negatives.

    Negative and uncertain answers are cached exactly like positive ones. A
    paper that turned out to only ``reference`` SQLancer, or an issue that
    turned out to be unrelated, must not consume tokens again next week.
    """

    def __init__(self, root: Path, *, model: str, policy_version: str,
                 taxonomy_version: str):
        super().__init__(root)
        self.model = model
        self.policy_version = policy_version
        self.taxonomy_version = taxonomy_version

    def key(self, source_id: str, evidence_hash: str, classifier_version: str) -> str:
        return "|".join([
            source_id,
            evidence_hash,
            classifier_version,
            self.policy_version,
            self.taxonomy_version,
            self.model,
        ])

    def lookup(self, source_id: str, evidence_hash: str,
               classifier_version: str) -> Optional[Dict[str, Any]]:
        entry = self.get(self.key(source_id, evidence_hash, classifier_version))
        return entry["value"] if entry else None

    def store(self, source_id: str, evidence_hash: str, classifier_version: str,
              result: Dict[str, Any]) -> Dict[str, Any]:
        value = {
            "source_id": source_id,
            "source_hash": evidence_hash,
            "classifier_version": classifier_version,
            "policy_version": self.policy_version,
            "taxonomy_version": self.taxonomy_version,
            "model": self.model,
            "classified_at": now(),
            "result": result,
        }
        self.put(self.key(source_id, evidence_hash, classifier_version), value)
        return value


class Caches:
    """Convenience bundle wiring all layers to the configured cache root."""

    def __init__(self, root: Optional[Path] = None, *, model: Optional[str] = None,
                 policy_version: Optional[str] = None,
                 taxonomy_version: Optional[str] = None):
        root = Path(root) if root else config.CACHE_DIR
        self.root = root
        self.http = RawCache(root / "http")
        self.github = RawCache(root / "github")
        self.papers = RawCache(root / "papers")
        self.artifacts = RawCache(root / "artifacts")
        self.derived = DerivedCache(root / "derived")
        self.classifications = ClassificationCache(
            root / "classifications",
            model=model or config.DEFAULT_MODEL,
            policy_version=policy_version or config.policy_version(),
            taxonomy_version=taxonomy_version or config.taxonomy_version(),
        )

    def summary(self) -> Dict[str, Dict[str, int]]:
        return {
            "http": self.http.stats(),
            "github": self.github.stats(),
            "papers": self.papers.stats(),
            "artifacts": self.artifacts.stats(),
            "derived": self.derived.stats(),
            "classifications": self.classifications.stats(),
        }


class State:
    """Persistent per-source discovery state driving incremental scans."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else config.STATE_FILE
        self.data: Dict[str, Any] = {}
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    self.data = json.load(handle)
            except (OSError, ValueError):
                self.data = {}

    def last_scan(self, source: str) -> Optional[str]:
        return self.data.get(source, {}).get("last_successful_scan")

    def last_reconcile(self, source: str) -> Optional[str]:
        return self.data.get(source, {}).get("last_full_reconciliation")

    def record_scan(self, source: str, *, full: bool = False) -> None:
        entry = self.data.setdefault(source, {})
        entry["last_successful_scan"] = now()
        if full:
            entry["last_full_reconciliation"] = now()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)
