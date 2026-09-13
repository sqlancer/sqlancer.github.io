"""Finds evidence that a database system's own developers use SQLancer.

The distinction this collector exists to protect is that SQLancer supporting a
database system is not the same as that project using SQLancer. So the search is
scoped to each registered project's own repository, and the strongest evidence
is the most objective: a CI workflow or test script, inside the project's
repository, that invokes SQLancer. That is accepted deterministically, with the
matching line quoted.

Softer evidence -- documentation, an issue or pull request describing use --
goes to the classifier, whose job is to separate "our developers run this" from
"someone tested us with this".
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from .. import config, taxonomy
from ..classify import Classifier, finder_names, resolve_finder
from ..github import GitHub
from ..util import content_hash, now, truncate, verbatim_excerpt

COLLECTOR = "adoption"
COLLECTOR_VERSION = "1.0.0"

# Paths whose presence in a project's own repository means the project runs it.
CI_PATH = re.compile(r"(^|/)\.github/workflows/|(^|/)\.circleci/|(^|/)\.gitlab-ci",
                     re.IGNORECASE)
TEST_PATH = re.compile(r"(^|/)(test|tests|testing|scripts|ci|tools)/",
                       re.IGNORECASE)
DOC_PATH = re.compile(r"\.(md|rst|txt|adoc)$", re.IGNORECASE)

# A line that actually runs or configures SQLancer. Deliberately does not match
# a bare repository link: a URL in a README is a mention, not an invocation.
INVOCATION = re.compile(
    r"(sqlancer[\w.-]*\.jar|java\s+-jar[^\n]{0,80}sqlancer|"
    r"gradlew?[^\n]{0,40}sqlancer|mvn[^\n]{0,60}sqlancer|"
    r"git\s+clone[^\n]{0,80}sqlancer|"
    r"docker[^\n]{0,80}sqlancer|"
    r"(?:^|\s)(?:run_|create_)?sqlancer\s*\(|"
    r"import\s+[^\n]{0,60}sqlancer|from\s+[^\n]{0,60}sqlancer\s+import|"
    r"uses:\s*[^\n]*sqlancer)",
    re.IGNORECASE)

# A sentence in which the project says, in prose, that it uses SQLancer. This is
# what a contributor guide looks like, and it is better evidence than a link.
# `(?:[^.\n]|\n(?!\n))` lets a sentence wrap across a single line break, which
# is how prose is written in Markdown, without running into the next paragraph.
_SENT = r"(?:[^.\n]|\n(?!\n))"
USAGE_STATEMENT = re.compile(
    rf"{_SENT}{{0,160}}\b(?:uses?|used|using|runs?|running|execute[sd]?"
    rf"|test(?:s|ed|ing)?|fuzz(?:es|ed|ing)?|employ(?:s|ed)?)\b"
    rf"{_SENT}{{0,120}}\bsqlancer\b{_SENT}{{0,200}}\.",
    re.IGNORECASE)

# A directory named after the tool inside the project's own repository: the
# project keeps a SQLancer setup, which is structural rather than textual.
SQLANCER_PATH = re.compile(r"(^|/)sqlancer([/.]|$)", re.IGNORECASE)

RELATIONSHIPS = ("official_ci", "official_testing", "developer_use",
                 "integration_contributed_by_dbms_team", "planned_adoption")

# Issues in a project's own tracker proposing that it adopt SQLancer. Intent is
# not use, so these are recorded under `planned_adoption` and counted apart from
# projects actually running it -- a proposal that never lands would otherwise
# inflate the reach figure.
CURATED_ISSUES = (
    {
        "dbms": "spiceai",
        "url": "https://github.com/spiceai/spiceai/issues/2119",
        "relationship": "planned_adoption",
    },
)


# A project proposing that it adopt SQLancer: "Introduce SQLancer", "support X
# in sqlancer", "add SQLancer to our CI". Written by the project's own people in
# the project's own tracker, which is what makes it adoption intent rather than
# an outsider's suggestion.
PROPOSAL = re.compile(
    r"\b(?:introduce|introducing|add|adding|support|supporting|integrate|"
    r"integrating|enable|enabling|set\s+up|setting\s+up|adopt|adopting|"
    r"run|running|use|using|bring)\b[^.\n]{0,80}?\bsqlancer\b"
    r"|\bsqlancer\b[^.\n]{0,60}?\b(?:integration|support|setup|adoption)\b",
    re.IGNORECASE)

# Only the project's own people can commit it to anything. CONTRIBUTOR belongs
# here: GitHub uses it for someone with merged commits in the repository, which
# is a developer of the project -- leaving it out was rejecting proposals from
# the very engineers who would carry them out.
INSIDER = {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"}


def proposal_records(gh: GitHub, issues: Sequence[dict], *, timestamp: str,
                     repo_ids: Optional[Dict[str, str]] = None
                     ) -> Tuple[List[dict], List[dict]]:
    """Adoption proposals found in projects' own issue trackers.

    Deliberately separate from adoption itself: an open proposal is an
    intention, and counting it as use would overstate reach. Accepted only when
    the author is an owner, member or collaborator of the project -- an
    outsider suggesting SQLancer says nothing about what the project will do.
    """
    from .github_bugs import _owner_repo, _repo_index

    repo_ids = repo_ids if repo_ids is not None else _repo_index()
    registry = {entry["id"]: entry for entry in _registry()}
    records: List[dict] = []
    needs_review: List[dict] = []
    seen = set()

    for issue in issues:
        url = issue.get("html_url") or ""
        if not url or url in seen:
            continue
        owner_repo = _owner_repo(issue.get("repository_url") or url)
        dbms_id = repo_ids.get((owner_repo or "").lower())
        entry = registry.get(dbms_id) if dbms_id else None
        if entry is None:
            continue

        text = "\n\n".join(part for part in
                           [issue.get("title") or "", issue.get("body") or ""]
                           if part)
        match = PROPOSAL.search(text)
        if not match:
            continue
        seen.add(url)

        association = (issue.get("author_association") or "").upper()
        author = (issue.get("user") or {}).get("login") or "a project member"
        excerpt = truncate(match.group(0).strip(), MAX_SNIPPET)
        if not verbatim_excerpt(text, excerpt):
            continue

        if association not in INSIDER:
            needs_review.append({
                "kind": "adoption", "url": url,
                "reason": (f"proposes SQLancer adoption for {entry['name']} but "
                           f"the author is not an owner, member or collaborator"),
            })
            continue

        state = issue.get("state") or "open"
        records.append(build_record(
            entry, "planned_adoption",
            # What this relationship means -- an intention rather than
            # evidence of use -- is said once where these records are
            # presented, not repeated in every summary. The summary's job is
            # to say who proposed it and where it stands.
            summary=truncate(
                f"{author}, {association.lower()} of the {entry['name']} "
                f"project, proposed adopting SQLancer; the issue is {state}.",
                600),
            evidence=[{
                "source_url": url,
                "source_type": "github_issue",
                "excerpt": excerpt,
                "excerpt_is_verbatim": True,
                "note": (f"Issue in the {entry['name']} tracker proposing "
                         f"SQLancer, opened by {author} ({association.lower()})."),
                "content_sha256": content_hash(text),
                "retrieved_at": timestamp,
                "first_seen": timestamp,
                "last_verified": timestamp,
            }],
            timestamp=timestamp))
    return records, needs_review


# Files in a project's own repository that show it keeping SQLancer work,
# where the repository search does not reach them. DuckDB is the case this
# exists for: it keeps a regression test per SQLancer finding under
# test/issues/rigger/, twenty of them, each naming SQLancer in its header --
# which is the project maintaining testing material, not a proposal to.
CURATED_FILES: Tuple[Dict[str, str], ...] = (
    {"dbms": "duckdb", "repository": "duckdb/duckdb",
     "path": "test/issues/rigger/instr_crash.test",
     "relationship": "official_testing",
     "summary": ("The DuckDB project keeps a regression test for each SQLancer "
                 "finding under test/issues/rigger/, so the bugs it found stay "
                 "fixed.")},
)


def from_curated_files(gh: GitHub, *, timestamp: str,
                       entries=None) -> Tuple[List[dict], List[dict]]:
    """Record a project's own SQLancer material, quoting the file itself."""
    records: List[dict] = []
    needs_review: List[dict] = []
    registry = {entry["id"]: entry for entry in _registry()}

    for item in (entries if entries is not None else CURATED_FILES):
        entry = registry.get(item["dbms"])
        owner, _, repo = item["repository"].partition("/")
        text, _ = (gh.raw_file(owner, repo, item["path"], "HEAD",
                               max_age_days=30) if entry else (None, None))
        if not entry or not text:
            needs_review.append({
                "kind": "adoption", "dbms": item["dbms"],
                "url": f"https://github.com/{item['repository']}/blob/HEAD/{item['path']}",
                "reason": "curated file could not be read",
            })
            continue
        match = re.search(r"[^.\n]{0,200}\bSQLancer\b[^.\n]{0,200}", text,
                          re.IGNORECASE)
        blob = f"https://github.com/{item['repository']}/blob/HEAD/{item['path']}"
        if not match or not verbatim_excerpt(text, match.group(0).strip()):
            needs_review.append({
                "kind": "adoption", "dbms": item["dbms"], "url": blob,
                "reason": "curated file does not mention SQLancer",
            })
            continue
        records.append(build_record(
            entry, item["relationship"],
            summary=truncate(item["summary"], 600),
            evidence=[{
                "source_url": blob,
                "source_type": "github_code",
                "excerpt": truncate(match.group(0).strip(), MAX_SNIPPET),
                "excerpt_is_verbatim": True,
                "note": (f"{item['path']} in the {entry['name']} repository, "
                         f"one of the files the project keeps for this."),
                "content_sha256": content_hash(text),
                "retrieved_at": timestamp,
                "first_seen": timestamp,
                "last_verified": timestamp,
            }],
            timestamp=timestamp))
    return records, needs_review


