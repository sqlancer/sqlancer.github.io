"""One file per citing paper: what it is about, and every source behind it.

``papers.json`` answers the questions the statistics need -- does this paper
reuse the codebase, extend a technique, compare against SQLancer -- and nothing
more. That is the right shape for a dataset and the wrong shape for a reader,
who wants to know what the paper actually is before deciding whether the
classification looks right.

So each paper also gets a note of its own, holding two clearly separated
things. A summary, written by a language model from the best text available for
that paper, marked as model-generated and stamped with the model and the exact
source it was given. And underneath it, every piece of evidence the record
rests on, quoted verbatim with its URL -- the citation sentences, the full-text
passages, the artifact markers.

The separation is the point. The summary is a convenience and is allowed to be
wrong; the evidence is the record and is not. A summary the model could not
ground in a quotation from the source is stored as ungrounded rather than
presented as a finding, and a paper with no API key available gets its note with
the evidence and no summary at all.
"""

from __future__ import annotations

import json

import pathlib
from typing import Dict, List, Optional, Tuple

from . import config, taxonomy
from .cache import Caches
from .classify import Classifier
from .util import content_hash, now, slugify, truncate

SCHEMA_VERSION = "1.0.0"
NOTES_DIR = config.DATA_DIR / "paper_notes"

# How much of a paper to hand the model. Enough for a long paper's introduction,
# related work and evaluation, which is where a relationship is stated.
MAX_SOURCE_CHARS = 60_000

# Shorter than this, an abstract is a stub rather than a description.
MIN_ABSTRACT_CHARS = 120

RELATIONSHIP_TITLES = {
    "references": "Cites SQLancer",
    "uses_infrastructure": "Uses the SQLancer codebase",
    "extends_technique": "Extends a SQLancer technique",
    "compares_with": "Compares against SQLancer",
    "describes_as_state_of_the_art": "Calls SQLancer state of the art",
}


def note_path(paper: dict) -> pathlib.Path:
    """Where a paper's note lives.

    Named from the paper id rather than its title: a title changes when a
    preprint becomes a publication, and the file should not move when it does.
    """
    return NOTES_DIR / f"{slugify(paper['id'])}.json"


def _best_source(paper: dict, fetcher=None) -> Tuple[Optional[str], str, str, str]:
    """The best text available for this paper.

    Returns ``(text, kind, label, source_url)``. Preference runs full text,
    then abstract, then the citation sentences -- and the kind travels with the
    summary, because a summary written from two citation sentences is a much
    weaker thing than one written from the paper, and a reader should be told
    which they are looking at.
    """
    from .collectors import fulltext

    # A supplied PDF is read straight off the disk -- pdf_text handles a
    # file:// URL without touching the network -- so it must not be skipped
    # merely because the caller had no fetcher to give. Getting that wrong is
    # quiet: the summary is written from the abstract while the paper itself
    # sits unread in the folder.
    local = fulltext.local_pdf(paper.get("doi"), paper.get("arxiv_id"),
                               paper.get("s2_paper_id"))
    if local:
        text = fulltext.pdf_text(fetcher, local)
        if text:
            return (text, "the full text", "supplied PDF",
                    paper.get("url") or local)

    if paper.get("arxiv_id") and fetcher is not None:
        url = f"https://arxiv.org/pdf/{paper['arxiv_id']}"
        text = fulltext.pdf_text(fetcher, url)
        if text:
            return text, "the full text", "arXiv PDF", url

    abstract = (paper.get("abstract") or "").strip()
    # Some indexed "abstracts" are a single stub line -- a subtitle, or the
    # first sentence of a chapter. Below this length the citation sentences say
    # more about the paper than the abstract does.
    if len(abstract) >= MIN_ABSTRACT_CHARS:
        return (abstract, "only the abstract", "abstract",
                paper.get("url") or "")

    contexts = []
    for key, entry in paper.get("relationships", {}).items():
        for item in entry.get("evidence", []):
            excerpt = item.get("excerpt")
            if excerpt and excerpt not in contexts:
                contexts.append(excerpt)
    if contexts:
        return ("\n\n".join(contexts),
                "only the sentences in which this paper cites SQLancer",
                "citation contexts", paper.get("url") or "")
    return None, "", "", ""


