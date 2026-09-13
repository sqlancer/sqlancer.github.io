"""Attaches an analysis to a paper's record, and refuses a bad one.

The analysis is the one part of a record written by a model rather than read off
a page, so it is the part that needs checking. Three things are enforced here,
and each rejects rather than repairs:

1. **Every cited mention must exist.** A claim resting on "M12" in a paper with
   nine mentions is not a claim.
2. **A "yes" must cite something.** A positive relationship with no mention ids
   is an assertion, and this dataset does not publish assertions.
3. **Quotations are attached, never supplied.** The model names mention ids; the
   sentences come from the inventory, which was read off the paper. A fabricated
   quotation is not something the format can express.

What the model does supply in its own words -- the summary, the narrative, the
reasoning -- is stored as such and labelled, so a reader can tell at a glance
which parts of a record are evidence and which are description.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from ..impact.util import now
from . import prompt, store

ANALYSIS_VERSION = "paper-analysis-v1"

# Cited like a mention, but standing for what the paper's repository shows
# rather than what its text says.
ARTIFACT_ID = "ARTIFACT"


class Rejected(ValueError):
    """The answer does not meet the conditions for being recorded."""


# A survey can mention SQLancer eighty times, and reading all of them decides no
# more than reading the informative ones. What matters is which are dropped: a
# sentence a check fired on, or one that names the tool in the paper's own
# prose, can settle a relationship, while the fortieth citation marker in a
# related-work list cannot.
MAX_MENTIONS = 32


def _priority(mention: dict, flagged: set) -> tuple:
    found = set(mention.get("found_by_all") or [])
    section = (mention.get("section") or "").lower()
    return (
        0 if mention["id"] in flagged else 1,
        0 if "name" in found else (1 if "technique" in found else 2),
        # Where a paper says what it built is where reuse is stated.
        0 if any(word in section for word in
                 ("implement", "evaluat", "experiment", "approach", "design"))
        else 1,
        mention.get("char_offset", 0),
    )


def task(record: dict, *, compact: bool = True,
         max_mentions: int = MAX_MENTIONS) -> dict:
    """What a reader needs in order to analyse this paper.

    ``compact`` drops the surrounding paragraphs, which are kept in the record
    for later re-reading but are rarely what decides a judgement.
    """
    flagged = {mention_id
               for ids in ((record.get("checks") or {}).get("suggests") or {}).values()
               for mention_id in ids}
    chosen = sorted(record["mentions"], key=lambda m: _priority(m, flagged))
    chosen = sorted(chosen[:max_mentions],
                    key=lambda m: m.get("char_offset", 0))
    omitted = len(record["mentions"]) - len(chosen)

    mentions = []
    for mention in chosen:
        entry = {
            "id": mention["id"],
            "found_by": mention.get("found_by_all") or [],
            "section": mention.get("section"),
            "page": mention.get("page"),
            "sentence": mention["sentence"],
        }
        if mention.get("techniques"):
            entry["techniques"] = mention["techniques"]
        if mention.get("cited_reference"):
            entry["cites"] = {
                "number": mention["cited_reference"]["number"],
                "matched_as": mention["cited_reference"].get("matched_as"),
                "text": mention["cited_reference"]["text"][:160],
            }
        if not compact:
            entry["context_before"] = mention.get("context_before")
            entry["context_after"] = mention.get("context_after")
        mentions.append(entry)

    paper = record["paper"]
    return {
        "paper_id": paper["id"],
        "title": paper.get("title"),
        "year": paper.get("year"),
        "venue": paper.get("venue"),
        "has_fulltext": record["document"]["has_fulltext"],
        "abstract_available": bool(record.get("abstract")),
        "artifact": record.get("artifact"),
        "sqlancer_references": [
            {"number": r["number"], "matched_as": r.get("matched_as"),
             "text": r["text"][:160]}
            for r in record.get("sqlancer_references") or []
        ],
        "mentions_omitted": omitted,
        "checks": (record.get("checks") or {}).get("suggests") or {},
        "suppressed_checks": (record.get("checks") or {}).get("suppressed") or [],
        "mentions": mentions,
    }


def pending(records: Optional[Sequence[dict]] = None) -> List[dict]:
    """Records with no analysis yet, most informative first.

    A paper with more mentions has more for a reader to weigh, and is the more
    likely to be saying something the dataset does not know.
    """
    records = records if records is not None else store.all_records()
    waiting = [r for r in records if not r.get("analysis")]
    waiting.sort(key=lambda r: (-len(r["mentions"]),
                                -(r["paper"].get("year") or 0)))
    return waiting


def apply(paper_id: str, answer: dict, *, model: str,
          produced_by: str = "claude_code_session") -> dict:
    """Record an analysis against a paper, after checking it.

    Raises :class:`Rejected` rather than storing something that cannot be
    checked: a silently dropped claim is worse than a loud refusal, because
    nobody looks at what they were not told about.
    """
    record = store.load(paper_id)
    if record is None:
        raise Rejected(f"no record for {paper_id}")

    known = {mention["id"]: mention for mention in record["mentions"]}

    # The artifact is evidence too, and often the only evidence there is: a
    # paper whose repository is a SQLancer fork need never say so in its text.
    # It is citable under a reserved id, and only when the record actually
    # carries inspection findings, so the id cannot be invented.
    artifact = record.get("artifact") or {}
    if artifact.get("markers"):
        known[ARTIFACT_ID] = {
            "id": ARTIFACT_ID,
            "sentence": ("Artifact " + str(artifact.get("url") or "") +
                         " carries: " + ", ".join(artifact["markers"])),
            "section": "artifact inspection",
            "page": None,
        }

    for key in ("summary", "narrative", "relationships"):
        if not answer.get(key):
            raise Rejected(f"{paper_id}: the answer has no {key}")

    roles = answer.get("roles") or {}
    for mention_id in roles:
        if mention_id == ARTIFACT_ID:
            continue
        if mention_id not in known:
            raise Rejected(f"{paper_id}: role given for unknown mention "
                           f"{mention_id}")
        if roles[mention_id] not in prompt.ROLES:
            raise Rejected(f"{paper_id}: {mention_id} has unknown role "
                           f"{roles[mention_id]!r}")

    relationships: Dict[str, dict] = {}
    for key in prompt.RELATIONSHIPS:
        entry = (answer["relationships"] or {}).get(key)
        if entry is None:
            raise Rejected(f"{paper_id}: no answer for {key}")
        value = entry.get("value")
        if value not in prompt.ANSWERS:
            raise Rejected(f"{paper_id}: {key} has unknown value {value!r}")
        ids = list(entry.get("mention_ids") or [])
        unknown = [i for i in ids if i not in known]
        if unknown:
            raise Rejected(f"{paper_id}: {key} cites mentions that do not "
                           f"exist: {', '.join(unknown)}")
        if value == "yes" and not ids:
            raise Rejected(f"{paper_id}: {key} is 'yes' with nothing cited")

        stored = {
            "value": value,
            "mention_ids": ids,
            # The quotations come from the inventory, not from the answer.
            "quotes": [{"mention_id": i, "sentence": known[i]["sentence"],
                        "section": known[i].get("section"),
                        "page": known[i].get("page")} for i in ids],
            "reasoning": entry.get("reasoning") or "",
        }
        if entry.get("techniques"):
            stored["techniques"] = entry["techniques"]
        if key == "uses_infrastructure" and entry.get("reuse_kind"):
            if entry["reuse_kind"] not in prompt.REUSE_KINDS:
                raise Rejected(f"{paper_id}: unknown reuse_kind "
                               f"{entry['reuse_kind']!r}")
            stored["reuse_kind"] = entry["reuse_kind"]
        relationships[key] = stored

    record["analysis"] = {
        "analysis_version": ANALYSIS_VERSION,
        "prompt_version": prompt.PROMPT_VERSION,
        "produced_by": produced_by,
        "model": model,
        "generated_at": now(),
        "is_model_written": True,
        "summary": answer["summary"].strip(),
        "narrative": answer["narrative"].strip(),
        "roles": roles,
        "relationships": relationships,
        "disagreements": list(answer.get("disagreements") or []),
        "unresolved": list(answer.get("unresolved") or []),
    }
    store.save(record)
    return record