def from_curated_issues(gh: GitHub, *, timestamp: str,
                        entries=None) -> Tuple[List[dict], List[dict]]:
    """Record proposals to adopt SQLancer, quoting the issue that made them."""
    from ..util import github_issue_ref

    records: List[dict] = []
    needs_review: List[dict] = []
    registry = {entry["id"]: entry for entry in _registry()}

    for item in (entries if entries is not None else CURATED_ISSUES):
        entry = registry.get(item["dbms"])
        reference = github_issue_ref(item["url"])
        if entry is None or reference is None:
            needs_review.append({
                "kind": "adoption", "url": item["url"],
                "reason": "curated issue does not resolve to a registered system",
            })
            continue
        owner, repo, _, number = reference
        try:
            issue, _ = gh.issue(owner, repo, number, max_age_days=30)
        except Exception:
            issue = None
        if not issue:
            needs_review.append({
                "kind": "adoption", "url": item["url"],
                "reason": "curated issue could not be fetched",
            })
            continue

        text = "\n\n".join(part for part in
                           [issue.get("title") or "", issue.get("body") or ""]
                           if part)
        match = re.search(r"[^.\n]{0,200}\bSQLancer\b[^.\n]{0,200}", text,
                          re.IGNORECASE)
        if not match or not verbatim_excerpt(text, match.group(0).strip()):
            needs_review.append({
                "kind": "adoption", "url": item["url"],
                "reason": "curated issue does not mention SQLancer",
            })
            continue
        excerpt = truncate(match.group(0).strip(), MAX_SNIPPET)
        author = (issue.get("user") or {}).get("login") or "a project member"
        state = issue.get("state") or "open"

        records.append(build_record(
            entry, item["relationship"],
            summary=truncate(
                f"{author} proposed that the {entry['name']} project adopt "
                f"SQLancer for SQL fuzz testing; the issue is {state}. This is "
                f"an intention, not evidence that the project runs SQLancer.",
                600),
            evidence=[{
                "source_url": item["url"],
                "source_type": "github_issue",
                "excerpt": excerpt,
                "excerpt_is_verbatim": True,
                "note": (f"Issue in the {entry['name']} tracker proposing "
                         f"SQLancer, opened by {author}."),
                "content_sha256": content_hash(text),
                "retrieved_at": timestamp,
                "first_seen": timestamp,
                "last_verified": timestamp,
            }],
            timestamp=timestamp))
    return records, needs_review

