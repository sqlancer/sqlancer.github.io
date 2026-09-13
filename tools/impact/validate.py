"""Schema and cross-file validation for the impact dataset.

JSON Schema catches shape errors. The checks after it catch the things a schema
cannot express: an id used twice, a bug pointing at a technique that is not in
the taxonomy, an accepted classification with no evidence behind it, an evidence
excerpt whose verbatim flag was not set. Both halves run in CI, and a failure
here blocks the pull request rather than publishing a bad record.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import jsonschema

from . import config, taxonomy
from .util import normalize_doi, normalize_url

URL_RE = re.compile(r"^https?://[^\s<>\"]+$")


class Problem:
    __slots__ = ("file", "path", "message")

    def __init__(self, file: str, path: str, message: str):
        self.file = file
        self.path = path
        self.message = message

    def __str__(self):
        location = f"{self.file}:{self.path}" if self.path else self.file
        return f"{location}: {self.message}"


def _resolver(schema: dict) -> jsonschema.RefResolver:
    """Resolve the relative ``common.schema.json#...`` references."""
    store = {}
    for path in config.SCHEMA_DIR.glob("*.schema.json"):
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        store[path.name] = loaded
        if "$id" in loaded:
            store[loaded["$id"]] = loaded
    base = config.SCHEMA_DIR.as_uri() + "/"
    return jsonschema.RefResolver(base_uri=base, referrer=schema, store=store)


def validate_schema(name: str, payload: dict) -> List[Problem]:
    schema_path = config.SCHEMA_FILES.get(name)
    if schema_path is None or not schema_path.exists():
        return [Problem(name, "", f"no schema file at {schema_path}")]
    schema = config.load_json(schema_path)
    validator = jsonschema.Draft7Validator(schema, resolver=_resolver(schema))
    problems = []
    for error in sorted(validator.iter_errors(payload), key=lambda e: list(e.path)):
        path = "/".join(str(part) for part in error.absolute_path)
        problems.append(Problem(f"{name}.json", path, error.message))
    return problems


def _iter_evidence(record: dict) -> Iterable[Tuple[str, dict]]:
    """Yield ``(path, evidence)`` for every evidence object inside a record."""
    def walk(node, path):
        if isinstance(node, dict):
            if "source_url" in node and "source_type" in node:
                yield path, node
            for key, value in node.items():
                yield from walk(value, f"{path}/{key}" if path else key)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from walk(value, f"{path}/{index}")
    yield from walk(record, "")


def check_evidence(name: str, records: List[dict]) -> List[Problem]:
    problems = []
    for record in records:
        rid = record.get("id", "?")
        for path, evidence in _iter_evidence(record):
            url = evidence.get("source_url")
            if not url or not URL_RE.match(url):
                problems.append(Problem(f"{name}.json", f"{rid}/{path}",
                                        f"malformed evidence URL {url!r}"))
            excerpt = evidence.get("excerpt")
            if excerpt is not None and not evidence.get("excerpt_is_verbatim"):
                problems.append(Problem(
                    f"{name}.json", f"{rid}/{path}",
                    "evidence carries an excerpt but excerpt_is_verbatim is not set; "
                    "excerpts must be copied verbatim from the source"))
            if excerpt is not None and not excerpt.strip():
                problems.append(Problem(f"{name}.json", f"{rid}/{path}",
                                        "evidence excerpt is empty"))
    return problems


def check_unique_ids(name: str, records: List[dict]) -> List[Problem]:
    counts = Counter(record.get("id") for record in records)
    return [Problem(f"{name}.json", str(rid), f"duplicate record id ({count} times)")
            for rid, count in counts.items() if count > 1]


