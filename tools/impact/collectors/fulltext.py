"""Reads a paper's full text and finds the sentences that decide its category.

Until now every judgement about a paper rested on citation contexts -- the one
or two sentences a scholarly index happens to have extracted around a citation.
That is a thin basis, and for most of this corpus there was nothing else: the
venues are largely ACM, and dl.acm.org refuses automated requests.

An arXiv preprint sidesteps that, and the full text is a far better source than
a handful of contexts. What a paper did with SQLancer is usually stated plainly
somewhere in it -- "we implemented our approach on top of SQLancer", "we compare
against TLP and NoREC" -- and this module finds those sentences and keeps them
verbatim.

The patterns here are deliberately narrow. They match statements a paper makes
about its own work, not descriptions of related work, and anything less direct
is left for the classifier rather than guessed at.
"""

from __future__ import annotations

import io
import pathlib
import re
from typing import Dict, List, Optional, Sequence, Tuple

from .. import taxonomy
from ..http import Fetcher
from ..util import content_hash, now, truncate

COLLECTOR = "fulltext"
COLLECTOR_VERSION = "1.0.0"
EXTRACTOR_VERSION = "fulltext-signals-v1"

MAX_PDF_BYTES = 25_000_000
MAX_TEXT_CHARS = 400_000
EXCERPT_LIMIT = 700

# A sentence, roughly. Papers are full of abbreviations and citation markers, so
# this splits on terminators followed by a capital rather than on any full stop.
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")

_TOOL = r"(?:SQLancer\+\+|SQLancer|ShQveL)"
_TECH = r"(?:TLP|NoREC|PQS|QPG|CERT|DQP|CODDTest|Ternary Logic Partitioning" \
        r"|Non-optimizing Reference Engine Construction|Pivoted Query Synthesis" \
        r"|Query Plan Guidance|query partitioning)"

# "We built our tool on SQLancer." Statements about the authors' own artifact.
# `we` or `our` is required: without it "the WHERE Extended case of TLP ... from
# SQLancer" -- a figure caption -- reads as a claim of reuse. And the
# prepositions are limited to ones that mean "built on": "from" and "with" match
# far too much ("a query from SQLancer's generator").
USES_INFRASTRUCTURE = re.compile(
    rf"\b(?:we|our)\b[^.]{{0,120}}?"
    rf"\b(?:implemented|implements|implement|built|build|developed|develops|"
    rf"prototyped|prototype|based|extended|extends|modified|integrated)\b"
    rf"[^.]{{0,80}}?\b(?:on\s+top\s+of|upon|on|in|into|using)\s+"
    rf"(?:the\s+)?{_TOOL}\b",
    re.IGNORECASE)

# The same claim written the other way round.
USES_INFRASTRUCTURE_ALT = re.compile(
    rf"\b(?:is|are|was|were)\s+(?:implemented|built|developed|based)\b"
    rf"[^.]{{0,60}}?\b(?:on\s+top\s+of|upon|on|in)\s+(?:the\s+)?{_TOOL}\b",
    re.IGNORECASE)

# "We compare X against TLP and NoREC." An empirical comparison the paper ran.
COMPARES_WITH = re.compile(
    rf"\b(?:we|our)\b[^.]{{0,140}}?"
    rf"\b(?:compare[ds]?|comparison|evaluat\w+|benchmark\w*|"
    rf"baseline[s]?|against)\b[^.]{{0,140}}?\b(?:{_TOOL}|{_TECH})\b",
    re.IGNORECASE)

COMPARES_WITH_ALT = re.compile(
    rf"\b(?:{_TOOL}|{_TECH})\b[^.]{{0,80}}?\bas\s+(?:a\s+|our\s+|the\s+)?"
    rf"baseline[s]?\b",
    re.IGNORECASE)

