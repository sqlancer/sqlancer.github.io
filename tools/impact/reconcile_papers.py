"""Makes the impact dataset's paper relationships follow the paper analyses.

Two things classified the same papers. The impact pipeline did it from citation
contexts and artifact inspection, a few sentences at a time; the papers
subproject does it from the whole extracted text, with every mention of
SQLancer in front of it and the reasoning recorded. The second is better, and
where they disagree the second is right -- it is why the subproject was built.

Until now they simply coexisted, so the impact page counted 19 papers reusing
SQLancer infrastructure while the analyses recorded 22. This copies each
analysed relationship across, keeping the analysis's value, its techniques and
its quoted sentences as evidence, and marks the method as the analysis rather
than as whatever determined the old value.

A paper with no analysis is left exactly as it was: an analysis is the only
thing that supersedes the earlier reading, and its absence says nothing.
"""

from __future__ import annotations

import json
import pathlib
from typing import Dict, List, Optional, Tuple

from . import config
from .util import content_hash, now, truncate

RECORD_DIR = config.REPO_ROOT / "_data" / "papers"
METHOD = "llm_classification"
RELATIONSHIPS = ("uses_infrastructure", "extends_technique", "compares_with",
                 "describes_as_state_of_the_art")

# The relationship the citation graph settles. An analysis can contest it, but
# it is established upstream and is not this step's to overwrite.
CITATION_GRAPH = "references"


def analyses() -> Dict[str, dict]:
    """Every stored analysis, keyed by the paper id it belongs to."""
    found: Dict[str, dict] = {}
    for path in sorted(RECORD_DIR.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        analysis = record.get("analysis")
        paper_id = (record.get("paper") or {}).get("id")
        if analysis and paper_id:
            found[paper_id] = {"analysis": analysis, "key": path.stem,
                               "record": record}
    return found


def _evidence(quotes: List[dict], key: str, timestamp: str) -> List[dict]:
    """The analysis's quoted sentences, as impact evidence.

    Each is a sentence stored verbatim when the paper's text was extracted, so
    the source is the record it lives in rather than a URL the sentence can be
    read at -- the paper itself is often behind a paywall, and pointing there
    would suggest the quote can be checked where it cannot.
    """
    url = ("https://github.com/sqlancer/sqlancer.github.io/blob/main/"
           f"_data/papers/{key}.json")
    evidence = []
    for quote in quotes or []:
        sentence = (quote.get("sentence") or "").strip()
        if not sentence:
            continue
        where = ", ".join(part for part in [
            quote.get("section"),
            f"page {quote['page']}" if quote.get("page") else None] if part)
        evidence.append({
            "source_url": url,
            "source_type": "paper",
            "excerpt": truncate(sentence, 500),
            "excerpt_is_verbatim": True,
            "note": (f"{quote.get('mention_id', 'A sentence')} in the paper's "
                     f"extracted text" + (f", {where}" if where else "") + "."),
            "content_sha256": content_hash(sentence),
            "retrieved_at": timestamp,
            "first_seen": timestamp,
            "last_verified": timestamp,
        })
    return evidence


def apply(papers: List[dict], found: Optional[Dict[str, dict]] = None,
          timestamp: Optional[str] = None) -> Tuple[int, List[dict]]:
    """Copy each analysis onto its paper; returns ``(changed, differences)``."""
    timestamp = timestamp or now()
    found = analyses() if found is None else found
    changed = 0
    differences: List[dict] = []

    for paper in papers:
        entry = found.get(paper.get("id"))
        if entry is None:
            continue
        analysis = entry["analysis"]
        touched = False
        for name in RELATIONSHIPS:
            decided = (analysis.get("relationships") or {}).get(name)
            if not decided:
                continue
            before = paper["relationships"].get(name) or {}
            evidence = _evidence(decided.get("quotes"), entry["key"], timestamp)
            if decided.get("value") == "yes" and not evidence:
                # The schema requires evidence for a positive claim, and an
                # analysis that cites no sentence cannot supply one. Leaving
                # the old value is wrong too, so the disagreement is reported.
                differences.append({
                    "paper": paper["id"], "relationship": name,
                    "was": before.get("value"), "now": decided["value"],
                    "reason": "analysis says yes but cites no sentence",
                    "applied": False,
                })
                continue
            after = {"value": decided["value"], "method": METHOD}
            if decided.get("techniques"):
                after["techniques"] = decided["techniques"]
            if evidence:
                after["evidence"] = evidence
            if before.get("value") != after["value"]:
                differences.append({
                    "paper": paper["id"], "relationship": name,
                    "was": before.get("value"), "now": after["value"],
                    "reason": decided.get("reasoning"), "applied": True,
                })
            if before != after:
                paper["relationships"][name] = after
                touched = True
        if touched:
            changed += 1
    return changed, differences


def main() -> int:
    from .dataset import Dataset

    dataset = Dataset()
    papers = dataset.records("papers")
    changed, differences = apply(papers)
    # Written before anything is reported. The report runs to hundreds of
    # lines, and piping it to `head` closed the pipe and killed the process
    # mid-print, losing the work it had already done.
    dataset.save(["papers"])

    flipped = [d for d in differences if d["applied"]]
    blocked = [d for d in differences if not d["applied"]]
    print(f"{changed} paper(s) updated, {len(flipped)} classification(s) changed"
          + (f", {len(blocked)} could not be applied" if blocked else ""))
    for row in flipped:
        print(f"  {row['relationship']}: {row['was']} -> {row['now']}  "
              f"{row['paper']}")
    for row in blocked:
        print(f"  SKIPPED {row['relationship']} on {row['paper']}: "
              f"{row['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
