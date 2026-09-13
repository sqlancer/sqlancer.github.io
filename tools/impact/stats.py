"""Derives ``_data/impact/stats.json`` from the authoritative records.

The website renders statistics from this file rather than counting in Liquid, so
that the plots, the impact page and the homepage all show the same numbers, and
so that the numbers themselves are reviewable in a diff. Nothing here is
hand-entered: ``regenerate`` reads only the records, and CI fails if the checked
in file differs from what the records produce.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

from . import config, taxonomy
from .util import content_hash

SCHEMA_VERSION = "1.0.0"

# Relationships that mean a paper builds on SQLancer rather than merely citing
# it. Calling SQLancer state of the art is recognition, not building on it, so
# it is reported alongside rather than folded into this total.
BUILDING_ON = ("uses_infrastructure", "extends_technique", "compares_with")

REPORTED_RELATIONSHIPS = ("references", "reusing_or_extending",
                          "compares_with", "describes_as_state_of_the_art")

RELATIONSHIP_LABELS = {
    "references": "Cites SQLancer",
    "uses_infrastructure": "Uses SQLancer infrastructure",
    "extends_technique": "Extends a SQLancer technique",
    "reusing_or_extending": "Uses or extends SQLancer",
    "compares_with": "Compares against SQLancer",
    "describes_as_state_of_the_art": "Describes SQLancer as state of the art",
}

# Reusing the codebase and developing the technique are recorded apart, because
# a paper can do either without the other. They are reported together: they
# answer one question, and a few papers do both, so two series would show a
# reader a total that counts those twice.
REUSING_OR_EXTENDING = ("uses_infrastructure", "extends_technique")

ADOPTION_LABELS = {
    "official_ci": "SQLancer in the project's CI",
    "official_testing": "Project-maintained SQLancer testing",
    "developer_use": "Developer-reported use",
    "integration_contributed_by_dbms_team": "Integration contributed by the DBMS team",
    "planned_adoption": "Adoption proposed but not yet shown",
}

# Intent is not use. A project that has proposed adopting SQLancer is worth
# recording and worth showing, but counting it as a user would overstate reach.
PLANNED = "planned_adoption"

# How each bug earned its place. Published because the clauses differ sharply
# in strength: one is the report naming the tool, another is only who filed it.
ATTRIBUTION_LABELS = {
    "explicit_tool_statement": "The report names SQLancer",
    "technique_attribution": "The report names a SQLancer oracle",
    "curated_primary_source": "Listed in the project's own bug repository",
    "campaign_evidence": "The reproducer carries SQLancer's generated schema",
    "campaign_reporter": "Filed by someone who runs SQLancer campaigns",
}

AFFILIATION_LABELS = {
    "project": "Found by the SQLancer project",
    "external": "Found by someone outside the project",
    "unknown": "Reporter not recorded",
}

STATUS_LABELS = {
    "fixed": "Fixed",
    "fixed_in_documentation": "Fixed in documentation",
    "verified": "Confirmed",
    "open": "Open",
    "closed_not_a_bug": "Closed as not a bug",
    "closed_duplicate": "Closed as duplicate",
    "unknown": "Unknown",
}

SYMPTOM_LABELS = {
    "logic": "Logic bug",
    "error": "Unexpected error",
    "crash": "Crash",
    "hang": "Hang",
    "performance": "Performance issue",
    "unknown": "Unclassified",
}

RESOURCE_LABELS = {
    "talk": "Talks",
    "blog_post": "Blog posts",
    "documentation_note": "Project documentation naming SQLancer",
    "dataset": "Datasets",
    "artifact": "Artifacts",
    "tool": "Tools",
    "educational_material": "Educational material",
}


def _series(counter: Counter, labels: Optional[Dict[str, str]] = None,
            *, order: Optional[List[str]] = None,
            by_count: bool = False) -> List[dict]:
    """Turn a counter into the ordered ``labelled_counts`` shape."""
    if order is not None:
        keys = [key for key in order if key in counter]
        keys += sorted(key for key in counter if key not in order)
    elif by_count:
        keys = [key for key, _ in sorted(counter.items(),
                                         key=lambda kv: (-kv[1], str(kv[0])))]
    else:
        keys = sorted(counter, key=str)
    return [{"key": str(key),
             "label": (labels or {}).get(key, str(key)),
             "count": counter[key]}
            for key in keys]


def round_down_headline(total: int) -> str:
    """Conservative display form of a bug total: 1483 -> '1,000+', 456 -> '400+'.

    Rounds down to one significant figure. Rounding down rather than to nearest
    keeps the public claim defensible -- the dataset is a lower bound on what
    SQLancer has found, never an overstatement -- and one significant figure
    keeps the headline stable as the number creeps up week to week.
    """
    if total < 10:
        return str(total)
    magnitude = 10 ** (len(str(total)) - 1)
    return f"{(total // magnitude) * magnitude:,}+"


def _counted_bugs(bugs: List[dict]) -> List[dict]:
    """Reports accepted as genuine bugs; rejected reports are kept but not counted."""
    return [bug for bug in bugs if bug.get("status_is_true_positive")]


def compute(data: Dict[str, dict]) -> dict:
    tax = taxonomy.Taxonomy(data["techniques"])
    registry = {entry["id"]: entry for entry in data["dbms"]["dbms"]}
    all_bugs = data["bugs"]["bugs"]
    bugs = _counted_bugs(all_bugs)
    papers = data["papers"]["papers"]
    adoption = data["adoption"]["adoption"]
    resources = data["resources"]["resources"]

    external_papers = [p for p in papers if not p.get("is_sqlancer_publication")]

    # -- bugs -------------------------------------------------------------
    by_dbms = Counter(bug["dbms"] for bug in bugs)
    by_year = Counter(bug["reported_year"] for bug in bugs
                      if bug.get("reported_year"))
    by_status = Counter(bug["status"] for bug in bugs)
    by_technique = Counter(bug["technique"] or "unattributed" for bug in bugs)
    by_finder = Counter(bug["finder"] for bug in bugs)
    by_symptom = Counter(bug["symptom"] for bug in bugs)
    by_rule = Counter(bug["attribution"]["rule"] for bug in bugs)
    by_affiliation = Counter(bug.get("reporter_affiliation", "unknown")
                             for bug in bugs)
    # Per system, split by who found it: the same total means something
    # different depending on whether the project or an adopter found them.
    dbms_affiliation: Dict[str, Counter] = defaultdict(Counter)
    for bug in bugs:
        dbms_affiliation[bug["dbms"]][
            bug.get("reporter_affiliation", "unknown")] += 1

    technique_labels = {tid: tax.display_name(tid) for tid in tax.techniques}
    technique_labels["unattributed"] = "Technique not recorded"
    finder_labels = {fid: tax.finder_display_name(fid) for fid in tax.finders}
    dbms_labels = {key: registry.get(key, {}).get("name", key) for key in by_dbms}

    # -- papers -----------------------------------------------------------
    def holds(paper: dict, relationship: str) -> bool:
        return paper["relationships"].get(relationship, {}).get("value") == "yes"

    building_on = [p for p in external_papers
                   if any(holds(p, rel) for rel in BUILDING_ON)]
    # Reusing the codebase and developing the technique are recorded
    # separately, because a paper can do either without the other. They are
    # counted together because they answer one question -- what did this paper
    # build on SQLancer -- and only a handful of papers do both, so two
    # figures would double-count them and neither would be the number a reader
    # wants. Comparing is a different claim and stays its own figure.
    reusing_or_extending = [
        p for p in external_papers
        if holds(p, "uses_infrastructure") or holds(p, "extends_technique")]
    comparing = [p for p in external_papers if holds(p, "compares_with")]

    def reported(paper: dict, relationship: str) -> bool:
        """Whether a paper counts under a reported relationship.

        "reusing_or_extending" is not a field on a record; it is the union of
        the two that are, deduplicated, so a paper doing both is one paper.
        """
        if relationship == "reusing_or_extending":
            return any(holds(paper, key) for key in REUSING_OR_EXTENDING)
        return holds(paper, relationship)

    paper_by_relationship = Counter()
    for relationship in REPORTED_RELATIONSHIPS:
        paper_by_relationship[relationship] = sum(
            1 for p in external_papers if reported(p, relationship))

    paper_years = sorted({p["year"] for p in external_papers if p.get("year")})
    papers_by_year = Counter(p["year"] for p in external_papers if p.get("year"))

    # Both halves are kept alongside the combined series: the page shows the
    # union, and a reader who wants to know how the union splits can still
    # find out from the data without recomputing it.
    yearly_keys = tuple(REPORTED_RELATIONSHIPS) + REUSING_OR_EXTENDING
    by_relationship_year: Dict[str, List[dict]] = {}
    for relationship in yearly_keys:
        yearly = Counter(
            p["year"] for p in external_papers
            if p.get("year") and reported(p, relationship))
        by_relationship_year[relationship] = [
            {"key": str(year), "label": str(year), "count": yearly.get(year, 0)}
            for year in paper_years
        ]

    # An extra series for the deduplicated "builds on SQLancer" total.
    building_yearly = Counter(p["year"] for p in building_on if p.get("year"))
    by_relationship_year["building_on"] = [
        {"key": str(year), "label": str(year), "count": building_yearly.get(year, 0)}
        for year in paper_years
    ]

    technique_mentions = Counter()
    for paper in external_papers:
        seen = set()
        for relationship in BUILDING_ON:
            entry = paper["relationships"].get(relationship, {})
            if entry.get("value") == "yes":
                seen.update(entry.get("techniques", []))
        technique_mentions.update(seen)

    # -- DBMS -------------------------------------------------------------
    adoption_by_dbms: Dict[str, List[str]] = defaultdict(list)
    planned_by_dbms: Dict[str, List[str]] = defaultdict(list)
    for entry in adoption:
        target = (planned_by_dbms if entry["relationship"] == PLANNED
                  else adoption_by_dbms)
        target[entry["dbms"]].append(entry["relationship"])

    supported_ids = {entry["id"] for entry in registry.values()
                     if entry.get("supported_by_sqlancer")}

    # Per-system breakdowns, for the detail page each system gets. The page is
    # rendered from these rather than counted in Liquid for the same reason
    # every other figure is: one place produces the number, and a diff shows
    # when it moves.
    detail: Dict[str, dict] = defaultdict(
        lambda: {"years": Counter(), "techniques": Counter(),
                 "statuses": Counter(), "symptoms": Counter(),
                 "rules": Counter(), "reporters": Counter(),
                 "affiliations": Counter(), "dates": [],
                 "rejected": 0})
    for bug in all_bugs:
        row = detail[bug["dbms"]]
        if not bug.get("status_is_true_positive"):
            row["rejected"] += 1
            continue
        row["years"][str(bug.get("reported_year") or "unknown")] += 1
        row["techniques"][bug.get("technique") or "unattributed"] += 1
        row["statuses"][bug.get("status") or "unknown"] += 1
        row["symptoms"][bug.get("symptom") or "unknown"] += 1
        row["rules"][(bug.get("attribution") or {}).get("rule") or "unknown"] += 1
        if bug.get("reporter"):
            row["reporters"][bug["reporter"]] += 1
        row["affiliations"][
            bug.get("reporter_affiliation", "unknown")] += 1
        if bug.get("reported_date"):
            row["dates"].append(bug["reported_date"])

    # Per-year breakdowns, for the page each year gets. Built the same way as
    # the per-system ones and for the same reason: a bar on a chart raises the
    # question of what is inside it, and the chart has no room to answer.
    per_year: Dict[str, dict] = defaultdict(
        lambda: {"systems": Counter(), "statuses": Counter(),
                 "techniques": Counter(), "symptoms": Counter(),
                 "rules": Counter(), "reporters": Counter(),
                 "affiliations": Counter(), "dates": [], "rejected": 0})
    for bug in all_bugs:
        year = bug.get("reported_year")
        if not year:
            continue
        row = per_year[str(year)]
        if not bug.get("status_is_true_positive"):
            row["rejected"] += 1
            continue
        row["systems"][bug["dbms"]] += 1
        row["statuses"][bug.get("status") or "unknown"] += 1
        row["techniques"][bug.get("technique") or "unattributed"] += 1
        row["symptoms"][bug.get("symptom") or "unknown"] += 1
        row["rules"][(bug.get("attribution") or {}).get("rule") or "unknown"] += 1
        if bug.get("reporter"):
            row["reporters"][bug["reporter"]] += 1
        row["affiliations"][bug.get("reporter_affiliation", "unknown")] += 1
        if bug.get("reported_date"):
            row["dates"].append(bug["reported_date"])

    year_rows = []
    for year in sorted(per_year, key=lambda k: int(k)):
        found = per_year[year]
        dates = sorted(found["dates"])
        year_rows.append({
            "year": int(year),
            "bugs": sum(found["statuses"].values()),
            "bugs_rejected": found["rejected"],
            "systems": len(found["systems"]),
            "by_dbms": _series(found["systems"], dbms_labels, by_count=True),
            "by_status": _series(found["statuses"], STATUS_LABELS,
                                 order=list(STATUS_LABELS)),
            "by_technique": _series(found["techniques"], technique_labels,
                                    by_count=True),
            "by_symptom": _series(found["symptoms"], SYMPTOM_LABELS,
                                  order=list(SYMPTOM_LABELS)),
            "by_attribution_rule": _series(found["rules"], ATTRIBUTION_LABELS,
                                           by_count=True),
            "by_affiliation": _series(found["affiliations"], AFFILIATION_LABELS,
                                      order=list(AFFILIATION_LABELS)),
            "top_reporters": _series(found["reporters"], by_count=True)[:8],
            "first_reported": dates[0] if dates else None,
            "last_reported": dates[-1] if dates else None,
        })

    rows = []
    for dbms_id in sorted(set(registry) | set(by_dbms) | set(adoption_by_dbms)
                          | set(planned_by_dbms),
                          key=lambda k: (-by_dbms.get(k, 0),
                                         registry.get(k, {}).get("name", k).lower())):
        entry = registry.get(dbms_id, {})
        rows.append({
            "id": dbms_id,
            "name": entry.get("name", dbms_id),
            "url": entry.get("url"),
            "supported": dbms_id in supported_ids,
            "bugs": by_dbms.get(dbms_id, 0),
            "adoption_relationships": sorted(
                set(adoption_by_dbms.get(dbms_id, []))),
            # A proposal is kept out of the use column entirely: an open issue
            # asking for SQLancer is not the project using it, and listing the
            # two together in one cell reads as though it were.
            "planned_adoption": (dbms_id in planned_by_dbms
                                 and dbms_id not in adoption_by_dbms),
        })
        found = detail.get(dbms_id)
        if found:
            dates = sorted(found["dates"])
            rows[-1].update({
                "repository": entry.get("repository"),
                "bugs_rejected": found["rejected"],
                "bugs_by_year": _series(found["years"],
                                        {"unknown": "Year not recorded"}),
                "bugs_by_technique": _series(
                    found["techniques"], technique_labels, by_count=True),
                "bugs_by_status": _series(found["statuses"], STATUS_LABELS,
                                          order=list(STATUS_LABELS)),
                "bugs_by_symptom": _series(found["symptoms"], SYMPTOM_LABELS,
                                           order=list(SYMPTOM_LABELS)),
                "bugs_by_attribution_rule": _series(
                    found["rules"], ATTRIBUTION_LABELS, by_count=True),
                "bugs_by_affiliation": _series(
                    found["affiliations"], AFFILIATION_LABELS,
                    order=list(AFFILIATION_LABELS)),
                "top_reporters": _series(found["reporters"],
                                         by_count=True)[:8],
                "first_reported": dates[0] if dates else None,
                "last_reported": dates[-1] if dates else None,
            })

    total_bugs = len(bugs)
    stats = {
        "schema_version": SCHEMA_VERSION,
        "generated_from": {
            name: content_hash(_canonical(data[name]))
            for name in ("bugs", "papers", "adoption", "resources", "dbms",
                         "techniques")
        },
        "headline": {
            "bugs_total": total_bugs,
            "bugs_total_rounded": round_down_headline(total_bugs),
            "bugs_found_externally": by_affiliation.get("external", 0),
            "dbms_supported": len(supported_ids),
            "dbms_with_bugs": len(by_dbms),
            "dbms_projects_using_sqlancer": len(adoption_by_dbms),
            # The measurable part of the undercount: projects whose own
            # evidence shows them running SQLancer, and whose bugs never reach
            # this dataset because no public report ties one to it.
            "dbms_using_sqlancer_without_counted_bugs": len(
                [dbms_id for dbms_id in adoption_by_dbms
                 if not by_dbms.get(dbms_id)]),
            "dbms_projects_planning_adoption": len(
                set(planned_by_dbms) - set(adoption_by_dbms)),
            "papers_building_on_sqlancer": len(building_on),
            "papers_reusing_or_extending_sqlancer": len(reusing_or_extending),
            "papers_comparing_against_sqlancer": len(comparing),
            "papers_citing_sqlancer": len(external_papers),
            "papers_calling_sqlancer_state_of_the_art": sum(
                1 for p in external_papers
                if holds(p, "describes_as_state_of_the_art")),
        },
        "bugs": {
            "total": total_bugs,
            "total_including_rejected": len(all_bugs),
            "status_known_count": sum(1 for bug in bugs
                                      if bug["status"] != "unknown"),
            "by_dbms": _series(by_dbms, dbms_labels, by_count=True),
            "by_year": _series(by_year),
            "years": year_rows,
            "by_status": _series(by_status, STATUS_LABELS,
                                 order=list(STATUS_LABELS)),
            "by_technique": _series(by_technique, technique_labels, by_count=True),
            "by_finder": _series(by_finder, finder_labels, by_count=True),
            "by_symptom": _series(by_symptom, SYMPTOM_LABELS,
                                  order=list(SYMPTOM_LABELS)),
            "by_attribution_rule": _series(
                by_rule, ATTRIBUTION_LABELS, order=list(ATTRIBUTION_LABELS)),
            "by_reporter_affiliation": _series(
                by_affiliation, AFFILIATION_LABELS,
                order=list(AFFILIATION_LABELS)),
            "by_dbms_and_affiliation": [
                {
                    "key": key,
                    "label": dbms_labels.get(key, key),
                    "count": sum(dbms_affiliation[key].values()),
                    "segments": [
                        {"key": affiliation,
                         "label": AFFILIATION_LABELS[affiliation],
                         "count": dbms_affiliation[key].get(affiliation, 0)}
                        for affiliation in AFFILIATION_LABELS
                    ],
                }
                for key in [row["key"] for row in _series(by_dbms, by_count=True)]
            ],
        },
        "papers": {
            "total": len(papers),
            "external_total": len(external_papers),
            "building_on_total": len(building_on),
            "by_relationship": _series(
                paper_by_relationship, RELATIONSHIP_LABELS,
                order=list(REPORTED_RELATIONSHIPS)),
            "by_year": _series(papers_by_year),
            "by_relationship_year": by_relationship_year,
            "by_technique": _series(
                technique_mentions,
                {tid: tax.display_name(tid) for tid in tax.techniques},
                by_count=True),
            "years": paper_years,
        },
        "dbms": {
            "supported": len(supported_ids),
            "with_bugs": len(by_dbms),
            "with_adoption": len(adoption_by_dbms),
            "planning_adoption": len(set(planned_by_dbms) - set(adoption_by_dbms)),
            "adoption_by_relationship": _series(
                Counter(entry["relationship"] for entry in adoption
                        if entry["relationship"] != PLANNED),
                ADOPTION_LABELS, order=list(ADOPTION_LABELS)),
            # The systems behind the headline count, so the page can name them
            # rather than assert a number and leave the reader to trust it.
            "using_without_bugs": [
                {"id": row["id"], "name": row["name"]} for row in rows
                if row.get("adoption_relationships") and not row.get("bugs")],
            "rows": rows,
        },
        "resources": {
            "total": len(resources),
            "by_type": _series(Counter(r["type"] for r in resources),
                               RESOURCE_LABELS, by_count=True),
        },
    }
    return stats


def _canonical(payload: dict) -> str:
    import json
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def load_data() -> Dict[str, dict]:
    return {name: config.load_json(config.DATA_FILES[name])
            for name in ("techniques", "dbms", "bugs", "papers", "adoption",
                         "resources")}


def regenerate() -> Tuple[dict, bool]:
    """Recompute stats and write them; returns ``(stats, changed_on_disk)``."""
    stats = compute(load_data())
    changed = config.write_json(config.DATA_FILES["stats"], stats)
    return stats, changed


def main() -> int:
    stats, changed = regenerate()
    head = stats["headline"]
    print(f"stats.json: {'updated' if changed else 'unchanged'}")
    print(f"  {head['bugs_total']} bugs ({head['bugs_total_rounded']}) | "
          f"{head['dbms_supported']} DBMSs supported | "
          f"{head['dbms_projects_using_sqlancer']} DBMS projects using SQLancer | "
          f"{head['papers_building_on_sqlancer']} papers building on SQLancer | "
          f"{head['papers_citing_sqlancer']} papers citing SQLancer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
