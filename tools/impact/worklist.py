"""Generates the list of papers whose full text has to be fetched by hand.

Two thirds of this corpus is published by ACM, IEEE or Springer, none of which
serve PDFs to an automated client, so for those papers the only evidence the
pipeline can gather on its own is a citation index's two-sentence contexts.

This writes a worklist: what to download, where it lives, and the exact filename
to save it as so the pipeline picks it up. Papers whose citation contexts already
hint at a relationship come first, since those are the ones where reading the
paper is most likely to change what the dataset says. Papers already supplied,
or reachable through arXiv or PVLDB, are left out.
"""

from __future__ import annotations

import re
from typing import List, Optional

from . import config
from .collectors import fulltext

OUTPUT = config.CACHE_DIR / "papers-pdf" / "WANTED.md"

# A relationship the collectors could not settle, on a paper worth reading.
UNDECIDED = ("uncertain", "insufficient_evidence")

JUDGEMENTS = ("uses_infrastructure", "extends_technique", "compares_with")


# Wording in a citation sentence that suggests a relationship the contexts were
# too short to settle. A paper whose citing sentences say "implemented on top
# of" is one where the full text is likely to change what the dataset records;
# a paper that only ever appears in a list of references is not.
RELATIONSHIP_HINT = re.compile(
    r"\b(?:implement\w*|built?\s+on|based\s+on|top\s+of|extend\w*|adapt\w*|"
    r"baselines?|compare[ds]?|evaluat\w*|derive[ds]?|integrat\w*|"
    r"state[- ]of[- ]the[- ]art|outperform\w*)\b", re.IGNORECASE)


def _hint_count(paper: dict) -> int:
    """How many of this paper's citation sentences hint at a relationship."""
    hits = 0
    for entry in paper.get("relationships", {}).values():
        for item in (entry.get("evidence") or []):
            excerpt = item.get("excerpt")
            if excerpt and RELATIONSHIP_HINT.search(excerpt):
                hits += 1
    return hits


# A proxy that carries an institutional subscription. IEEE will not serve a
# paper otherwise, so its links go through this; the others resolve without it,
# and prefixing them anyway would only add a login step.
PROXY = "https://libproxy1.nus.edu.sg/login?url="


def download_url(doi: Optional[str], url: Optional[str], publisher: str) -> str:
    """Where to get the PDF, as directly as each publisher allows.

    ACM and Springer both expose a PDF at a path derived from the DOI, so those
    links land on the file itself. IEEE's PDF path needs an article number that
    the DOI does not contain, so its link resolves the DOI to the article page
    and the download is one click from there.

    Every link goes through the library proxy. A paywalled paper needs it, and
    an open-access one is unharmed by it -- the proxy passes the request on
    either way, so there is no reason to make the reader judge which is which.
    """
    if not doi:
        return f"{PROXY}{url}" if url else ""
    if publisher == "ACM":
        direct = f"https://dl.acm.org/doi/pdf/{doi}"
    elif publisher == "Springer":
        direct = f"https://link.springer.com/content/pdf/{doi}.pdf"
    else:
        direct = f"https://doi.org/{doi}"
    return f"{PROXY}{direct}"


def _publisher(doi: Optional[str]) -> str:
    doi = (doi or "").lower()
    if doi.startswith("10.1145/"):
        return "ACM"
    if doi.startswith("10.1109/"):
        return "IEEE"
    if doi.startswith("10.1007/"):
        return "Springer"
    if doi.startswith("10.14778/"):
        return "PVLDB"
    return "other"


def wanted(papers: Optional[List[dict]] = None) -> List[dict]:
    """Papers needing a hand-supplied PDF, most useful first."""
    if papers is None:
        papers = config.load_json(config.DATA_FILES["papers"])["papers"]

    rows = []
    for paper in papers:
        if paper.get("is_sqlancer_publication"):
            continue
        if fulltext.reachable_without_help(paper):
            continue
        filename = fulltext.wanted_filename(paper.get("doi"),
                                            paper.get("arxiv_id"),
                                            paper.get("s2_paper_id"))
        if not filename:
            continue
        if fulltext.local_pdf(paper.get("doi"), paper.get("arxiv_id"),
                              paper.get("s2_paper_id")):
            continue  # already supplied

        undecided = [key for key in JUDGEMENTS
                     if paper["relationships"][key]["value"] in UNDECIDED]
        settled = [key for key in JUDGEMENTS
                   if paper["relationships"][key]["value"] == "yes"]
        rows.append({
            "hints": _hint_count(paper),
            "title": paper["title"],
            "url": paper.get("url") or "",
            "filename": filename,
            "year": paper.get("year"),
            "venue": paper.get("venue") or "",
            "publisher": _publisher(paper.get("doi")),
            "download_url": download_url(paper.get("doi"), paper.get("url"),
                                         _publisher(paper.get("doi"))),
            "undecided": undecided,
            "settled": settled,
        })

    # Ordered by how much reading the paper is likely to change. First those
    # whose contexts already tie them to SQLancer, where a wrong call costs
    # most; then those whose citing sentences hint at a relationship the two
    # indexed sentences could not settle; then the rest, newest first, since a
    # recent paper is the more likely to still be uncatalogued elsewhere.
    rows.sort(key=lambda r: (-len(r["settled"]), -r["hints"], -(r["year"] or 0),
                             r["title"].lower()))
    return rows


def render(rows: List[dict]) -> str:
    lines = [
        "# Papers to download",
        "",
        f"{len(rows)} papers whose publisher will not serve a PDF to an "
        "automated client. Downloading one is entirely optional: a missing "
        "paper costs coverage for that paper, never correctness.",
        "",
        "Save each file into this directory under the **Save as** name — that "
        "is how the pipeline finds it — then re-run:",
        "",
        "```sh",
        "python3 -m tools.impact.run collect --only papers --full",
        "```",
        "",
        "Claims drawn from a supplied PDF quote it verbatim, exactly as they "
        "would from a preprint, so they stay checkable by anyone holding the "
        "same paper.",
        "",
        "Papers on arXiv, in PVLDB, or at a USENIX venue are not listed: the "
        "pipeline reads those itself.",
        "",
        "IEEE links go through the NUS library proxy, which is what makes them "
        "resolve; ACM and Springer links point straight at the PDF.",
        "",
        "| # | Paper | Download | Known so far | Save as |",
        "| ---: | --- | --- | --- | --- |",
    ]
    for index, row in enumerate(rows, start=1):
        title = row["title"].replace("|", "\\|")
        link = f"[{title}]({row['url']})" if row["url"] else title
        year = f" ({row['year']})" if row["year"] else ""
        # What the citation contexts already established, so it is obvious
        # which papers the full text would actually change.
        known = ", ".join(k.replace("_", " ") for k in row["settled"])
        if not known and row["hints"]:
            known = f"cites only ({row['hints']} sentences hint at more)"
        download = (f"[{row['publisher']} PDF]({row['download_url']})"
                    if row["download_url"] else row["publisher"])
        lines.append(
            f"| {index} | {link}{year} | {download} | "
            f"{known or 'cites only'} | `{row['filename']}` |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    rows = wanted()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render(rows), encoding="utf-8")
    print(f"{OUTPUT}: {len(rows)} paper(s) to download")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