# "We extend TLP to graph databases." A claimed contribution over a technique.
EXTENDS_TECHNIQUE = re.compile(
    rf"\b(?:we|our)\b[^.]{{0,120}}?"
    rf"\b(?:extend(?:ed|s)?|generali[sz]\w+|adapt(?:ed|s)?|build[s]?\s+(?:up)?on|"
    rf"inspired\s+by|derive[ds]?\s+from|improve[sd]?\s+(?:up)?on)\b"
    rf"[^.]{{0,80}}?\b(?:{_TECH}|{_TOOL})\b",
    re.IGNORECASE)

# Wording that marks a sentence as about someone else's work, not the authors'.
RELATED_WORK = re.compile(
    r"\b(?:previous|prior|existing|earlier|related)\s+(?:work|approaches|"
    r"studies|techniques|tools)\b|\bhas\s+been\s+(?:proposed|shown)\b",
    re.IGNORECASE)

# "We adapted SQLancer as a baseline for comparison" matches the extension
# pattern on "adapted ... SQLancer" and means the opposite: the tool was taken
# up in order to be measured against, not built upon. The rest of the sentence
# is what settles it, so a sentence that says baseline is a comparison only.
AS_A_BASELINE = re.compile(
    r"\b(?:as\s+(?:a\s+|our\s+|the\s+)?baselines?|for\s+comparison|"
    r"to\s+compare\s+(?:against|with)|as\s+(?:a\s+|our\s+)?"
    r"(?:comparison|reference)\s+(?:point|tool|system)?)\b",
    re.IGNORECASE)

SIGNALS = {
    "uses_infrastructure": (USES_INFRASTRUCTURE, USES_INFRASTRUCTURE_ALT),
    "compares_with": (COMPARES_WITH, COMPARES_WITH_ALT),
    "extends_technique": (EXTENDS_TECHNIQUE,),
}

# A relationship that a sentence cannot claim once it says "as a baseline".
NOT_WHEN_BASELINE = ("extends_technique", "uses_infrastructure")


# PVLDB puts every paper on vldb.org, free, at a URL that can be derived from
# the volume, the first page and the first author's surname -- all of which
# OpenAlex records. It is the one major venue in this corpus whose full text is
# reachable without an arXiv preprint.
PVLDB_DOI_PREFIX = "10.14778/"
PVLDB_FIRST_VOLUME_YEAR = 2007


def pvldb_pdf_url(doi: Optional[str], work: Optional[dict]) -> Optional[str]:
    """The vldb.org PDF for a PVLDB paper, or None."""
    if not doi or not doi.lower().startswith(PVLDB_DOI_PREFIX) or not work:
        return None
    biblio = work.get("biblio") or {}
    first_page = (biblio.get("first_page") or "").strip()
    volume = (biblio.get("volume") or "").strip()
    if not volume:
        year = work.get("publication_year")
        volume = str(year - PVLDB_FIRST_VOLUME_YEAR) if year else ""
    authorships = work.get("authorships") or []
    surname = ""
    if authorships:
        name = ((authorships[0].get("author") or {}).get("display_name") or "")
        parts = [part for part in name.split() if part]
        surname = parts[-1].lower() if parts else ""
    if not (first_page and volume and surname):
        return None
    return (f"https://www.vldb.org/pvldb/vol{volume}/"
            f"p{first_page}-{surname}.pdf")


# USENIX publishes every paper openly, and serves the PDF from a path built
# from the venue, the year and the first author. Worth deriving: seven papers in
# this corpus are USENIX, and because USENIX assigns no DOI they are exactly the
# records that fall back to a Semantic Scholar id and end up on the hand-download
# list -- SQLRight, Pinolo, DynSQL, WingFuzz among them.
USENIX_SLUGS = {
    r"security": ("usenixsecurity", "sec"),
    r"annual technical|atc": ("atc",),
    r"operating systems design|osdi": ("osdi",),
    r"networked systems design|nsdi": ("nsdi",),
    r"file and storage|fast": ("fast",),
}