def check_affiliation_rulings(records: List[dict],
                              roster: List[dict]) -> List[Problem]:
    """The rulings in ``people_decisions.json`` are in force and still apply.

    Two ways this file goes quietly wrong, and both make it a no-op rather than
    an error: the records can be stale, so a ruling was made but never applied;
    and a handle can be misspelled, so the ruling matches nobody. Neither shows
    up anywhere -- the numbers simply stay as they were.
    """
    from .collectors import people as people_collector

    problems: List[Problem] = []
    ruling = people_collector.decisions()
    entries = ruling.get("counts_as_external") or []
    if not entries:
        return problems

    named = people_collector.counted_as_external(ruling)
    known = {(entry.get("github") or "").strip().lower() for entry in roster}
    known |= {(entry.get("name") or "").strip().lower() for entry in roster}
    known.discard("")
    for entry in entries:
        handle = (entry.get("github") or "").strip().lower()
        if handle and handle not in known:
            problems.append(Problem(
                "people_decisions.json", handle,
                "no one on the roster goes by this name, so the ruling has no "
                "effect; check the spelling"))

    for record in records:
        reporter = (record.get("reporter") or "").strip().lower()
        if reporter in named and record.get("reporter_affiliation") != "external":
            problems.append(Problem(
                "bugs.json", record.get("id", "?"),
                f"{record.get('reporter')} is ruled external in "
                f"people_decisions.json but this record says "
                f"{record.get('reporter_affiliation')!r}; re-run collect"))
    return problems


def check_paper_records() -> List[Problem]:
    """Validate every analysed paper against its schema.

    These 190 files carry the sentences behind every claim the impact page
    makes about research, and they were the one part of the dataset nothing
    checked -- there was no schema for them at all.
    """
    import json as _json

    schema_path = config.REPO_ROOT / "schemas" / "papers" / "paper-analysis.schema.json"
    directory = config.REPO_ROOT / "_data" / "papers"
    if not schema_path.exists() or not directory.exists():
        return []
    schema = config.load_json(schema_path)
    validator = jsonschema.Draft7Validator(schema)
    problems: List[Problem] = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = _json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            problems.append(Problem("papers/", path.name, f"invalid JSON: {error}"))
            continue
        for error in sorted(validator.iter_errors(record),
                            key=lambda e: list(e.path))[:3]:
            where = "/".join(str(part) for part in error.path)
            problems.append(Problem("papers/", f"{path.name}:{where}",
                                    error.message[:200]))
    return problems


def check_readable_excerpts(papers: List[dict]) -> List[Problem]:
    """No published quotation may be one nobody can read.

    Some PDFs set a passage with letter-spacing, and extraction returns it one
    character at a time. The mention is real, but the text is not a quotation
    any reader can use, and the word breaks are not in the file to put back --
    so it must not reach a page. Every case so far had other evidence for the
    same claim, which is what made dropping it safe.
    """
    from tools.papers.extract import is_letter_spaced

    problems: List[Problem] = []
    for paper in papers:
        for key, relationship in (paper.get("relationships") or {}).items():
            for item in (relationship or {}).get("evidence") or []:
                if is_letter_spaced(item.get("excerpt") or ""):
                    problems.append(Problem(
                        "papers.json", f"{paper.get('id')}/{key}",
                        "quotes a passage whose word breaks extraction lost"))
    return problems


