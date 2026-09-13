"""The bounded questions the classifier is allowed to ask.

Each entry pairs one narrow question with a strict output schema. The design
constraint throughout is that the model is a reader, not a researcher: it is
handed source text that deterministic collection already fetched, and it may
only answer about that text. It cannot browse, cannot introduce a candidate, and
cannot supply an excerpt that is not literally present in what it was given --
the caller re-checks every returned excerpt against the source and discards the
answer if it does not match.

``uncertain`` and ``insufficient_evidence`` are first-class answers. The prompts
say so explicitly, because a classifier that feels obliged to choose yes or no
is exactly how unfounded claims get into a dataset.
"""

from __future__ import annotations

from typing import Dict, List, Optional

# Bumping a version here invalidates the cached answers for that question only.
VERSIONS = {
    "bug_attribution": "bug-attribution-v1",
    "paper_infrastructure": "paper-infrastructure-v1",
    "paper_extends": "paper-extends-v1",
    "paper_compares": "paper-compares-v1",
    "adoption": "adoption-v1",
    "paper_summary": "paper-summary-v1",
}

SHARED_RULES = """\
Rules that apply to every answer:

- Answer only from the SOURCE TEXT provided below. You have no other knowledge \
of this artefact and must not assume anything the text does not state.
- Every excerpt you return must be copied character-for-character from the \
SOURCE TEXT. Do not paraphrase, summarise, translate, correct, or join \
non-contiguous passages. If you cannot find a passage that supports an answer, \
you do not have the evidence for that answer.
- Prefer "uncertain" when the text points both ways, and \
"insufficient_evidence" when the text simply does not address the question. \
Neither is a failure; forcing a "yes" or "no" is.
- Answer "yes" only when the SOURCE TEXT itself supports it."""