def usenix_pdf_urls(paper: dict) -> List[str]:
    """Candidate USENIX URLs for a paper, best first.

    Several, because the naming is not quite consistent: USENIX Security was
    "sec22" and became "usenixsecurity24", and a paper's file is sometimes named
    for its first author alone and sometimes with a word of the title after it.
    Each candidate is a cheap conditional request, and a wrong guess simply
    404s.
    """
    venue = (paper.get("venue") or "").lower()
    if "usenix" not in venue and "osdi" not in venue:
        return []
    year = paper.get("year")
    authors = paper.get("authors") or []
    if not year or not authors:
        return []

    slugs: List[str] = []
    for pattern, names in USENIX_SLUGS.items():
        if re.search(pattern, venue):
            slugs.extend(names)
    if not slugs:
        return []

    surname = re.sub(r"[^a-z]", "", (authors[0].split()[-1] if authors[0] else "").lower())
    if not surname:
        return []
    # A paper published at the conference of year N appears under N, but a
    # winter deadline can put it under N-1.
    short_years = [str(year)[-2:], str(year - 1)[-2:]]
    first_word = ""
    for word in re.sub(r"[^A-Za-z ]", " ", paper.get("title") or "").split():
        if len(word) > 3 and word.lower() not in ("with", "from", "that", "this"):
            first_word = word.lower()
            break

    # Where two papers by different people would collide, USENIX disambiguates
    # with the author's given name rather than a word of the title.
    given = re.sub(r"[^a-z-]", "", (authors[0].split()[0] if authors[0] else "").lower())

    urls: List[str] = []
    for slug in slugs:
        for short in short_years:
            stem = f"{slug}{short}-{surname}"
            urls.append(f"https://www.usenix.org/system/files/{stem}.pdf")
            for suffix in (given, first_word):
                if suffix and suffix != surname:
                    urls.append(
                        f"https://www.usenix.org/system/files/{stem}-{suffix}.pdf")
    return urls


def local_pdf(doi: Optional[str], arxiv_id: Optional[str],
              s2_paper_id: Optional[str] = None) -> Optional[str]:
    """A PDF supplied by hand, for papers no free route reaches.

    Two thirds of this corpus is published by ACM, IEEE or Springer, all of
    which refuse automated fetching, so citation contexts are otherwise the
    only evidence for them. Dropping a PDF into the papers directory -- named
    for its DOI with slashes replaced, or its arXiv id -- lets the same
    extraction run over it, and every resulting claim still quotes the paper
    verbatim.
    """
    from .. import config

    directory = config.REPO_ROOT / ".cache" / "impact" / "papers-pdf"
    if not directory.is_dir():
        return None
    names = []
    if doi:
        names.append(doi.replace("/", "_"))
    if arxiv_id:
        names.append(arxiv_id)
    if s2_paper_id:
        names.append(f"s2_{s2_paper_id}")
    for name in names:
        for candidate in (directory / f"{name}.pdf", directory / f"{name}.PDF"):
            if candidate.is_file():
                return candidate.as_uri()
    return None


def wanted_filename(doi: Optional[str], arxiv_id: Optional[str],
                    s2_paper_id: Optional[str] = None) -> Optional[str]:
    """The name a hand-supplied PDF must have for the pipeline to find it.

    The Semantic Scholar id is the last resort, and it is needed: a dozen papers
    here have neither a DOI nor an arXiv id, and two of them are the ones whose
    citation sentences hint hardest at a relationship. Without a name they could
    never be supplied at all, which is a poor reason to leave a paper unread.
    """
    if doi:
        return f"{doi.replace('/', '_')}.pdf"
    if arxiv_id:
        return f"{arxiv_id}.pdf"
    if s2_paper_id:
        return f"s2_{s2_paper_id}.pdf"
    return None


def reachable_without_help(paper: dict) -> bool:
    """Whether the pipeline can already read this paper's full text.

    arXiv preprints, PVLDB and USENIX are all free; everything else in this
    corpus is behind a publisher that refuses automated requests. USENIX
    belongs here even though it assigns no DOI, which is what made its papers
    look unreachable and put them on the hand-download list.
    """
    if paper.get("arxiv_id"):
        return True
    doi = (paper.get("doi") or "").lower()
    if doi.startswith(PVLDB_DOI_PREFIX):
        return True
    return bool(usenix_pdf_urls(paper))