def check_talks(talks: List[dict]) -> List[Problem]:
    """A talk record says where each mention came from, and stays checkable.

    Two things can go quietly wrong here that a schema will not catch. A
    relationship can cite a mention that has since been renumbered, leaving a
    claim with nothing behind it. And a caption mention can lose its deep link
    -- which matters more here than anywhere else in the dataset, because the
    caption text is a machine's transcription and the link to the moment is the
    only way a reader can check what was really said.
    """
    problems: List[Problem] = []
    for talk in talks:
        tid = talk.get("id") or "?"
        ids = {m.get("id") for m in talk.get("mentions") or []}
        if len(ids) != len(talk.get("mentions") or []):
            problems.append(Problem("talks.json", tid,
                                    "two mentions share an id"))
        kinds = {s.get("kind") for s in talk.get("sources") or []}
        for mention in talk.get("mentions") or []:
            where = f"{tid}/{mention.get('id')}"
            source = mention.get("source")
            if source not in kinds:
                problems.append(Problem(
                    "talks.json", where,
                    f"read from {source!r}, which is not one of this talk's "
                    "sources"))
            if source == "captions":
                if mention.get("at_seconds") is None:
                    problems.append(Problem("talks.json", where,
                                            "a caption mention with no timestamp"))
                elif f"t={mention['at_seconds']}s" not in (mention.get("url") or ""):
                    problems.append(Problem(
                        "talks.json", where,
                        "the link does not point at the moment it was said"))
            # Any mention may carry a picture -- a frame from a recording, or
            # the slide itself from a published deck -- and a page that links a
            # missing one is worse than one that shows nothing.
            if mention.get("image") and not (
                    config.REPO_ROOT / mention["image"].lstrip("/")).exists():
                problems.append(Problem(
                    "talks.json", where,
                    f"the image {mention['image']} is not in the repository"))
            if source == "frame":
                if not mention.get("image"):
                    problems.append(Problem("talks.json", where,
                                            "a frame mention with no image"))
                if mention.get("excerpt"):
                    problems.append(Problem(
                        "talks.json", where,
                        "text read off a picture cannot be quoted as an excerpt"))
                length = talk.get("duration_seconds")
                if length and (mention.get("at_seconds") or 0) > length:
                    problems.append(Problem(
                        "talks.json", where,
                        "a frame captured after the talk ends"))
            if source == "watched" and mention.get("excerpt"):
                problems.append(Problem(
                    "talks.json", where,
                    "a mention nobody read cannot carry a quotation"))
            if mention.get("excerpt") and not mention.get("excerpt_is_verbatim"):
                problems.append(Problem("talks.json", where,
                                        "an excerpt not marked verbatim"))
        cited = set((talk.get("relationship") or {}).get("mention_ids") or [])
        missing = sorted(cited - ids)
        if missing:
            problems.append(Problem(
                "talks.json", tid,
                f"the relationship cites {', '.join(missing)}, which this "
                "talk does not have"))
    return problems


def check_recognition_highlights(papers: List[dict]) -> List[Problem]:
    """Every featured quote is one the paper's own record actually holds.

    The highlights file names a paper and a mention id rather than copying the
    sentence, so a quote cannot be edited into something the paper did not
    say. What it can do is name a mention that no longer exists, or a paper
    whose classification has since changed, and then the page would quote
    nothing or quote it under a claim the data no longer makes.
    """
    import json as _json

    path = config.DATA_DIR / "recognition_highlights.json"
    if not path.exists():
        return []
    try:
        highlights = config.load_json(path).get("highlights") or []
    except ValueError as error:
        return [Problem("recognition_highlights.json", "",
                        f"invalid JSON: {error}")]

    by_id = {paper.get("id"): paper for paper in papers}
    problems: List[Problem] = []
    for entry in highlights:
        pid = entry.get("paper_id")
        paper = by_id.get(pid)
        if paper is None:
            problems.append(Problem("recognition_highlights.json", pid or "?",
                                    "no paper with this id"))
            continue
        relationship = (paper.get("relationships") or {}).get(
            "describes_as_state_of_the_art") or {}
        if relationship.get("value") != "yes":
            problems.append(Problem(
                "recognition_highlights.json", pid,
                "this paper no longer describes SQLancer as state of the art"))
            continue
        # The quote lives on the paper record in the papers subproject; the
        # impact record carries the same sentences as evidence.
        record = config.REPO_ROOT / "_data" / "papers" / (
            pid.replace(":", "_").replace(".", "_").replace("/", "_") + ".json")
        if not record.exists():
            problems.append(Problem("recognition_highlights.json", pid,
                                    "no analysed record for this paper"))
            continue
        analysis = _json.loads(record.read_text(encoding="utf-8")).get(
            "analysis") or {}
        quotes = ((analysis.get("relationships") or {}).get(
            "describes_as_state_of_the_art") or {}).get("quotes") or []
        if not any(q.get("mention_id") == entry.get("mention_id")
                   for q in quotes):
            problems.append(Problem(
                "recognition_highlights.json", f"{pid}/{entry.get('mention_id')}",
                "the record has no quoted sentence with this mention id"))
    return problems


