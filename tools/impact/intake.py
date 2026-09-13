"""Files downloaded papers into the drop folder, under the names it expects.

A publisher hands you `3769828.pdf`, or a title with the spaces turned into
underscores, and the pipeline wants `10.1145_3769828.pdf`. Renaming by hand is
dull and easy to get wrong, and getting it wrong is quiet: the file simply is
not found, and the paper stays unread.

Order is not used to decide what a file is, even when the downloads were made
in order. Order is a claim about the session; the first page of the PDF is a
fact about the file. Each PDF is read and matched against the titles of the
papers still wanted, and it is filed only when one of them clearly wins.
"""

from __future__ import annotations

import difflib
import pathlib
import re
import shutil
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import worklist

# How much of a paper's title must appear on the PDF's first page before the
# file is filed as that paper, and how far ahead of the runner-up it must be.
MIN_SCORE = 0.75
MIN_MARGIN = 0.15

MANIFEST_SCHEMA_VERSION = "1.0.0"

# Enough of the front matter to carry the title, without the bibliography.
FIRST_PAGE_CHARS = 4000


def _digest(path: pathlib.Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


# Publishers name a download after the paper's identifier: ACM uses the DOI
# suffix ("3729175.pdf", "3597503.3639200.pdf"), arXiv the arXiv id.
FILENAME_IDENTIFIER = re.compile(r"^(\d{4}\.\d{4,5}|\d{6,8}(?:\.\d{6,8})?)$")


def doi_on_page(page_text: str, rows: Sequence[dict]) -> Optional[dict]:
    """The paper whose DOI is printed on this page.

    Exact rather than heuristic, and it works where titles fail: a journal
    masthead can push the title down the page past anything a layout rule will
    find, but the DOI is printed there too. Extraction scatters spaces through
    it -- "10.22399/ijcesen. 5462" -- so the comparison is made with all
    whitespace removed from both sides.
    """
    flat = re.sub(r"\s+", "", page_text or "").lower()
    if not flat:
        return None
    best = None
    for row in rows:
        doi = row.get("doi")
        if not doi:
            continue
        needle = re.sub(r"\s+", "", doi).lower()
        if len(needle) >= 12 and needle in flat:
            # The longest match wins: one DOI can be a prefix of another.
            if best is None or len(needle) > len(re.sub(r"\s+", "", best["doi"])):
                best = row
    return best


def identifier_from_filename(path: pathlib.Path,
                             rows: Sequence[dict]) -> Optional[dict]:
    """The paper a download's own filename names, if it names one.

    The last resort, and sometimes the only one. A magazine extract can carry
    neither a declared title nor its title on any page -- the AWS article
    begins mid-sentence -- and then the filename the publisher chose is all
    there is to go on. Matching is exact against the identifier already in the
    expected filename, so this cannot guess.
    """
    stem = path.stem.strip()
    if not FILENAME_IDENTIFIER.match(stem):
        return None
    for row in rows:
        name = row["filename"]
        if name.endswith(f"_{stem}.pdf") or name == f"{stem}.pdf":
            return row
    return None


def metadata_title(path: pathlib.Path) -> str:
    """The title the PDF declares about itself, if it declares one.

    Free, and right more often than anything recovered from the page: a
    conference template usually fills it in. It is only a candidate, scored
    like the rest, because plenty of PDFs carry a LaTeX job name here instead.
    """
    try:
        import pypdf

        title = (pypdf.PdfReader(str(path)).metadata or {}).get("/Title")
        title = str(title or "").strip()
        return title if len(title) > 8 and title.lower() != "none" else ""
    except Exception:
        return ""


def first_page(path: pathlib.Path) -> str:
    try:
        import pypdf

        reader = pypdf.PdfReader(str(path))
        if not reader.pages:
            return ""
        return (reader.pages[0].extract_text() or "")[:FIRST_PAGE_CHARS]
    except Exception:
        return ""


# Where a reference list can start. A DOI printed after this point belongs to
# a paper being cited, not to the paper in hand.
REFERENCES_HEADING = re.compile(
    r"^\s*(?:\d+\.?\s*)?(?:references|bibliography|referensi)\s*$",
    re.IGNORECASE | re.MULTILINE)


def front_pages(path: pathlib.Path, pages: int = 2) -> str:
    """Text from the opening pages, cut off before any reference list.

    A journal masthead does not always sit on page one: AJSE prints the DOI on
    page two, which put one paper out of reach of an exact DOI match. Reading
    a little further finds it. The cut at the reference list is what keeps that
    safe -- a cited paper's DOI must never be mistaken for this paper's own.
    """
    try:
        import pypdf

        reader = pypdf.PdfReader(str(path))
        text = "\n".join((page.extract_text() or "")
                          for page in reader.pages[:pages])
    except Exception:
        return ""
    heading = REFERENCES_HEADING.search(text)
    if heading:
        text = text[:heading.start()]
    return text[:FIRST_PAGE_CHARS * pages]


def _words(text: str) -> List[str]:
    return [w for w in re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split()
            if len(w) > 3]


# Lines a publisher puts above the title on the first page.
FRONT_MATTER = re.compile(
    r"^\s*(?:\d{4}\s|20\d\d\s+(?:ieee|acm)|permission to make|this work is|"
    r"acm isbn|doi:|https?://|arxiv:|proceedings of|\d+(?:st|nd|rd|th)\s|"
    r"ieee|acm\b|©|copyright|open access to the|international journal|"
    r"journal of|vol\.|issn|research article|received\s*:|revised\s*:)",
    re.IGNORECASE)

# USENIX prints a cover sheet whose last line runs straight into the title:
# "sponsored by USENIX.Detecting Logical Bugs of DBMS with". The title starts
# after the boilerplate, on the same line.
USENIX_COVER = re.compile(
    r"(?:is\s+)?sponsored\s+by\s*(?:USENIX)?\s*\.?\s*", re.IGNORECASE)

# Where the title stops and the author block starts.
AUTHOR_LINE = re.compile(
    r"@|\b(?:University|Universität|Institute|Laborator|College|School\s+of|"
    r"Academy|Inc\.|Ltd|GmbH|Corporation|Group|Research|Tech\.|"
    r"Abstract|ABSTRACT|A BSTRACT)\b|^\s*\d+\s*$")


def title_candidates(page_text: str) -> List[str]:
    """Plausible renderings of the paper's title from its first page.

    Several, not one, because the first page is laid out differently by every
    publisher and a single rule gets some of them wrong. ACM wraps a long title
    over two lines, the second of which can be a single short word -- "DBMS" --
    that a minimum-length filter throws away, taking the title with it. USENIX
    prints a cover sheet and runs the title onto the end of its last line.

    The caller scores every candidate and keeps the best, so a rule that
    misfires costs nothing as long as another one works.
    """
    lines = [line.strip() for line in (page_text or "").splitlines()]
    lines = [line for line in lines if line]

    # A cover sheet is looked for across the whole top of the page, because its
    # own lines do not all look like front matter and the title is glued to the
    # end of one of them.
    start = 0
    for index, line in enumerate(lines[:10]):
        match = USENIX_COVER.search(line)
        if match and match.end() < len(line):
            lines = [line[match.end():].strip()] + lines[index + 1:]
            break
    else:
        # No cover sheet. Front matter sits above the title, so the first line
        # that is not front matter ends it -- scanning further would let an
        # author line ("1st Panta Kittisatra, ...") move the start past the
        # title itself.
        for index, line in enumerate(lines[:10]):
            if FRONT_MATTER.match(line):
                start = index + 1
                continue
            break

    collected: List[str] = []
    for line in lines[start:start + 6]:
        if AUTHOR_LINE.search(line):
            break
        collected.append(line)
        if sum(len(part) for part in collected) > 130:
            break

    candidates = []
    if collected:
        candidates.append(" ".join(collected))
        candidates.append(collected[0])
        if len(collected) > 1:
            candidates.append(" ".join(collected[:2]))
    return [c for c in dict.fromkeys(candidates) if len(c) > 8]


def pdf_title(page_text: str) -> str:
    """The single best guess at the title, for reporting."""
    candidates = title_candidates(page_text)
    return candidates[0] if candidates else ""


def score(title: str, page_text: str,
          extra_titles: Sequence[str] = ()) -> float:
    """How strongly this page is that paper, from 0 to 1.

    Two views, and the weaker one governs. Containment asks how much of the
    candidate title appears anywhere on the page, which is robust to a PDF
    whose title extraction came out badly. Similarity compares the candidate
    against the page's own title, which is what stops a generic title matching
    everything. Taking the smaller of the two means a file must look right by
    both readings.
    """
    words = _words(title)
    if not words:
        return 0.0
    flat = " " + re.sub(r"\s+", " ",
                        re.sub(r"[^a-z0-9 ]+", " ", (page_text or "").lower())) + " "
    containment = sum(1 for word in words if word in flat) / len(words)

    candidates = title_candidates(page_text) + [t for t in extra_titles if t]
    if not candidates:
        return 0.0
    # A declared title needs no containment check: the page it belongs to may
    # not even carry the title, as with a magazine extract.
    if any(difflib.SequenceMatcher(None, " ".join(_words(title)),
                                   " ".join(_words(extra))).ratio() >= 0.90
           for extra in extra_titles if extra):
        return 1.0
    target = " ".join(_words(title))
    similarity = max(
        difflib.SequenceMatcher(None, target, " ".join(_words(c))).ratio()
        for c in candidates)
    return min(containment, similarity)


def identify(page_text: str, rows: List[dict], *,
             wanted: Optional[set] = None, extra_titles: Sequence[str] = ()
             ) -> Tuple[Optional[dict], float, float]:
    """The paper this page is, its score, and the runner-up's.

    ``rows`` must be every paper worth considering, not only the ones still
    wanted. A PDF whose own paper has already been filed would otherwise be
    matched against the remaining candidates and filed as the closest of them,
    which is how a correct download becomes a wrong quotation. The caller checks
    afterwards whether the winner is still wanted.

    The runner-up matters too: two papers by the same group on the same
    technique share most of their title words.
    """
    if not page_text.strip() and not any(extra_titles):
        return None, 0.0, 0.0
    ranked = sorted(((score(row["title"], page_text, extra_titles), row)
                     for row in rows),
                    key=lambda pair: pair[0], reverse=True)
    if not ranked:
        return None, 0.0, 0.0
    best_score, best = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
    if best_score < MIN_SCORE:
        return None, best_score, runner_up

    if best_score - runner_up < MIN_MARGIN:
        # A near-tie is usually a paper and its own preprint, which share a
        # title. If exactly one of the tied candidates is still wanted, that is
        # the one the file is for: the others are already filed, or the
        # pipeline can read them without help. Only a tie between two papers we
        # genuinely need is left for a person.
        if wanted is None:
            return None, best_score, runner_up
        tied = [row for value, row in ranked
                if best_score - value < MIN_MARGIN]
        needed = [row for row in tied if row["filename"] in wanted]
        if not needed:
            # None of the tied candidates needs a file. Usually this is a paper
            # already filed under one of its two identities, so returning the
            # best match lets the caller report it as filed rather than as a
            # decision waiting on a person.
            return best, best_score, runner_up
        if len(needed) != 1:
            return None, best_score, runner_up
        return needed[0], best_score, runner_up

    return best, best_score, runner_up


def is_ambiguous(best: float, runner_up: float) -> bool:
    """Whether a file was refused for being between two papers, not unknown.

    The usual cause is a paper and its own preprint, whose titles differ by a
    word. Worth saying out loud: the file is almost certainly wanted, and only a
    person can say which of the two it is.
    """
    return best >= MIN_SCORE and best - runner_up < MIN_MARGIN


def run(source: pathlib.Path, *, dry_run: bool = False,
        limit: Optional[int] = None,
        destination: Optional[pathlib.Path] = None,
        candidates: Optional[List[dict]] = None,
        wanted: Optional[Set[str]] = None) -> Dict[str, int]:
    """File every PDF in ``source`` that is a paper the worklist still wants.

    The dataset supplies the candidates and the wanted set by default; passing
    them lets a test exercise the matching without a real papers file.
    """
    from . import config
    from .collectors import fulltext

    destination = destination or worklist.OUTPUT.parent
    destination.mkdir(parents=True, exist_ok=True)

    if candidates is None:
        # Every paper is a candidate, so a PDF is recognised as itself even
        # when that paper is already filed; only the wanted set is written to.
        candidates = []
        for paper in config.load_json(config.DATA_FILES["papers"])["papers"]:
            if paper.get("is_sqlancer_publication"):
                continue
            filename = fulltext.wanted_filename(paper.get("doi"),
                                                paper.get("arxiv_id"),
                                                paper.get("s2_paper_id"))
            if filename:
                candidates.append({"title": paper["title"],
                                   "filename": filename,
                                   "doi": paper.get("doi")})
    if wanted is None:
        wanted = {row["filename"] for row in worklist.wanted()}

    pdfs = sorted((p for p in source.glob("*.pdf")),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if limit:
        pdfs = pdfs[:limit]

    counts = {"filed": 0, "already_there": 0, "not_wanted": 0, "ambiguous": 0,
              "unmatched": 0}
    filed: List[dict] = []
    unresolved: List[dict] = []
    for pdf in pdfs:
        page_text = first_page(pdf)
        declared = metadata_title(pdf)
        match, best, runner_up = identify(page_text, candidates, wanted=wanted,
                                          extra_titles=(declared,))
        if match is None and not is_ambiguous(best, runner_up):
            named = (doi_on_page(page_text, candidates)
                     or doi_on_page(front_pages(pdf), candidates)
                     or identifier_from_filename(pdf, candidates))
            if named is not None:
                match, best, runner_up = named, 1.0, 0.0
        if match is None:
            if is_ambiguous(best, runner_up):
                counts["ambiguous"] += 1
                near = sorted(((score(row["title"], page_text, (declared,)), row)
                               for row in candidates), reverse=True,
                              key=lambda pair: pair[0])[:2]
                print(f"  {pdf.name[:46]:46s} ?? between "
                      + " and ".join(f"{row['title'][:34]!r}" for _, row in near))
                unresolved.append({
                    "source": str(pdf),
                    "reason": "two papers match almost equally",
                    "candidates": [{"title": row["title"],
                                    "filename": row["filename"],
                                    "match_score": round(value, 3)}
                                   for value, row in near],
                })
            else:
                counts["unmatched"] += 1
                # Naming it matters: a PDF that is silently dropped looks to
                # the person who downloaded it exactly like one that was
                # filed, and they download it again.
                closest = max(((score(row["title"], page_text, (declared,)), row)
                               for row in candidates), default=(0.0, None),
                              key=lambda pair: pair[0])
                title = closest[1]["title"][:40] if closest[1] else "nothing"
                print(f"  {pdf.name[:46]:46s} -- no match "
                      f"(closest {title!r} at {closest[0]:.0%})")
                unresolved.append({
                    "source": str(pdf),
                    "reason": "no paper in the dataset matches this PDF",
                    "candidates": ([{"title": closest[1]["title"],
                                     "filename": closest[1]["filename"],
                                     "match_score": round(closest[0], 3)}]
                                   if closest[1] else []),
                })
            continue
        target = destination / match["filename"]
        if target.exists():
            counts["already_there"] += 1
            print(f"  already filed  {match['filename']}")
            continue
        if match["filename"] not in wanted:
            # Recognised, but the pipeline can already read this paper from
            # arXiv or PVLDB, so a copy here would add nothing.
            counts["not_wanted"] += 1
            print(f"  {pdf.name[:46]:46s} -- {match['title'][:40]} "
                  f"is already reachable without a PDF")
            continue
        if not dry_run:
            shutil.copy2(pdf, target)
        counts["filed"] += 1
        filed.append({
            "source": str(pdf),
            "filed_as": match["filename"],
            "title": match["title"],
            "match_score": round(best, 3),
            "runner_up_score": round(runner_up, 3),
            "bytes": pdf.stat().st_size,
            # So a manifest can be checked against the folder later: these
            # files are not in git, and this is the only record that a given
            # PDF is the paper it was filed as.
            "sha256": _digest(pdf),
        })
        print(f"  {pdf.name[:46]:46s} -> {match['filename']}  "
              f"({best:.0%}, next {runner_up:.0%})")
        # The paper stays a candidate. One file per paper is already enforced
        # by the destination check above, and leaving the row in place means a
        # second copy of the same download is reported as already filed rather
        # than as an unrecognised PDF -- which would read as a reason to
        # download it again.
    counts["_filed"] = filed
    counts["_unresolved"] = unresolved
    return counts


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="File downloaded paper PDFs into the drop folder.")
    parser.add_argument("--source", type=pathlib.Path,
                        default=pathlib.Path.home() / "Downloads",
                        help="where the downloads are (default: ~/Downloads)")
    parser.add_argument("--limit", type=int, default=40,
                        help="only look at this many of the newest PDFs")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be filed, copy nothing")
    parser.add_argument("--json", type=pathlib.Path,
                        help="write a manifest of what was filed to this path")
    args = parser.parse_args(argv)

    if not args.source.is_dir():
        print(f"no such directory: {args.source}")
        return 1
    counts = run(args.source, dry_run=args.dry_run, limit=args.limit)
    print(f"\n{counts['filed']} filed, {counts['already_there']} already there, "
          f"{counts['not_wanted']} reachable without a PDF, "
          f"{counts['ambiguous']} too close to call, "
          f"{counts['unmatched']} not a paper in the dataset"
          + (" (dry run, nothing copied)" if args.dry_run else ""))

    if args.json:
        import json as json_module
        from .util import content_hash, now

        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "filed_at": now(),
            "source_directory": str(args.source),
            "destination_directory": str(worklist.OUTPUT.parent),
            "dry_run": bool(args.dry_run),
            "counts": {key: value for key, value in counts.items()
                       if not key.startswith("_")},
            "filed": counts["_filed"],
            "unresolved": counts["_unresolved"],
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json_module.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        print(f"manifest written to {args.json}")
    if counts["filed"]:
        print("Now run: python3 -m tools.impact.run collect --only papers --full")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