MAX_SNIPPET = 700


def _registry() -> List[dict]:
    return config.load_json(config.DATA_FILES["dbms"])["dbms"]


def _owner_repo(repository: Optional[str]) -> Optional[Tuple[str, str]]:
    if not repository:
        return None
    parts = repository.rstrip("/").split("/")
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


def _relationship_for(path: str) -> Optional[str]:
    if CI_PATH.search(path):
        return "official_ci"
    if TEST_PATH.search(path):
        return "official_testing"
    if DOC_PATH.search(path):
        return "official_testing"
    return None


def _best_excerpt(text: str, path: str) -> Optional[Tuple[str, str]]:
    """Pick the strongest verbatim line, and say what kind of evidence it is.

    Preference order matters. A command that runs SQLancer is checked first,
    because a build script's own words ("RUN java -jar ...") would otherwise be
    caught by the prose pattern and quoted as if they were a sentence. Prose
    comes next, and a structural path match last.
    """
    invocation = INVOCATION.search(text)
    if invocation:
        return _line_excerpt(text, invocation), "invocation"
    statement = USAGE_STATEMENT.search(text)
    if statement:
        return truncate(statement.group(0).strip(), MAX_SNIPPET), "statement"
    if SQLANCER_PATH.search(path):
        mention = re.search(r"sqlancer", text, re.IGNORECASE)
        if mention:
            return _line_excerpt(text, mention), "path"
    return None


