"""Collects resources related to SQLancer.

The failure mode this collector is designed against is turning into a link
dump. A resource has to be substantially about SQLancer or one of its
techniques, which in practice means it comes from one of two places: the
project's own material (its repository, its documentation, the talks recorded
alongside its papers), or a repository that exists specifically to integrate
SQLancer with something else.

The project's own README is the primary source for links to other people's
work. What the project publishes about itself -- its documentation, its blog,
its own papers -- is deliberately excluded: the site already links all of it,
and repeating it here would pad the list without saying anything about reach.
Each entry keeps the verbatim line it came from.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Dict, List, Optional, Sequence, Tuple

from .. import config, taxonomy
from ..github import GitHub
from ..util import (content_hash, normalize_url, now, stable_digest, truncate,
                    verbatim_excerpt)

COLLECTOR = "resources"
COLLECTOR_VERSION = "1.1.0"

# Several publisher and video sites answer the default client with a 403.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36")

OWNER, REPO, REF = "sqlancer", "sqlancer", "main"
README_URL = f"https://github.com/{OWNER}/{REPO}/blob/{REF}/README.md"

MARKDOWN_LINK = re.compile(r"\[([^\]\n]{2,120})\]\((https?://[^)\s]+)\)")

# How a URL maps onto the resource vocabulary. Order matters: first match wins.
#
# There is no catch-all. Documentation, tutorials and paper references were
# removed from the vocabulary because in practice they were the project's own
# material -- its README, its blog, its own publications -- which the site
# already links and which says nothing about anyone else picking SQLancer up.
# A link that fits none of the remaining kinds is now rejected rather than
# filed under a generic label.
TYPE_BY_HOST: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"youtube\.com|youtu\.be|vimeo\.com"), "talk"),
    (re.compile(r"zenodo\.org|figshare\.com"), "dataset"),
    (re.compile(r"github\.com/"), "tool"),
    (re.compile(r"blog|medium\.com|substack\.com|dev\.to"), "blog_post"),
)

# Link text that signals what a resource is, overriding the host heuristic.
TYPE_BY_LABEL: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bvideo\b|\btalk\b|\bpresentation\b|\bkeynote\b", re.I), "talk"),
    (re.compile(r"\bartifact\b", re.I), "artifact"),
    (re.compile(r"\bblog\b|\bpost\b", re.I), "blog_post"),
    (re.compile(r"\bdataset\b", re.I), "dataset"),
)

# The project's own properties. Material published here is SQLancer talking
# about itself, which belongs on the rest of the site rather than in a list of
# what other people have made.
OWN_PROPERTY = re.compile(
    r"^https://(github\.com/sqlancer(/|$)|sqlancer\.github\.io"
    r"|search\.maven\.org/artifact/com\.sqlancer"
    r"|hub\.docker\.com/r/mrigger/sqlancer)", re.IGNORECASE)

# The project names its own things after itself -- "SQLancer Talks", "SQLancer
# Tutorial Playlist" -- while other people's projects carry their own names.
# That is enough to tell the two apart on hosts the pattern above cannot cover,
# such as the project's YouTube playlists.
OWN_NAME = re.compile(r"^SQLancer\b", re.IGNORECASE)

# Links that are infrastructure rather than resources about SQLancer.
IGNORE_URL = re.compile(
    r"(shields\.io|travis-ci|codecov|badge|/actions/workflows/|"
    r"opensource\.org/licenses|creativecommons\.org|"
    r"github\.com/sqlancer/sqlancer/(issues|pull|blob|commit|releases|tree)/|"
    r"github\.com/[^/]+/[^/]+/(issues|pull|commit)/)",
    re.IGNORECASE)

# README sections that collect resources rather than instructions. A link
# outside them has to earn its place some other way -- this is what keeps the
# build-tool prerequisite and the DBMS manuals quoted in the FAQ out of the
# list, without needing a denylist of hosts.
RESOURCE_SECTION = re.compile(
    r"\b(links?|papers?|approach\w*|resources?|talks?|related)\b",
    re.IGNORECASE)

# A recording linked from the project's own README is about the project, wherever
# in the README it sits.
MEDIA_HOST = re.compile(r"youtube\.com|youtu\.be|vimeo\.com", re.IGNORECASE)

HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)

# Link texts that carry no information; the surrounding line names the resource.
UNINFORMATIVE_LABEL = re.compile(
    r"^(here|this|link|these|it|paper|video|docs?|more|read more|"
    r"click here|see here|blog|website|github|gitlab)[.:]?$", re.IGNORECASE)


# Sources that no automated route here reaches: vendor engineering blogs,
# package registries, conference recordings. Each is fetched and quoted like
# any other source -- being on this list decides that a URL is worth looking at,
# never what it says.
CURATED: Tuple[Dict[str, object], ...] = (
    {
        "url": ("https://techcommunity.microsoft.com/blog/adforpostgresql/"
                "mining-for-logic-bugs-in-the-citus-extension-to-postgres-"
                "with-sqlancer/1634393"),
        "type": "blog_post",
        "title": ("Mining for logic bugs in the Citus extension to Postgres "
                  "with SQLancer"),
        "year": 2020,
        "publisher": "Microsoft Tech Community",
        "related_dbms": ["citus", "postgresql"],
        "related_techniques": ["tlp"],
    },
    {
        "url": "https://www.yugabyte.com/blog/yugabytedb-database-testing/",
        "type": "blog_post", "title": "How we test YugabyteDB",
        "publisher": "Yugabyte", "related_dbms": ["yugabytedb"],
        "related_techniques": [],
    },
    {
        "url": "https://materialize.com/blog/qa-process-overview/",
        "type": "blog_post", "title": "An overview of the Materialize QA process",
        "publisher": "Materialize", "related_dbms": ["materialize"],
        "related_techniques": [],
    },
    {
        "url": "https://www.monetdb.org/blogs/faster-robuster-features/",
        "type": "blog_post",
        "title": "Faster, more robust, and with more features",
        "publisher": "MonetDB", "related_dbms": ["monetdb"],
        "related_techniques": [],
    },
    {
        # Later than the other two Citus posts, and the reason it is here: it
        # says the use carried on after the fork stopped being updated.
        "url": ("https://www.citusdata.com/blog/2021/04/10/"
                "talk-at-cmu-how-citus-distributes-postgresql-via-extension-"
                "apis/"),
        "type": "blog_post",
        "title": ("Talk at CMU: How Citus distributes PostgreSQL via extension "
                  "APIs"),
        "year": 2021,
        "publisher": "Citus Data",
        "related_dbms": ["citus", "postgresql"],
        "related_techniques": [],
    },
    {
        "url": ("https://www.citusdata.com/blog/2020/09/04/"
                "mining-for-logic-bugs-in-citus-with-sqlancer/"),
        "type": "blog_post",
        "title": "Mining for logic bugs in Citus with SQLancer",
        "year": 2020, "publisher": "Citus Data",
        "related_dbms": ["citus", "postgresql"], "related_techniques": ["tlp"],
    },
    {
        # The project's own testing page, which names the author rather than
        # the tool -- and is the reason SQLite appears in half the talks here.
        "url": "https://www.sqlite.org/testing.html",
        "type": "documentation_note",
        "title": "How SQLite Is Tested",
        "publisher": "SQLite",
        "related_dbms": ["sqlite"],
        "related_techniques": [],
    },
    {
        "url": "https://duckdb.org/why_duckdb",
        "type": "documentation_note", "title": "Why DuckDB",
        "publisher": "DuckDB", "related_dbms": ["duckdb"],
        "related_techniques": [],
    },
    {
        "url": "https://datafusion.apache.org/contributor-guide/testing.html",
        "type": "documentation_note",
        "title": "Apache DataFusion contributor guide: testing",
        "publisher": "Apache DataFusion", "related_dbms": ["datafusion"],
        "related_techniques": [],
    },
    # datafusion-sqlancer is deliberately absent. DataFusion's use of SQLancer
    # is already an adoption record backed by the project's own testing guide,
    # and it has its own page carrying 54 bugs; listing the repository here as
    # well says nothing the adoption record does not, and puts one project in
    # two places under two headings.
    {
        "url": "https://github.com/PingCAP-QE/go-sqlancer",
        # Distinguished from chaos-mesh/go-sqlancer, which the SQLancer README
        # links and which carries the same name.
        "type": "tool", "title": "go-sqlancer (PingCAP-QE)",
        "publisher": "PingCAP", "related_dbms": ["tidb"],
        "related_techniques": ["pqs", "norec", "tlp"],
    },
    {
        "url": "https://questdb.com/blog/fuzz-testing-questdb/",
        "type": "blog_post",
        "title": "Fuzz testing QuestDB",
        "year": 2022,
        "publisher": "QuestDB",
        "related_dbms": ["questdb"],
        "related_techniques": [],
    },
    {
        # The repository is the primary source, as it is for every other tool
        # here; the Open Exchange listing only republishes it.
        "url": "https://github.com/caretdev/sqlancer-iris",
        "type": "tool",
        "title": "sqlancer-iris",
        "publisher": "CaretDev",
        "related_dbms": [],
        "related_techniques": [],
    },
    {
        # The write-up behind that integration, by its author: what it took to
        # bring the NoREC oracle to IRIS, and what the attempt turned up.
        "url": ("https://community.intersystems.com/post/when-sqlancer-meets-"
                "iris-what-happens-when-we-push-database-its-limits"),
        "type": "blog_post",
        "title": ("When SQLancer Meets IRIS: What Happens When We Push a "
                  "Database to Its Limits"),
        "year": 2025,
        "publisher": "InterSystems Developer Community",
        "related_dbms": [],
        "related_techniques": ["norec"],
    },
)

# Talks are not listed here. They live in _data/impact/talks.json, which
# records where in a talk SQLancer comes up and how far its words can be
# trusted; ``from_talks`` turns each of those records into a resource so the
# two never drift apart.

TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
# The name is not the only way a page is about SQLancer. SQLite's testing page
# is the strongest endorsement the project has anywhere and never uses the word:
# it names the author, and calls them "Dr. Rigger's fuzzers". What a curated
# source is about is settled by the list of them; this pattern only has to find
# the sentence worth quoting once that decision is made.
SENTENCE_WITH_SQLANCER = re.compile(
    r"[^.!?]{0,240}\b(?:SQLancer|Manuel Rigger|Rigger's)\b[^.!?]{0,240}[.!?]")

# Share of lower-case words above which a match reads as a sentence rather than
# as a row of navigation labels.
PROSE_RATIO = 0.55


GITHUB_REPOSITORY = re.compile(
    r"^https://github\.com/([\w.-]+)/([\w.-]+)/?$", re.IGNORECASE)
README_NAMES = ("README.md", "README.rst", "README.txt", "readme.md")

# Markdown furniture that would otherwise end up inside a quoted sentence.
MARKDOWN_BADGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
MARKDOWN_NOISE = re.compile(r"[#*_`>|]+|\[([^\]]*)\]\([^)]*\)")
EMPTY_BRACKETS = re.compile(r"\[\s*\]\s*\(\s*\)|\[\s*\]|\(\s*\)")

# A bare URL in a README ends sentences everywhere its path has a dot, so a
# quote taken from the line around it starts mid-address.
BARE_URL = re.compile(r"<?https?://\S+>?")


def readme_text(fetcher, url: str) -> Optional[str]:
    """A repository's README as plain text, or ``None``.

    A repository page is mostly chrome -- the file list, the language bar, the
    sidebar -- and quoting it produces a sentence that starts in the middle of
    a navigation menu. The README is what the project says about itself.
    """
    match = GITHUB_REPOSITORY.match(url)
    if not match:
        return None
    owner, repo = match.group(1), match.group(2)
    for name in README_NAMES:
        raw = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{name}"
        try:
            entry, _ = fetcher.fetch(raw, max_age_days=90)
        except Exception:
            continue
        body = entry.get("body") or ""
        if entry.get("status") == 200 and body:
            # Badges first: they are images inside links, and taking the link
            # apart from the outside leaves their brackets behind.
            text = MARKDOWN_BADGE.sub(" ", body)
            text = MARKDOWN_NOISE.sub(lambda m: m.group(1) or " ", text)
            text = BARE_URL.sub(" ", text)
            text = EMPTY_BRACKETS.sub(" ", text)
            return re.sub(r"\s+", " ", text).strip()
    return None


def best_sentence(text: str) -> Optional[str]:
    """The most prose-like sentence mentioning SQLancer, or ``None``.

    Taking the first match quotes page furniture: a navigation bar carries no
    sentence-ending punctuation, so a title further down the page gets the menu
    above it swallowed into the match. Running text is mostly lower-case words,
    while furniture is a run of capitalised labels, which separates the two
    well enough to pick a sentence a reader would recognise as the page
    speaking.
    """
    best, best_score = None, -1.0
    for match in SENTENCE_WITH_SQLANCER.finditer(text):
        candidate = match.group(0).strip()
        words = [w for w in re.findall(r"[A-Za-z][\w'-]*", candidate) if len(w) > 1]
        if len(words) < 6:
            continue
        score = sum(1 for w in words if w[0].islower()) / len(words)
        # The first sentence that reads as prose is the one a person would
        # quote: pages introduce what they are about before elaborating on it.
        if score >= PROSE_RATIO:
            return candidate
        if score > best_score:
            best, best_score = candidate, score
    return best


def page_text(html: str) -> str:
    """Readable text of a fetched page, for quoting."""
    text = SCRIPT_RE.sub(" ", html)
    text = TAG_RE.sub(" ", text)
    text = unescape(text).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    # Stripping a link out of running text leaves a space before the full stop
    # it was followed by. That space is an artefact of reading the page, not
    # something the page wrote, and it would end up inside a quotation.
    return re.sub(r" +([.,;:!?])", r"\1", text).strip()


def from_talks(*, timestamp: Optional[str] = None) -> List[dict]:
    """Resource records for the talks file, quoting its strongest mention.

    A talk's evidence is ranked by how directly it reaches the speaker: their
    own slides first, then captions -- which get said out loud in the note to
    be a machine's transcription -- then a viewer's account, which carries no
    quote at all.
    """
    from .. import talks as talks_store

    timestamp = timestamp or now()
    rank = {"slides": 0, "captions": 1, "watched": 2}
    records: List[dict] = []
    for talk in talks_store.load().get("talks", []):
        mentions = sorted(talk.get("mentions") or [],
                          key=lambda m: rank.get(m.get("source"), 3))
        if not mentions:
            continue
        best = mentions[0]
        source = best.get("source")
        if source == "slides":
            note = "Sentence from the talk's own slides."
        elif source == "captions":
            heard = best.get("heard_as")
            note = ("From the talk's automatic captions, a machine "
                    "transcription of speech"
                    + (f" -- it renders the name as \"{heard}\"" if heard else "")
                    + f". The link points at {best.get('timestamp')}, where a "
                      "reader can listen to what was actually said.")
        else:
            note = best.get("note") or "Reported by a maintainer who watched it."

        summary = (talk.get("relationship") or {}).get("summary") or talk["title"]
        records.append({
            "id": f"resource:talk:{stable_digest(talk['url'])}",
            "title": truncate(talk["title"], 300),
            "type": "talk",
            "url": talk["url"],
            "description": truncate(summary, 590),
            "official": False,
            "evidence": [{
                "source_url": best.get("url") or talk["url"],
                "source_type": "video",
                "excerpt": best.get("excerpt"),
                "excerpt_is_verbatim": bool(best.get("excerpt")),
                "note": truncate(note, 500),
                "retrieved_at": timestamp,
                "first_seen": timestamp,
                "last_verified": timestamp,
            }],
            "provenance": {
                "collector": COLLECTOR,
                "collector_version": COLLECTOR_VERSION,
                "first_seen": timestamp,
                "last_verified": timestamp,
                "source_url": talk["url"],
                "source_type": "video",
            },
            **({"year": talk["year"]} if talk.get("year") else {}),
            **({"publisher": talk["publisher"]} if talk.get("publisher") else {}),
            **({"authors": talk["speakers"]} if talk.get("speakers") else {}),
            **({"related_techniques": talk["related_techniques"]}
               if talk.get("related_techniques") else {}),
            **({"related_dbms": talk["related_dbms"]}
               if talk.get("related_dbms") else {}),
        })
    return records


def from_curated(fetcher, *, timestamp: Optional[str] = None,
                 entries: Optional[Sequence[Dict[str, object]]] = None
                 ) -> Tuple[List[dict], List[dict]]:
    """Fetch each curated source and quote what it actually says.

    A source that cannot be fetched, or that turns out not to mention SQLancer,
    is reported rather than published: the list says where to look, not what is
    there.
    """
    timestamp = timestamp or now()
    tax = taxonomy.load()
    records: List[dict] = []
    needs_review: List[dict] = []

    for item in (entries if entries is not None else CURATED):
        url = normalize_url(str(item["url"]))
        if not url:
            continue
        try:
            entry, _ = fetcher.fetch(str(item["url"]), max_age_days=90)
        except Exception:
            entry = {}
        body = entry.get("body") or ""
        if entry.get("status") != 200 or not body:
            needs_review.append({
                "kind": "resource", "url": url,
                "reason": "curated source could not be fetched",
            })
            continue

        text = readme_text(fetcher, url) or page_text(body)
        sentence = best_sentence(text)
        if sentence:
            excerpt = truncate(sentence, 900)
            note = "Sentence on the page describing SQLancer."
            if not verbatim_excerpt(text, excerpt):
                continue
        else:
            # A source with nothing quotable is reported, never published with
            # a note standing in for a quote. The one kind of source where that
            # is unavoidable -- a recording, whose words are not in its page --
            # is handled in talks.json, which says so explicitly.
            needs_review.append({
                "kind": "resource", "url": url,
                "reason": "curated source does not mention SQLancer in its text",
            })
            continue

        kind = str(item["type"])
        record = {
            "id": f"resource:{kind}:{stable_digest(url)}",
            "title": truncate(str(item["title"]), 300),
            "type": kind,
            "url": url,
            "description": truncate(excerpt, 590),
            "official": False,
            "evidence": [{
                "source_url": url,
                "source_type": ("blog_post" if kind == "blog_post"
                                else "video" if kind == "talk" else "website"),
                "excerpt": excerpt,
                "note": note,
                "content_sha256": content_hash(text),
                "retrieved_at": timestamp,
                "first_seen": timestamp,
                "last_verified": timestamp,
            }],
            "provenance": {
                "collector": COLLECTOR,
                "collector_version": COLLECTOR_VERSION,
                "first_seen": timestamp,
                "last_verified": timestamp,
                "source_url": url,
                "source_type": "website",
            },
        }
        if item.get("year"):
            record["year"] = item["year"]
        if item.get("publisher"):
            record["publisher"] = str(item["publisher"])
        techniques = list(item.get("related_techniques") or [])
        techniques += [m.technique_id for m in tax.find_techniques(text)]
        techniques = [t for t in dict.fromkeys(techniques) if t in tax.techniques]
        if techniques:
            record["related_techniques"] = sorted(techniques)
        if item.get("related_dbms"):
            record["related_dbms"] = sorted(item["related_dbms"])
        if excerpt is not None:
            record["evidence"][0]["excerpt_is_verbatim"] = True
        records.append(record)
    return records, needs_review


def classify_type(label: str, url: str) -> Optional[str]:
    """The resource kind, or None when the link is not one we keep."""
    for pattern, kind in TYPE_BY_LABEL:
        if pattern.search(label):
            return kind
    for pattern, kind in TYPE_BY_HOST:
        if pattern.search(url):
            return kind
    return None


def _line_of(text: str, index: int) -> str:
    start = text.rfind("\n", 0, index)
    start = 0 if start < 0 else start + 1
    end = text.find("\n", index)
    end = len(text) if end < 0 else end
    return truncate(text[start:end].strip(), 900)


def _sections(text: str) -> List[Tuple[int, str]]:
    """Offsets of each heading, so a link can be attributed to its section.

    Inline links are stripped from the heading text first: a heading like
    ``# Getting Started [[Video Guide]](...)`` must not read as a section about
    videos and pull the whole installation section in with it.
    """
    return [(match.start(),
             MARKDOWN_LINK.sub("", match.group(2)).replace("[", " ")
             .replace("]", " ").strip())
            for match in HEADING.finditer(text)]


def _section_at(sections: List[Tuple[int, str]], index: int) -> str:
    current = ""
    for offset, title in sections:
        if offset > index:
            break
        current = title
    return current


def _title_for(label: str, line: str, url: str) -> str:
    """Use the link text unless it says nothing, then fall back sensibly.

    The line only serves as a title when it holds exactly one link; otherwise it
    describes several resources at once and would title all of them the same.
    """
    label = label.strip()
    if not UNINFORMATIVE_LABEL.match(label):
        return truncate(label, 300)
    if len(MARKDOWN_LINK.findall(line)) == 1:
        stripped = MARKDOWN_LINK.sub(r"\1", line).lstrip("*-# ").strip()
        stripped = re.sub(r"\s+", " ", stripped).strip("*").rstrip(".")
        if stripped and len(stripped) > len(label):
            return truncate(stripped, 300)
    if "sqlancer" in url.lower():
        return truncate(f"SQLancer {label}", 300)
    return truncate(label, 300)


def _describe(label: str, line: str, kind: str) -> str:
    """A short factual description built from the source line, not invented.

    Falls back to a plain statement of what the resource is rather than
    guessing at content the source does not describe.
    """
    stripped = MARKDOWN_LINK.sub(r"\1", line).lstrip("*-# ").strip()
    stripped = re.sub(r"\s+", " ", stripped)
    if len(stripped) > len(label) + 15:
        return truncate(stripped, 590)
    readable = kind.replace("_", " ")
    return f"{label}. Linked from the SQLancer README as a {readable}."


def from_readme(gh: GitHub, *, timestamp: Optional[str] = None
                ) -> Tuple[List[dict], List[dict]]:
    """Parse the SQLancer README's links into resource records."""
    timestamp = timestamp or now()
    tax = taxonomy.load()
    text, _ = gh.raw_file(OWNER, REPO, "README.md", REF, max_age_days=7)
    if not text:
        return [], []

    readme_hash = content_hash(text)
    sections = _sections(text)
    records: List[dict] = []
    rejected: List[dict] = []
    seen: Dict[str, str] = {}

    for match in MARKDOWN_LINK.finditer(text):
        label, url = match.group(1).strip(), match.group(2).strip()
        canonical = normalize_url(url)
        if not canonical or IGNORE_URL.search(url):
            continue
        if canonical in seen:
            continue
        line = _line_of(text, match.start())
        if not verbatim_excerpt(text, line):
            continue

        # Substantially about SQLancer, or nothing. Sitting in a section that
        # lists resources is not enough on its own: the README's links section
        # also names peer tools -- Jepsen, SQLsmith, Squirrel -- which are other
        # people's testing tools that have nothing to do with SQLancer, and
        # listing them here would pad the count with work that is not evidence
        # of reach. What qualifies is the link pointing at something SQLancer,
        # or the README's own line about it naming SQLancer or one of its
        # techniques, or it being a recording, which the README only links when
        # the talk is about the project.
        section = _section_at(sections, match.start())
        about_sqlancer = (
            "sqlancer" in canonical.lower()
            or bool(tax.mentions_finder(line) or tax.find_techniques(line))
            or bool(MEDIA_HOST.search(canonical)))
        if not (about_sqlancer and RESOURCE_SECTION.search(section)):
            rejected.append({
                "kind": "resource", "url": canonical, "title": label,
                "reason": (f"linked from the README section {section!r} but the "
                           f"line about it does not connect it to SQLancer"
                           if RESOURCE_SECTION.search(section) else
                           f"linked from the README section {section!r}, which "
                           f"does not list resources"),
            })
            continue

        if OWN_PROPERTY.match(canonical) or OWN_NAME.match(label.strip()):
            rejected.append({
                "kind": "resource", "url": canonical, "title": label,
                "reason": "the project's own material, not external adoption",
            })
            continue

        kind = classify_type(label, url)
        if kind is None:
            rejected.append({
                "kind": "resource", "url": canonical, "title": label,
                "reason": "does not fit any kept resource kind",
            })
            continue

        techniques = [m.technique_id for m in tax.find_techniques(line)]
        record = {
            "id": f"resource:{kind}:{stable_digest(canonical)}",
            "title": _title_for(label, line, canonical),
            "type": kind,
            "url": canonical,
            "description": _describe(label, line, kind),
            "official": "github.com/sqlancer/" in canonical,
            "evidence": [{
                "source_url": README_URL,
                "source_type": "documentation",
                "excerpt": line,
                "excerpt_is_verbatim": True,
                "note": "Line in the SQLancer README that links this resource.",
                "content_sha256": readme_hash,
                "retrieved_at": timestamp,
                "first_seen": timestamp,
                "last_verified": timestamp,
            }],
            "provenance": {
                "collector": COLLECTOR,
                "collector_version": COLLECTOR_VERSION,
                "first_seen": timestamp,
                "last_verified": timestamp,
                "source_url": README_URL,
                "source_type": "documentation",
                "content_sha256": readme_hash,
            },
        }
        if techniques:
            record["related_techniques"] = sorted(set(techniques))
        records.append(record)
        seen[canonical] = record["id"]

    return records, rejected


def collect(gh: GitHub, *, timestamp: Optional[str] = None,
            fetcher=None) -> Tuple[List[dict], List[dict], List[dict]]:
    """Return ``(records, rejected, needs_review)`` for all resource sources."""
    timestamp = timestamp or now()
    records, rejected = from_readme(gh, timestamp=timestamp)
    by_url = {record["url"] for record in records}

    needs_review: List[dict] = []
    if fetcher is None:
        from ..cache import Caches
        from ..http import Fetcher
        fetcher = Fetcher(Caches().http, min_interval=0.5, max_retries=2,
                          timeout=20.0,
                          headers={"User-Agent": BROWSER_USER_AGENT})
    curated, needs_review = from_curated(fetcher, timestamp=timestamp)
    for record in list(curated) + from_talks(timestamp=timestamp):
        if record["url"] not in by_url:
            records.append(record)
            by_url.add(record["url"])
    return records, rejected, needs_review
