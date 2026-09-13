"""Turns a paper into a structured document that can be judged without reopening it.

This is the expensive step -- parsing a PDF, finding where its sections begin,
splitting its bibliography -- and the whole point of the subproject is that it
happens once per paper and everything it learns is kept.

Two families of PDF cover almost this entire corpus, and they need different
handling:

* ACM papers from ``acmart`` carry a real outline, numbered headings
  ("3.1 Overview") and a bibliography whose entries start ``[n]``.
* IEEE papers carry no outline at all. Their headings are roman-numbered and
  set in small caps, which text extraction renders with the first letter
  detached -- "III. A PPROACH" -- so a heading has to be repaired before it can
  be recognised.

Nothing here decides anything about SQLancer. It produces the document; the
mention inventory and the analysis are built on top of it.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

EXTRACTOR_VERSION = "paper-extract-v1"

MAX_PDF_BYTES = 40_000_000

# A heading, in the two conventions this corpus uses. Both are anchored to a
# whole line: a numbered clause in running text should not be mistaken for one.
NUMBERED_HEADING = re.compile(
    r"^\s*(\d{1,2}(?:\.\d{1,2}){0,2})\.?\s+([A-Z][^\n]{2,70})\s*$")
ROMAN_HEADING = re.compile(
    r"^\s*((?:[IVXL]{1,6}|[A-Z])\.)\s+([A-Z][^\n]{2,70})\s*$")

# Small caps come out of extraction with the first letter split off the rest:
# "I NTRODUCTION", "A PPROACH", "R EFERENCES". Rejoining them is what makes an
# IEEE paper's structure legible at all.
SMALL_CAPS = re.compile(r"\b([A-Z])\s([A-Z]{2,})\b")

# Where the bibliography starts, in either convention.
# A running page number is often glued to the heading with no space between
# them -- "894REFERENCES" -- which left one paper's whole bibliography
# unparsed and so gave it no citation-marker mentions at all.
REFERENCES_HEADING = re.compile(
    r"^\s*\d{0,4}\s*(?:[IVXL]+\.\s*)?"
    r"(?:R\s?EFERENCES?|References?|REFERENCES?|BIBLIOGRAPHY|"
    r"Bibliography)\s*$", re.MULTILINE)

# Where entry one of a bibliography starts, for papers whose heading cannot be
# matched at all -- one runs the heading into the last line of the conclusion
# ("...environments. REFERENCE"), and another has no heading in the text layer.
# A bibliography entry is recognised by what follows the marker rather than by
# a heading: an author initial, a quoted title, a volume or a page range.
BIBLIOGRAPHY_START = re.compile(
    # At the start of a line: a bibliography lays its entries out that way,
    # while a citation in prose sits inside a sentence.
    r"(?:^|\n)[ \t]*\[1\]\s{0,3}(?=[^\n]{0,200}?"
    # Followed by something only a reference carries: an author initial, a
    # publication year, a volume or page range, or a quoted title. The year is
    # what reaches a bibliography spelling first names in full -- "Djallel
    # Bouneffouf. 2016." has no initial to match on.
    r"(?:[A-Z]\.\s|\b(?:19|20)\d{2}\b|\bvol\.|\bpp\.|[\u201c\u201d\"]))")

# A bibliography entry marker. Numeric styles only: an author-year bibliography
# has no marker to key citations against, and is reported as unresolvable
# rather than guessed at.
REFERENCE_MARKER = re.compile(r"\[(\d{1,3})\]")

# Springer numbers its bibliography "1." rather than "[1]", so the bracketed
# marker finds nothing at all in an LNCS paper -- which is how Artemis,
# Differential Monitoring and the Solidity compiler paper each arrived with
# zero references and, in consequence, no citation-marker mentions. Anchored to
# the line start and requiring a following space or URL, because "vol. 5," and
# "pp. 12." appear inside entries constantly.
DOTTED_MARKER = re.compile(r"(?:^|\n)\s*(\d{1,3})\.(?=\s|https?://)")

# Sections whose content is not the paper's own argument.
BACK_MATTER = re.compile(
    r"^(references|bibliography|acknowledg|appendix|artifact appendix)",
    re.IGNORECASE)


def repair_small_caps(text: str) -> str:
    """Rejoin a small-caps word whose first letter extraction split off."""
    previous = None
    while previous != text:
        previous = text
        text = SMALL_CAPS.sub(r"\1\2", text)
    return text


# A word broken across a line, whose hyphen the typesetter inserted: the second
# half starts lowercase, so the hyphen is not the author's and goes away with
# the break -- "re - vealed" is "revealed".
BROKEN_WORD = re.compile(r"([A-Za-z])\s*-\s+([a-z])")

# A hyphen the author wrote, with extraction's spaces around it: the second half
# starts with a capital or a digit, so the hyphen stays -- "Time -Series" is
# "Time-Series", "STATE -AWARE" is "STATE-AWARE".
SPACED_HYPHEN = re.compile(r"([A-Za-z])\s+-\s*([A-Z0-9])")

# Some PDFs set a passage with letter-spacing, and extraction puts a space
# between every glyph: "w i t h i n S Q L a n c e r". Unlike the joins below
# this cannot be repaired -- the gaps between words are the same width as the
# gaps inside them, in the file as well as on the page, so the word breaks are
# not there to recover. Rejoining would give "systemwithinSQLancer"; guessing
# the breaks from a dictionary would be writing text rather than quoting it.
# So it is detected and refused instead: a quotation nobody can read is worse
# than no quotation.
LETTER_SPACED = re.compile(r"(?:\b[A-Za-z] ){6,}[A-Za-z]\b")


def is_letter_spaced(text: str) -> bool:
    """Whether a passage lost its word breaks to letter-spacing."""
    return bool(LETTER_SPACED.search(text or ""))


# A word running straight into an all-caps one, where extraction lost the space
# between them: "all columns areNOT NULL", "PQS andTLP". The capital run is what
# makes splitting safe -- a product name carries its capital at the front
# ("PostgreSQL", "DuckDB", "CockroachDB"), so a token that *starts* lower-case
# and then turns to capitals is a join that was never written.
GLUED_KEYWORD = re.compile(r"\b([a-z]{2,})([A-Z]{2,})")

# Small caps set a word as one capital followed by the rest in smaller
# capitals, and extraction puts a space between the two runs: "SQL ANCER",
# "N OREC", "C ONI". Both runs must be capitals and the join must not be two
# ordinary words ("SQL AND" is two words, and stays two).
SPLIT_SMALL_CAPS = re.compile(r"\b([A-Z]{1,4})\s([A-Z]{2,})\b")

# Words that follow a capitalised run and are words in their own right, so a
# join would be wrong.
NOT_A_SPLIT = {
    "AND", "OR", "NOT", "THE", "FOR", "WITH", "FROM", "INTO", "USING", "WHERE",
    "SELECT", "INSERT", "UPDATE", "DELETE", "TABLE", "INDEX", "NULL", "TRUE",
    "FALSE", "JOIN", "GROUP", "ORDER", "BY", "ON", "IN", "IS", "AS", "ALL",
    "API", "SQL", "DBMS", "PDF", "URL", "CPU", "GPU", "IEEE", "ACM", "USENIX",
    "ABSTRACT", "INTRODUCTION", "CONCLUSION", "REFERENCES", "RELATED", "WORK",
    "EVALUATION", "BACKGROUND", "APPROACH", "IMPLEMENTATION", "DISCUSSION",
}


def _join_small_caps(match: "re.Match") -> str:
    head, tail = match.group(1), match.group(2)
    if tail in NOT_A_SPLIT or head in ("A", "I"):
        return match.group(0)
    return head + tail


def tidy(text: str) -> str:
    """Repair the artefacts a PDF's text layer leaves behind.

    Extraction is not transcription: a PDF stores glyphs and positions, and
    turning those back into words introduces damage that is systematic and
    therefore repairable. A small-caps heading arrives as "R EFERENCES", a tool
    name as "SQL ANCER", a hyphenated word as "Time -Series", and a word broken
    across a line as "re - vealed".

    Repairing them here means every consumer sees the same clean text, and a
    quotation reads as the author wrote it rather than as the typesetter left
    it. What is never done is changing words: this only removes spacing the
    author did not put there.
    """
    # A PDF's text layer can decode to unpaired surrogates and other characters
    # that cannot be encoded back to UTF-8, which breaks hashing and JSON. They
    # carry no meaning, so they go.
    text = (text or "").encode("utf-8", "replace").decode("utf-8", "replace")
    text = text.replace("\ufffd", "")
    # Control characters survive extraction from some PDFs -- a form feed at a
    # page break, a vertical tab in a table. They carry no meaning, they are
    # invisible in a diff, and they make the JSON unreadable to anything
    # stricter than Python: Jekyll refuses a data file containing them.
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    # Noncharacters and C1 controls come out of a broken text layer -- U+FFFF
    # in place of a ligature, "o\ufffdcial" for "official". Python and Ruby's
    # JSON accept them; YAML does not, and Jekyll reads data files as YAML, so
    # one of these makes the whole site fail to build.
    text = re.sub(r"[\u0080-\u009f\ufdd0-\ufdef\ufffe\uffff]", "", text)
    text = re.sub(r"-\s*\n\s*", "", text)          # hyphenated line break
    text = BROKEN_WORD.sub(r"\1\2", text)
    text = SPACED_HYPHEN.sub(r"\1-\2", text)
    text = GLUED_KEYWORD.sub(r"\1 \2", text)
    text = SPLIT_SMALL_CAPS.sub(_join_small_caps, text)
    text = repair_small_caps(text)
    # A name ending in "+" runs into the next word: "SQLANCER+by adapting".
    text = re.sub(r"(\w\+)([a-z])", r"\1 \2", text)
    # Extraction leaves a space before punctuation: "RANDOOP , and".
    text = re.sub(r"\s+([,;:.!?])(\s|$)", r"\1\2", text)
    # Collapse runs of spaces and tabs, but never newlines: paragraph breaks
    # are what sentence splitting relies on.
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"[ \t]+\n", "\n", text)


def page_texts(raw: bytes) -> List[str]:
    """The text of each page, in order. Empty list if the PDF cannot be read."""
    if not raw or not raw.lstrip().startswith(b"%PDF") or len(raw) > MAX_PDF_BYTES:
        return []
    try:
        import io

        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(raw))
        return [(page.extract_text() or "") for page in reader.pages]
    except Exception:
        return []


def outline_titles(raw: bytes) -> List[str]:
    """Section titles from the PDF's own outline, if it has one."""
    try:
        import io

        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(raw))
        found: List[str] = []

        def walk(entries):
            for entry in entries:
                if isinstance(entry, list):
                    walk(entry)
                    continue
                title = getattr(entry, "title", None)
                if title:
                    found.append(str(title).strip())

        walk(reader.outline or [])
        return found
    except Exception:
        return []


