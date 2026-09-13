"""Manually verified paper classifications for the seed dataset.

The automated classifier answers these questions in the weekly run. This file is
what the dataset was bootstrapped with: a small set of relationships a person
read the citation contexts and confirmed, so the whole pipeline -- schemas,
merging, statistics, plots, the page -- could be exercised against real records
before broad automatic discovery was switched on.

Each entry names the paper by its stable record id and quotes the passage the
classification rests on. The quotes are not typed out here as a convenience:
:func:`apply` refuses to write an entry whose excerpt is not present verbatim in
the citation contexts already stored for that paper, so this file cannot
introduce a claim that the collected evidence does not support.

Entries are recorded with ``method: manual_curation`` and stay put; the
classifier only fills in relationships that are not already decided.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .. import config, taxonomy
from ..util import content_hash, now, verbatim_excerpt

CURATOR_VERSION = "manual-curation-v1"

# paper id -> relationship -> {techniques, excerpts}
CLASSIFICATIONS: Dict[str, Dict[str, dict]] = {
    # GDBMeter: takes Query Partitioning / TLP, proposed for relational DBMSs,
    # and applies it to graph database engines.
    "paper:doi:10.1145/3597926.3598044": {
        "extends_technique": {
            "techniques": ["tlp"],
            "excerpts": [
                "The key insight of this paper is that the high-level idea of "
                "Query Partitioning, and speciﬁcally TLP, is applicable and "
                "eﬀective in ﬁnding logic bugs in GDBMSs and addresses the "
                "aforementioned challenges.",
                "This paper has demonstrated that Ternary Logic Partitioning "
                "(TLP), a testing approach that was previously proposed for "
                "testing RDBMSs, can also be applied to testing GDBMSs.",
            ],
        },
    },
    # Extends query partitioning into compound graph-aware metamorphic relations.
    "paper:doi:10.14778/3636218.3636236": {
        "extends_technique": {
            "techniques": ["tlp"],
            "excerpts": [
                "We next propose the pattern partitioning technique to generate "
                "compound MRs by extending the query partitioning technique in "
                "the literature [34, 42].",
                "It ports the idea of ternary query partitioning [42] used in "
                "relational databases to GDBs.",
            ],
        },
    },
    # XPress: adapts the PQS pivot-row idea to a different data model, XML.
    "paper:doi:10.1145/3597503.3639208": {
        "extends_technique": {
            "techniques": ["pqs"],
            "excerpts": [
                "The targeted node in XPress was inspired by the pivot row in "
                "Pivoted Query Synthesis (PQS) [36], which was originally "
                "proposed to test relational DBMSs.",
            ],
        },
    },
    # Applies the PQS idea to validating isolation-level implementations.
    "paper:doi:10.1145/3627703.3650080": {
        "extends_technique": {
            "techniques": ["pqs"],
            "excerpts": [
                "This approach is inspired by pivoted query synthesis [41], "
                "which uses a data-base system specific SQL interpreter to "
                "execute a predicate condition to determine if a given row "
                "would match the predicate.",
            ],
        },
    },
    "paper:doi:10.1145/3797871": {
        "extends_technique": {
            "techniques": ["pqs"],
            "excerpts": [
                "This approach is inspired by pivoted query synthesis [47], "
                "which uses a database system specific SQL interpreter to "
                "execute a predicate condition to determine if a given row "
                "would match the predicate.",
            ],
        },
    },
    # Modified the SQLancer tool itself to reach database connectors.
    "paper:doi:10.1109/ase63991.2025.00322": {
        "uses_infrastructure": {
            "techniques": [],
            "excerpts": [
                "We adapted SQLancer [2], a state-of-the-art JDBC-based "
                "database testing tool, to support multiple database connectors "
                "and evaluated both approaches over 100 rounds.",
            ],
        },
    },
    # SRS is built on the SQLancer codebase and evaluated against its oracles.
    "paper:doi:10.1145/3769828": {
        "uses_infrastructure": {
            "techniques": [],
            "excerpts": [
                "We implemented SRS on top of SQLancer 1 , a DBMS testing "
                "framework designed for the random generation of database "
                "states and SQL queries, which also supports multiple test "
                "oracles [23, 24].",
            ],
        },
        "compares_with": {
            "techniques": ["pqs", "tlp", "norec", "dqp"],
            "excerpts": [
                "We selected six state-of-the-art approaches for comparison: "
                "PQS [25], TLP [24], NoREC [23], Pinolo [11], EET [13], and "
                "DQP [3].",
            ],
        },
    },
    "paper:arxiv:2608.30385": {
        "compares_with": {
            "techniques": ["tlp"],
            "excerpts": [
                "We selected 4 state-of-the-art DBMS logic bug detection "
                "approaches as baselines: EDC [4], Radar [34], EET [11], and "
                "TLP [30].",
            ],
        },
    },
    "paper:arxiv:2406.09469": {
        "compares_with": {
            "techniques": ["tlp", "norec"],
            "excerpts": [
                "We compared S EM C ON T with TLP [41] and NoREC [40], which "
                "are state-of-the-art metamorphic testing methods for testing "
                "RDBMS.",
            ],
        },
    },
    # BuzzBee, evaluated against SQLancer's PQS oracle.
    "paper:s2:d933042eb3c2a8f2e208515490d6b6a400e00fec": {
        "compares_with": {
            "techniques": ["pqs"],
            "excerpts": [
                "For relational DBMSs, we compare B UZZ B EE with S QUIRREL "
                "[56] and SQL ANCER (PQS [43]), two DBMS fuzzers specialized in "
                "SQL DBMS fuzzing.",
            ],
        },
    },
    "paper:s2:c5c2bcb84acde8c7fe8964aba2d6d27f220ae80b": {
        "compares_with": {
            "techniques": ["pqs", "norec", "tlp"],
            "excerpts": [
                "We compared P INOLO with the three state-of-the-art logical "
                "bug detection techniques, namely PQS [36], N O REC [34], and "
                "TLP [35], respectively, which correspond to three kinds of "
                "test oracles.",
            ],
        },
    },
    "paper:s2:74820ccf6c039c7745f8e1c30857dee61b243464": {
        "compares_with": {
            "techniques": ["tlp"],
            "excerpts": [
                "To this end, we seek to compare against the TLP technique in "
                "SQLancer, which is the closest related effort [40–42].",
            ],
        },
    },
}


def _stored_contexts(record: dict) -> List[str]:
    """Every citation context already stored on a paper record.

    A curated excerpt has to come from this pool; that is what stops this file
    from being a place where new claims can be written by hand.
    """
    contexts: List[str] = []
    for relationship in record.get("relationships", {}).values():
        for item in relationship.get("evidence", []) or []:
            if item.get("excerpt"):
                contexts.append(item["excerpt"])
    return contexts


def apply(*, timestamp: Optional[str] = None, strict: bool = False) -> dict:
    """Write the curated classifications into ``papers.json``.

    Returns a summary of what was applied and what was skipped. With
    ``strict``, an excerpt that cannot be verified against the stored contexts
    raises instead of being skipped.
    """
    timestamp = timestamp or now()
    tax = taxonomy.load()
    payload = config.load_json(config.DATA_FILES["papers"])
    by_id = {record["id"]: record for record in payload["papers"]}

    applied: List[str] = []
    skipped: List[str] = []

    for paper_id, relationships in CLASSIFICATIONS.items():
        record = by_id.get(paper_id)
        if record is None:
            skipped.append(f"{paper_id}: not in papers.json")
            continue
        pool = "\n\n".join(_stored_contexts(record))
        for relationship, entry in relationships.items():
            verified = [excerpt for excerpt in entry["excerpts"]
                        if verbatim_excerpt(pool, excerpt)]
            missing = [e for e in entry["excerpts"] if e not in verified]
            if missing:
                message = (f"{paper_id}/{relationship}: "
                           f"{len(missing)} excerpt(s) not found in the "
                           f"collected citation contexts")
                if strict:
                    raise ValueError(message)
                skipped.append(message)
            if not verified:
                continue
            for technique in entry["techniques"]:
                if technique not in tax.techniques:
                    raise ValueError(f"{paper_id}: unknown technique {technique}")

            record["relationships"][relationship] = {
                "value": "yes",
                "method": "manual_curation",
                "techniques": entry["techniques"],
                "evidence": [{
                    "source_url": record["url"],
                    "source_type": "paper_citation_context",
                    "excerpt": excerpt,
                    "excerpt_is_verbatim": True,
                    "note": ("Passage in the paper, as indexed by Semantic "
                             "Scholar, supporting this relationship."),
                    "content_sha256": content_hash(excerpt),
                    "retrieved_at": timestamp,
                    "first_seen": timestamp,
                    "last_verified": timestamp,
                } for excerpt in verified],
                "classifier": {
                    "method": "manual_curation",
                    "classifier_version": CURATOR_VERSION,
                    "policy_version": config.policy_version(),
                    "taxonomy_version": config.taxonomy_version(),
                    "classified_at": timestamp,
                    "model": None,
                    "rationale": ("Reviewed by a maintainer against the "
                                  "citation contexts collected for this paper."),
                },
            }
            applied.append(f"{paper_id}/{relationship}")

    config.write_json(config.DATA_FILES["papers"], payload)
    return {"applied": applied, "skipped": skipped}


def main() -> int:
    result = apply()
    print(f"applied {len(result['applied'])} curated classification(s)")
    for entry in result["applied"]:
        print(f"  + {entry}")
    for entry in result["skipped"]:
        print(f"  ! {entry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