def _line_excerpt(text: str, match: re.Match) -> str:
    start = text.rfind("\n", 0, match.start())
    start = 0 if start < 0 else start + 1
    end = text.find("\n", match.end())
    end = len(text) if end < 0 else end
    return truncate(text[start:end].strip(), MAX_SNIPPET)


def build_record(entry: dict, relationship: str, *, summary: str,
                 evidence: List[dict], timestamp: str,
                 finder: Optional[str] = "sqlancer",
                 classifier_record: Optional[dict] = None) -> dict:
    record = {
        "id": f"adoption:{entry['id']}:{relationship}",
        "dbms": entry["id"],
        "relationship": relationship,
        "finder": finder,
        "summary": truncate(summary, 600),
        "active": True,
        "evidence": evidence,
        "provenance": {
            "collector": COLLECTOR,
            "collector_version": COLLECTOR_VERSION,
            "policy_version": config.policy_version(),
            "first_seen": timestamp,
            "last_verified": timestamp,
            "source_url": evidence[0]["source_url"],
            "source_type": evidence[0]["source_type"],
        },
    }
    if entry.get("repository"):
        record["project_repository"] = entry["repository"]
    if classifier_record:
        record["classifier"] = classifier_record
    return record


def inspect_project(gh: GitHub, entry: dict, *, timestamp: str,
                    max_files: int = 6, tax: Optional[taxonomy.Taxonomy] = None
                    ) -> Tuple[List[dict], List[dict]]:
    """Look for SQLancer invocations inside one project's own repository.

    Returns ``(records, candidates)``; candidates are files that mention
    SQLancer without clearly invoking it, which is a question for the
    classifier rather than a fact.
    """
    tax = tax or taxonomy.load()
    parsed = _owner_repo(entry.get("repository"))
    if not parsed:
        return [], []
    owner, repo = parsed

    records: List[dict] = []
    candidates: List[dict] = []
    by_relationship: Dict[str, dict] = {}

    query = f"repo:{owner}/{repo} sqlancer"
    # Inspect the most telling files first: a directory named after the tool,
    # then CI, then tests, then prose. This spends the per-project file budget
    # where the evidence is strongest, and keeps a speculative sentence in a
    # design document from outranking an actual test harness.
    def priority(item: dict) -> int:
        path = item.get("path") or ""
        if SQLANCER_PATH.search(path):
            return 0
        if CI_PATH.search(path):
            return 1
        if TEST_PATH.search(path):
            return 2
        return 3

    items = sorted(gh.search_code(query, max_pages=1), key=priority)
    inspected = 0
    for item in items:
        if inspected >= max_files:
            break
        path = item.get("path") or ""
        relationship = _relationship_for(path)
        if relationship is None:
            continue
        inspected += 1
        text, _ = gh.raw_file(owner, repo, path,
                              item.get("ref") or "HEAD", max_age_days=30)
        if not text:
            continue
        blob_url = (item.get("html_url")
                    or f"https://github.com/{owner}/{repo}/blob/HEAD/{path}")
        best = _best_excerpt(text, path)
        if best is None:
            candidates.append({
                "dbms": entry["id"], "url": blob_url, "path": path,
                "text": truncate(text, 20000),
                "relationship_hint": relationship,
            })
            continue

        excerpt, kind = best
        if not verbatim_excerpt(text, excerpt):
            continue
        evidence = [{
            "source_url": blob_url,
            "source_type": ("github_workflow" if relationship == "official_ci"
                            else "github_code"),
            "excerpt": excerpt,
            "excerpt_is_verbatim": True,
            "note": (f"{path} in the {entry['name']} repository "
                     + ("states that the project uses SQLancer."
                        if kind == "statement" else
                        "invokes SQLancer." if kind == "invocation" else
                        "is part of a SQLancer setup the project maintains.")),
            "content_sha256": content_hash(text),
            "retrieved_at": timestamp,
            "first_seen": timestamp,
            "last_verified": timestamp,
        }]
        summary = (
            f"The {entry['name']} project runs SQLancer from its own repository "
            f"({path})."
            if relationship == "official_ci" else
            f"The {entry['name']} project maintains SQLancer testing setup in "
            f"its own repository ({path}).")

        # Credit the specific umbrella tool the file names, so a project that
        # runs SQLancer++ is not recorded as running plain SQLancer.
        named = tax.mentions_finder(excerpt) or tax.mentions_finder(text)
        finder = named[0] if named else "sqlancer"

        existing = by_relationship.get(relationship)
        if existing is None:
            record = build_record(entry, relationship, summary=summary,
                                  evidence=evidence, timestamp=timestamp,
                                  finder=finder)
            by_relationship[relationship] = record
            records.append(record)
        else:
            existing["evidence"].extend(evidence)

    return records, candidates