def _clean_page(text: str) -> str:
    """One page of extracted text, cleaned of furniture and artefacts."""
    from ..impact.collectors.fulltext import _strip_page_furniture

    return tidy(_strip_page_furniture(text or ""))


def assemble(raw: bytes) -> Dict[str, object]:
    """The whole document: text, page offsets, sections, references.

    Page offsets are kept because a mention's page number is the cheapest way
    for a person to find it again in the PDF, and it cannot be recovered later
    from the concatenated text.
    """
    pages = [_clean_page(page) for page in page_texts(raw)]
    if not pages:
        return {"text": "", "pages": [], "sections": [], "references": [],
                "page_count": 0, "has_outline": False}

    text_parts: List[str] = []
    page_spans: List[Dict[str, int]] = []
    cursor = 0
    for index, page in enumerate(pages, start=1):
        page_spans.append({"page": index, "start": cursor,
                           "end": cursor + len(page)})
        text_parts.append(page)
        cursor += len(page) + 1
    text = "\n".join(text_parts)

    outline = outline_titles(raw)
    sections = find_sections(text, outline)
    references_start, references = parse_references(text)
    return {
        "text": text,
        "pages": page_spans,
        "page_count": len(pages),
        "sections": sections,
        "references": references,
        "references_start": references_start,
        "has_outline": bool(outline),
    }


