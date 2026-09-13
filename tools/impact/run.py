"""Command-line entry point for the impact pipeline.

Subcommands mirror the stages of the weekly run, and each is runnable on its own
so a failure in one source can be re-run without repeating the rest:

    python -m tools.impact.run collect      # everything, incrementally
    python -m tools.impact.run collect --full        # periodic reconciliation
    python -m tools.impact.run collect --only papers
    python -m tools.impact.run stats        # regenerate derived figures
    python -m tools.impact.run plots        # regenerate the charts
    python -m tools.impact.run validate     # schemas + cross-file checks
    python -m tools.impact.run build        # stats + plots + validate
    python -m tools.impact.run report       # write the pull-request body

``collect`` never writes a partially validated dataset: it merges, regenerates
the derived outputs, validates, and only then reports what changed. If nothing
changed, it says so and exits 0, which is what stops the weekly job from opening
an empty pull request.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import config, plots, stats as stats_module, taxonomy, validate
from .cache import Caches, State
from .classify import Classifier
from .dataset import Dataset, MergeReport
from .github import GitHub
from .util import now

SOURCES = ("bugs", "papers", "adoption", "resources", "talks", "dbms",
           "people")


class RunReport:
    """What one run did, in the shape the pull-request body needs."""

    def __init__(self):
        self.per_source: Dict[str, MergeReport] = {}
        self.errors: List[str] = []
        self.cache_summary: Dict[str, Dict[str, int]] = {}
        self.classifier_stats: Dict[str, int] = {}
        self.files_changed: Dict[str, bool] = {}
        # Open items in the review queue, by collector, after this run.
        self.review_queue: Dict[str, int] = {}
        self.started_at = now()

    @property
    def data_changed(self) -> bool:
        return any(changed for name, changed in self.files_changed.items())

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": now(),
            "files_changed": self.files_changed,
            "errors": self.errors,
            "cache": self.cache_summary,
            "classifier": self.classifier_stats,
            "review_queue": self.review_queue,
            "sources": {
                name: {
                    "added": [r.get("id") for r in report.added],
                    "updated": [after.get("id") for _, after in report.updated],
                    "unchanged": report.unchanged,
                    "removed": [
                        {"id": r.get("id"), "title": r.get("title"),
                         "url": r.get("url") or r.get("primary_url")}
                        for r in report.removed
                    ],
                    "rejected": report.rejected,
                    "needs_review": report.needs_review,
                }
                for name, report in self.per_source.items()
            },
        }


def _collect_dbms(dataset: Dataset, gh: GitHub, report: RunReport,
                  timestamp: str) -> None:
    from .collectors import dbms_registry
    records = dbms_registry.collect(gh, timestamp)
    payload = dbms_registry.build_payload(records)
    dataset.ensure("dbms", {"schema_version": payload["schema_version"]})
    report.per_source["dbms"] = dataset.merge("dbms", payload["dbms"],
                                              timestamp=timestamp)


def _collect_bugs(dataset: Dataset, gh: GitHub, classifier: Classifier,
                  report: RunReport, timestamp: str, state: State,
                  full: bool) -> None:
    from .collectors import (gitee_bugs, github_bugs, mariadb_jira, nus_test,
                             provider_bugs, sqlancer_bugs)

    dataset.ensure("bugs", {"schema_version": "1.0.0",
                            "policy_version": config.policy_version()})

    # Sources are merged weakest first, because a later candidate overwrites an
    # earlier one describing the same bug. The project's curated repository is
    # the best record there is -- it names the oracle that found each bug --
    # so it is merged last and wins over a lab-list entry or an issue search
    # that reaches the same report with less to say about it.
    since = None if full else state.last_scan("github")
    try:
        found, rejected, needs_review = github_bugs.collect(
            gh, classifier, since=since, timestamp=timestamp, full=full)
    except Exception as error:  # a search failure must not lose the curated import
        report.errors.append(f"github bug search failed: {error}")
        found, rejected, needs_review = [], [], []
    dataset.merge("bugs", found, timestamp=timestamp,
                  rejected=rejected, needs_review=needs_review)

    # The TEST lab's bug list is a backlog rather than a feed, so each run reads
    # a bounded slice of it; the raw cache makes an already-inspected report
    # free, and successive runs work through the rest.
    try:
        lab_found, lab_rejected, lab_review = nus_test.collect(
            gh, classifier, timestamp=timestamp,
            max_reports=1200 if full else 250)
    except Exception as error:
        report.errors.append(f"NUS TEST lab import failed: {error}")
        lab_found, lab_rejected, lab_review = [], [], []
    dataset.merge("bugs", lab_found, timestamp=timestamp,
                  rejected=lab_rejected, needs_review=lab_review)

    # Gitee hosts the trackers of several Chinese database projects, which the
    # GitHub search cannot see at all.
    try:
        gitee_found, gitee_rejected, gitee_review = gitee_bugs.collect(
            timestamp=timestamp)
    except Exception as error:
        report.errors.append(f"Gitee import failed: {error}")
        gitee_found, gitee_rejected, gitee_review = [], [], []
    dataset.merge("bugs", gitee_found, timestamp=timestamp,
                  rejected=gitee_rejected, needs_review=gitee_review)

    # MariaDB takes no bug reports on GitHub, so the repository search reaches
    # none of its tracker; its Jira is open and answers a JQL query.
    try:
        jira_found, jira_rejected, jira_review = mariadb_jira.collect(
            timestamp=timestamp)
    except Exception as error:
        report.errors.append(f"MariaDB Jira import failed: {error}")
        jira_found, jira_rejected, jira_review = [], [], []
    dataset.merge("bugs", jira_found, timestamp=timestamp,
                  rejected=jira_rejected, needs_review=jira_review)

    # Bugs recorded in SQLancer's own source, one file per provider. Read from
    # forks as well as upstream: a vendor's own fork is the only public record
    # of what SQLancer found in a system with no upstream provider.
    try:
        provider_found, _, provider_review = provider_bugs.collect(
            gh, _bug_file_checkouts(gh), timestamp=timestamp)
    except Exception as error:
        report.errors.append(f"provider bug import failed: {error}")
        provider_found, provider_review = [], []
    dataset.merge("bugs", provider_found, timestamp=timestamp,
                  needs_review=provider_review)

    dataset.merge("bugs", sqlancer_bugs.collect(gh, timestamp=timestamp),
                  timestamp=timestamp)

    _stamp_affiliation(gh, dataset.records("bugs"))

    report.per_source["bugs"] = dataset.reports["bugs"]
    state.record_scan("github", full=full)
    state.record_scan("nus_test", full=full)


def _collect_people(dataset: Dataset, gh: GitHub, report: RunReport,
                    timestamp: str) -> None:
    """Refresh the roster of people whose reports are worth reading.

    Kept as a data file rather than derived each run because the handles are
    resolved from issues, which costs requests, and because a resolution that
    goes wrong should be reviewable in a diff rather than silently different
    next week.
    """
    from .collectors import people

    dataset.ensure("people", {"schema_version": "1.0.0",
                              "policy_version": config.policy_version()})
    try:
        found = people.discover(gh, timestamp=timestamp)
    except Exception as error:
        report.errors.append(f"people roster refresh failed: {error}")
        return
    dataset.merge("people", found, timestamp=timestamp)
    report.per_source["people"] = dataset.reports["people"]


def _bug_file_checkouts(gh: GitHub) -> List[Tuple[str, str, str]]:
    """Where to read provider bug files from: upstream, and vendors' forks.

    The forks are the real ones, listed from GitHub and kept when their owner
    is a registered database system -- guessing a name from the registry got
    Oxla's fork right and invented five that do not exist.
    """
    from .collectors import forks, provider_bugs

    checkouts = [provider_bugs.UPSTREAM]
    try:
        owners = forks._owner_index()
        for fork in forks.list_forks(gh):
            login = ((fork.get("owner") or {}).get("login") or "").lower()
            full_name = fork.get("full_name") or ""
            if login in owners and "/" in full_name:
                owner, repo = full_name.split("/", 1)
                checkouts.append((owner, repo,
                                  fork.get("default_branch") or "main"))
    except Exception:
        pass
    return checkouts


def _stamp_affiliation(gh: GitHub, records: List[dict]) -> None:
    """Mark each bug as found by the project or by an outside adopter.

    Done in one place after merging rather than in each collector, because it
    depends on a roster none of them owns: people.json, plus the taxonomy's
    contributor list and the TEST lab's current members.

    Who found a bug matters for what the number means. The project finding bugs
    with its own tool shows the tool works; someone outside the project finding
    them shows it travelled.
    """
    from .collectors import people

    tax = taxonomy.load()
    try:
        # Identities, not just handles: the same person is a GitHub login in one
        # tracker and a display name in another.
        handles = people.identities(gh)
    except Exception:
        handles = []
    # A ruling wins over the roster. The roster can see that someone is listed
    # on the lab's people page; it cannot see whether their work is this
    # project's, and a lab lists everyone working in it.
    external = people.counted_as_external()
    for record in records:
        reporter = record.get("reporter")
        if not reporter:
            record["reporter_affiliation"] = "unknown"
        elif reporter.strip().lower() in external:
            record["reporter_affiliation"] = "external"
        elif tax.is_project_contributor(reporter, handles):
            record["reporter_affiliation"] = "project"
        else:
            record["reporter_affiliation"] = "external"


def _collect_papers(dataset: Dataset, caches: Caches, classifier: Classifier,
                    report: RunReport, timestamp: str, state: State,
                    full: bool, gh: Optional[GitHub] = None,
                    artifact_budget: Optional[int] = None) -> None:
    from .collectors import papers as papers_collector
    from .scholarly import OpenAlex, OpenCitations, SemanticScholar

    dataset.ensure("papers", {"schema_version": "1.0.0",
                              "policy_version": config.policy_version()})
    from .http import Fetcher
    from .scholarly import ArXiv

    s2 = SemanticScholar(caches.papers)
    oa = OpenAlex(caches.papers)
    # Reading preprints is only worth its cost on a reconciliation; the results
    # are cached, so a weekly run reuses them without refetching.
    arxiv = ArXiv(caches.papers) if full else None
    # A third citation graph. Every candidate it finds has to be resolved
    # through Crossref one DOI at a time, so it is walked on a reconciliation
    # rather than on every run.
    oc = OpenCitations(caches.papers) if full else None
    pdf_fetcher = Fetcher(caches.artifacts, min_interval=0.5, max_retries=2,
                          timeout=40.0)
    try:
        candidates, unresolved = papers_collector.gather(
            s2=s2, oa=oa, oc=oc)
    except Exception as error:
        report.errors.append(f"paper discovery failed: {error}")
        return
    for seed in unresolved:
        report.errors.append(
            f"seed publication for "
            f"{seed.get('seed_id') or seed.get('technique_id')} "
            f"could not be resolved")

    existing = dataset.index("papers")
    records: List[dict] = []
    needs_review: List[dict] = []
    rejected: List[dict] = []
    artifacts_done = 0

    # Repositories that contain SQLancer's own source files, found once per run
    # by code search. This is the route that reaches artifacts whose authors
    # never mention SQLancer anywhere a reader would look.
    derived_repositories: List[dict] = []
    if gh is not None:
        from .collectors import artifacts as artifact_collector
        from .collectors import forks as forks_collector
        repository_names: List[str] = []
        try:
            repository_names.extend(
                artifact_collector.find_sqlancer_derived_repositories(gh))
        except Exception as error:
            report.errors.append(f"artifact code search failed: {error}")
        # Code search excludes forks entirely, so a forked artifact is invisible
        # to it. Renamed forks are the ones that became a project of their own.
        try:
            for full_name in forks_collector.artifact_candidates(gh=gh):
                if full_name not in repository_names:
                    repository_names.append(full_name)
        except Exception as error:
            report.errors.append(f"fork artifact scan failed: {error}")
        try:
            for full_name in repository_names:
                if "/" not in full_name:
                    continue
                owner, repo = full_name.split("/", 1)
                entry = artifact_collector.repository_text(gh, owner, repo)
                if entry:
                    derived_repositories.append(entry)
        except Exception as error:
            report.errors.append(f"artifact text fetch failed: {error}")
        if derived_repositories:
            print(f"  {len(derived_repositories)} SQLancer-derived "
                  f"repositories to match against")

    for candidate in candidates.values():
        # A record the index could not describe cannot be published: it would
        # appear on the page with a cover-page title or fall out of every plot.
        unusable = papers_collector.usable_metadata(candidate)
        if unusable:
            needs_review.append({
                "kind": "paper", "id": candidate.record_id,
                "title": candidate.title, "reason": unusable,
            })
            continue
        prior = existing.get(candidate.record_id)
        classifications = _classify_paper(
            candidate, classifier, prior, needs_review, rejected, timestamp)

        is_ours = papers_collector.looks_like_sqlancer_publication(candidate)

        # The paper's own words come first: they settle more than anything a
        # citation index or a repository can, and they are what a person would
        # read. Only relationships the text does not establish fall through to
        # the artifact pass and the classifier.
        if arxiv is not None and not is_ours:
            found, resolved_arxiv = _fulltext_pass(
                pdf_fetcher, arxiv, candidate, prior, timestamp, openalex=oa)
            for key, relationship in found.items():
                if classifications.get(key, {}).get("value") != "yes":
                    classifications[key] = relationship
            if resolved_arxiv and not candidate.arxiv:
                candidate.arxiv = resolved_arxiv
                candidate.external_ids.setdefault("ArXiv", resolved_arxiv)

        paper_artifacts = (prior or {}).get("artifacts") or []
        if gh is not None and not is_ours and (
                artifact_budget is None or artifacts_done < artifact_budget):
            found, paper_artifacts = _artifact_pass(
                gh, candidate, prior, needs_review, timestamp,
                derived=derived_repositories)
            artifacts_done += 1
            if found is not None:
                classifications["uses_infrastructure"] = found

        records.append(papers_collector.build_record(
            candidate, timestamp=timestamp, classifications=classifications,
            artifacts=paper_artifacts, is_sqlancer_publication=is_ours))

    # A paper run always walks every seed, so on a full reconciliation the
    # candidate set is the complete one and records that no longer cite any
    # SQLancer publication can be retired. Incremental runs never remove.
    report.per_source["papers"] = dataset.merge(
        "papers", records, timestamp=timestamp,
        rejected=rejected, needs_review=needs_review, authoritative=full)
    state.record_scan("papers", full=full)


def _artifact_pass(gh: GitHub, candidate, prior: Optional[dict],
                   needs_review: List[dict], timestamp: str,
                   derived: Optional[List[dict]] = None
                   ) -> Tuple[Optional[dict], List[dict]]:
    """Look for the paper's artifact and inspect it for SQLancer reuse.

    This is where ``uses_infrastructure`` is actually decided. A paper that
    reuses the codebase very often says so nowhere -- not in its text, not in
    its artifact's README -- and the only honest evidence is the artifact
    itself: SQLancer's package layout, its source files, its build coordinates.

    Returns ``(relationship_or_None, artifact_records)``.
    """
    from .collectors import artifacts as artifact_collector

    # A decision already made from an artifact is kept: re-searching GitHub for
    # every paper every week would be the most expensive thing this pipeline
    # does, for almost no new information.
    #
    # The exception is a decision that rests on a curated paper-to-repository
    # link. Those are the ones most likely to be wrong, and reusing them
    # unconditionally would freeze a mistake in place: the entry can be
    # corrected in KNOWN_ARTIFACTS and the stale claim would still stand.
    if prior:
        previous = prior.get("relationships", {}).get("uses_infrastructure", {})
        if previous.get("method") in ("artifact_inspection", "manual_curation"):
            evidence = previous.get("evidence") or [{}]
            curated = "Recorded by a maintainer" in (evidence[0].get("note") or "")
            still_listed = (artifact_collector.KNOWN_ARTIFACTS.get(
                candidate.record_id) is not None)
            if not curated or still_listed:
                return previous, prior.get("artifacts", [])

    paper = {"title": candidate.title, "doi": candidate.doi,
             "arxiv_id": candidate.arxiv}
    tool_names = artifact_collector.candidate_tool_names(
        candidate.title, candidate.s2.get("abstract"))

    # The known SQLancer derivatives are matched first, from text already in
    # memory. Only a paper none of them claims is worth spending searches on.
    matched: List[Tuple[dict, dict]] = []
    for entry in (derived or []):
        link = artifact_collector.link_evidence_from_text(
            entry, paper, tool_names=tool_names, timestamp=timestamp)
        if link is not None:
            matched.append((entry, link))

    repositories: List[dict] = [
        {"full_name": entry["full_name"], "_link": link}
        for entry, link in matched
    ]
    known = artifact_collector.KNOWN_ARTIFACTS.get(candidate.record_id)
    if known and not any(r["full_name"] == known for r in repositories):
        repositories.insert(0, {"full_name": known, "_curated": True})
    if not repositories:
        try:
            repositories = artifact_collector.find_repository_candidates(
                gh, candidate.title, tool_names=tool_names, limit=4)
        except Exception:
            return None, []

    artifact_records: List[dict] = []
    for item in repositories:
        full_name = item.get("full_name") or ""
        if "/" not in full_name:
            continue
        owner, repo = full_name.split("/", 1)
        if item.get("_link"):
            link = item["_link"]
        elif item.get("_curated"):
            link = {
                "source_url": f"https://github.com/{owner}/{repo}",
                "source_type": "github_repository",
                "excerpt": None,
                "note": ("Recorded by a maintainer as this paper's artifact "
                         "repository."),
                "retrieved_at": timestamp,
                "first_seen": timestamp,
                "last_verified": timestamp,
            }
        else:
            try:
                link = artifact_collector.link_evidence(
                    gh, owner, repo, paper, tool_names=tool_names,
                    timestamp=timestamp)
            except Exception:
                continue
        if link is None:
            continue

        try:
            markers, marker_evidence = artifact_collector.inspect_repository(
                gh, owner, repo, timestamp=timestamp)
        except Exception:
            continue
        if not markers:
            continue

        artifact_records.append({
            "url": f"https://github.com/{owner}/{repo}",
            "kind": "github",
            "inspected": True,
            "inspected_at": timestamp,
            "sqlancer_markers": markers,
            "marker_evidence": [link] + marker_evidence,
        })

        if artifact_collector.markers_are_conclusive(markers):
            return {
                "value": "yes",
                "method": "artifact_inspection",
                "evidence": [link] + marker_evidence,
                "classifier": {
                    "method": "artifact_inspection",
                    "classifier_version": artifact_collector.EXTRACTOR_VERSION,
                    "policy_version": config.policy_version(),
                    "taxonomy_version": config.taxonomy_version(),
                    "classified_at": timestamp,
                    "model": None,
                    "rationale": ("Artifact carries SQLancer markers: "
                                  + ", ".join(markers)),
                },
            }, artifact_records

        # Markers present but not decisive on their own: a human should look.
        needs_review.append({
            "kind": "paper", "id": candidate.record_id,
            "relationship": "uses_infrastructure", "title": candidate.title,
            "url": f"https://github.com/{owner}/{repo}",
            "reason": ("artifact shows weak SQLancer markers ("
                       + ", ".join(markers) + "); needs a human decision"),
        })
    return None, artifact_records


def _fulltext_pass(fetcher, arxiv, candidate, prior: Optional[dict],
                   timestamp: str, openalex=None
                   ) -> Tuple[Dict[str, dict], Optional[str]]:
    """Judge a paper from its own full text, via its arXiv preprint.

    Most of this corpus is published by ACM, which refuses automated requests,
    so for the majority of papers a citation index's two-sentence contexts were
    the only evidence available. A preprint gives the paper itself, and what it
    did with SQLancer is usually stated plainly in it.

    Returns the relationships the text establishes and the arXiv id used.
    """
    from .collectors import fulltext as fulltext_collector

    arxiv_id = candidate.arxiv or (prior or {}).get("arxiv_id")
    if not arxiv_id:
        preprint = arxiv.find_preprint(
            candidate.title,
            [a.get("name") for a in (candidate.s2.get("authors") or [])
             if a.get("name")])
        if preprint is not None:
            arxiv_id = preprint["arxiv_id"]

    # Preferred order: a PDF supplied by hand, then the arXiv preprint, then
    # the publisher if it is one that serves papers freely. ACM, IEEE and
    # Springer refuse automated requests, which is why the hand-supplied route
    # exists at all.
    url = None
    text = None
    supplied = fulltext_collector.local_pdf(candidate.doi, arxiv_id,
                                            candidate.s2_paper_id)
    if supplied:
        text = fulltext_collector.pdf_text(fetcher, supplied)
        url = candidate.s2.get("url") or f"https://doi.org/{candidate.doi}"
    if not text and arxiv_id:
        url = f"https://arxiv.org/abs/{arxiv_id}"
        text = fulltext_collector.pdf_text(
            fetcher, f"https://arxiv.org/pdf/{arxiv_id}")
    if not text:
        # USENIX serves everything openly, from a path built out of the venue,
        # the year and the first author.
        for candidate_url in fulltext_collector.usenix_pdf_urls({
                "venue": candidate.s2.get("venue"),
                "year": candidate.s2.get("year"),
                "title": candidate.title,
                "authors": [a.get("name") for a in (candidate.s2.get("authors") or [])
                            if a.get("name")]}):
            text = fulltext_collector.pdf_text(fetcher, candidate_url)
            if text:
                url = candidate_url
                break
    if not text and candidate.doi and openalex is not None:
        try:
            work = openalex.work_by_doi(candidate.doi)
        except Exception:
            work = None
        pvldb = fulltext_collector.pvldb_pdf_url(candidate.doi, work)
        if pvldb:
            text = fulltext_collector.pdf_text(fetcher, pvldb)
            url = pvldb
    if not text:
        return {}, arxiv_id

    tax = taxonomy.load()
    signals = fulltext_collector.find_signals(text, tax)
    notes = {
        "uses_infrastructure": ("The paper states that its implementation is "
                                "built on SQLancer."),
        "compares_with": ("The paper states that it evaluates against SQLancer "
                          "or one of its techniques."),
        "extends_technique": ("The paper states that it extends or adapts a "
                              "SQLancer technique."),
    }

    out: Dict[str, dict] = {}
    for key, sentences in signals.items():
        if not sentences:
            continue
        techniques: List[str] = []
        for sentence in sentences:
            techniques.extend(fulltext_collector.techniques_in(sentence, tax))
        relationship = {
            "value": "yes",
            "method": "deterministic",
            "evidence": [
                fulltext_collector.evidence_from(sentence, url, notes[key],
                                                 timestamp)
                for sentence in sentences[:4]
            ],
        }
        deduped = list(dict.fromkeys(techniques))
        if deduped:
            relationship["techniques"] = deduped
        elif key != "uses_infrastructure":
            # The schema requires a named technique for these two; without one
            # the claim is about SQLancer as a whole, so the cited seeds stand in.
            relationship["techniques"] = list(candidate.seeds)
        out[key] = relationship
    return out, arxiv_id


def _classify_paper(candidate, classifier: Classifier, prior: Optional[dict],
                    needs_review: List[dict], rejected: List[dict],
                    timestamp: str) -> Dict[str, dict]:
    """Decide the three judgement relationships for one paper.

    Carries a previous decision forward when the evidence has not changed, so a
    paper classified last month is neither re-asked nor silently downgraded.
    """
    from .classify import (excluded_names, finder_names, resolve_technique,
                           technique_names)
    from .collectors.papers import context_evidence

    tax = taxonomy.load()
    signals = candidate.signals()
    out: Dict[str, dict] = {}

    prior_hash = (prior or {}).get("provenance", {}).get("content_sha256")
    evidence_unchanged = prior_hash == candidate.evidence_hash()

    questions = {
        "uses_infrastructure": "paper_infrastructure",
        "extends_technique": "paper_extends",
        "compares_with": "paper_compares",
    }

    for relationship, question in questions.items():
        previous = (prior or {}).get("relationships", {}).get(relationship)
        # A decision a person made is never overwritten by a later run, even
        # when the index returns different sentences for the same citation.
        if previous and previous.get("method") == "manual_curation":
            out[relationship] = previous
            continue
        hits = signals[relationship]
        if not hits:
            if evidence_unchanged and prior:
                out[relationship] = prior["relationships"][relationship]
            continue

        source_text = "\n\n".join(context for _, context in hits)
        kwargs = {"source_label": f"citation contexts for {candidate.title}",
                  "source_text": source_text}
        if question == "paper_infrastructure":
            kwargs["markers"] = []
        elif question == "paper_extends":
            kwargs["technique_names"] = technique_names(tax)
        else:
            kwargs["technique_names"] = technique_names(tax)

        result = None
        if classifier is not None:
            result = classifier.classify(question,
                                         source_id=candidate.record_id, **kwargs)
        if result is None:
            if evidence_unchanged and prior:
                out[relationship] = prior["relationships"][relationship]
            else:
                needs_review.append({
                    "kind": "paper", "id": candidate.record_id,
                    "relationship": relationship, "title": candidate.title,
                    "url": candidate.s2.get("url"),
                    "reason": "needs semantic classification; classifier unavailable",
                })
            continue

        if not result.is_positive:
            rejected.append({
                "kind": "paper", "id": candidate.record_id,
                "relationship": relationship, "answer": result.answer,
                "title": candidate.title,
                "reason": result.reason or "classifier did not confirm",
            })
            out[relationship] = {"value": result.answer,
                                 "method": "llm_classification",
                                 "classifier": result.classifier_record()}
            continue

        techniques = []
        for name in result.extra.get("techniques") or []:
            resolved = resolve_technique(tax, name)
            if resolved:
                techniques.append(resolved)
        if not techniques and relationship != "uses_infrastructure":
            techniques = list(candidate.seeds)

        pairs = [(techniques[0] if techniques else candidate.seeds[0], excerpt)
                 for excerpt in result.excerpts]
        out[relationship] = {
            "value": "yes",
            "method": "llm_classification",
            "techniques": techniques,
            "evidence": context_evidence(
                candidate.s2.get("url") or f"https://doi.org/{candidate.doi}",
                pairs, "Passage supporting this relationship to {technique}.",
                timestamp),
            "classifier": result.classifier_record(),
        }
    return out


def _collect_adoption(dataset: Dataset, gh: GitHub, classifier: Classifier,
                      report: RunReport, timestamp: str, state: State,
                      full: bool) -> None:
    from .collectors import adoption as adoption_collector
    from .collectors import forks as forks_collector

    dataset.ensure("adoption", {"schema_version": "1.0.0",
                                "policy_version": config.policy_version()})
    try:
        records, rejected, needs_review = adoption_collector.collect(
            gh, classifier, timestamp=timestamp)
    except Exception as error:
        report.errors.append(f"adoption discovery failed: {error}")
        records, rejected, needs_review = [], [], []

    # Forks are the one source code search cannot see, and a database system
    # that keeps its own fork of SQLancer is doing so deliberately.
    try:
        fork_records, fork_review = forks_collector.adoption_records(
            gh, timestamp=timestamp)
    except Exception as error:
        report.errors.append(f"fork scan failed: {error}")
        fork_records, fork_review = [], []
    needs_review.extend(fork_review)

    # One record per (system, relationship): a project can show up both through
    # its fork and through a file in its own repository, and both belong on the
    # same record as separate evidence.
    by_id = {record["id"]: record for record in records}
    for record in fork_records:
        existing = by_id.get(record["id"])
        if existing is None:
            by_id[record["id"]] = record
            records.append(record)
        else:
            existing["evidence"].extend(record["evidence"])
            existing["summary"] = record["summary"]
    report.per_source["adoption"] = dataset.merge(
        "adoption", records, timestamp=timestamp,
        rejected=rejected, needs_review=needs_review)
    state.record_scan("adoption", full=full)


def _collect_resources(dataset: Dataset, gh: GitHub, report: RunReport,
                       timestamp: str, state: State, full: bool) -> None:
    from .collectors import resources as resources_collector

    dataset.ensure("resources", {"schema_version": "1.0.0"})
    try:
        records, rejected, needs_review = resources_collector.collect(
            gh, timestamp=timestamp)
    except Exception as error:
        report.errors.append(f"resource discovery failed: {error}")
        return
    # The resource collector re-derives its whole list every run from the
    # README and the technique papers, so it is always authoritative: a
    # resource that is no longer linked, or whose technique is no longer
    # SQLancer's, is retired rather than left behind.
    report.per_source["resources"] = dataset.merge(
        "resources", records, timestamp=timestamp, rejected=rejected,
        needs_review=needs_review, authoritative=True)
    state.record_scan("resources", full=full)


def _collect_talks(dataset: Dataset, report: RunReport, timestamp: str,
                   state: State, full: bool) -> None:
    """Refresh the talks file.

    This one does not go through ``Dataset.merge``. A talk's caption mentions
    come from a transcript somebody captured by hand, and a run made without
    that capture in front of it knows less than the file already does -- so the
    merge that preserves them lives in ``talks.merge`` and is used here instead.
    """
    from . import talks as talks_store
    from .collectors import talks as talks_collector
    from .http import Fetcher

    fetcher = Fetcher(Caches().http, min_interval=1.0, max_retries=2,
                      timeout=25.0)
    try:
        records, needs_review = talks_collector.collect(
            fetcher, timestamp=timestamp)
    except Exception as error:
        report.errors.append(f"talk collection failed: {error}")
        return
    added, updated = [], []
    for record in records:
        if talks_store.merge(record, timestamp=timestamp) == "added":
            added.append(record)
        else:
            updated.append((record, record))
    # The dataset snapshot was taken before those writes, and it saves every
    # file it holds at the end of the run. Re-read the merged file into it, or
    # the stale copy in memory would be written straight back over the new one.
    dataset.data["talks"] = config.load_json(talks_store.TALKS_FILE)
    outcome = MergeReport(added=added, updated=updated,
                          needs_review=list(needs_review))
    report.per_source["talks"] = outcome
    # Filing it here as well puts anything needing review through the same
    # pass as every other source, rather than a second path of its own.
    dataset.reports["talks"] = outcome
    state.record_scan("talks", full=full)


def collect(only: Optional[List[str]] = None, *, full: bool = False,
            dry_run: bool = False) -> RunReport:
    timestamp = now()
    report = RunReport()
    caches = Caches()
    state = State()
    gh = GitHub(caches.github)
    classifier = Classifier(caches.classifications, dry_run=dry_run)
    dataset = Dataset()
    wanted = set(only or SOURCES)

    if "dbms" in wanted:
        _collect_dbms(dataset, gh, report, timestamp)
    # The roster is refreshed before the bug search, which reads it: a reporter
    # who appears on the lab's list this week should be searched this week.
    if "people" in wanted or "bugs" in wanted:
        _collect_people(dataset, gh, report, timestamp)
    if "bugs" in wanted:
        _collect_bugs(dataset, gh, classifier, report, timestamp, state, full)
    if "papers" in wanted:
        # Artifact hunting is the most request-hungry thing here, so a weekly
        # run works through a slice of the backlog and a reconciliation takes
        # the lot. Decisions already made from an artifact are never redone.
        _collect_papers(dataset, caches, classifier, report, timestamp, state,
                        full, gh=gh,
                        artifact_budget=None if full else 40)
    if "adoption" in wanted:
        _collect_adoption(dataset, gh, classifier, report, timestamp, state, full)
    if "resources" in wanted:
        _collect_resources(dataset, gh, report, timestamp, state, full)
    if "talks" in wanted:
        _collect_talks(dataset, report, timestamp, state, full)

    report.files_changed.update(dataset.save(wanted))
    # Before the derived files, so a run that fails later still leaves the
    # queue it built. An item admitted by this very run drops out here.
    queue_counts = _persist_review(dataset, report, timestamp)
    if any(queue_counts.values()):
        print(f"needs review: +{queue_counts['added']} new, "
              f"{queue_counts['seen_again']} seen again, "
              f"{queue_counts['resolved']} resolved")
    stats, stats_changed = stats_module.regenerate()
    report.files_changed["stats"] = stats_changed
    for name, changed in plots.write_all(stats).items():
        report.files_changed[f"plot:{name}"] = changed

    report.cache_summary = caches.summary()
    report.classifier_stats = classifier.stats()
    state.save()
    return report


def _print_report(report: RunReport) -> None:
    for name, merge in sorted(report.per_source.items()):
        print(f"{name}: +{len(merge.added)} new, ~{len(merge.updated)} updated, "
              f"-{len(merge.removed)} removed, "
              f"={merge.unchanged} unchanged, "
              f"{len(merge.rejected)} rejected, "
              f"{len(merge.needs_review)} need review")
    changed = [name for name, did in report.files_changed.items() if did]
    print(f"files changed: {', '.join(sorted(changed)) if changed else 'none'}")
    print(f"classifier: {report.classifier_stats}")
    for error in report.errors:
        print(f"warning: {error}", file=sys.stderr)


def _persist_review(dataset: "Dataset", report: RunReport,
                    timestamp: str) -> Dict[str, int]:
    """Keep this run's unplaceable candidates, so the list survives the run.

    Without this the queue lived only in the run report. Three real gaps were
    found by hand that it had already flagged and thrown away.
    """
    from . import review
    from .util import normalize_url

    queue = review.load()
    counts = {"added": 0, "seen_again": 0, "resolved": 0}
    from .dataset import LIST_FIELD

    admitted = set()
    # Only the files that hold records: techniques and policy are settings,
    # and asking for their record list raises.
    for name in dataset.data:
        if name not in LIST_FIELD:
            continue
        for record in dataset.records(name):
            # An adoption record keeps its address in its evidence rather than
            # in a url field, so looking only at the record's own url left
            # those queue items open long after the record had landed.
            urls = [record.get("primary_url"), record.get("url")]
            urls += [item.get("source_url")
                     for item in record.get("evidence") or []]
            urls += [item.get("source_url") for item in
                     (record.get("attribution") or {}).get("evidence") or []]
            for url in urls:
                canonical = normalize_url(url or "")
                if canonical:
                    admitted.add(canonical)

    for name, merged in dataset.reports.items():
        found = review.merge(queue, merged.needs_review, source=name,
                             timestamp=timestamp, admitted=admitted)
        for key in counts:
            counts[key] += found[key]

    # A candidate that was classified and turned down is settled, not pending.
    # Without this it stays open for ever: the queue only dropped items whose
    # record was admitted, so every negative answer looked exactly like work
    # nobody had done yet, and real candidates hid among hundreds of them.
    turned_down = {}
    for merged in dataset.reports.values():
        for item in merged.rejected or []:
            canonical = normalize_url(item.get("url") or "")
            if canonical:
                turned_down[canonical] = item.get("reason") or "rejected"
    settled = [row["id"] for row in queue.get("open") or []
               if normalize_url(row.get("url") or "") in turned_down]
    if settled:
        counts["resolved"] += review.dismiss(
            queue, settled, "classified and turned down on a later run",
            decided_by="pipeline", timestamp=timestamp)
    review.save(queue)
    report.review_queue = review.summary(queue)
    return counts


def build() -> int:
    from . import pages as pages_module

    stats, stats_changed = stats_module.regenerate()
    changed = plots.write_all(stats)
    # After the plots: the chart links to these pages, so a system that has just
    # appeared must have a page by the time the link to it is written.
    page_outcome = pages_module.write_all(stats)
    page_outcome.update({f"year:{k}": v
                         for k, v in pages_module.write_years(stats).items()})
    page_outcome.update({f"talk:{k}": v
                         for k, v in pages_module.write_talks().items()})
    problems = validate.validate_all()
    for problem in problems:
        print(problem, file=sys.stderr)
    updated = [name for name, did in changed.items() if did]
    moved = [name for name, state in page_outcome.items()
             if state != "unchanged"]
    print(f"stats.json {'updated' if stats_changed else 'unchanged'}; "
          f"{len(updated)} plot(s) updated; "
          f"{len(page_outcome)} database system page(s), "
          f"{len(moved) or 'none'} changed")
    if problems:
        print(f"{len(problems)} validation problem(s)", file=sys.stderr)
        return 1
    print("impact data: all files valid")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="tools.impact.run")
    sub = parser.add_subparsers(dest="command", required=True)

    collect_parser = sub.add_parser("collect", help="run the collectors")
    collect_parser.add_argument("--only", nargs="+", choices=SOURCES,
                                help="restrict to these sources")
    collect_parser.add_argument("--full", action="store_true",
                                help="periodic full reconciliation")
    collect_parser.add_argument("--dry-run", action="store_true",
                                help="never call the classifier")
    collect_parser.add_argument("--report", type=Path,
                                help="write the run report as JSON")

    sub.add_parser("stats", help="regenerate stats.json")
    sub.add_parser("plots", help="regenerate the charts")
    sub.add_parser("pages", help="regenerate the per-database-system pages")
    sub.add_parser("review",
                   help="show the queue of candidates needing a decision")
    sub.add_parser("validate", help="validate the dataset")
    sub.add_parser("build", help="stats + plots + validate")
    sub.add_parser("notes",
                   help="write one note per citing paper (summary + evidence)")
    intake_parser = sub.add_parser(
        "intake", help="file downloaded paper PDFs into the drop folder")
    intake_parser.add_argument("--source", type=Path)
    intake_parser.add_argument("--limit", type=int, default=40)
    intake_parser.add_argument("--dry-run", action="store_true")
    intake_parser.add_argument("--json", type=Path)
    dedupe_parser = sub.add_parser(
        "dedupe", help="merge paper records that are the same work")
    dedupe_parser.add_argument("--apply", action="store_true")
    sub.add_parser("worklist",
                   help="list the papers whose PDF has to be supplied by hand")

    report_parser = sub.add_parser("report", help="render a pull-request body")
    report_parser.add_argument("--input", type=Path, required=True)
    report_parser.add_argument("--output", type=Path)

    args = parser.parse_args(argv)

    if args.command == "collect":
        report = collect(args.only, full=args.full, dry_run=args.dry_run)
        _print_report(report)
        problems = validate.validate_all()
        for problem in problems:
            print(problem, file=sys.stderr)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
        return 1 if problems else 0

    if args.command == "stats":
        return stats_module.main()
    if args.command == "plots":
        return plots.main()
    if args.command == "pages":
        from . import pages as pages_module
        return pages_module.main()
    if args.command == "review":
        from . import review as review_module
        return review_module.main()
    if args.command == "validate":
        return validate.main()
    if args.command == "build":
        return build()
    if args.command == "notes":
        from . import notes as notes_module
        return notes_module.main()
    if args.command == "intake":
        from . import intake
        argv = []
        if args.source:
            argv += ["--source", str(args.source)]
        argv += ["--limit", str(args.limit)]
        if args.dry_run:
            argv.append("--dry-run")
        if args.json:
            argv += ["--json", str(args.json)]
        return intake.main(argv)
    if args.command == "dedupe":
        from . import dedupe as dedupe_module
        return dedupe_module.main(apply=args.apply)
    if args.command == "worklist":
        from . import worklist
        return worklist.main()
    if args.command == "report":
        from .pr import render
        body = render(json.loads(args.input.read_text(encoding="utf-8")))
        if args.output:
            args.output.write_text(body, encoding="utf-8")
        else:
            print(body)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