def collect(gh: GitHub, classifier: Optional[Classifier] = None, *,
            timestamp: Optional[str] = None, entries: Optional[List[dict]] = None
            ) -> Tuple[List[dict], List[dict], List[dict]]:
    """Return ``(records, rejected, needs_review)`` across all known systems."""
    timestamp = timestamp or now()
    tax = taxonomy.load()
    entries = entries if entries is not None else _registry()

    records: List[dict] = []
    rejected: List[dict] = []
    needs_review: List[dict] = []

    for entry in entries:
        if not entry.get("repository"):
            continue
        try:
            found, candidates = inspect_project(gh, entry, timestamp=timestamp,
                                                tax=tax)
        except RuntimeError:
            # A failed search for one project must not end the run.
            needs_review.append({
                "kind": "adoption", "dbms": entry["id"],
                "reason": "repository search failed",
            })
            continue
        records.extend(found)

        for candidate in candidates:
            if any(record["dbms"] == candidate["dbms"] for record in found):
                continue  # already established for this project
            # Ask before giving up: the answer may be cached, and a
            # cached answer needs no key. Checking availability first
            # made a warm cache useless offline.
            result = None if classifier is None else classifier.classify(
                "adoption",
                source_id=f"adoption:{candidate['url']}",
                source_text=candidate["text"],
                source_label=f"{entry['name']} repository file {candidate['path']}",
                dbms_name=entry["name"],
                finder_names=finder_names(tax))
            if result is None:
                needs_review.append({
                    "kind": "adoption", "dbms": candidate["dbms"],
                    "url": candidate["url"],
                    "reason": "classification did not complete",
                })
                continue
            if not result.is_positive:
                rejected.append({
                    "kind": "adoption", "dbms": candidate["dbms"],
                    "url": candidate["url"], "answer": result.answer,
                    "reason": result.reason or "not developer use",
                })
                continue
            relationship = result.extra.get("relationship")
            if relationship not in RELATIONSHIPS:
                relationship = candidate["relationship_hint"]
            evidence = [{
                "source_url": candidate["url"],
                "source_type": "github_code",
                "excerpt": result.excerpts[0],
                "excerpt_is_verbatim": True,
                "note": f"{candidate['path']} in the {entry['name']} repository.",
                "content_sha256": content_hash(candidate["text"]),
                "retrieved_at": timestamp,
                "first_seen": timestamp,
                "last_verified": timestamp,
            }]
            records.append(build_record(
                entry, relationship,
                summary=truncate(result.reason or
                                 f"The {entry['name']} project uses SQLancer.", 600),
                evidence=evidence, timestamp=timestamp,
                finder=resolve_finder(tax, result.extra.get("finder")) or "sqlancer",
                classifier_record=result.classifier_record()))

    curated, curated_review = from_curated_issues(gh, timestamp=timestamp)
    records.extend(curated)
    needs_review.extend(curated_review)

    files, file_review = from_curated_files(gh, timestamp=timestamp)
    records.extend(files)
    needs_review.extend(file_review)

    # Proposals live in the same per-repository issue searches the bug
    # collector runs, so they cost nothing extra beyond reading them.
    try:
        from . import github_bugs
        tax_ = taxonomy.load()
        repo_ids = github_bugs._repo_index()
        issues: List[dict] = []
        for query in github_bugs.search_queries(tax_, sorted(repo_ids)):
            issues.extend(gh.search_issues(query, max_pages=2))
        proposals, proposal_review = proposal_records(
            gh, issues, timestamp=timestamp, repo_ids=repo_ids)
        records.extend(proposals)
        needs_review.extend(proposal_review)
    except Exception:
        pass

    # One record per (dbms, relationship); merge evidence rather than duplicate.
    merged: Dict[str, dict] = {}
    for record in records:
        existing = merged.get(record["id"])
        if existing is None:
            merged[record["id"]] = record
        else:
            existing["evidence"].extend(record["evidence"])
    return list(merged.values()), rejected, needs_review