def _schema(properties: Dict[str, dict], required: List[str]) -> dict:
    return {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


ANSWER_ENUM = ["yes", "no", "uncertain", "insufficient_evidence"]

_EXCERPTS = {
    "type": "array",
    "items": {"type": "string"},
    "description": ("Passages copied verbatim from the SOURCE TEXT that support "
                    "the answer. Empty unless the answer is 'yes'."),
}

_REASON = {
    "type": "string",
    "description": ("One or two sentences explaining the answer in your own "
                    "words. This is your reasoning, not evidence, and is stored "
                    "separately from the excerpts."),
}


def bug_attribution(*, source_label: str, source_text: str,
                    technique_names: List[str], finder_names: List[str],
                    excluded_names: List[str]) -> dict:
    """Does this bug report attribute the find to SQLancer or one of its techniques?"""
    system = f"""\
You classify bug reports for the SQLancer project's public impact dataset.

{SHARED_RULES}

A bug counts as found by SQLancer when the report connects it to one of these \
tools: {', '.join(finder_names)}; or to one of these testing techniques, which \
originated in SQLancer: {', '.join(technique_names)}.

Two traps to avoid:

- These technique names are also ordinary acronyms in other fields. "TLP" is \
thread-level parallelism and a Linux power-management tool; "PQS" and "CERT" \
have unrelated meanings too. Answer "yes" only when the text is clearly about \
testing a database system.
- These techniques did NOT originate in SQLancer and do not count, even if a \
SQLancer implementation of them exists: {', '.join(excluded_names) or 'none'}. \
If the report credits one of those, the answer is "no"."""

    user = f"""\
Question: Does this bug report attribute the discovery to SQLancer, to one of \
its umbrella tools, or to a testing technique that originated in SQLancer?

If yes, name the specific tool and technique the report credits. Leave either \
as null if the report does not say.

SOURCE ({source_label}):
<source_text>
{source_text}
</source_text>"""

    schema = _schema({
        "answer": {"type": "string", "enum": ANSWER_ENUM},
        "finder": {"type": ["string", "null"],
                   "description": "Tool credited, or null if unnamed."},
        "technique": {"type": ["string", "null"],
                      "description": "Technique credited, or null if unnamed."},
        "excerpts": _EXCERPTS,
        "reason": _REASON,
    }, ["answer", "finder", "technique", "excerpts", "reason"])

    return {"system": system, "user": user, "output_format": schema}


def paper_uses_infrastructure(*, source_label: str, source_text: str,
                              markers: List[str]) -> dict:
    """Does this paper's implementation use or derive from the SQLancer codebase?"""
    marker_note = (
        "Deterministic inspection of the artefact found these signals: "
        + ", ".join(markers) + ".\n"
        if markers else
        "Deterministic inspection of the artefact found no reuse signals.\n")

    system = f"""\
You classify research papers for the SQLancer project's public impact dataset.

{SHARED_RULES}

"Uses SQLancer infrastructure" means the paper's implementation reuses or \
derives from the SQLancer codebase: a fork of the repository, retained SQLancer \
source files or its Java package layout, a SQLancer copyright notice, git \
history derived from it, or a README saying so.

Judge the implementation, not the framing. Citing SQLancer, comparing against \
it, or building on one of its ideas is NOT infrastructure reuse. Equally, no \
single file name settles it: SQLancer's Randomly.java in particular turns up in \
projects that copied one utility and nothing else, so weigh the signals together."""

    user = f"""\
Question: Does this paper's implementation use or derive from the SQLancer \
codebase?

{marker_note}
SOURCE ({source_label}):
<source_text>
{source_text}
</source_text>"""

    schema = _schema({
        "answer": {"type": "string", "enum": ANSWER_ENUM},
        "excerpts": _EXCERPTS,
        "reason": _REASON,
    }, ["answer", "excerpts", "reason"])

    return {"system": system, "user": user, "output_format": schema}


def paper_extends_technique(*, source_label: str, source_text: str,
                            technique_names: List[str]) -> dict:
    """Does this paper extend, generalise or adapt a SQLancer technique?"""
    system = f"""\
You classify research papers for the SQLancer project's public impact dataset.

{SHARED_RULES}

Techniques that originated in SQLancer: {', '.join(technique_names)}.

"Extends a SQLancer technique" means the paper claims a technical contribution \
that extends, generalises, adapts, or substantially builds upon one of them --- \
generalising TLP to a new setting, extending NoREC with a new transformation, \
adapting PQS to another data model, or a new method whose central mechanism is \
explicitly built on one of these techniques.

Merely citing a technique as related work, describing it in a background \
section, or using it unchanged as a baseline is NOT extending it. If the text \
only shows the technique being described or compared against, answer "no"."""

    user = f"""\
Question: Does this paper claim a technical contribution that extends, \
generalises, or adapts a technique introduced through SQLancer?

If yes, name the technique or techniques and quote the passages that show the \
extension.

SOURCE ({source_label}):
<source_text>
{source_text}
</source_text>"""

    schema = _schema({
        "answer": {"type": "string", "enum": ANSWER_ENUM},
        "techniques": {
            "type": "array",
            "items": {"type": "string"},
            "description": "SQLancer techniques the paper extends.",
        },
        "excerpts": _EXCERPTS,
        "reason": _REASON,
    }, ["answer", "techniques", "excerpts", "reason"])

    return {"system": system, "user": user, "output_format": schema}


def paper_compares_with(*, source_label: str, source_text: str,
                        technique_names: List[str]) -> dict:
    """Does this paper empirically compare against SQLancer or one of its oracles?"""
    system = f"""\
You classify research papers for the SQLancer project's public impact dataset.

{SHARED_RULES}

SQLancer techniques: {', '.join(technique_names)}.

"Compares against SQLancer" means the paper runs an empirical comparison ---
using SQLancer, a SQLancer implementation, or one of its test oracles as a \
baseline, and reporting results against it.

A citation in related work is not a comparison. A stated intention to compare, \
with no results, is not a comparison. The text must show the comparison being \
made or its outcome being reported."""

    user = f"""\
Question: Does this paper empirically evaluate its approach against SQLancer or \
a SQLancer technique?

If yes, name which ones it compares against.

SOURCE ({source_label}):
<source_text>
{source_text}
</source_text>"""

    schema = _schema({
        "answer": {"type": "string", "enum": ANSWER_ENUM},
        "techniques": {
            "type": "array",
            "items": {"type": "string"},
            "description": "SQLancer techniques used as a baseline.",
        },
        "excerpts": _EXCERPTS,
        "reason": _REASON,
    }, ["answer", "techniques", "excerpts", "reason"])

    return {"system": system, "user": user, "output_format": schema}


def adoption(*, source_label: str, source_text: str, dbms_name: str,
             finder_names: List[str]) -> dict:
    """Does this show the DBMS project's own developers using SQLancer?"""
    system = f"""\
You classify evidence for the SQLancer project's public impact dataset.

{SHARED_RULES}

The question is whether the {dbms_name} project's OWN developers use or \
integrate one of these tools: {', '.join(finder_names)}.

What counts: SQLancer running in the project's continuous integration; scripts \
or configuration the project maintains for running it; the project's testing \
documentation describing it; a developer of the project describing their use of \
it; a SQLancer integration contributed or maintained by the project's team.

What does not count, and this is the distinction that matters most: SQLancer \
supporting {dbms_name} is not adoption. Nor is an outside researcher testing \
{dbms_name} with SQLancer, or a bug report filed by someone who is not part of \
the project. The evidence must come from the project or its own developers."""

    user = f"""\
Question: Does this source show that {dbms_name}'s own developers use or \
integrate SQLancer?

If yes, classify how, using exactly one of these values: official_ci, \
official_testing, developer_use, integration_contributed_by_dbms_team.

SOURCE ({source_label}):
<source_text>
{source_text}
</source_text>"""

    schema = _schema({
        "answer": {"type": "string", "enum": ANSWER_ENUM},
        "relationship": {
            "type": ["string", "null"],
            "enum": ["official_ci", "official_testing", "developer_use",
                     "integration_contributed_by_dbms_team", None],
        },
        "excerpts": _EXCERPTS,
        "reason": _REASON,
    }, ["answer", "relationship", "excerpts", "reason"])

    return {"system": system, "user": user, "output_format": schema}


def paper_summary(*, source_label: str, source_text: str,
                  title: str, source_kind: str) -> dict:
    """Summarise a citing paper and say how it relates to SQLancer.

    The one question here whose output is prose rather than a verdict, so it is
    the one place the no-paraphrase rule cannot apply to the answer itself. It
    applies to the excerpts instead: the summary has to be accompanied by
    passages that are literally in the text, and the caller drops any that are
    not. A summary nobody can check against a quotation is marked as such
    rather than published as fact.
    """
    system = f"""\
You are summarising a research paper for a dataset that records how other work \
relates to SQLancer, a testing tool for database management systems.

You are given {source_kind} for the paper. Write a short factual summary of \
what the paper is about and what it does, in three or four sentences, in your \
own words. Then, separately, say what the text shows about its relationship to \
SQLancer -- reusing its code, extending one of its techniques (NoREC, TLP, PQS, \
QPG, CERT, DQP, CODDTest), comparing against it, calling it state of the art, \
or only citing it.

{SHARED_RULES}

Two more rules specific to this task:

- The summary is yours to write in your own words; the excerpts are not. Every \
excerpt must be copied character-for-character from the SOURCE TEXT.
- Describe only what this text supports. If you are working from an abstract or \
from citation sentences rather than the full paper, say less rather than \
guessing at the rest. Do not state what the paper's results were unless the \
text says so."""

    user = f"""\
PAPER: {title}
SOURCE: {source_label}

SOURCE TEXT:
-----
{source_text}
-----

Summarise the paper, state its relationship to SQLancer, and quote the passages \
you relied on."""

    return {
        "system": system,
        "user": user,
        "output_format": _schema(
            {
                "answer": {
                    "type": "string",
                    "enum": ANSWER_ENUM,
                    "description": ("'yes' when the text was enough to write a "
                                    "grounded summary, 'insufficient_evidence' "
                                    "when it was not."),
                },
                "summary": {
                    "type": "string",
                    "description": ("Three or four sentences on what the paper "
                                    "is about and what it does. Your own words."),
                },
                "relationship_to_sqlancer": {
                    "type": "string",
                    "description": ("One or two sentences on how this paper "
                                    "relates to SQLancer, as far as this text "
                                    "shows. Say so plainly if it only cites it."),
                },
                "excerpts": _EXCERPTS,
                "reason": _REASON,
            },
            ["answer", "summary", "relationship_to_sqlancer", "excerpts", "reason"]),
    }


BUILDERS = {
    "bug_attribution": bug_attribution,
    "paper_summary": paper_summary,
    "paper_infrastructure": paper_uses_infrastructure,
    "paper_extends": paper_extends_technique,
    "paper_compares": paper_compares_with,
    "adoption": adoption,
}