def check_bugs(records: List[dict], registry: Dict[str, dict],
               tax: taxonomy.Taxonomy) -> List[Problem]:
    problems: List[Problem] = []
    by_url: Dict[str, List[str]] = defaultdict(list)
    for record in records:
        rid = record.get("id", "?")
        if record.get("dbms") not in registry:
            problems.append(Problem("bugs.json", rid,
                                    f"unknown dbms {record.get('dbms')!r}"))
        technique = record.get("technique")
        if not tax.is_known_technique(technique):
            problems.append(Problem("bugs.json", rid,
                                    f"unknown SQLancer technique {technique!r}"))
        if technique in tax.excluded:
            problems.append(Problem(
                "bugs.json", rid,
                f"technique {technique!r} is excluded by the attribution policy"))
        finder = record.get("finder")
        if finder not in tax.finders:
            problems.append(Problem("bugs.json", rid, f"unknown finder {finder!r}"))
        elif not tax.finders[finder]["umbrella_member"]:
            problems.append(Problem(
                "bugs.json", rid,
                f"finder {finder!r} is not a SQLancer umbrella member"))
        if not record.get("links"):
            problems.append(Problem("bugs.json", rid, "bug record has no links"))
        if not record.get("attribution", {}).get("evidence"):
            problems.append(Problem("bugs.json", rid, "bug record has no evidence"))
        # A technique_attribution record must actually name the technique.
        if record.get("attribution", {}).get("rule") == "technique_attribution" \
                and technique is None:
            problems.append(Problem(
                "bugs.json", rid,
                "attribution rule is technique_attribution but no technique is set"))
        url = normalize_url(record.get("primary_url"))
        if url:
            by_url[url].append(rid)

    for url, ids in by_url.items():
        if len(ids) > 1:
            # Two curated reports can share one upstream ticket; flag only when
            # the titles match too, which would mean a genuine duplicate.
            titles = {r["title"] for r in records if r["id"] in ids}
            if len(titles) < len(ids):
                problems.append(Problem(
                    "bugs.json", ", ".join(ids),
                    f"duplicate bug URL with identical title: {url}"))
    return problems


def check_papers(records: List[dict], tax: taxonomy.Taxonomy) -> List[Problem]:
    problems: List[Problem] = []
    seen_doi: Dict[str, str] = {}
    for record in records:
        rid = record.get("id", "?")
        doi = normalize_doi(record.get("doi"))
        if doi:
            if doi in seen_doi:
                problems.append(Problem("papers.json", rid,
                                        f"duplicate DOI {doi} (also {seen_doi[doi]})"))
            else:
                seen_doi[doi] = rid
        known_seeds = set(tax.seed_ids())
        for seed in record.get("cites_seed_techniques", []):
            if seed not in tax.techniques and seed not in known_seeds:
                problems.append(Problem("papers.json", rid,
                                        f"unknown seed technique {seed!r}"))
        relationships = record.get("relationships", {})
        if relationships.get("references", {}).get("value") != "yes":
            problems.append(Problem(
                "papers.json", rid,
                "every collected paper must have references = yes"))
        for key, relationship in relationships.items():
            for technique in relationship.get("techniques", []):
                # "references" carries which seed the paper cites, and a seed
                # can be a tool rather than a technique. The three judged
                # relationships name techniques only.
                allowed = (tax.techniques if key != "references"
                           else set(tax.techniques) | known_seeds)
                if technique not in allowed:
                    problems.append(Problem(
                        "papers.json", f"{rid}/{key}",
                        f"unknown SQLancer technique {technique!r}"))
            if relationship.get("value") == "yes" and not relationship.get("evidence"):
                problems.append(Problem(
                    "papers.json", f"{rid}/{key}",
                    "positive relationship with no evidence"))
            # Extending is always extending something in particular, so an
            # extension that names no technique is incomplete. Comparing is
            # not: a paper can run SQLancer as a whole against its own tool
            # without naming an oracle, and demanding one would mean inventing
            # it. Twenty papers say exactly that.
            if relationship.get("value") == "yes" \
                    and key == "extends_technique" \
                    and not relationship.get("techniques"):
                problems.append(Problem(
                    "papers.json", f"{rid}/{key}",
                    "positive relationship does not name the technique involved"))
    return problems


