"""The analysis task, written once and used by both runners.

The design constraint that shapes everything here: **the model never supplies a
quotation.** It is given a numbered inventory of mentions, each already carrying
a sentence taken verbatim from the paper, and it answers by citing those
numbers. The writer substitutes the stored text. A fabricated quotation is not
detected and discarded, as the old classifier had to do -- it is not expressible.

The criteria below are not general good sense. Each one is written the way it is
because the previous approach got that exact case wrong:

* "we adapted SQLancer as a baseline for comparison" was recorded as *extending*
  a technique, because a pattern matched the first half of the sentence.
* Chronos uses SQLancer only to generate a workload for MySQL-Cluster, and the
  dataset could say it reused the infrastructure but not in what sense.
* A paper citing "[9]-[11]" says nothing a name search can see, so the fact that
  reference 9 is the PQS paper has to be given to the reader explicitly.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Sequence

PROMPT_VERSION = "paper-analysis-v1"

RELATIONSHIPS = ("uses_infrastructure", "extends_technique", "compares_with",
                 "describes_as_state_of_the_art")

ROLES = (
    "background", "motivation", "definition", "reuse_implementation",
    "reuse_component", "extension", "baseline", "result_comparison",
    "state_of_the_art", "incidental",
)

ANSWERS = ("yes", "no", "uncertain", "insufficient_evidence")

REUSE_KINDS = ("implementation", "generator", "workload", "unclear")

SYSTEM = """\
You are recording how one research paper relates to SQLancer, a testing tool for
database management systems, and to the techniques introduced through it: PQS
(Pivoted Query Synthesis), NoREC (Non-optimizing Reference Engine Construction),
TLP (Ternary Logic Partitioning), QPG (Query Plan Guidance), CERT, DQP
(Differential Query Plans) and CODDTest.

You are given the paper's metadata and a numbered inventory of every place the
paper refers to SQLancer -- by name, by technique, or through a citation marker
resolving to one of its publications. Each mention carries the sentence it
appears in, the paragraph around it, its section and its page.

You do not have the paper. You have these mentions, and they are the evidence.

## Steps, in this order

1. Read the metadata and the entire inventory before deciding anything. A paper
   that reuses SQLancer usually says so once, in one sentence, somewhere in the
   implementation or evaluation section.
2. Give every mention a role from this list: background, motivation, definition,
   reuse_implementation, reuse_component, extension, baseline,
   result_comparison, state_of_the_art, incidental.
3. Decide each relationship using the criteria below, citing the mention ids
   that settle it. A mention cited for a relationship must have a role
   consistent with it.
4. Write two pieces of prose in your own words: a summary of what the paper is
   and does, and a narrative of how SQLancer figures in it.
5. Compare your decisions with the advisory regex findings you were given.
   Explain any disagreement, in either direction.
6. Say what you could not determine, and why.

## Criteria

**uses_infrastructure** -- yes only if the paper's *own implementation* reuses
the SQLancer codebase. "We implemented X on top of SQLancer", "our framework is
derived from SQLancer", "our database generation is adopted from SQLancer" all
qualify. Also set reuse_kind:
  - implementation: the paper's tool is built on SQLancer
  - generator: it uses SQLancer's query or database generation inside its own tool
  - workload: it runs SQLancer only to produce input for something else, and is
    not built on it in any other sense
  - unclear: reuse is stated but its extent is not
Workload use is still reuse, but the narrative must say which kind it is.

**extends_technique** -- yes only if extending, generalising or adapting a
SQLancer technique is part of what the paper claims as its contribution.
Carrying TLP to graph queries is extension. Adapting the tool in order to *run
it as a baseline* is not: that is compares_with, and nothing else. If a sentence
contains both "adapted" and "as a baseline", it is a comparison.

**compares_with** -- yes if the paper empirically runs SQLancer or one of its
techniques and reports the comparison, including reporting that it found more
bugs or more coverage. Appearing in a list of related work is not a comparison.

**describes_as_state_of_the_art** -- yes if the text calls SQLancer or one of
its techniques the state of the art, leading, or the most effective approach.
An author's own tool being better is not this; being described as the bar to
beat is.

## Rules

- Answer each relationship with yes, no, uncertain, or insufficient_evidence.
  Prefer uncertain to a guess. "insufficient_evidence" means the mentions do not
  address the question at all.
- Every "yes" must cite at least one mention id. A yes with no ids is rejected.
- Cite ids; never write a quotation. The text of a mention is already recorded
  and will be attached to whatever you cite.
- Judge only what the mentions show. If a paper's artifact was inspected you are
  told so separately; do not infer reuse from a paper's subject matter, its
  authors, or its venue.