def pdf_text(fetcher: Fetcher, url: str) -> Optional[str]:
    """Extract the text of a PDF, or None if it cannot be read."""
    if url.startswith("file://"):
        try:
            from urllib.request import url2pathname
            from urllib.parse import urlparse
            raw = pathlib.Path(url2pathname(urlparse(url).path)).read_bytes()
        except Exception:
            return None
    else:
        try:
            raw, _ = fetcher.fetch_binary(url, max_age_days=365)
        except Exception:
            return None
    if not raw or not raw.lstrip().startswith(b"%PDF") or len(raw) > MAX_PDF_BYTES:
        return None
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(raw))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return None
    text = _strip_page_furniture(text)
    # PDF extraction hyphenates across line breaks and scatters newlines.
    text = re.sub(r"-\s*\n\s*", "", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return truncate(text.strip(), MAX_TEXT_CHARS) or None


# Stamps a publisher prints on every page. Extraction drops them into the
# middle of whatever sentence spans the page break, so a quotation taken from
# an IEEE or ACM PDF arrives with a licence notice inside it. They are removed
# before anything is quoted -- an excerpt has to be the author's sentence, and
# page furniture is not part of it.
PAGE_FURNITURE = re.compile(
    r"Authorized\s+licensed\s+use\s+limited\s+to.{0,200}?Restrictions\s+apply\s*\.?"
    r"|Authorized\s+licensed\s+use\s+limited\s+to[^.]{0,200}\."
    r"|Proc\.\s+ACM\s+\w+\.?\s*(?:Data|Manag)[^.]{0,80}?\."
    r"|Permission\s+to\s+make\s+digital\s+or\s+hard\s+copies.{0,600}?fee\s*\."
    r"|\d{4}\s+IEEE(?:\s+International)?[^.\n]{0,60}?(?:Conference|Symposium)[^.\n]{0,60}",
    re.IGNORECASE | re.DOTALL)


def _strip_page_furniture(text: str) -> str:
    return PAGE_FURNITURE.sub(" ", text or "")


def sentences(text: str) -> List[str]:
    return [s.strip() for s in SENTENCE_SPLIT.split(text or "") if s.strip()]


def find_signals(text: str, tax: Optional[taxonomy.Taxonomy] = None
                 ) -> Dict[str, List[str]]:
    """Sentences in ``text`` that state each relationship, verbatim.

    A sentence framed as related work is skipped: "previous work implemented
    this on top of SQLancer" says nothing about what these authors did.
    """
    tax = tax or taxonomy.load()
    found: Dict[str, List[str]] = {key: [] for key in SIGNALS}
    if not text:
        return found

    for sentence in sentences(text):
        if len(sentence) > 900:
            continue
        if RELATED_WORK.search(sentence):
            continue
        # Excluded techniques never establish a SQLancer relationship.
        if tax.find_excluded_techniques(sentence) and not tax.mentions_finder(sentence):
            continue
        baseline = bool(AS_A_BASELINE.search(sentence))
        for key, patterns in SIGNALS.items():
            if baseline and key in NOT_WHEN_BASELINE:
                continue
            if any(pattern.search(sentence) for pattern in patterns):
                clean = truncate(re.sub(r"\s+", " ", sentence), EXCERPT_LIMIT)
                if clean not in found[key]:
                    found[key].append(clean)
    return found


def techniques_in(sentence: str, tax: Optional[taxonomy.Taxonomy] = None
                  ) -> List[str]:
    tax = tax or taxonomy.load()
    return sorted({match.technique_id
                   for match in tax.find_techniques(sentence)})


def evidence_from(sentence: str, source_url: str, note: str,
                  timestamp: Optional[str] = None) -> dict:
    timestamp = timestamp or now()
    return {
        "source_url": source_url,
        "source_type": "paper",
        "excerpt": sentence,
        "excerpt_is_verbatim": True,
        "note": note,
        "content_sha256": content_hash(sentence),
        "retrieved_at": timestamp,
        "first_seen": timestamp,
        "last_verified": timestamp,
    }