def check_adoption(records: List[dict], registry: Dict[str, dict],
                   tax: taxonomy.Taxonomy) -> List[Problem]:
    problems: List[Problem] = []
    seen: Dict[Tuple[str, str], str] = {}
    for record in records:
        rid = record.get("id", "?")
        dbms = record.get("dbms")
        if dbms not in registry:
            problems.append(Problem("adoption.json", rid, f"unknown dbms {dbms!r}"))
        key = (dbms, record.get("relationship"))
        if key in seen:
            problems.append(Problem(
                "adoption.json", rid,
                f"duplicate adoption relationship {key} (also {seen[key]})"))
        else:
            seen[key] = rid
        finder = record.get("finder")
        if finder is not None and finder not in tax.finders:
            problems.append(Problem("adoption.json", rid,
                                    f"unknown finder {finder!r}"))
        if not record.get("evidence"):
            problems.append(Problem("adoption.json", rid,
                                    "adoption record has no evidence"))
    return problems


def check_resources(records: List[dict], registry: Dict[str, dict],
                    tax: taxonomy.Taxonomy) -> List[Problem]:
    problems: List[Problem] = []
    seen: Dict[str, str] = {}
    for record in records:
        rid = record.get("id", "?")
        url = normalize_url(record.get("url"))
        if not url:
            problems.append(Problem("resources.json", rid,
                                    f"malformed URL {record.get('url')!r}"))
        elif url in seen:
            problems.append(Problem("resources.json", rid,
                                    f"duplicate resource URL {url} (also {seen[url]})"))
        else:
            seen[url] = rid
        for technique in record.get("related_techniques", []):
            if technique not in tax.techniques:
                problems.append(Problem("resources.json", rid,
                                        f"unknown technique {technique!r}"))
        for dbms in record.get("related_dbms", []):
            if dbms not in registry:
                problems.append(Problem("resources.json", rid,
                                        f"unknown dbms {dbms!r}"))
    return problems


def validate_all(data_dir: Optional[Path] = None) -> List[Problem]:
    """Validate every authoritative file plus the cross-file relationships."""
    files = config.DATA_FILES
    if data_dir is not None:
        files = {name: Path(data_dir) / path.name
                 for name, path in config.DATA_FILES.items()}

    problems: List[Problem] = []
    payloads: Dict[str, dict] = {}
    for name, path in files.items():
        if not path.exists():
            if name == "stats":
                continue
            problems.append(Problem(path.name, "", "file is missing"))
            continue
        try:
            payloads[name] = config.load_json(path)
        except ValueError as error:
            problems.append(Problem(path.name, "", f"invalid JSON: {error}"))

    for name, payload in payloads.items():
        problems.extend(validate_schema(name, payload))

    if "techniques" not in payloads:
        return problems

    tax = taxonomy.Taxonomy(payloads["techniques"])
    registry = {entry["id"]: entry for entry in payloads.get("dbms", {}).get("dbms", [])}

    from .dataset import LIST_FIELD
    for name, field in LIST_FIELD.items():
        records = payloads.get(name, {}).get(field, [])
        problems.extend(check_unique_ids(name, records))
        problems.extend(check_evidence(name, records))

    problems.extend(check_bugs(payloads.get("bugs", {}).get("bugs", []), registry, tax))
    problems.extend(check_recognition_highlights(
        payloads.get("papers", {}).get("papers", [])))
    problems.extend(check_talks(payloads.get("talks", {}).get("talks", [])))
    problems.extend(check_readable_excerpts(
        payloads.get("papers", {}).get("papers", [])))
    problems.extend(check_paper_records())
    problems.extend(check_affiliation_rulings(
        payloads.get("bugs", {}).get("bugs", []),
        payloads.get("people", {}).get("people", [])))
    problems.extend(check_papers(payloads.get("papers", {}).get("papers", []), tax))
    problems.extend(check_adoption(
        payloads.get("adoption", {}).get("adoption", []), registry, tax))
    problems.extend(check_resources(
        payloads.get("resources", {}).get("resources", []), registry, tax))
    problems.extend(check_paper_notes(payloads.get("papers", {}).get("papers", [])))
    problems.extend(check_review_queue())
    return problems