def _evidence_sections(paper: dict) -> List[dict]:
    """Every source behind this paper's record, grouped by what it establishes."""
    sections = []
    for key, entry in paper.get("relationships", {}).items():
        evidence = entry.get("evidence") or []
        if not evidence:
            continue
        sections.append({
            "relationship": key,
            "title": RELATIONSHIP_TITLES.get(key, key.replace("_", " ")),
            "value": entry.get("value"),
            "method": entry.get("method"),
            "techniques": entry.get("techniques", []),
            "sources": [
                {
                    "source_url": item.get("source_url"),
                    "source_type": item.get("source_type"),
                    "excerpt": item.get("excerpt"),
                    "excerpt_is_verbatim": item.get("excerpt_is_verbatim", False),
                    "note": item.get("note"),
                    "retrieved_at": item.get("retrieved_at"),
                }
                for item in evidence
            ],
        })

    for artifact in paper.get("artifacts", []) or []:
        sources = []
        for item in (artifact.get("marker_evidence") or []):
            sources.append({
                "source_url": item.get("source_url"),
                "source_type": item.get("source_type"),
                "excerpt": item.get("excerpt"),
                "excerpt_is_verbatim": item.get("excerpt_is_verbatim", False),
                "note": item.get("note"),
                "retrieved_at": item.get("retrieved_at"),
            })
        sections.append({
            "relationship": "artifact",
            "title": "Artifact",
            "value": artifact.get("url"),
            "method": "artifact_inspection",
            "techniques": [],
            "markers": artifact.get("sqlancer_markers", []),
            "sources": sources,
        })
    return sections


def build_note(paper: dict, classifier: Optional[Classifier] = None,
               fetcher=None, timestamp: Optional[str] = None) -> dict:
    """The note for one paper: a summary if one can be had, and the evidence."""
    timestamp = timestamp or now()
    text, kind, label, source_url = _best_source(paper, fetcher)

    summary: Optional[dict] = None
    if text and classifier is not None:
        result = classifier.classify(
            "paper_summary",
            source_id=f"paper-summary:{paper['id']}",
            source_text=truncate(text, MAX_SOURCE_CHARS),
            source_label=label,
            title=paper["title"],
            source_kind=kind)
        if result is not None:
            summary = {
                "text": result.extra.get("summary") or "",
                "relationship_to_sqlancer":
                    result.extra.get("relationship_to_sqlancer") or "",
                # Said plainly, because everything else in this dataset is not.
                "is_model_generated": True,
                "grounded_in_source": bool(result.excerpts),
                "written_from": label,
                "written_from_url": source_url,
                "source_sha256": content_hash(text),
                "supporting_excerpts": result.excerpts,
                "model": result.model,
                "classifier_version": result.classifier_version,
                "generated_at": timestamp,
            }

    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": config.policy_version(),
        "paper": {
            "id": paper["id"],
            "title": paper["title"],
            "authors": paper.get("authors", []),
            "year": paper.get("year"),
            "venue": paper.get("venue"),
            "doi": paper.get("doi"),
            "url": paper.get("url"),
        },
        "summary": summary,
        "evidence": _evidence_sections(paper),
    }


class UngroundedSummary(ValueError):
    """A supplied excerpt was not found in the text it claims to quote."""


