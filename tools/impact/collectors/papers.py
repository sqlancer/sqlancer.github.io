"""Discovers papers that cite SQLancer and prepares them for classification.

Discovery is entirely deterministic: every paper reached here was found by
walking the citation graph of the foundational SQLancer publications, so the
``references`` relationship is a fact about the graph rather than a judgement.

Two graphs are walked, because neither is complete. Semantic Scholar indexes
preprints well and -- decisively -- returns ``contexts``, the sentences in the
citing paper that surround the citation, which are verbatim source text and so
usable directly as evidence. OpenAlex has different coverage and stays available
when Semantic Scholar's shared pool is rate limiting. OpenCitations is built
from Crossref's own reference data and holds edges neither of the others
returned, at the cost of giving back nothing but a pair of DOIs -- and of being
blind to a seed with no DOI, which PQS, the most cited of them, is. Results are
merged on the most stable identifier available.

The three stronger relationships are judgements, and this module does not make
them. It assembles the evidence a classifier needs and flags which candidates
are worth asking about. A paper with no signal at all is recorded as
``insufficient_evidence`` and never reaches the model, which keeps the weekly
token cost proportional to new work rather than to corpus size.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .. import config, taxonomy
from ..scholarly import (OpenAlex, OpenCitations, SemanticScholar,
                         _normalise_title, _openalex_id, resolve_seed)
from ..util import (content_hash, dedupe_preserving_order, normalize_doi,
                    normalize_url, truncate)

COLLECTOR = "papers"
COLLECTOR_VERSION = "1.0.0"
EXTRACTOR_VERSION = "paper-signals-v1"

# Lower bound on a usable citation context. Shorter fragments are index noise.
MIN_CONTEXT_CHARS = 40
MAX_CONTEXT_CHARS = 1200
MAX_EVIDENCE_PER_RELATIONSHIP = 6

# Deterministic pre-filters. These decide only whether a candidate is worth a
# semantic question; they never decide the answer.
COMPARISON_CUES = re.compile(
    r"\b(baselines?|compared?|compares|comparison|comparing|evaluated?\s+"
    r"against|state-of-the-art|state\s+of\s+the\s+art|outperform\w*|"
    r"versus|vs\.?|against\s+(?:sqlancer|tlp|norec|pqs|qpg)|"
    r"than\s+(?:sqlancer|tlp|norec|pqs|qpg))\b",
    re.IGNORECASE)

EXTENSION_CUES = re.compile(
    r"\b(extend\w*|generali[sz]\w*|adapt\w*|builds?\s+(?:up)?on|"
    r"based\s+on|inspired\s+by|improves?\s+(?:up)?on|augment\w*|"
    r"we\s+follow|derived\s+from|variant\s+of)\b",
    re.IGNORECASE)

INFRASTRUCTURE_CUES = re.compile(
    r"\b(implemented?\s+(?:it\s+)?(?:on\s+top\s+of|in|within|upon)|"
    r"built?\s+(?:on\s+top\s+of|upon)|on\s+top\s+of\s+sqlancer|"
    r"prototype\s+(?:in|on)|our\s+implementation|fork\w*|"
    r"extend\w*\s+sqlancer|integrat\w*\s+into\s+sqlancer)\b",
    re.IGNORECASE)

ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)

# "state of the art", however it is hyphenated. Deliberately narrow: this is a
# claim about standing, and looser wording ("widely used", "popular", "the most
# effective") is a different claim that would need its own judgement.
STATE_OF_THE_ART = re.compile(r"state[-\s]of[-\s]the[-\s]art", re.IGNORECASE)


def state_of_the_art_contexts(contexts: Iterable[Tuple[str, str]],
                              tax: Optional["taxonomy.Taxonomy"] = None
                              ) -> List[Tuple[str, str]]:
    """Contexts that call SQLancer or one of its techniques state of the art.

    A citation context is a single sentence, so a sentence containing both the
    phrase and a SQLancer name is almost always saying the one about the other
    -- which is why this needs no model. Techniques the taxonomy excludes never
    match, so a sentence naming only EET or DQE is not recognition of SQLancer.
    """
    tax = tax or taxonomy.load()
    found: List[Tuple[str, str]] = []
    for technique_id, context in contexts:
        if not STATE_OF_THE_ART.search(context):
            continue
        matches = tax.find_techniques(context)
        mentions = tax.mentions_finder(context)
        if not matches and not mentions:
            continue
        attributed = matches[0].technique_id if matches else technique_id
        if (attributed, context) not in found:
            found.append((attributed, context))
    return found

# Titles the scholarly indexes sometimes produce from a PDF's cover page rather
# than from the paper. A record like this cannot be presented or plotted, so it
# is held back for a human rather than published with a nonsense title.
BOILERPLATE_TITLE = re.compile(
    r"^(this paper is included|proceedings of the|open access to the|"
    r"the following paper|table of contents|front matter|"
    r"conference (?:proceedings|program))",
    re.IGNORECASE)


def usable_metadata(candidate: "Candidate") -> Optional[str]:
    """Why a candidate is not publishable yet, or None when it is fine."""
    title = (candidate.title or "").strip()
    if len(title) < 8:
        return "no usable title from the scholarly index"
    if BOILERPLATE_TITLE.match(title):
        return "index returned proceedings boilerplate instead of a title"
    if not candidate.s2.get("year"):
        return "no publication year from the scholarly index"
    return None


def _clean_context(text: str) -> Optional[str]:
    """Normalise a citation context, or reject it as too short to be evidence."""
    if not text:
        return None
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) < MIN_CONTEXT_CHARS:
        return None
    return truncate(collapsed, MAX_CONTEXT_CHARS)


def paper_identity(external_ids: Dict[str, str], fallback_id: str
                   ) -> Tuple[str, Optional[str], Optional[str]]:
    """Return ``(record_id, doi, arxiv_id)`` preferring the most stable id.

    A DataCite arXiv DOI is folded back into a plain arXiv id, so the same
    preprint reached through OpenAlex (which reports the DOI) and through
    Semantic Scholar (which reports the arXiv id) lands on one record.
    """
    doi = normalize_doi(external_ids.get("DOI"))
    arxiv = external_ids.get("ArXiv")
    if doi:
        match = ARXIV_DOI.match(doi)
        if match:
            arxiv = arxiv or match.group(1)
            doi = None
    if doi:
        return f"paper:doi:{doi}", doi, arxiv
    if arxiv:
        return f"paper:arxiv:{arxiv}", None, arxiv
    return fallback_id, None, None


def _best_url(doi: Optional[str], arxiv: Optional[str],
              s2_paper_id: Optional[str], openalex_id: Optional[str]) -> str:
    if doi:
        return f"https://doi.org/{doi}"
    if arxiv:
        return f"https://arxiv.org/abs/{arxiv}"
    if s2_paper_id:
        return f"https://www.semanticscholar.org/paper/{s2_paper_id}"
    return f"https://openalex.org/{openalex_id}"


def normalise_openalex(work: dict) -> dict:
    """Project an OpenAlex work onto the Semantic Scholar citingPaper shape."""
    ids = work.get("ids") or {}
    external = {}
    if work.get("doi"):
        external["DOI"] = work["doi"]
    location = work.get("primary_location") or {}
    source = (location.get("source") or {}) if isinstance(location, dict) else {}
    best_oa = work.get("best_oa_location") or {}
    return {
        "paperId": None,
        "openalexId": _openalex_id(work.get("id", "")),
        "externalIds": external,
        "title": work.get("display_name") or work.get("title") or "",
        "year": work.get("publication_year"),
        "venue": source.get("display_name"),
        "authors": [{"name": (a.get("author") or {}).get("display_name")}
                    for a in (work.get("authorships") or [])],
        "openAccessPdf": {"url": best_oa.get("pdf_url")} if best_oa else {},
        "abstract": None,
        "_openalex_ids": ids,
    }


class Candidate:
    """A citing paper accumulated across seeds and across both citation graphs."""

    def __init__(self, citing: dict):
        self.external_ids = dict(citing.get("externalIds") or {})
        self.s2_paper_id = citing.get("paperId")
        self.openalex_id = citing.get("openalexId")
        self.record_id, self.doi, self.arxiv = paper_identity(
            self.external_ids,
            f"paper:s2:{self.s2_paper_id}" if self.s2_paper_id
            else f"paper:openalex:{self.openalex_id}")
        self.s2 = dict(citing)
        self.seeds: List[str] = []
        # (technique_id, context) pairs; the technique identifies the cited seed.
        self.contexts: List[Tuple[str, str]] = []
        self.intents: List[str] = []
        self.influential = False
        self.sources: List[str] = []

    @property
    def title(self) -> str:
        return self.s2.get("title") or ""

    def merge_metadata(self, other: dict) -> None:
        """Fill gaps from a second source without overwriting what we have."""
        for key in ("title", "year", "venue", "abstract"):
            if not self.s2.get(key) and other.get(key):
                self.s2[key] = other[key]
        if not self.s2.get("authors") and other.get("authors"):
            self.s2["authors"] = other["authors"]
        if not (self.s2.get("openAccessPdf") or {}).get("url"):
            if (other.get("openAccessPdf") or {}).get("url"):
                self.s2["openAccessPdf"] = other["openAccessPdf"]
        self.s2_paper_id = self.s2_paper_id or other.get("paperId")
        self.openalex_id = self.openalex_id or other.get("openalexId")
        for key, value in (other.get("externalIds") or {}).items():
            self.external_ids.setdefault(key, value)

    def add_edge(self, technique_id: str, edge: dict, source: str) -> None:
        if technique_id not in self.seeds:
            self.seeds.append(technique_id)
        if source not in self.sources:
            self.sources.append(source)
        for raw in edge.get("contexts") or []:
            context = _clean_context(raw)
            if context and (technique_id, context) not in self.contexts:
                self.contexts.append((technique_id, context))
        self.intents.extend(edge.get("intents") or [])
        self.influential = self.influential or bool(edge.get("isInfluential"))

    # -- deterministic signals -------------------------------------------

    def matching_contexts(self, pattern: re.Pattern) -> List[Tuple[str, str]]:
        return [(tech, ctx) for tech, ctx in self.contexts if pattern.search(ctx)]

    def signals(self) -> Dict[str, List[Tuple[str, str]]]:
        """Contexts suggesting each stronger relationship, for the classifier."""
        return {
            "compares_with": self.matching_contexts(COMPARISON_CUES),
            "extends_technique": self.matching_contexts(EXTENSION_CUES),
            "uses_infrastructure": self.matching_contexts(INFRASTRUCTURE_CUES),
        }

    def has_any_signal(self) -> bool:
        return any(self.signals().values())

    def evidence_hash(self) -> str:
        """Stable hash of everything a classifier would be shown.

        Keying the classification cache on this is what guarantees unchanged
        evidence is never sent to the model twice.
        """
        parts = [self.record_id, self.title, self.s2.get("abstract") or ""]
        parts.extend(f"{tech}:{ctx}" for tech, ctx in sorted(self.contexts))
        return content_hash("\n".join(parts))


def _merge_into(candidates: Dict[str, Candidate], citing: dict,
                technique_id: str, edge: dict, source: str,
                by_title: Dict[Tuple[str, Optional[int]], str]) -> None:
    """Add one citation edge, merging by identifier and then by title+year."""
    candidate = Candidate(citing)
    key = candidate.record_id
    existing = candidates.get(key)
    if existing is None:
        # Same work reached through the other graph without a shared identifier.
        title_key = (_normalise_title(candidate.title), citing.get("year"))
        if title_key[0] and title_key in by_title:
            existing = candidates[by_title[title_key]]
    if existing is None:
        candidates[key] = candidate
        title_key = (_normalise_title(candidate.title), citing.get("year"))
        if title_key[0]:
            by_title.setdefault(title_key, key)
        existing = candidate
    else:
        existing.merge_metadata(citing)
    existing.add_edge(technique_id, edge, source)


def gather(*, s2: Optional[SemanticScholar] = None,
           oa: Optional[OpenAlex] = None,
           oc: Optional["OpenCitations"] = None,
           seeds: Optional[Sequence[dict]] = None,
           progress: bool = False) -> Tuple[Dict[str, Candidate], List[dict]]:
    """Walk both citation graphs. Returns ``(candidates, unresolved_seeds)``.

    A failure of one source is logged and skipped rather than fatal: losing
    Semantic Scholar for a week must not empty the dataset, and the periodic
    reconciliation picks up whatever a degraded run missed.
    """
    tax = taxonomy.load()
    seeds = list(seeds if seeds is not None else tax.citation_seeds())
    candidates: Dict[str, Candidate] = {}
    by_title: Dict[Tuple[str, Optional[int]], str] = {}
    unresolved: List[dict] = []

    for seed in seeds:
        # The seed's own id, which is a technique for an oracle paper and the
        # tool for SQLancer++'s. What a citing paper records is which
        # foundational publication it cites, and both are that.
        technique_id = seed.get("seed_id") or seed["technique_id"]
        found_any = False

        if s2 is not None:
            try:
                resolved = resolve_seed(s2, seed)
            except Exception as error:  # network/rate-limit; try the other graph
                resolved = None
                if progress:
                    print(f"  seed {technique_id}: semantic scholar failed "
                          f"({type(error).__name__})")
            if resolved:
                found_any = True
                count = 0
                try:
                    for edge in s2.citations(resolved["paperId"]):
                        citing = edge.get("citingPaper") or {}
                        if not citing.get("paperId"):
                            continue
                        _merge_into(candidates, citing, technique_id, edge,
                                    "semantic_scholar", by_title)
                        count += 1
                except Exception as error:
                    if progress:
                        print(f"  seed {technique_id}: citation paging stopped "
                              f"({type(error).__name__})")
                if progress:
                    print(f"  seed {technique_id}: {count} citations from "
                          f"Semantic Scholar")

        if oa is not None:
            try:
                work_ids = oa.versions_of(seed)
            except Exception:
                work_ids = []
            count = 0
            for work_id in work_ids:
                found_any = found_any or True
                try:
                    for work in oa.citing_works(work_id):
                        citing = normalise_openalex(work)
                        if not citing["title"]:
                            continue
                        _merge_into(candidates, citing, technique_id,
                                    {"contexts": [], "intents": []},
                                    "openalex", by_title)
                        count += 1
                except Exception:
                    continue
            if progress and work_ids:
                print(f"  seed {technique_id}: {count} citations from OpenAlex "
                      f"({len(work_ids)} version(s))")

        if oc is not None and seed.get("doi"):
            # A third graph, from Crossref's own reference data. It returns
            # identifiers and nothing else, so a DOI already reached through
            # either of the other two is skipped rather than resolved again:
            # the metadata is better there, and this call costs a request.
            try:
                citing_dois = oc.citing_dois(seed["doi"])
            except Exception as error:
                citing_dois = []
                if progress:
                    print(f"  seed {technique_id}: OpenCitations failed "
                          f"({type(error).__name__})")
            seen_dois = {normalize_doi(c.doi) for c in candidates.values()
                         if c.doi}
            count = 0
            for doi in citing_dois:
                if doi in seen_dois:
                    continue
                found_any = True
                try:
                    citing = oc.work_by_doi(doi)
                except Exception:
                    continue
                if not citing or not citing.get("title"):
                    continue
                # No contexts: the edge carries the fact of the citation and
                # nothing else, which is exactly what it is evidence of.
                _merge_into(candidates, citing, technique_id,
                            {"contexts": [], "intents": []},
                            "opencitations", by_title)
                seen_dois.add(doi)
                count += 1
            if progress and citing_dois:
                print(f"  seed {technique_id}: {len(citing_dois)} citations "
                      f"from OpenCitations, {count} new")

        if not found_any:
            unresolved.append(seed)
        if progress:
            print(f"  -> {len(candidates)} distinct papers so far")

    return candidates, unresolved


def _relationship(value: str, method: str, *, evidence: Optional[List[dict]] = None,
                  techniques: Optional[List[str]] = None,
                  classifier: Optional[dict] = None) -> dict:
    relationship = {"value": value, "method": method}
    if techniques:
        relationship["techniques"] = dedupe_preserving_order(techniques)
    if evidence:
        relationship["evidence"] = evidence
    if classifier:
        relationship["classifier"] = classifier
    return relationship


def context_evidence(url: str, pairs: Iterable[Tuple[str, str]], note: str,
                     timestamp: str) -> List[dict]:
    """Turn citation contexts into evidence items.

    The excerpt is the context exactly as the index returned it; nothing here
    rewrites or summarises it.
    """
    tax = taxonomy.load()
    evidence = []
    for technique_id, context in list(pairs)[:MAX_EVIDENCE_PER_RELATIONSHIP]:
        evidence.append({
            "source_url": url,
            "source_type": "paper_citation_context",
            "excerpt": context,
            "excerpt_is_verbatim": True,
            "note": note.format(technique=tax.display_name(technique_id)),
            "content_sha256": content_hash(context),
            "retrieved_at": timestamp,
            "first_seen": timestamp,
            "last_verified": timestamp,
        })
    return evidence


def select_reference_contexts(candidate: Candidate
                              ) -> List[Tuple[str, str]]:
    """Choose which citation contexts to publish as evidence.

    An index often returns the same sentence once per cited seed, and the first
    few contexts are frequently the least informative ones. So duplicates are
    dropped, sentences that say something about the relationship are preferred,
    and at least one context per cited seed is kept so every seed the paper
    cites is represented.
    """
    seen_text = set()
    scored: List[Tuple[int, int, int, str, str]] = []
    for index, (technique_id, context) in enumerate(candidate.contexts):
        key = context.strip().lower()
        if key in seen_text:
            continue
        seen_text.add(key)
        informative = any(cue.search(context) for cue in
                          (INFRASTRUCTURE_CUES, EXTENSION_CUES, COMPARISON_CUES))
        scored.append((0 if informative else 1, -len(context), index,
                       technique_id, context))
    scored.sort()

    chosen: List[Tuple[str, str]] = []
    covered = set()
    for _, _, _, technique_id, context in scored:
        if len(chosen) >= MAX_EVIDENCE_PER_RELATIONSHIP:
            break
        chosen.append((technique_id, context))
        covered.add(technique_id)
    # Make sure no cited seed is left without a context of its own.
    for _, _, _, technique_id, context in scored:
        if technique_id in covered:
            continue
        if chosen:
            chosen.pop()
        chosen.append((technique_id, context))
        covered.add(technique_id)
    return chosen


def build_record(candidate: Candidate, *, timestamp: str,
                 classifications: Optional[Dict[str, dict]] = None,
                 artifacts: Optional[List[dict]] = None,
                 is_sqlancer_publication: bool = False) -> dict:
    """Assemble a paper record.

    ``references`` is always ``yes`` -- the paper is here because the citation
    graph says so. The other three default to ``insufficient_evidence`` and are
    only raised by a supplied classification, so an unclassified paper is never
    silently counted as building on SQLancer.
    """
    tax = taxonomy.load()
    s2 = candidate.s2
    url = _best_url(candidate.doi, candidate.arxiv, candidate.s2_paper_id,
                    candidate.openalex_id)
    classifications = classifications or {}

    reference_pairs = select_reference_contexts(candidate)
    if reference_pairs:
        reference_evidence = context_evidence(
            url, reference_pairs,
            "Sentence in this paper citing {technique}, as indexed by "
            "Semantic Scholar.", timestamp)
    else:
        # No indexed full text: the citation edge itself is the evidence.
        seed_names = ", ".join(tax.display_name(t) for t in candidate.seeds)
        index_url = (f"https://www.semanticscholar.org/paper/{candidate.s2_paper_id}"
                     if candidate.s2_paper_id
                     else f"https://openalex.org/{candidate.openalex_id}")
        index_name = ("Semantic Scholar" if candidate.s2_paper_id else "OpenAlex")
        reference_evidence = [{
            "source_url": index_url,
            "source_type": "paper",
            "excerpt": None,
            "note": (f"{index_name} records this paper as citing the "
                     f"publication that introduced {seed_names}."),
            "retrieved_at": timestamp,
            "first_seen": timestamp,
            "last_verified": timestamp,
        }]

    relationships = {
        "references": _relationship(
            "yes", "citation_graph", evidence=reference_evidence,
            techniques=candidate.seeds),
    }
    for key in ("uses_infrastructure", "extends_technique", "compares_with"):
        relationships[key] = classifications.get(key) or _relationship(
            "insufficient_evidence", "deterministic")

    # Recognition is read straight off the citation contexts: the sentence that
    # calls SQLancer state of the art is itself the evidence.
    sota = classifications.get("describes_as_state_of_the_art")
    if sota is None:
        pairs = state_of_the_art_contexts(candidate.contexts, tax)
        if pairs:
            sota = _relationship(
                "yes", "deterministic",
                techniques=[technique for technique, _ in pairs],
                evidence=context_evidence(
                    url, pairs,
                    "Sentence in this paper describing {technique} as state of "
                    "the art.", timestamp))
        else:
            sota = _relationship("no", "deterministic")
    relationships["describes_as_state_of_the_art"] = sota

    authors = [a.get("name") for a in (s2.get("authors") or []) if a.get("name")]
    record = {
        "id": candidate.record_id,
        "title": s2.get("title") or "(untitled)",
        "authors": authors[:60],
        "year": s2.get("year"),
        "venue": s2.get("venue") or None,
        "doi": candidate.doi,
        "arxiv_id": candidate.arxiv,
        "s2_paper_id": candidate.s2_paper_id,
        "url": url,
        "open_access_pdf": normalize_url((s2.get("openAccessPdf") or {}).get("url")),
        # Kept for the paper's note. For a paywalled paper it is the only
        # description of the work we can obtain at all, and a summary written
        # from an abstract is worth far more than one written from two
        # citation sentences.
        "abstract": truncate(s2.get("abstract") or "", 6000) or None,
        "cites_seed_techniques": candidate.seeds,
        "relationships": relationships,
        "provenance": {
            "collector": COLLECTOR,
            "collector_version": COLLECTOR_VERSION,
            "policy_version": config.policy_version(),
            "first_seen": timestamp,
            "last_verified": timestamp,
            "source_url": (f"https://www.semanticscholar.org/paper/"
                           f"{candidate.s2_paper_id}" if candidate.s2_paper_id
                           else f"https://openalex.org/{candidate.openalex_id}"),
            "source_type": "paper",
            "content_sha256": candidate.evidence_hash(),
        },
    }
    if artifacts:
        record["artifacts"] = artifacts
    if is_sqlancer_publication:
        record["is_sqlancer_publication"] = True
    return record


def _norm_title(title: str) -> str:
    return "".join(ch for ch in (title or "").lower() if ch.isalnum())


def is_project_publication(candidate: Candidate,
                           tax: Optional[taxonomy.Taxonomy] = None) -> bool:
    """Whether this candidate *is* one of the taxonomy's own publications.

    The foundational papers cite each other, so they turn up in their own
    citation graphs. Matching them on identifier or title is exact, which is
    better than inferring authorship.
    """
    tax = tax or taxonomy.load()
    papers = [technique.get("origin_paper")
              for technique in tax.techniques.values()]
    papers += [finder.get("paper") for finder in tax.finders.values()]
    papers += tax.project_publications
    for paper in papers:
        if not paper:
            continue
        if candidate.doi and normalize_doi(paper.get("doi")) == candidate.doi:
            return True
        if candidate.arxiv and paper.get("arxiv_id") == candidate.arxiv:
            return True
        if (candidate.s2_paper_id
                and paper.get("s2_paper_id") == candidate.s2_paper_id):
            return True
        if _norm_title(paper.get("title", "")) == _norm_title(candidate.title):
            return True
    return False


def looks_like_sqlancer_publication(candidate: Candidate) -> bool:
    """Whether a citing paper is itself part of the SQLancer project.

    Either it is a publication the taxonomy names -- a technique's origin paper,
    a tool's paper, or one listed explicitly -- or one of the project's authors
    is on it. Co-authorship alone settles it: the project's own papers are not
    external work building on SQLancer, whatever their subject, and a rule that
    also demanded a SQLancer name in the title missed papers whose titles are
    about something else.
    """
    tax = taxonomy.load()
    if is_project_publication(candidate, tax):
        return True
    return any(tax.is_project_author(author.get("name"))
               for author in (candidate.s2.get("authors") or []))