def check_review_queue(payload: Optional[dict] = None) -> List[Problem]:
    """Validate the queue of candidates no rule settled.

    Two things the schema cannot see. An id in both lists means an item was
    dismissed and is still being raised, which is the failure the queue exists
    to prevent -- a person rules on something and the next run asks again. And
    a duplicated id means two different candidates share one decision, so
    dismissing one silently dismisses the other.
    """
    schema_path = config.SCHEMA_DIR / "needs_review.schema.json"
    if not schema_path.exists():
        return [Problem("needs_review", "", f"no schema file at {schema_path}")]
    if payload is None:
        path = config.DATA_DIR / "needs_review.json"
        if not path.exists():
            return []
        try:
            payload = config.load_json(path)
        except ValueError as error:
            return [Problem("needs_review", "", f"invalid JSON: {error}")]

    schema = config.load_json(schema_path)
    validator = jsonschema.Draft7Validator(schema, resolver=_resolver(schema))
    problems = [
        Problem("needs_review", "/".join(str(part) for part in error.absolute_path),
                error.message)
        for error in sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    ]

    seen: Dict[str, str] = {}
    for name in ("open", "dismissed"):
        for entry in payload.get(name) or []:
            item_id = entry.get("id")
            if not item_id:
                continue
            if item_id in seen:
                where = seen[item_id]
                problems.append(Problem(
                    "needs_review", item_id,
                    f"also in {where}"
                    if where != name else f"listed twice in {name}"))
            else:
                seen[item_id] = name
    return problems

def check_paper_notes(papers: List[dict]) -> List[Problem]:
    """Validate each paper's note file, and that it belongs to a real paper.

    An orphan note is a real risk here: the notes are one file per paper and
    nothing else points at them, so a paper leaving the dataset would otherwise
    leave a note behind that no longer describes anything.
    """
    from . import notes

    if not notes.NOTES_DIR.is_dir():
        return []
    schema_path = config.SCHEMA_DIR / "paper-note.schema.json"
    if not schema_path.exists():
        return [Problem("paper_notes", "", f"no schema file at {schema_path}")]
    schema = config.load_json(schema_path)
    validator = jsonschema.Draft7Validator(schema, resolver=_resolver(schema))

    expected = {notes.note_path(paper).name for paper in papers
                if not paper.get("is_sqlancer_publication")}
    ids = {paper["id"] for paper in papers}

    problems: List[Problem] = []
    for path in sorted(notes.NOTES_DIR.glob("*.json")):
        label = f"paper_notes/{path.name}"
        if path.name not in expected:
            problems.append(Problem(label, "", "note for a paper not in papers.json"))
            continue
        try:
            payload = config.load_json(path)
        except ValueError as error:
            problems.append(Problem(label, "", f"invalid JSON: {error}"))
            continue
        for error in sorted(validator.iter_errors(payload), key=lambda e: list(e.path)):
            problems.append(Problem(
                label, "/".join(str(part) for part in error.absolute_path),
                error.message))
        paper_id = (payload.get("paper") or {}).get("id")
        if paper_id not in ids:
            problems.append(Problem(label, "paper/id",
                                    f"unknown paper {paper_id!r}"))
    return problems


def main() -> int:
    problems = validate_all()
    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} validation problem(s)")
        return 1
    print("impact data: all files valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
