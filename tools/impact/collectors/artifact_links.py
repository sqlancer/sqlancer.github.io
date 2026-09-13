"""Extracts artifact links from the papers themselves.

The other routes to a paper's artifact are guesses: code search finds
repositories that look derived, and repository search finds ones whose name
resembles the tool. This one reads what the paper actually says. Authors put
the link in the abstract, in a footnote on the first page, or in an artifact
availability section, and following it is both the most reliable route and the
one a person would use.

Three sources are tried in order of cost: the arXiv abstract page, which is
small HTML; the open-access PDF, whose first and last pages hold the link
almost every time; and the DOI landing page. Anything found is a URL the paper
published, so it needs no separate argument that the repository belongs to it.
"""

from __future__ import annotations

import io
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..http import Fetcher
from ..util import normalize_url, now, truncate

COLLECTOR = "artifact_links"
COLLECTOR_VERSION = "1.0.0"

# Hosts that hold research artifacts. Anything else a paper links is not one.
ARTIFACT_URL = re.compile(
    r"https?://(?:www\.)?"
    r"(github\.com/[\w.-]+/[\w.-]+"
    r"|gitlab\.com/[\w.-]+/[\w.-]+"
    r"|bitbucket\.org/[\w.-]+/[\w.-]+"
    r"|zenodo\.org/(?:record|records|doi)/[\w./-]+"
    r"|figshare\.com/[\w./-]+"
    r"|doi\.org/10\.5281/zenodo\.[\w.]+)",
    re.IGNORECASE)

# GitHub paths that are not a project: the site's own pages and user profiles.
NOT_A_REPOSITORY = re.compile(
    r"github\.com/(about|features|pricing|topics|collections|sponsors|"
    r"readme|site|security|apps|marketplace|orgs|settings|login|join|"
    r"blog|explore)(/|$)", re.IGNORECASE)

# Text around a link that marks it as the paper's own artifact rather than a
# tool it merely cites. Used to rank, not to exclude.
ARTIFACT_PHRASE = re.compile(
    r"(artifact|available at|open.?sourc\w*|our\s+(?:tool|implementation|"
    r"prototype|code)|source\s+code|replication|reproduc\w*|"
    r"we\s+(?:release|publish|provide))",
    re.IGNORECASE)

PDF_MAX_BYTES = 12_000_000
PDF_PAGES_FRONT = 2
PDF_PAGES_BACK = 2
EXCERPT_LIMIT = 500


def _clean(url: str) -> Optional[str]:
    """Normalise a link found in running text.

    PDF extraction leaves trailing punctuation and citation markers glued to
    URLs, and a repository URL often arrives with a branch or file path on the
    end; both are trimmed back to the repository itself.
    """
    url = url.rstrip(").,;:'\"]}>")
    canonical = normalize_url(url)
    if not canonical or NOT_A_REPOSITORY.search(canonical):
        return None
    match = re.match(r"^(https://github\.com/[\w.-]+/[\w.-]+)", canonical)
    if match:
        canonical = match.group(1)
        if canonical.endswith(".git"):
            canonical = canonical[:-4]
    return canonical


def urls_in(text: str) -> List[Tuple[str, str]]:
    """``(url, surrounding text)`` for every artifact-hosting link in ``text``."""
    found: List[Tuple[str, str]] = []
    seen = set()
    for match in ARTIFACT_URL.finditer(text or ""):
        canonical = _clean(match.group(0))
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        start = max(0, match.start() - 240)
        end = min(len(text), match.end() + 120)
        found.append((canonical, re.sub(r"\s+", " ", text[start:end]).strip()))
    return found


def _rank(entries: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Links whose surrounding text calls them an artifact come first."""
    return sorted(entries, key=lambda item: 0 if ARTIFACT_PHRASE.search(item[1])
                  else 1)


def from_arxiv(fetcher: Fetcher, arxiv_id: str) -> List[Tuple[str, str]]:
    """Links on an arXiv abstract page."""
    try:
        entry, _ = fetcher.fetch(f"https://arxiv.org/abs/{arxiv_id}",
                                 max_age_days=90)
    except Exception:
        return []
    if entry.get("status") != 200:
        return []
    text = re.sub(r"<[^>]+>", " ", entry.get("body") or "")
    return urls_in(text)


def from_pdf(fetcher: Fetcher, url: str) -> List[Tuple[str, str]]:
    """Links on the first and last pages of an open-access PDF.

    Only the ends of the paper are read: the artifact link lives in a first-page
    footnote or an availability section at the back, and parsing every page of
    every paper would cost far more for almost nothing.
    """
    try:
        entry, _ = fetcher.fetch(url, max_age_days=180)
    except Exception:
        return []
    body = entry.get("body") or ""
    if entry.get("status") != 200 or not body:
        return []
    raw = body.encode("utf-8", errors="replace")
    if not raw.lstrip().startswith(b"%PDF") or len(raw) > PDF_MAX_BYTES:
        # Not a PDF: publishers often answer with an HTML landing page.
        return urls_in(re.sub(r"<[^>]+>", " ", body))
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(raw))
        pages = reader.pages
        wanted = list(range(min(PDF_PAGES_FRONT, len(pages))))
        wanted += [i for i in range(max(0, len(pages) - PDF_PAGES_BACK),
                                    len(pages)) if i not in wanted]
        text = "\n".join(pages[i].extract_text() or "" for i in wanted)
    except Exception:
        return []
    # PDF extraction frequently breaks a URL across a line.
    text = re.sub(r"-\s*\n\s*", "", text)
    text = re.sub(r"\n", " ", text)
    return urls_in(text)


def for_paper(fetcher: Fetcher, paper: dict) -> List[dict]:
    """Artifact links this paper publishes, best first.

    Each entry carries the sentence the link was found in, so the claim that a
    repository belongs to a paper is backed by the paper's own words.
    """
    entries: List[Tuple[str, str, str]] = []

    if paper.get("arxiv_id"):
        for url, context in from_arxiv(fetcher, paper["arxiv_id"]):
            entries.append((url, context, f"https://arxiv.org/abs/{paper['arxiv_id']}"))

    pdf = paper.get("open_access_pdf")
    if pdf:
        for url, context in from_pdf(fetcher, pdf):
            entries.append((url, context, pdf))

    seen = set()
    ranked = _rank([(url, context) for url, context, _ in entries])
    order = {url: index for index, (url, _) in enumerate(ranked)}
    entries.sort(key=lambda item: order.get(item[0], 99))

    out: List[dict] = []
    for url, context, source in entries:
        if url in seen:
            continue
        seen.add(url)
        out.append({
            "url": url,
            "context": truncate(context, EXCERPT_LIMIT),
            "source_url": source,
            "states_artifact": bool(ARTIFACT_PHRASE.search(context)),
        })
    return out


def link_evidence(entry: dict, timestamp: Optional[str] = None) -> dict:
    """Evidence that the paper itself points at this repository."""
    timestamp = timestamp or now()
    return {
        "source_url": entry["source_url"],
        "source_type": "paper",
        "excerpt": entry["context"],
        "excerpt_is_verbatim": True,
        "note": (f"The paper links {entry['url']} as its artifact."
                 if entry.get("states_artifact") else
                 f"The paper links {entry['url']}."),
        "retrieved_at": timestamp,
        "first_seen": timestamp,
        "last_verified": timestamp,
    }
