"""Clients for the scholarly APIs used to discover SQLancer-citing papers.

Semantic Scholar is the primary source because it merges preprint and
conference versions of the same work into one record and, crucially, returns
``contexts``: the sentences in the citing paper that surround the citation.
Those sentences are verbatim source text, which makes them usable directly as
evidence excerpts and means most classification questions can be asked about
real quoted material rather than about a guess.

OpenAlex is used as a fallback for metadata (DOI, venue, open-access links)
when Semantic Scholar has gaps.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterator, List, Optional, Sequence

from . import config
from .cache import RawCache
from .http import Fetcher, with_params

S2_API = "https://api.semanticscholar.org/graph/v1"
OPENALEX_API = "https://api.openalex.org"
OPENCITATIONS_API = "https://opencitations.net/index/api/v1"
CROSSREF_API = "https://api.crossref.org"

CITATION_FIELDS = ",".join([
    "paperId", "title", "year", "venue", "externalIds", "authors",
    "openAccessPdf", "abstract", "publicationTypes",
])

PAPER_FIELDS = ",".join([
    "paperId", "title", "year", "venue", "externalIds", "citationCount",
    "authors", "openAccessPdf", "abstract",
])


class SemanticScholar:
    """Paged access to the Semantic Scholar graph API.

    The unauthenticated pool is shared and returns 429 often, so requests are
    spaced out and the fetcher's backoff does the rest. Setting
    ``SEMANTIC_SCHOLAR_API_KEY`` lifts both limits considerably.
    """

    def __init__(self, cache: RawCache, *, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        # The shared unauthenticated pool returns 429 readily; long backoff is
        # cheaper than losing a weekly run to it.
        self.fetcher = Fetcher(
            cache, min_interval=1.0 if self.api_key else 4.0, headers=headers,
            max_retries=8, initial_backoff=5.0, max_backoff=120.0)

    def paper(self, identifier: str, *, max_age_days: int = 30) -> Optional[dict]:
        url = with_params(f"{S2_API}/paper/{identifier}", {"fields": PAPER_FIELDS})
        data, _ = self.fetcher.fetch_json(url, max_age_days=max_age_days)
        return data if isinstance(data, dict) and data.get("paperId") else None

    def search_one(self, title: str, *, max_age_days: int = 90) -> Optional[dict]:
        url = with_params(f"{S2_API}/paper/search",
                          {"query": title, "fields": PAPER_FIELDS, "limit": 5})
        data, _ = self.fetcher.fetch_json(url, max_age_days=max_age_days)
        if not isinstance(data, dict):
            return None
        wanted = _normalise_title(title)
        for candidate in data.get("data", []):
            if _normalise_title(candidate.get("title", "")) == wanted:
                return candidate
        return None

    def citations(self, paper_id: str, *, page_size: int = 500,
                  max_age_days: int = 7) -> Iterator[dict]:
        """Yield every citation edge pointing at ``paper_id``.

        Each item carries the citing paper's metadata plus ``contexts`` and
        ``intents`` for that specific citation.
        """
        offset = 0
        while True:
            url = with_params(f"{S2_API}/paper/{paper_id}/citations", {
                "fields": CITATION_FIELDS + ",contexts,intents,isInfluential",
                "limit": page_size,
                "offset": offset,
            })
            data, _ = self.fetcher.fetch_json(
                url, cache_key=f"s2:citations:{paper_id}:{offset}:{page_size}",
                max_age_days=max_age_days)
            if not isinstance(data, dict):
                return
            batch = data.get("data", [])
            for item in batch:
                yield item
            if "next" not in data or not batch:
                return
            offset = data["next"]
            if offset >= 10000:  # API hard limit
                return


class OpenAlex:
    """Secondary metadata source; polite pool keyed by a contact address."""

    def __init__(self, cache: RawCache, *, mailto: Optional[str] = None):
        self.mailto = mailto or config.CONTACT_EMAIL
        self.fetcher = Fetcher(cache, min_interval=0.2,
                               headers={"Accept": "application/json"})

    def work_by_doi(self, doi: str, *, max_age_days: int = 90) -> Optional[dict]:
        url = with_params(f"{OPENALEX_API}/works/doi:{doi}", {"mailto": self.mailto})
        data, _ = self.fetcher.fetch_json(url, max_age_days=max_age_days)
        return data if isinstance(data, dict) and data.get("id") else None

    def search(self, query: str, *, per_page: int = 5,
               max_age_days: int = 90) -> List[dict]:
        url = with_params(f"{OPENALEX_API}/works", {
            "search": query, "per-page": per_page, "mailto": self.mailto})
        data, _ = self.fetcher.fetch_json(url, max_age_days=max_age_days)
        if not isinstance(data, dict):
            return []
        return data.get("results", [])

    def versions_of(self, seed: dict, *, max_age_days: int = 90) -> List[str]:
        """OpenAlex work ids for every version of a seed publication.

        OpenAlex keeps the preprint and the conference version as separate
        works, each with its own citation list, so a seed has to be resolved to
        all of them or its citations are undercounted.
        """
        found: List[str] = []
        if seed.get("doi"):
            work = self.work_by_doi(seed["doi"], max_age_days=max_age_days)
            if work:
                found.append(_openalex_id(work["id"]))
        if seed.get("arxiv_id"):
            work = self.work_by_doi(f"10.48550/arxiv.{seed['arxiv_id']}",
                                    max_age_days=max_age_days)
            if work:
                found.append(_openalex_id(work["id"]))
        wanted = _normalise_title(seed.get("title", ""))
        for candidate in self.search(seed.get("title", ""), per_page=8,
                                     max_age_days=max_age_days):
            if _normalise_title(candidate.get("display_name", "")) == wanted:
                found.append(_openalex_id(candidate["id"]))
        return [wid for i, wid in enumerate(found) if wid not in found[:i]]

    def citing_works(self, work_id: str, *, max_age_days: int = 7
                     ) -> Iterator[dict]:
        """Yield every work citing ``work_id``, paged by cursor."""
        cursor = "*"
        while cursor:
            url = with_params(f"{OPENALEX_API}/works", {
                "filter": f"cites:{work_id}",
                "per-page": 200,
                "cursor": cursor,
                "mailto": self.mailto,
                "select": ("id,doi,title,display_name,publication_year,"
                           "authorships,primary_location,best_oa_location,"
                           "type,ids"),
            })
            data, _ = self.fetcher.fetch_json(
                url, cache_key=f"openalex:cites:{work_id}:{cursor}",
                max_age_days=max_age_days)
            if not isinstance(data, dict):
                return
            for item in data.get("results", []):
                yield item
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not data.get("results"):
                return


def _openalex_id(url_or_id: str) -> str:
    return (url_or_id or "").rsplit("/", 1)[-1]


class OpenCitations:
    """A third citation graph, built from Crossref's own reference data.

    Different coverage again: it holds edges neither Semantic Scholar nor
    OpenAlex returned, including an ICSE-SEIP study of eBay's SQL analytics
    platform that cites both NoREC and TLP. What it gives back is only a pair
    of DOIs, so a candidate found here has to be resolved to metadata before
    it is any use, and it carries no citation context -- there is nothing in
    the edge but the fact of the citation.

    A seed with no DOI is invisible to it. That is a real limit rather than a
    detail: PQS is a USENIX paper with no DOI and the most cited seed there
    is.
    """

    def __init__(self, cache: RawCache, *, mailto: Optional[str] = None):
        self.mailto = mailto or config.CONTACT_EMAIL
        self.fetcher = Fetcher(cache, min_interval=1.0,
                               headers={"Accept": "application/json"})

    def citing_dois(self, doi: str, *, max_age_days: int = 7) -> List[str]:
        """Every DOI recorded as citing ``doi``."""
        from .util import normalize_doi

        data, _ = self.fetcher.fetch_json(
            f"{OPENCITATIONS_API}/citations/{doi}",
            cache_key=f"opencitations:citations:{doi}",
            max_age_days=max_age_days)
        if not isinstance(data, list):
            return []
        found = []
        for row in data:
            # The field occasionally holds two DOIs run together; a value with
            # whitespace in it is not one DOI and is not guessed at.
            raw = (row.get("citing") or "").strip()
            if not raw or any(c.isspace() for c in raw):
                continue
            citing = normalize_doi(raw)
            if citing and citing not in found:
                found.append(citing)
        return found

    def work_by_doi(self, doi: str, *, max_age_days: int = 90) -> Optional[dict]:
        """Crossref metadata for a DOI, in the shape a candidate expects.

        OpenCitations returns identifiers only, so the metadata comes from
        Crossref, which is where its reference data came from in the first
        place.
        """
        url = with_params(f"{CROSSREF_API}/works/{doi}", {"mailto": self.mailto})
        data, _ = self.fetcher.fetch_json(url, max_age_days=max_age_days)
        message = (data or {}).get("message") if isinstance(data, dict) else None
        if not isinstance(message, dict):
            return None
        issued = ((message.get("issued") or {}).get("date-parts") or [[None]])[0]
        authors = []
        for author in message.get("author") or []:
            name = " ".join(part for part in (author.get("given"),
                                              author.get("family")) if part)
            if name:
                authors.append({"name": name})
        return {
            "paperId": None,
            "externalIds": {"DOI": doi},
            "title": (message.get("title") or [None])[0],
            "year": issued[0] if issued else None,
            "venue": (message.get("container-title") or [None])[0],
            "authors": authors,
            "abstract": None,
            "url": f"https://doi.org/{doi}",
        }


class ArXiv:
    """Finds the preprint of a paper whose published version is paywalled.

    Most of the venues in this corpus are ACM, and dl.acm.org answers automated
    requests with a 403 -- which leaves the majority of papers with no readable
    full text at all. A great many of them have an arXiv preprint, and that
    preprint is a perfectly good basis for judging what a paper does.

    Matching is strict on purpose. A title search returns near neighbours, so a
    candidate is accepted only when its title matches exactly once normalised
    and it shares an author surname with the record being resolved.
    """

    API = "http://export.arxiv.org/api/query"

    def __init__(self, cache: RawCache):
        # arXiv asks for no more than one request every three seconds.
        self.fetcher = Fetcher(cache, min_interval=3.0, max_retries=3,
                               timeout=30.0, headers={"Accept": "application/atom+xml"})

    def _entries(self, query: str, limit: int) -> List[dict]:
        url = with_params(self.API, {"search_query": query,
                                     "max_results": limit,
                                     "sortBy": "relevance"})
        try:
            entry, _ = self.fetcher.fetch(url, max_age_days=180)
        except Exception:
            return []
        if entry.get("status") != 200:
            return []
        return _parse_atom(entry.get("body") or "")

    def find_preprint(self, title: str, authors: Sequence[str] = ()
                      ) -> Optional[dict]:
        """The arXiv preprint of ``title``, or None if there is no clear match."""
        if not title or len(title) < 12:
            return None
        wanted = _normalise_title(title)
        surnames = {name.split()[-1].lower()
                    for name in authors if name and name.split()}
        escaped = re.sub(r'["\\]', " ", title)
        for query in (f'ti:"{escaped}"', f'all:"{escaped}"'):
            for candidate in self._entries(query, 5):
                if _normalise_title(candidate["title"]) != wanted:
                    continue
                if surnames:
                    found = {name.split()[-1].lower()
                             for name in candidate["authors"] if name.split()}
                    if not (surnames & found):
                        continue
                return candidate
        return None


def _parse_atom(xml: str) -> List[dict]:
    """Entries of an arXiv Atom response.

    Parsed with the standard library rather than a dependency; the response is
    small and its shape is fixed.
    """
    import xml.etree.ElementTree as ET

    namespace = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    entries = []
    for node in root.findall("a:entry", namespace):
        identifier = (node.findtext("a:id", "", namespace) or "").rsplit("/", 1)[-1]
        if not identifier:
            continue
        title = re.sub(r"\s+", " ",
                       node.findtext("a:title", "", namespace) or "").strip()
        authors = [re.sub(r"\s+", " ", (a.findtext("a:name", "", namespace) or "")).strip()
                   for a in node.findall("a:author", namespace)]
        pdf = None
        for link in node.findall("a:link", namespace):
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf = link.get("href")
        entries.append({
            "arxiv_id": identifier,
            "versionless_id": re.sub(r"v\d+$", "", identifier),
            "title": title,
            "authors": [a for a in authors if a],
            "pdf_url": pdf or f"https://arxiv.org/pdf/{identifier}",
            "abstract": re.sub(r"\s+", " ",
                               node.findtext("a:summary", "", namespace) or "").strip(),
        })
    return entries


def _normalise_title(title: str) -> str:
    return "".join(ch for ch in (title or "").lower() if ch.isalnum())


def resolve_seed(s2: SemanticScholar, seed: dict) -> Optional[dict]:
    """Resolve a foundational publication to a Semantic Scholar record.

    Tries the recorded paper id, then the DOI, then an exact title match, so a
    seed without a DOI (several SQLancer papers predate one) still resolves.
    """
    for identifier in filter(None, [
        seed.get("s2_paper_id"),
        f"DOI:{seed['doi']}" if seed.get("doi") else None,
        f"ARXIV:{seed['arxiv_id']}" if seed.get("arxiv_id") else None,
    ]):
        paper = s2.paper(identifier)
        if paper:
            return paper
    if seed.get("title"):
        return s2.search_one(seed["title"])
    return None
