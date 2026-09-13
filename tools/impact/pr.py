"""Renders the pull-request body for a collection run.

The body is written for a reviewer whose job is to decide whether each new claim
is justified. So it leads with counts, then lists every newly accepted record
with a direct link to the evidence behind it, and finally says what was rejected
and what could not be decided. A change that cannot be followed to a source is a
change that should not be merged.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from . import config

MAX_LISTED = 30


def _load(name: str) -> Dict[str, dict]:
    path = config.DATA_FILES[name]
    if not path.exists():
        return {}
    payload = config.load_json(path)
    from .dataset import LIST_FIELD
    return {record["id"]: record for record in payload.get(LIST_FIELD[name], [])}


def _evidence_links(record: dict, limit: int = 2) -> str:
    """First few evidence URLs, so the reviewer can follow the claim."""
    urls: List[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("source_url") and node.get("source_type"):
                if node["source_url"] not in urls:
                    urls.append(node["source_url"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(record.get("attribution") or record.get("relationships")
         or record.get("evidence") or record)
    return " ".join(f"[evidence]({url})" for url in urls[:limit])


def _bug_line(record: dict) -> str:
    technique = record.get("technique") or "technique not recorded"
    return (f"- **{record['dbms']}** — {record['title']} "
            f"(`{record['finder']}` / `{technique}`, {record['status']}) "
            f"{_evidence_links(record)}")


def _paper_line(record: dict, relationship: str) -> str:
    entry = record["relationships"][relationship]
    techniques = ", ".join(entry.get("techniques", [])) or "-"
    excerpt = ""
    for item in entry.get("evidence", []):
        if item.get("excerpt"):
            excerpt = f"\n  > {item['excerpt'][:300]}"
            break
    link = record.get("url") or ""
    return (f"- [{record['title']}]({link}) ({record.get('year')}) — "
            f"techniques: {techniques}{excerpt}")


def _adoption_line(record: dict) -> str:
    return (f"- **{record['dbms']}** — `{record['relationship']}`: "
            f"{record['summary']} {_evidence_links(record)}")


def _resource_line(record: dict) -> str:
    return f"- [{record['title']}]({record['url']}) — {record['type']}"


def _section(title: str, lines: List[str], *, empty: Optional[str] = None) -> str:
    if not lines:
        return f"### {title}\n\n{empty or '_None._'}\n"
    shown = lines[:MAX_LISTED]
    body = "\n".join(shown)
    if len(lines) > MAX_LISTED:
        body += f"\n- _...and {len(lines) - MAX_LISTED} more._"
    return f"### {title}\n\n{body}\n"


def render(report: dict) -> str:
    """Build the markdown body from a run report plus the data on disk."""
    sources = report.get("sources", {})
    bugs = _load("bugs")
    papers = _load("papers")
    adoption = _load("adoption")
    resources = _load("resources")

    def added(name: str) -> List[str]:
        return sources.get(name, {}).get("added", []) or []

    def updated(name: str) -> List[str]:
        return sources.get(name, {}).get("updated", []) or []

    new_bugs = [bugs[i] for i in added("bugs") if i in bugs]
    status_changes = [bugs[i] for i in updated("bugs") if i in bugs]
    new_papers = [papers[i] for i in added("papers") if i in papers]
    new_adoption = [adoption[i] for i in added("adoption") if i in adoption]
    new_resources = [resources[i] for i in added("resources") if i in resources]

    def relationship_added(relationship: str) -> List[dict]:
        """Papers whose relationship is newly positive, new or updated."""
        out = []
        for record_id in added("papers") + updated("papers"):
            record = papers.get(record_id)
            if not record:
                continue
            entry = record["relationships"].get(relationship, {})
            if entry.get("value") == "yes":
                out.append(record)
        return out

    infrastructure = relationship_added("uses_infrastructure")
    extends = relationship_added("extends_technique")
    compares = relationship_added("compares_with")

    rejected: List[dict] = []
    needs_review: List[dict] = []
    removed: List[dict] = []
    for entry in sources.values():
        rejected.extend(entry.get("rejected") or [])
        needs_review.extend(entry.get("needs_review") or [])
        removed.extend(entry.get("removed") or [])

    lines = [
        "## Impact data update",
        "",
        "Automated collection run. Every newly accepted claim below links to the "
        "primary source it rests on; please spot-check the evidence before "
        "merging.",
        "",
        "| Change | Count |",
        "| --- | ---: |",
        f"| New bugs | {len(new_bugs)} |",
        f"| Bug records with changed status or metadata | {len(status_changes)} |",
        f"| New citing papers | {len(new_papers)} |",
        f"| New `uses_infrastructure` relationships | {len(infrastructure)} |",
        f"| New `extends_technique` relationships | {len(extends)} |",
        f"| New `compares_with` relationships | {len(compares)} |",
        f"| New DBMS adoption evidence | {len(new_adoption)} |",
        f"| New resources | {len(new_resources)} |",
        f"| Records retired | {len(removed)} |",
        f"| Candidates rejected for insufficient evidence | {len(rejected)} |",
        f"| Candidates needing human judgement | {len(needs_review)} |",
        "",
    ]

    lines.append(_section("New bugs", [_bug_line(r) for r in new_bugs]))
    lines.append(_section("Changed bug records",
                          [_bug_line(r) for r in status_changes]))
    lines.append(_section(
        "Papers now recorded as using SQLancer infrastructure",
        [_paper_line(r, "uses_infrastructure") for r in infrastructure]))
    lines.append(_section(
        "Papers now recorded as extending a SQLancer technique",
        [_paper_line(r, "extends_technique") for r in extends]))
    lines.append(_section(
        "Papers now recorded as comparing against SQLancer",
        [_paper_line(r, "compares_with") for r in compares]))
    lines.append(_section("New DBMS adoption evidence",
                          [_adoption_line(r) for r in new_adoption]))
    lines.append(_section("New resources",
                          [_resource_line(r) for r in new_resources]))
    lines.append(_section(
        "New citing papers (relationship not yet established)",
        [f"- [{r['title']}]({r.get('url') or ''}) ({r.get('year')})"
         for r in new_papers]))

    lines.append(_section(
        "Records retired",
        [f"- [{entry.get('title') or entry.get('id')}]({entry.get('url') or ''})"
         for entry in removed],
        empty="_Nothing was retired this run._"))
    lines.append(_section(
        "Rejected for insufficient evidence",
        [f"- `{entry.get('kind', '?')}` {entry.get('url') or entry.get('id') or ''}"
         f" — {entry.get('reason', '')}" for entry in rejected],
        empty="_Nothing was rejected this run._"))
    lines.append(_section(
        "Needs human judgement",
        [f"- `{entry.get('kind', '?')}` {entry.get('url') or entry.get('id') or ''}"
         f" — {entry.get('reason', '')}" for entry in needs_review],
        empty="_Nothing is waiting on a human this run._"))

    classifier = report.get("classifier") or {}
    cache = report.get("cache") or {}
    lines.extend([
        "---",
        "",
        "<details><summary>Run details</summary>",
        "",
        f"- Started {report.get('started_at')}, finished {report.get('finished_at')}",
        f"- Classifier: {classifier.get('api_calls', 0)} call(s), "
        f"{classifier.get('cache_hits', 0)} served from cache, "
        f"{classifier.get('skipped_no_key', 0)} skipped",
        f"- Cache: {cache}",
        "",
    ])
    errors = report.get("errors") or []
    if errors:
        lines.append("Warnings during the run:")
        lines.append("")
        lines.extend(f"- {error}" for error in errors)
        lines.append("")
    lines.append("</details>")
    return "\n".join(lines) + "\n"


def has_meaningful_changes(report: dict) -> bool:
    """Whether this run produced authoritative-data changes worth a pull request.

    Cache and state files are deliberately not counted: a run that only warmed
    the cache has nothing for a human to review.
    """
    for name, changed in (report.get("files_changed") or {}).items():
        if not changed:
            continue
        if name.startswith("plot:") or name == "stats":
            # Derived outputs only matter alongside a record change; on their
            # own they mean the generator changed, which is a code review.
            continue
        return True
    return False
