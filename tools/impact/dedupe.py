"""Finds paper records that are the same work under two identities.

A paper reaches this dataset through two citation graphs, and both index the
preprint and the published version as separate works. Nothing downstream
noticed: the survey on DBMS fuzzing was counted twice, AMOEBA three times, and
the download list asked for papers already in hand under their other name.

The test for sameness is authorship first. Two records with the same full set of
author surnames are by the same people; the question is then whether they are
the same paper or two papers by one group, and title similarity alone cannot
tell those apart -- "Dependency-Aware Metamorphic Testing of Datalog Engines"
and "Metamorphic testing of Datalog engines" score 0.82 and are genuinely
different papers, exactly like the two SQLaser records that are not.

What separates them is the preprint relationship. A preprint and a published
paper by the same authors with similar titles are one work; two published papers
by the same authors are two works, however alike their titles. So a merge is
proposed automatically only across that boundary, or when the titles are
practically identical. Everything else is reported for a person to decide.
"""

from __future__ import annotations

import difflib
import re
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

# A preprint and its published version, when there are no abstracts to compare.
PREPRINT_TITLE_SIMILARITY = 0.60

# Near-identical titles settle it whatever else disagrees.
IDENTICAL_TITLE_SIMILARITY = 0.90

# Two abstracts this alike are the same paper. This is the strongest signal
# available and it is why the property-based-testing pair is not merged: same
# authors, same year, titles 0.75 alike, abstracts 0.09 -- a position paper and
# a full paper, not one work twice.
ABSTRACT_SIMILARITY = 0.60

# Same authors, plausibly the same work, but nothing above settled it. Reported
# for a person rather than merged. The conformance-testing pair lands here: the
# published version rewrote its abstract and added two systems, so it scores
# 0.04 against its own preprint.
REVIEW_TITLE_SIMILARITY = 0.60

PREPRINT_VENUE = re.compile(r"arxiv|preprint|corr\b", re.IGNORECASE)


def surname(name: str) -> str:
    parts = [part for part in re.sub(r"[^A-Za-z .-]", " ", name or "").split()
             if part]
    return parts[-1].lower() if parts else ""


def author_key(paper: dict) -> frozenset:
    return frozenset(s for s in (surname(a) for a in (paper.get("authors") or []))
                     if s)


# A tool's name as papers write it in a title: capitalised, distinctive, and
# usually before the colon. Common words are excluded; so are acronyms a whole
# subfield shares.
TOOL_NAME = re.compile(r"\b([A-Z][A-Za-z]*[A-Z][A-Za-z0-9]*|[A-Z][a-z]{2,}[A-Z]\w*)\b")
NOT_A_TOOL = {
    "DBMS", "DBMSs", "SQL", "RDBMS", "RDBMSs", "GDBMS", "GDBMSs", "TSMS",
    "DBMSes", "LLM", "LLMs", "API", "APIs", "CI", "IO", "OS", "AI", "ML",
    "ACID", "OLTP", "OLAP", "JDBC", "ORM", "PHP", "JIT", "SMT", "TCP", "FHE",
    "GPU", "CPU", "DISC", "NoSQL", "NewSQL", "PBT", "MPC", "ZK",
}


def tool_names(title: str) -> List[str]:
    """Distinctive tool names a title contains, such as "DBStorm"."""
    head = title.split(":")[0] if ":" in title else title
    found = []
    for match in TOOL_NAME.finditer(head):
        name = match.group(1)
        if name not in NOT_A_TOOL and len(name) >= 4:
            found.append(name)
    return found


def normalise_title(title: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower()).split())


def title_similarity(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, normalise_title(left),
                                   normalise_title(right)).ratio()


def abstract_similarity(left: dict, right: dict) -> Optional[float]:
    """How alike two records' abstracts are, or None if one is missing."""
    first = (left.get("abstract") or "").strip().lower()
    second = (right.get("abstract") or "").strip().lower()
    if len(first) < 200 or len(second) < 200:
        return None
    return difflib.SequenceMatcher(None, first[:1500], second[:1500]).ratio()