"""


def _mention_block(mention: dict) -> str:
    parts = [f"{mention['id']}  [{mention.get('section') or 'unknown section'}"
             f", page {mention.get('page') or '?'}]"]
    if mention.get("cited_reference"):
        reference = mention["cited_reference"]
        parts.append(f"    cites [{reference['number']}] = {reference['text'][:150]}")
    if mention.get("techniques"):
        parts.append(f"    techniques named: {', '.join(mention['techniques'])}")
    parts.append(f"    SENTENCE: {mention['sentence']}")
    if mention.get("context_before"):
        parts.append(f"    before: ...{mention['context_before'][-260:]}")
    if mention.get("context_after"):
        parts.append(f"    after: {mention['context_after'][:260]}...")
    return "\n".join(parts)


def build(paper: dict, inventory: dict, findings: dict,
          artifact: Optional[dict] = None) -> Dict[str, str]:
    """The system and user messages for one paper."""
    metadata = {
        "title": paper.get("title"),
        "authors": (paper.get("authors") or [])[:10],
        "year": paper.get("year"),
        "venue": paper.get("venue"),
        "doi": paper.get("doi"),
        "abstract": (paper.get("abstract") or "")[:2000] or None,
    }

    lines = [f"PAPER: {json.dumps(metadata, ensure_ascii=False, indent=1)}", ""]

    references = inventory.get("sqlancer_references") or []
    if references:
        lines.append("REFERENCES IN THIS PAPER'S BIBLIOGRAPHY THAT MATTER HERE")
        for reference in references:
            kind = ("a SQLancer publication"
                    if reference.get("matched_as") == "sqlancer_publication"
                    else "by a SQLancer author, but NOT a SQLancer paper")
            lines.append(f"  [{reference['number']}] ({kind}) "
                         f"{reference['text'][:180]}")
        lines.append("")
        lines.append("A mention found only through a reference of the second "
                     "kind is not evidence about SQLancer. Say so if that is "
                     "all there is.")
        lines.append("")

    if artifact and artifact.get("markers"):
        lines.append("ARTIFACT INSPECTION (evidence from the paper's repository, "
                     "not from its text). Cite it as ARTIFACT, like a mention.")
        lines.append(f"  ARTIFACT  repository: {artifact.get('url')}")
        lines.append(f"            markers found: {', '.join(artifact['markers'])}")
        lines.append("  A paper whose repository is a SQLancer fork need never "
                     "say so in its text, so this can be the only evidence of "
                     "reuse. It says nothing about the other relationships.")
        lines.append("")

    mentions = inventory.get("mentions") or []
    lines.append(f"MENTIONS ({len(mentions)})")
    lines.append("")
    for mention in mentions:
        lines.append(_mention_block(mention))
        lines.append("")

    suggests = (findings or {}).get("suggests") or {}
    suppressed = (findings or {}).get("suppressed") or []
    lines.append("ADVISORY REGEX FINDINGS (a second opinion, not a verdict)")
    if suggests:
        for key, ids in suggests.items():
            lines.append(f"  a pattern for {key} matched: {', '.join(ids)}")
    else:
        lines.append("  no pattern matched any mention")
    for note in suppressed:
        lines.append(f"  {note['mention_id']}: a {note['pattern']} pattern "
                     f"matched but was set aside because "
                     f"{note['suppressed_because']}")
    lines.append("")
    lines.append("Now carry out the six steps and answer with the JSON object "
                 "described in the output schema.")
    return {"system": SYSTEM, "user": "\n".join(lines)}


def output_schema() -> dict:
    """The shape of the answer, for a structured-output request."""
    return {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "narrative", "roles", "relationships",
                         "disagreements", "unresolved"],
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Three to five sentences: what the paper is "
                                   "and what it does. Your own words.",
                },
                "narrative": {
                    "type": "string",
                    "description": "One to three sentences on how SQLancer "
                                   "figures in this paper. Your own words.",
                },
                "roles": {
                    "type": "object",
                    "description": "Mention id to role, for every mention.",
                    "additionalProperties": {"type": "string", "enum": list(ROLES)},
                },
                "relationships": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(RELATIONSHIPS),
                    "properties": {
                        key: {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["value", "mention_ids", "reasoning"],
                            "properties": {
                                "value": {"type": "string", "enum": list(ANSWERS)},
                                "mention_ids": {"type": "array",
                                                "items": {"type": "string"}},
                                "reasoning": {"type": "string"},
                                **({"reuse_kind": {"type": "string",
                                                   "enum": list(REUSE_KINDS)}}
                                   if key == "uses_infrastructure" else {}),
                                "techniques": {"type": "array",
                                               "items": {"type": "string"}},
                            },
                        }
                        for key in RELATIONSHIPS
                    },
                },
                "disagreements": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Where you differ from the advisory findings, "
                                   "and why.",
                },
                "unresolved": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "What the mentions could not settle.",
                },
            },
        },
    }
