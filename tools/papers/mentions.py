"""Every place a paper refers to SQLancer, however it refers to it.

Three ways a paper can point at SQLancer, and a search for the name finds only
the first:

1. **By name** -- "SQLancer", "SQLancer++", "ShQveL".
2. **By technique** -- PQS, NoREC, TLP, QPG, CERT, DQP, CODDTest, or their
   expansions. Several are ambiguous as bare acronyms, which the taxonomy
   already knows about.
3. **By citation marker alone** -- "Existing works [9]-[11] also face the same
   problem", where reference 9 is a SQLancer paper. Measured on one IEEE paper,
   5 of the 14 sentences citing a SQLancer publication never name it. Nothing
   that matches on names can see them.

Each mention keeps enough context to be re-judged later without reopening the
PDF: the sentence, the paragraph around it, the section, the page, and the
offset. That is the point of the subproject -- the reading happens once.

Nothing here decides what the paper's relationship to SQLancer is. It finds the
places worth reading and hands them to the analysis.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence

from ..impact import taxonomy
from . import extract

INVENTORY_VERSION = "paper-mentions-v1"

# How much of the surrounding text to keep with each mention. Generous on
# purpose: a later question about the mention should be answerable from the
# stored context rather than from the PDF.
CONTEXT_BEFORE = 600
CONTEXT_AFTER = 600

# A citation marker, and the ranges publishers write them in. "[9]-[11]" and
# "[9]–[11]" both mean three references, and the middle one is invisible unless
# the range is expanded.
# Publishers space these differently -- "[11]", "[ 1,2]", and groups that wrap
# across a line ("[ 9,10,11,12,13,\n14,15,16]") -- so whitespace is allowed
# anywhere inside the bracket. Requiring a digit straight after "[" silently
# found nothing at all in papers that space them out.
CITATION_GROUP = re.compile(
    r"\[\s*(\d{1,3}(?:\s*[,–—-]\s*\d{1,3})*)\s*\]")
CITATION_RANGE = re.compile(r"(\d{1,3})\s*[–—-]\s*(\d{1,3})")

# Authors and titles that mark a bibliography entry as a SQLancer publication.
SQLANCER_REFERENCE = re.compile(
    r"sqlancer|pivoted\s+query\s+synthesis|non-?optimizing\s+reference\s+engine|"
    r"ternary\s+logic\s+partitioning|query\s+plan\s+guidance|"
    r"finding\s+bugs\s+in\s+database\s+systems\s+via\s+query\s+partitioning|"
    r"constant[- ]optimization[- ]driven|cardinality\s+estimation\s+restriction|"
    r"differential\s+query\s+plans",
    re.IGNORECASE)

RIGGER = re.compile(r"\bM(?:anuel)?\.?\s+Rigger\b|\bRigger\b", re.IGNORECASE)

# An author-year citation, which is what ACM's current format uses instead of
# numbers: "databases [Rigger and Su 2020; Wang et al. 2021b]". A paper in that
# style has no numbered markers at all, so the whole citation-marker route finds
# nothing in it and the tool's name may never appear in the prose either.
AUTHOR_YEAR = re.compile(
    r"\bRigger\b[^\]\).;]{0,40}?\b(19|20)\d{2}[a-z]?\b", re.IGNORECASE)


def expand_markers(group: str) -> List[int]:
    """The reference numbers a citation group names, ranges included."""
    numbers: List[int] = []
    for part in re.split(r"\s*,\s*", group):
        part = part.strip()
        if not part:
            continue
        span = CITATION_RANGE.fullmatch(part)
        if span:
            first, last = int(span.group(1)), int(span.group(2))
            if 0 < last - first < 40:
                numbers.extend(range(first, last + 1))
                continue
        if part.isdigit():
            numbers.append(int(part))
    return numbers


# A range written as two brackets rather than one: "[9]-[11]", which is how
# IEEE sets it. Everything between the endpoints is cited and none of it is
# visible unless the pair is read together.
BRACKET_RANGE = re.compile(
    r"\[\s*(\d{1,3})\s*\]\s*[–—-]\s*\[\s*(\d{1,3})\s*\]")


def _citation_spans(text: str) -> List[dict]:
    """Every citation in the text, with the reference numbers it names."""
    spans: List[dict] = []
    consumed: List[tuple] = []
    for match in BRACKET_RANGE.finditer(text):
        first, last = int(match.group(1)), int(match.group(2))
        if 0 < last - first < 40:
            spans.append({"start": match.start(), "text": match.group(0),
                          "numbers": list(range(first, last + 1))})
            consumed.append((match.start(), match.end()))
    for match in CITATION_GROUP.finditer(text):
        if any(start <= match.start() < end for start, end in consumed):
            continue
        spans.append({"start": match.start(), "text": match.group(0),
                      "numbers": expand_markers(match.group(1))})
    return spans


def sqlancer_references(references: Sequence[dict],
                        tax: Optional[taxonomy.Taxonomy] = None
                        ) -> Dict[int, dict]:
    """Which of this paper's bibliography entries are SQLancer publications.

    Matched on the tool and technique names, on the title of each origin paper
    the taxonomy knows, and on authorship -- a Rigger paper in a database
    testing bibliography is one of these often enough to be worth catching, and
    the entry is recorded with what matched so a wrong call is visible.
    """
    tax = tax or taxonomy.load()
    titles = []
    identifiers = []
    for seed in tax.citation_seeds():
        label = seed.get("seed_id") or seed.get("technique_id")
        for title in [seed.get("title")] + list(seed.get("also_titled") or []):
            if title:
                titles.append((label, seed.get("technique_id"), title.lower()))
        # A DOI or arXiv id is what a bibliography prints for a paper whose
        # title it has rewritten. CERT is cited as "CERT: Finding performance
        # issues ... through the lens of cardinality estimation", which shares
        # no opening words with the preprint title the taxonomy carries, and
        # was recognised only as a paper by one of the project's authors.
        for key in ("doi", "arxiv_id"):
            value = seed.get(key)
            if value:
                identifiers.append((label, seed.get("technique_id"), key,
                                    re.sub(r"\s+", "", str(value).lower())))

    found: Dict[int, dict] = {}
    for entry in references:
        text = str(entry.get("text") or "")
        lowered = text.lower()
        # Extraction glues words together in some bibliographies --
        # "ManuelRiggerandZhendongSu.2020. FindingBugsinDatabaseSystemsvia
        # QueryPartitioning" is one real entry -- so every match is also tried
        # with all whitespace removed from both sides. Without this the TLP
        # paper went unrecognised in its own citing paper's bibliography.
        squashed = re.sub(r"\s+", "", lowered)
        reasons: List[str] = []
        technique = None
        for seed_label, technique_id, title in titles:
            head = " ".join(title.split()[:6])
            if head and (head in lowered
                         or re.sub(r"\s+", "", head) in squashed):
                reasons.append(f"cites the paper introducing {seed_label}")
                technique = technique or technique_id
        for seed_label, technique_id, key, value in identifiers:
            if value in squashed:
                kind = "DOI" if key == "doi" else "arXiv id"
                reasons.append(f"prints the {kind} of the paper introducing "
                               f"{seed_label}")
                technique = technique or technique_id
        if SQLANCER_REFERENCE.search(text) or SQLANCER_REFERENCE.search(squashed):
            reasons.append("names SQLancer or one of its techniques")
        matched_as = "sqlancer_publication" if reasons else None
        if not reasons and (RIGGER.search(text) or "rigger" in squashed):
            # A paper by the project's authors, which is not the same thing as a
            # SQLancer paper: FlowFusion is a PHP fuzzer Rigger co-authored, and
            # calling its reference a SQLancer publication would be wrong. It is
            # kept because a paper citing it is worth reading, and labelled so
            # the analysis can weigh it accordingly.
            reasons.append("written by an author of the SQLancer project, but "
                           "not itself a SQLancer paper")
            matched_as = "project_authored"
        if reasons:
            found[int(entry["number"])] = {
                "number": int(entry["number"]),
                "text": text,
                "technique": technique,
                "matched_as": matched_as,
                "why": reasons,
            }
    return found


# A sentence ends at a terminator or a blank line, not at every line break:
# extraction puts a newline at the end of every typeset line, so stopping there
# cut "For example, SQLancer [11] relies on" off mid-clause.
SENTENCE_BREAK = re.compile(r"[.!?]|\n\s*\n")


def _sentence_bounds(text: str, index: int) -> tuple:
    start = index
    while start > 0:
        window = text[max(0, start - 2):start]
        if text[start - 1] in ".!?" or "\n\n" in window:
            break
        start -= 1
    end = index
    while end < len(text):
        if text[end] in ".!?" or text.startswith("\n\n", end):
            break
        end += 1
    return start, min(end + 1, len(text))


def spaced_pattern(name: str) -> str:
    """A pattern matching ``name`` however extraction spaced it out.

    Small caps come out of a PDF with spaces inside the word -- "SQL ANCER" for
    SQLANCER, "N OREC" for NOREC -- so a plain name search misses exactly the
    places a paper is most likely to name the tool, its own prose. The text is
    not rewritten to fix this: what is stored stays as the PDF renders it, and
    the pattern is what bends.
    """
    return r"\s?".join(re.escape(char) for char in name if not char.isspace())


def _mention(document: dict, index: int, surface: str, found_by: str,
             technique: Optional[str], number: int,
             reference: Optional[dict] = None) -> dict:
    text = document["text"]
    start, end = _sentence_bounds(text, index)
    sentence = re.sub(r"\s+", " ", text[start:end]).strip()
    before = re.sub(r"\s+", " ", text[max(0, start - CONTEXT_BEFORE):start]).strip()
    after = re.sub(r"\s+", " ", text[end:end + CONTEXT_AFTER]).strip()
    entry = {
        "id": f"M{number}",
        "found_by": found_by,
        "surface": surface,
        "technique": technique,
        "sentence": sentence,
        "context_before": before,
        "context_after": after,
        "section": extract.section_at(document.get("sections") or [], start),
        "page": extract.page_at(document.get("pages") or [], start),
        "char_offset": start,
    }
    if reference is not None:
        entry["cited_reference"] = {
            "number": reference["number"],
            "text": reference["text"][:300],
            "matched_as": reference.get("matched_as"),
            "why": reference["why"],
        }
    return entry


def build(document: dict, tax: Optional[taxonomy.Taxonomy] = None) -> dict:
    """The inventory: every mention, and the references behind the markers."""
    tax = tax or taxonomy.load()
    text = document.get("text") or ""
    body_end = document.get("references_start") or len(text)

    references = sqlancer_references(document.get("references") or [], tax)
    seen: Dict[int, dict] = {}
    counter = 0

    def add(index: int, surface: str, found_by: str,
            technique: Optional[str], reference: Optional[dict] = None) -> None:
        nonlocal counter
        start, _ = _sentence_bounds(text, index)
        if start in seen:
            # One sentence, one mention: record the strongest way it was found,
            # but keep every technique it names.
            existing = seen[start]
            if technique and technique not in (existing.get("techniques") or []):
                existing.setdefault("techniques", []).append(technique)
            if found_by not in existing["found_by_all"]:
                existing["found_by_all"].append(found_by)
            return
        counter += 1
        entry = _mention(document, index, surface, found_by, technique,
                         counter, reference)
        entry["found_by_all"] = [found_by]
        entry["techniques"] = [technique] if technique else []
        seen[start] = entry

    # 1. By name, tolerating the spacing small caps leave behind.
    names = "|".join(spaced_pattern(name)
                     for name in ("SQLancer++", "SQLancer", "ShQveL"))
    for match in re.finditer(rf"(?<![\w+])({names})(?![\w])",
                             text[:body_end], re.IGNORECASE):
        add(match.start(), match.group(0), "name", None)

    # 2. By technique name.
    for match in tax.find_techniques(text[:body_end], require_context=False):
        add(match.start, match.matched_name, "technique", match.technique_id)

    # 3. By author-year citation, for bibliographies with no numbers to resolve.
    for match in AUTHOR_YEAR.finditer(text[:body_end]):
        add(match.start(), match.group(0), "author_year_citation", None)

    # 4. By citation marker resolving to a SQLancer publication.
    if references:
        for match in _citation_spans(text[:body_end]):
            cited = [references[number] for number in match["numbers"]
                     if number in references]
            if not cited:
                continue
            # One group can cite both a SQLancer paper and a paper merely
            # written by one of its authors -- "[2-5,8,13,14,17]" cites both.
            # The stronger match decides how the sentence is labelled, so a
            # sentence citing PQS is not filed under project_authored because
            # a neighbouring number happened to resolve first.
            reference = next(
                (item for item in cited
                 if item.get("matched_as") == "sqlancer_publication"), cited[0])
            found_by = ("citation_marker"
                        if reference.get("matched_as") == "sqlancer_publication"
                        else "citation_marker_project_authored")
            add(match["start"], match["text"], found_by,
                reference.get("technique"), reference)

    mentions = sorted(seen.values(), key=lambda entry: entry["char_offset"])
    for position, entry in enumerate(mentions, start=1):
        entry["id"] = f"M{position}"
    return {
        "inventory_version": INVENTORY_VERSION,
        "mentions": mentions,
        "sqlancer_references": sorted(references.values(),
                                      key=lambda r: r["number"]),
    }