def is_preprint(paper: dict) -> bool:
    """Whether this record is a preprint rather than a publication."""
    if paper.get("arxiv_id") and not paper.get("doi"):
        return True
    return bool(PREPRINT_VENUE.search(paper.get("venue") or ""))


def _rank(paper: dict) -> tuple:
    """How good a canonical record this is. Higher wins.

    A published record beats a preprint, a DOI beats no DOI, and more evidence
    beats less -- the surviving record should be the one a reader would cite.
    """
    evidence = sum(len(entry.get("evidence") or [])
                   for entry in (paper.get("relationships") or {}).values())
    return (0 if is_preprint(paper) else 1,
            1 if paper.get("doi") else 0,
            paper.get("year") or 0,
            evidence)


def load_decisions() -> Tuple[List[dict], List[frozenset]]:
    """Merges and non-merges a person has decided, with their reasons."""
    from . import config

    path = config.DATA_DIR / "paper_decisions.json"
    if not path.exists():
        return [], []
    data = config.load_json(path)
    same = list(data.get("same_work") or [])
    different = [frozenset(entry["ids"]) for entry in data.get("different_work") or []]
    return same, different


def find_duplicates(papers: Sequence[dict]
                    ) -> Tuple[List[List[dict]], List[Tuple[dict, dict, float]]]:
    """``(groups_to_merge, pairs_for_review)``.

    A group is every record of one work, best canonical record first.
    """
    considered = [p for p in papers if not p.get("is_sqlancer_publication")]
    by_authors: Dict[frozenset, List[dict]] = defaultdict(list)
    for paper in considered:
        names = author_key(paper)
        if len(names) >= 2:      # one shared surname is a coincidence
            by_authors[names].append(paper)

    parent: Dict[str, str] = {p["id"]: p["id"] for p in considered}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    decided_same, decided_different = load_decisions()
    settled = {frozenset(entry["ids"]) for entry in decided_same} | set(decided_different)

    review: List[Tuple[dict, dict, float]] = []
    for members in by_authors.values():
        for index, left in enumerate(members):
            for right in members[index + 1:]:
                if frozenset((left["id"], right["id"])) in settled:
                    continue      # a person has already ruled on this pair
                score = title_similarity(left["title"], right["title"])
                abstracts = abstract_similarity(left, right)
                across_preprint = is_preprint(left) != is_preprint(right)

                if score >= IDENTICAL_TITLE_SIMILARITY:
                    union(left["id"], right["id"])
                elif abstracts is not None:
                    # With both abstracts in hand, they decide.
                    if abstracts >= ABSTRACT_SIMILARITY:
                        union(left["id"], right["id"])
                    elif score >= REVIEW_TITLE_SIMILARITY:
                        review.append((left, right, score))
                elif across_preprint and score >= PREPRINT_TITLE_SIMILARITY:
                    union(left["id"], right["id"])
                elif score >= REVIEW_TITLE_SIMILARITY:
                    review.append((left, right, score))

    # Merges a person decided, which no automatic rule was going to reach.
    known = {paper["id"] for paper in considered}
    for entry in decided_same:
        ids = [pid for pid in entry["ids"] if pid in known]
        for other in ids[1:]:
            union(ids[0], other)

    # An index stub can carry no authors at all, and then nothing above can
    # compare it with anything. What such a record still has is the name of the
    # tool it is about, which a paper puts in its title and almost never shares
    # with an unrelated paper. Two records naming the same tool are raised for a
    # person -- never merged automatically, because a group really can publish
    # twice about one tool.
    authorless = [p for p in considered if not author_key(p)]
    if authorless:
        by_tool: Dict[str, List[dict]] = defaultdict(list)
        for paper in considered:
            for name in tool_names(paper["title"]):
                by_tool[name].append(paper)
        for name, members in by_tool.items():
            if len(members) < 2:
                continue
            for index, left in enumerate(members):
                for right in members[index + 1:]:
                    if not author_key(left) or not author_key(right):
                        pair = frozenset((left["id"], right["id"]))
                        if pair in settled or find(left["id"]) == find(right["id"]):
                            continue
                        review.append((left, right,
                                       title_similarity(left["title"],
                                                        right["title"])))

    grouped: Dict[str, List[dict]] = defaultdict(list)
    for paper in considered:
        grouped[find(paper["id"])].append(paper)

    chosen = {entry["canonical"] for entry in decided_same if entry.get("canonical")}
    groups = [sorted(members,
                     key=lambda paper: (paper["id"] in chosen, _rank(paper)),
                     reverse=True)
              for members in grouped.values() if len(members) > 1]
    groups.sort(key=lambda members: members[0]["title"].lower())
    # A pair already merged needs no review.
    merged_ids = {p["id"] for group in groups for p in group}
    review = [(a, b, s) for a, b, s in review
              if not (a["id"] in merged_ids and find(a["id"]) == find(b["id"]))]
    return groups, review