def find_sections(text: str, outline: Optional[List[str]] = None
                  ) -> List[Dict[str, object]]:
    """Where each section starts, by offset.

    The PDF outline gives the titles but not reliable offsets, so it is used to
    confirm a heading rather than to locate one: a line that matches a heading
    pattern *and* appears in the outline is certainly a heading, and one that
    matches only the pattern is accepted anyway when there is no outline.
    """
    known = {re.sub(r"\s+", " ", title).strip().lower()
             for title in (outline or [])}

    # Section numbers ascend. That is what separates a heading from the many
    # other numbered lines in a paper -- a pseudocode step ("5 Avail
    # s<-selectRandomAvailableFunc"), a figure caption, a table row. Matching
    # the pattern alone let all of those through.
    candidates: List[Dict[str, object]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        for style, pattern in (("roman", ROMAN_HEADING),
                               ("numbered", NUMBERED_HEADING)):
            match = pattern.match(stripped)
            if not match:
                continue
            number, title = match.group(1), match.group(2).strip()
            if CODE_LINE.search(stripped):
                break      # an algorithm step, not a heading
            full = re.sub(r"\s+", " ", f"{number} {title}").strip().lower()
            bare = title.lower()
            in_outline = bool(known) and (
                full in known or bare in known
                or any(entry.endswith(bare) for entry in known))
            candidates.append({"style": style, "number": number.rstrip("."),
                               "title": title, "start": offset,
                               "in_outline": in_outline})
            break
        offset += len(line)

    found = _ascending_run(candidates, bool(known))

    for index, section in enumerate(found):
        section["end"] = (found[index + 1]["start"] if index + 1 < len(found)
                          else len(text))
        section["is_back_matter"] = bool(BACK_MATTER.match(str(section["title"])))
    return found


# Characters that mark a line as code or mathematics rather than a heading.
CODE_LINE = re.compile(r"[←→⇐⇒∈∀∃∧∨≤≥≠∪∩⊆{}();]|:=|<-|\bfor\b|\bwhile\b")


def _top_level(number: str) -> Optional[int]:
    """The section's top-level number, as an integer, if it has one."""
    head = number.split(".")[0]
    if head.isdigit():
        return int(head)
    romans = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7,
              "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13}
    return romans.get(head.upper())


def _ascending_run(candidates: List[Dict[str, object]], has_outline: bool
                   ) -> List[Dict[str, object]]:
    """The longest run of candidates whose top-level numbers ascend from one.

    A paper's sections are numbered 1, 2, 3 in order. Anything else that
    matched -- a pseudocode step, a caption -- does not fit that sequence, and
    dropping whatever breaks it is what tells them apart. Subsections are kept
    with whichever top-level section they follow.
    """
    kept: List[Dict[str, object]] = []
    expected = 1
    style: Optional[str] = None
    for entry in candidates:
        top = _top_level(str(entry["number"]))
        is_sub = "." in str(entry["number"]) or entry["style"] == "roman" and \
            not str(entry["number"]).upper().strip(".").strip("IVXL") == ""
        if "." in str(entry["number"]) or (
                entry["style"] == "roman" and _top_level(str(entry["number"])) is None):
            # A subsection: keep it if we are inside a section already.
            if kept:
                kept.append(entry)
            continue
        if has_outline and not entry["in_outline"]:
            continue
        if top == expected and style in (None, entry["style"]):
            # A paper numbers its sections one way throughout. Without this, an
            # arabic "2" from a figure caption was accepted straight after the
            # roman "I. Introduction" it followed.
            style = entry["style"]
            kept.append(entry)
            expected += 1
    return [{k: v for k, v in entry.items() if k not in ("style", "in_outline")}
            for entry in kept]


def _split_entries(tail: str, marker: "re.Pattern") -> List[Dict[str, object]]:
    """Bibliography entries, split on whichever numbering style is in use."""
    parts = marker.split(tail)
    entries: List[Dict[str, object]] = []
    for index in range(1, len(parts) - 1, 2):
        try:
            number = int(parts[index])
        except ValueError:
            continue
        body = re.sub(r"\s+", " ", parts[index + 1]).strip()
        if body:
            entries.append({"number": number, "text": body})
    return entries


def parse_references(text: str) -> Tuple[Optional[int], List[Dict[str, object]]]:
    """The bibliography, split into numbered entries.

    Returns the offset the bibliography starts at, so that everything before it
    can be treated as the paper's own prose. A citation marker inside the
    bibliography is another paper's reference number, not this one's.
    """
    match = None
    for candidate in REFERENCES_HEADING.finditer(text):
        match = candidate  # the last one: a paper may cite the word earlier
    if match is None:
        # No heading anywhere. Fall back to the shape of the entries: the last
        # "[1]" that reads like a bibliography entry begins the bibliography.
        fallback = None
        for candidate in BIBLIOGRAPHY_START.finditer(text):
            fallback = candidate
        if fallback is None:
            return None, []
        start = fallback.start()
        return start, _renumber(_split_entries(text[start:], REFERENCE_MARKER))

    tail = text[match.end():]
    entries = _split_entries(tail, REFERENCE_MARKER)
    if len(entries) < 2:
        # Few or no bracketed entries: try the dotted style, and keep whichever
        # reading finds more. Taking the fallback unconditionally lost a
        # bracketed bibliography that had parsed, just not well.
        dotted = _split_entries(tail, DOTTED_MARKER)
        if len(dotted) > len(entries):
            entries = dotted
    return match.start(), _renumber(entries)


def _renumber(entries: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """Keep only entries whose numbering runs consecutively from one.

    A bibliography numbers its entries consecutively, so the next entry is
    always the next number. Anything else is a marker inside the entry being
    read -- an entry whose own title cites "[3]" does not begin entry three.
    """
    cleaned: List[Dict[str, object]] = []
    expected = 1
    for entry in entries:
        if entry["number"] == expected:
            cleaned.append(entry)
            expected += 1
        elif cleaned:
            cleaned[-1]["text"] += f" [{entry['number']}] {entry['text']}"
    return cleaned


def section_at(sections: List[Dict[str, object]], offset: int) -> Optional[str]:
    """The title of the section containing ``offset``."""
    for section in sections:
        if section["start"] <= offset < section["end"]:
            number = section.get("number")
            return f"{number} {section['title']}" if number else str(section["title"])
    return None


def page_at(pages: List[Dict[str, int]], offset: int) -> Optional[int]:
    for span in pages:
        if span["start"] <= offset <= span["end"]:
            return span["page"]
    return None