def apply_summary(paper: dict, *, text: str, relationship_to_sqlancer: str,
                  excerpts: List[str], written_from: str,
                  written_from_url: str = "", source_text: str,
                  model: str, generated_by: str = "claude_code_session",
                  timestamp: Optional[str] = None) -> dict:
    """Attach a summary written outside the batch classifier.

    The pipeline's own summaries come from ``build_note`` calling the API. This
    is the same thing written interactively, and it is held to the same rule:
    every excerpt has to appear, character for character, in the source text the
    summary claims to be written from. An excerpt that does not is refused
    rather than stored, because an unverifiable quotation is worse than none.

    ``generated_by`` records which route produced it, so a summary written this
    way is never mistaken for one the pipeline can reproduce on its own.
    """
    from .util import verbatim_excerpt

    timestamp = timestamp or now()
    verified: List[str] = []
    for excerpt in excerpts:
        excerpt = (excerpt or "").strip()
        if not excerpt:
            continue
        if not verbatim_excerpt(source_text, excerpt):
            raise UngroundedSummary(
                f"{paper['id']}: excerpt is not in the source text: {excerpt[:80]!r}")
        if excerpt not in verified:
            verified.append(excerpt)

    path = note_path(paper)
    if path.exists():
        note = json.loads(path.read_text(encoding="utf-8"))
    else:
        note = build_note(paper, classifier=None, fetcher=None,
                          timestamp=timestamp)

    note["summary"] = {
        "text": text.strip(),
        "relationship_to_sqlancer": relationship_to_sqlancer.strip(),
        "is_model_generated": True,
        "grounded_in_source": bool(verified),
        "written_from": written_from,
        "written_from_url": written_from_url,
        "source_sha256": content_hash(source_text),
        "supporting_excerpts": verified,
        "model": model,
        "generated_by": generated_by,
        "classifier_version": None,
        "generated_at": timestamp,
    }
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(note, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return note


def summary_source(paper: dict) -> Tuple[Optional[str], str, str]:
    """The text available to summarise this paper from, without fetching.

    Returns ``(text, label, url)``. Deliberately offline: the abstract if the
    record has one, otherwise the sentences in which the paper cites SQLancer.
    """
    text, _, label, url = _best_source(paper, fetcher=None)
    return text, label, url


def _unchanged(path, note: dict) -> bool:
    """Whether writing this note would change anything but the timestamp."""
    if not path.exists():
        return False
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return False
    return _comparable(existing) == _comparable(note)


def _comparable(note: dict) -> str:
    copy = json.loads(json.dumps(note))
    if copy.get("summary"):
        copy["summary"].pop("generated_at", None)
    return json.dumps(copy, sort_keys=True)


def write_all(*, limit: Optional[int] = None, dry_run: bool = False,
              papers: Optional[List[dict]] = None) -> Dict[str, int]:
    """Write a note for every citing paper. Returns a count of what happened."""
    from .http import Fetcher

    if papers is None:
        papers = config.load_json(config.DATA_FILES["papers"])["papers"]
    caches = Caches()
    classifier = Classifier(caches.classifications, dry_run=dry_run)
    fetcher = Fetcher(caches.artifacts, min_interval=0.5, max_retries=2,
                      timeout=40.0)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = now()

    counts = {"written": 0, "unchanged": 0, "summarised": 0, "no_summary": 0,
              "removed": 0}
    kept = set()
    written = 0
    for paper in papers:
        if paper.get("is_sqlancer_publication"):
            continue
        path = note_path(paper)
        kept.add(path.name)
        if limit is not None and written >= limit and path.exists():
            continue
        note = build_note(paper, classifier=classifier, fetcher=fetcher,
                          timestamp=timestamp)
        # Never drop a summary that already exists because this run could not
        # produce one -- no API key, an API failure, or a source that has since
        # become unreachable. Regenerating the evidence must not cost the note
        # its summary.
        if not note.get("summary") and path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                existing = {}
            if existing.get("summary"):
                note["summary"] = existing["summary"]
        if note.get("summary"):
            counts["summarised"] += 1
        else:
            counts["no_summary"] += 1
        if _unchanged(path, note):
            counts["unchanged"] += 1
            continue
        path.write_text(json.dumps(note, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        counts["written"] += 1
        written += 1

    # A paper that leaves the dataset leaves no orphan note behind.
    for stale in NOTES_DIR.glob("*.json"):
        if stale.name not in kept:
            stale.unlink()
            counts["removed"] += 1
    return counts


def main() -> int:
    counts = write_all()
    print(f"paper notes: {counts['written']} written, "
          f"{counts['unchanged']} unchanged, {counts['removed']} removed; "
          f"{counts['summarised']} with a summary, "
          f"{counts['no_summary']} without")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