def merge(group: Sequence[dict]) -> dict:
    """One record for the work, keeping everything either record knew.

    The published version survives, but the preprint's identifiers and evidence
    are not thrown away: an arXiv id is how the pipeline reads a paper's full
    text, so losing it would cost the merged record its own source.
    """
    canonical = dict(group[0])
    others = group[1:]

    for other in others:
        for field in ("doi", "arxiv_id", "s2_paper_id", "abstract",
                      "open_access_pdf", "url", "venue", "year"):
            if not canonical.get(field) and other.get(field):
                canonical[field] = other[field]
        canonical["cites_seed_techniques"] = sorted(
            set(canonical.get("cites_seed_techniques") or [])
            | set(other.get("cites_seed_techniques") or []))

        for key, entry in (other.get("relationships") or {}).items():
            mine = canonical.setdefault("relationships", {}).setdefault(
                key, {"value": "insufficient_evidence", "method": "deterministic",
                      "evidence": []})
            seen = {(item.get("source_url"), item.get("excerpt"))
                    for item in (mine.get("evidence") or [])}
            for item in (entry.get("evidence") or []):
                if (item.get("source_url"), item.get("excerpt")) not in seen:
                    mine.setdefault("evidence", []).append(item)
                    seen.add((item.get("source_url"), item.get("excerpt")))
            # A positive finding on either record is a finding about the work.
            if entry.get("value") == "yes" and mine.get("value") != "yes":
                mine["value"] = "yes"
                mine["method"] = entry.get("method", "deterministic")
            for technique in entry.get("techniques") or []:
                mine.setdefault("techniques", [])
                if technique not in mine["techniques"]:
                    mine["techniques"].append(technique)

        if other.get("artifacts") and not canonical.get("artifacts"):
            canonical["artifacts"] = other["artifacts"]

    canonical["also_indexed_as"] = sorted(
        {other["id"] for other in others}
        | set(canonical.get("also_indexed_as") or []))
    return canonical


def main(*, apply: bool = False) -> int:
    """Report duplicate records, and merge them with ``--apply``."""
    import json

    from . import config

    path = config.DATA_FILES["papers"]
    payload = config.load_json(path)
    groups, review = find_duplicates(payload["papers"])

    for group in groups:
        print(f"same work: {group[0]['title'][:66]}")
        print(f"    keep  {group[0]['id']}")
        for other in group[1:]:
            print(f"    merge {other['id']}")
    for left, right, score in review:
        print(f"undecided ({score:.2f}): {left['id']} / {right['id']}")
        print(f"    {left['title'][:70]}")
        print(f"    {right['title'][:70]}")
        print("    decide it in _data/impact/paper_decisions.json")

    if not apply:
        print(f"\n{len(groups)} group(s) to merge, {len(review)} undecided "
              f"(nothing written; pass --apply)")
        return 1 if review else 0

    if review:
        print("\nrefusing to merge while pairs are undecided")
        return 1

    replaced, merged_records = {}, {}
    for group in groups:
        record = merge(group)
        merged_records[record["id"]] = record
        for other in group[1:]:
            replaced[other["id"]] = record["id"]
    payload["papers"] = [merged_records.get(p["id"], p) for p in payload["papers"]
                         if p["id"] not in replaced]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(f"\nmerged {len(replaced)} record(s); {len(payload['papers'])} remain")
    return 0
