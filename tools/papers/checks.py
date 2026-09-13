"""Regular expressions run over the mention inventory, as a second opinion.

These do not decide anything. They exist because a model reading a hundred
papers will occasionally miss a sentence that plainly says "we implemented our
tool on top of SQLancer", and because a disagreement between a pattern and a
judgement is worth surfacing either way: sometimes the pattern is wrong,
sometimes the reading is, and which it is can only be settled by looking.

The patterns are the ones the old pipeline used to decide with, kept here in
the role they are actually good at. Their weaknesses are on record: "we adapted
SQLancer as a baseline for comparison" matched the extension pattern until the
sentence was read to the end, which is exactly the sort of thing an advisory
check should raise rather than settle.
"""

from __future__ import annotations

import re
from typing import Dict, List, Sequence

CHECKS_VERSION = "paper-checks-v1"

_TOOL = r"(?:SQLancer\+\+|SQLancer|ShQveL)"
_TECH = (r"(?:TLP|NoREC|PQS|QPG|CERT|DQP|CODDTest|Ternary Logic Partitioning"
         r"|Non-optimizing Reference Engine Construction|Pivoted Query Synthesis"
         r"|Query Plan Guidance|query partitioning)")

PATTERNS = {
    "uses_infrastructure": (
        # A claim about the authors' own artefact. "we" or "our" is required:
        # without it a figure caption reads as a claim of reuse.
        re.compile(rf"\b(?:we|our)\b[^.]{{0,120}}?"
                   rf"\b(?:implemented|implements|implement|built|build|"
                   rf"developed|develops|prototyped|prototype|based|extended|"
                   rf"extends|modified|integrated|adapted|adopt(?:ed)?)\b"
                   rf"[^.]{{0,80}}?\b(?:on\s+top\s+of|upon|on|in|into|using|"
                   rf"from)\s+(?:the\s+)?{_TOOL}\b", re.IGNORECASE),
        re.compile(rf"\b(?:is|are|was|were)\s+(?:implemented|built|developed|"
                   rf"based|derived)\b[^.]{{0,60}}?\b(?:on\s+top\s+of|upon|on|"
                   rf"in|from)\s+(?:the\s+)?{_TOOL}\b", re.IGNORECASE),
        re.compile(rf"\bframework\s+is\s+derived\s+from\s+{_TOOL}\b", re.IGNORECASE),
    ),
    "compares_with": (
        re.compile(rf"\b(?:we|our)\b[^.]{{0,140}}?\b(?:compare[ds]?|comparison|"
                   rf"evaluat\w+|benchmark\w*|baseline[s]?|against|outperform\w*)"
                   rf"\b[^.]{{0,140}}?\b(?:{_TOOL}|{_TECH})\b", re.IGNORECASE),
        re.compile(rf"\b(?:{_TOOL}|{_TECH})\b[^.]{{0,80}}?\bas\s+"
                   rf"(?:a\s+|our\s+|the\s+)?baseline[s]?\b", re.IGNORECASE),
        re.compile(rf"\b(?:than|versus|vs\.?)\s+(?:{_TOOL}|{_TECH})\b",
                   re.IGNORECASE),
    ),
    "extends_technique": (
        re.compile(rf"\b(?:we|our)\b[^.]{{0,120}}?\b(?:extend(?:ed|s)?|"
                   rf"generali[sz]\w+|adapt(?:ed|s)?|build[s]?\s+(?:up)?on|"
                   rf"inspired\s+by|derive[ds]?\s+from|improve[sd]?\s+(?:up)?on)"
                   rf"\b[^.]{{0,80}}?\b(?:{_TECH}|{_TOOL})\b", re.IGNORECASE),
    ),
    "describes_as_state_of_the_art": (
        re.compile(rf"\b(?:{_TOOL}|{_TECH})\b[^.]{{0,90}}?\b(?:state[- ]of[- ]"
                   rf"the[- ]art|leading|most\s+effective|best[- ]known)\b",
                   re.IGNORECASE),
        re.compile(rf"\b(?:state[- ]of[- ]the[- ]art|leading|most\s+effective)\b"
                   rf"[^.]{{0,90}}?\b(?:{_TOOL}|{_TECH})\b", re.IGNORECASE),
    ),
}

# Wording that turns a sentence into a statement about somebody else's work.
RELATED_WORK = re.compile(
    r"\b(?:previous|prior|existing|earlier|related)\s+(?:work|approaches|"
    r"studies|techniques|tools)\b|\bhas\s+been\s+(?:proposed|shown)\b",
    re.IGNORECASE)

# "as a baseline" turns adopting a tool into measuring against it, which is the
# opposite of building on it. This is the sentence that made the old pipeline
# record TSGuard as extending a technique.
AS_A_BASELINE = re.compile(
    r"\bas\s+(?:a\s+|our\s+|the\s+)?baselines?\b|\bfor\s+comparison\b",
    re.IGNORECASE)

NEVER_WITH_BASELINE = ("extends_technique", "uses_infrastructure")


def run(mentions: Sequence[dict]) -> Dict[str, object]:
    """Which mentions each pattern fires on, and what qualifies the result."""
    fired: Dict[str, List[str]] = {key: [] for key in PATTERNS}
    notes: List[dict] = []

    for mention in mentions:
        sentence = mention.get("sentence") or ""
        baseline = bool(AS_A_BASELINE.search(sentence))
        related = bool(RELATED_WORK.search(sentence))
        for key, patterns in PATTERNS.items():
            if not any(pattern.search(sentence) for pattern in patterns):
                continue
            if baseline and key in NEVER_WITH_BASELINE:
                notes.append({
                    "mention_id": mention["id"],
                    "pattern": key,
                    "suppressed_because": (
                        "the sentence says the tool was taken up as a baseline, "
                        "which is comparison rather than reuse or extension"),
                })
                continue
            if related and key != "describes_as_state_of_the_art":
                notes.append({
                    "mention_id": mention["id"],
                    "pattern": key,
                    "suppressed_because": (
                        "the sentence is framed as related work, so it "
                        "describes somebody else's approach"),
                })
                continue
            fired[key].append(mention["id"])

    return {
        "checks_version": CHECKS_VERSION,
        "suggests": {key: ids for key, ids in fired.items() if ids},
        "suppressed": notes,
    }
