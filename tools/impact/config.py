"""Paths, versions and shared constants for the impact pipeline."""

from __future__ import annotations

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = REPO_ROOT / "_data" / "impact"
SCHEMA_DIR = REPO_ROOT / "schemas" / "impact"
CACHE_DIR = Path(os.environ.get("IMPACT_CACHE_DIR", REPO_ROOT / ".cache" / "impact"))
PLOT_DIR = REPO_ROOT / "_includes" / "impact" / "plots"

# Authoritative data files, keyed by the stem used everywhere in the pipeline.
DATA_FILES = {
    "techniques": DATA_DIR / "techniques.json",
    "dbms": DATA_DIR / "dbms.json",
    "bugs": DATA_DIR / "bugs.json",
    "papers": DATA_DIR / "papers.json",
    "adoption": DATA_DIR / "adoption.json",
    "resources": DATA_DIR / "resources.json",
    "talks": DATA_DIR / "talks.json",
    "people": DATA_DIR / "people.json",
    "policy": DATA_DIR / "policy.json",
    "stats": DATA_DIR / "stats.json",
}

# Files the collectors may propose changes to. ``stats`` is generated, and
# ``policy`` changes are a deliberate human act.
COLLECTABLE = ("bugs", "papers", "adoption", "resources", "dbms", "people")

SCHEMA_FILES = {name: SCHEMA_DIR / f"{name}.schema.json" for name in DATA_FILES}

# Cache layers. Kept apart from the authoritative data on purpose: everything
# here is an optimisation and the pipeline must work with an empty cache.
CACHE_LAYERS = {
    "http": CACHE_DIR / "http",             # raw HTTP bodies + validators
    "github": CACHE_DIR / "github",         # issue/PR/repo metadata
    "papers": CACHE_DIR / "papers",         # scholarly API responses
    "artifacts": CACHE_DIR / "artifacts",   # research artifact metadata
    "derived": CACHE_DIR / "derived",       # deterministic extraction results
    "classifications": CACHE_DIR / "classifications",  # LLM answers
}

STATE_FILE = CACHE_DIR / "state.json"

# Bumping any of these invalidates the affected cached classifications.
CLASSIFIER_VERSIONS = {
    "bug_attribution": "bug-attribution-v1",
    "paper_infrastructure": "paper-infrastructure-v1",
    "paper_extends": "paper-extends-v1",
    "paper_compares": "paper-compares-v1",
    "adoption": "adoption-v1",
    "paper_summary": "paper-summary-v1",
}

DEFAULT_MODEL = os.environ.get("IMPACT_MODEL", "claude-opus-5")

USER_AGENT = (
    "sqlancer-impact-collector/1.0 "
    "(+https://github.com/sqlancer/sqlancer.github.io)"
)
CONTACT_EMAIL = os.environ.get("IMPACT_CONTACT_EMAIL", "sqlancer@googlegroups.com")

# How long a cached source is trusted before a reconciliation run re-checks it.
RECONCILE_MAX_AGE_DAYS = int(os.environ.get("IMPACT_RECONCILE_DAYS", "90"))


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload) -> bool:
    """Write ``payload`` deterministically. Returns True when bytes changed.

    Stable formatting matters: the weekly run must be idempotent, so an
    unchanged dataset has to serialise to byte-identical output.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    if path.exists():
        with open(path, "r", encoding="utf-8") as handle:
            if handle.read() == text:
                return False
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return True


def policy_version() -> str:
    return load_json(DATA_FILES["policy"])["policy_version"]


def taxonomy_version() -> str:
    return load_json(DATA_FILES["techniques"])["taxonomy_version"]
