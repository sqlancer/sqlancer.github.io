"""The catalogue of talks that discuss SQLancer, and how each one is read.

Unlike every other collector here, this one does not search. Nothing indexes
what is said out loud in a conference room, so a talk arrives because somebody
saw it. What the collector does is turn that pointer into evidence: it fetches
the speaker's slides where a deck exists, folds in a caption transcript where
one was captured from the browser, and otherwise records plainly that the only
source is a person who watched it.

The routes are deliberately not merged into one notion of "the text of the
talk" -- see ``tools/impact/talks.py`` for why an automatic caption is not a
quotation.

Frames are the fourth route and the one that reaches what the others cannot:
some talks name SQLancer only on a slide, and a slide is never spoken. A frame
carries no excerpt, ever. Every other excerpt in this dataset is a substring of
something that was fetched, and a check can say so; text read off a picture is
not, so the picture is shown and a note says what is in it.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Sequence, Tuple

from .. import taxonomy
from ..talks import (TRANSCRIPT_CACHE, deep_link, findings_from_segments,
                     findings_from_slides, frame_for, talk_id, timestamp_of,
                     video_id)
from ..util import content_hash, now

COLLECTOR = "talks"
COLLECTOR_VERSION = "1.0.0"

TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)


# Every entry names its speaker and where it was given, because that is what
# makes a talk worth recording: who said it, and to whom.
TALKS: Tuple[Dict[str, object], ...] = (
    {
        "url": "https://www.youtube.com/watch?v=QRwxHGpWaUA",
        "title": "The Art of Database Testing by Alperen Keles | DC Systems 011",
        "speakers": ["Alperen Keles"],
        "publisher": "Antithesis",
        "event": "DC Systems 011",
        "year": 2026,
        "duration_seconds": 2431,
        "related_techniques": ["pqs", "tlp"],
        "no_frame": [
            {"at_seconds": 2087,
             "why": ("The closing slide with the speaker's contact details is "
                     "on screen; this mention is in the questions afterwards.")},
        ],
        "frames": [
            {"at_seconds": 205, "matched": ["sqlancer"],
             "note": ("The slide behind the speaker is SQLancer's logo above "
                      "its eight oracles, named in full: PQS, NoREC, TLP, DQE, "
                      "QPG, CERT, DQP and CODDTest."),
             "technique_ids": ["pqs", "norec", "tlp", "qpg", "cert", "dqp",
                               "coddtest"]},
        ],
        "roles": ["explains_technique", "background"],
        "summary": ("A survey of database testing methodology that walks an "
                    "audience through SQLancer's oracles, naming pivoted query "
                    "synthesis outright and putting ternary logic partitioning "
                    "on a slide; the tool itself comes up again in questions."),
    },
    {
        "url": "https://www.youtube.com/watch?v=6YGqFRTe2D0",
        "title": ("[FUZZING'23] \"Three Colours of Fuzzing: Reflections and "
                  "Open Challenges\" Keynote by Cristian Cadar"),
        "speakers": ["Cristian Cadar"],
        "publisher": "International Fuzzing Workshop",
        "event": "FUZZING'23",
        "year": 2023,
        "duration_seconds": 3638,
        "frames": [
            {"at_seconds": 295, "matched": ["sqlancer"],
             "note": ("A slide of fuzzers that made a difference \u2014 KLEE, "
                      "SAGE, AFL, OSS-Fuzz, Csmith and EMI \u2014 ending with "
                      "SQLancer as "
                      "the DBMS fuzzer, with the bug count it found in popular "
                      "database systems.")},
            {"at_seconds": 1509, "matched": ["author"],
             "note": ("The slide displays SQLite's own paragraph about the "
                      "project's author, attributed on the slide to the SQLite "
                      "webpage. Its wording is in the dataset already, quoted "
                      "from sqlite.org/testing.html.")},
        ],
        "roles": ["cites_as_example", "recognition"],
        "summary": ("A fuzzing keynote that reaches for SQLancer as its "
                    "example of a fuzzer that found hundreds of bugs in mature "
                    "database systems, and later reads out SQLite's own line "
                    "about the project's author, agreeing with it."),
    },
    {
        "url": "https://www.youtube.com/watch?v=L90MBb6NLBE&t=1703s",
        "title": ("FUZZING'25 Keynote: \"Constraining Fuzzing without Paying "
                  "Too Much\" by Miryung Kim"),
        "speakers": ["Miryung Kim"],
        "publisher": "International Fuzzing Workshop",
        "event": "FUZZING'25",
        "year": 2025,
        "duration_seconds": 2786,
        "frames": [
            {"at_seconds": 1703, "matched": ["sqlancer"],
             "note": ("The keynote's table of what fuzzer customisations cost "
                      "to build lists SQLancer among them, with its "
                      "contributor, commit and paper counts.")},
        ],
        "roles": ["recognition"],
        "summary": ("A fuzzing keynote that puts SQLancer in its table of what "
                    "building a custom fuzzer costs, at 28:23."),
        "watched": ("A maintainer watched the keynote and noted that SQLancer "
                    "is highlighted at 28:23. The page carries no transcript."),
    },
    {
        "url": "https://www.youtube.com/watch?v=V_qzqY1bb7I",
        "title": "Reliability Lessons From SQLite - Richard Hipp | SSW 2026",
        "speakers": ["Richard Hipp"],
        "publisher": "Software Should Work",
        "event": "SSW 2026",
        "year": 2026,
        "duration_seconds": 3261,
        "related_dbms": ["sqlite"],
        "frames": [
            {"at_seconds": 2836, "matched": ["author"],
             "note": ("The slide behind him is SQLite's project timeline: query "
                      "fuzzing arrives around 2020, after 3.0.0, 100% MC/DC "
                      "coverage and profile-guided fuzzing.")},
        ],
        "roles": ["recognition", "explains_technique"],
        "summary": ("SQLite's creator on twenty-six years of testing it, "
                    "crediting Rigger with the idea of fuzzing for "
                    "inconsistencies in SQL rather than for crashes, "
                    "explaining the sub-query equivalence that finds them, and "
                    "saying SQLite's own fuzzer had to be extended to do the "
                    "same."),
    },
    {
        "url": "https://www.youtube.com/watch?v=CW4Ntdtp7lg",
        "title": "Fuzzing databases is difficult",
        "speakers": ["Pedro Ferreira"],
        "publisher": "ClickHouse",
        "event": "FOSDEM 2025",
        "year": 2025,
        "related_dbms": ["clickhouse"],
        "duration_seconds": 1611,
        "frames": [
            {"at_seconds": 110, "matched": ["sqlancer"],
             "note": ("The slide \"Testing with Fuzzers\" names the fuzzers "
                      "ClickHouse runs, SQLancer first among them.")},
            {"at_seconds": 827, "matched": ["sqlancer"],
             "note": ("The slide \"Finding wrong results\" credits SQLancer by "
                      "name with pioneering the comparison of equivalent "
                      "queries against an oracle.")},
        ],
        "roles": ["reports_adoption", "recognition"],
        "summary": ("A ClickHouse engineer on how the system is fuzzed. "
                    "SQLancer appears on the early list of the fuzzers "
                    "ClickHouse runs, again where the talk explains detecting "
                    "wrong results by comparing equivalent queries, and once "
                    "more in the closing recommendations."),
        "watched": ("Reported by a maintainer who watched the talk: SQLancer "
                    "is listed among ClickHouse's fuzzers, credited with "
                    "pioneering the comparison of equivalent queries against "
                    "an oracle, and recommended by name for query "
                    "correctness. No transcript has been captured."),
    },
    {
        "url": "https://www.youtube.com/watch?v=BgC79Zt2fPs",
        "title": "Keynote 1: DuckDB Testing - Present and Future",
        "speakers": ["Mark Raasveldt"],
        "publisher": "DBTest Workshop",
        "event": "DBTest 2022",
        "year": 2022,
        "duration_seconds": 3849,
        "related_dbms": ["duckdb"],
        "frames": [
            {"at_seconds": 2144, "matched": ["author"],
             "note": ("The slide behind the story: \u201cand then Dr. Rigger "
                      "came along!\u201d, over a DuckDB bug report.")},
            {"at_seconds": 2170, "matched": ["sqlancer", "author"],
             "note": ("The next slide puts a number on it: the bugs SQLancer "
                      "found in DuckDB, which the test suites DuckDB had "
                      "borrowed from other systems did not.")},
            {"at_seconds": 2455, "matched": ["sqlancer"],
             "note": ("The robot that runs fuzzers in DuckDB's CI, and what it "
                      "was running at the time: SQLancer and SQLsmith.")},
        ],
        "no_frame": [
            {"at_seconds": 2268,
             "why": ("The slide makes the general case for running many kinds "
                     "of fuzzer; SQLancer is named only in what is said.")},
        ],
        "roles": ["recognition", "reports_adoption", "compares"],
        "summary": ("DuckDB's testing, from one of its authors: the keynote "
                    "credits SQLancer with around eighty bugs that the SQLite "
                    "and Postgres test suites DuckDB had borrowed all missed, "
                    "argues for running it beside SQLsmith because each finds "
                    "what the other does not, and says the robot in DuckDB's "
                    "CI runs both."),
    },
    {
        "url": "https://www.youtube.com/watch?v=wHo-VtzTHx0",
        "title": "CockroachDB's Query Optimizer (Rebecca Taft, Cockroach Labs)",
        "speakers": ["Rebecca Taft"],
        "publisher": "CMU Database Group",
        "event": "CMU Quarantine Tech Talks 2020",
        "year": 2020,
        "duration_seconds": 3852,
        "related_dbms": ["cockroachdb"],
        "no_frame": [
            {"at_seconds": 3507,
             "why": ("A slide from the talk proper is still on screen; this "
                     "mention is in the questions afterwards.")},
        ],
        "roles": ["recognition"],
        "summary": ("CockroachDB's optimizer, from an engineer who builds it; "
                    "asked in the questions about random testing, she separates "
                    "SQLsmith's crashes from the logical bugs Rigger was after, "
                    "credits him with a batch of GitHub issues against "
                    "CockroachDB, and says she is trying to get SQLancer "
                    "running in their own system."),
    },
    {
        "url": "https://presentations.clickhouse.com/2021-cpp-siberia/index.html",
        "title": "Fuzzing: Practical approaches in ClickHouse",
        "speakers": ["Alexey Milovidov"],
        "publisher": "ClickHouse",
        "event": "C++ Siberia 2021",
        "year": 2021,
        "related_dbms": ["clickhouse"],
        "slides": "https://presentations.clickhouse.com/2021-cpp-siberia/index.html",
        "slide_image": "clickhouse-2021-cpp-siberia",
        "roles": ["reports_adoption"],
        "summary": ("A tour of everything ClickHouse fuzzes with, giving "
                    "SQLancer a section of its own: who wrote it, who brought "
                    "it into ClickHouse, and what it does."),
    },
    {
        "url": "https://presentations.clickhouse.com/2022-release-22.3/index.html",
        "title": "ClickHouse Release 22.3 Webinar",
        "speakers": ["Alexey Milovidov"],
        "publisher": "ClickHouse",
        "event": "ClickHouse 22.3 release webinar",
        "year": 2022,
        "related_dbms": ["clickhouse"],
        "slides": "https://presentations.clickhouse.com/2022-release-22.3/index.html",
        "slide_image": "clickhouse-2022-release-22.3",
        "roles": ["reports_adoption"],
        "summary": ("The release webinar's section on continuous integration "
                    "lists SQLancer among the fuzzing methods ClickHouse runs, "
                    "beside libFuzzer, the AST query fuzzer and Jepsen."),
    },
)


def deck_text(html: str) -> str:
    """Readable text of a slide deck, for quoting."""
    text = SCRIPT_RE.sub(" ", html)
    text = TAG_RE.sub(" ", text)
    from html import unescape
    return re.sub(r"\s+", " ", unescape(text)).strip()


def captured_transcript(url: str) -> Optional[dict]:
    """A caption transcript captured from the browser, if one is on disk."""
    found = video_id(url)
    if not found:
        return None
    path = TRANSCRIPT_CACHE / f"{found}.json"
    if not path.exists():
        return None
    try:
        with path.open() as handle:
            return json.load(handle)
    except ValueError:
        return None


def collect(fetcher, *, timestamp: Optional[str] = None,
            entries: Optional[Sequence[Dict[str, object]]] = None
            ) -> Tuple[List[dict], List[dict]]:
    """``(talks, needs_review)`` for the catalogue above."""
    timestamp = timestamp or now()
    tax = taxonomy.load()
    records: List[dict] = []
    needs_review: List[dict] = []

    for item in (entries if entries is not None else TALKS):
        url = str(item["url"])
        mentions: List[dict] = []
        sources: List[dict] = []

        slides = item.get("slides")
        if slides:
            try:
                entry, _ = fetcher.fetch(str(slides), max_age_days=90)
            except Exception:
                entry = {}
            body = entry.get("body") or ""
            if entry.get("status") == 200 and body:
                text = deck_text(body)
                found = findings_from_slides(text, url=str(slides), tax=tax)
                image = frame_for(str(item.get("slide_image") or "")) or None
                if image and found:
                    # The deck's own words are quotable, so this picture sits
                    # beside them rather than standing in for them.
                    found[0]["image"] = image
                mentions.extend(found)
                sources.append({
                    "kind": "slides", "status": "extracted", "url": str(slides),
                    "retrieved_at": timestamp, "characters": len(text),
                    "content_sha256": content_hash(text),
                })
                if not found:
                    needs_review.append({
                        "kind": "talk", "url": url,
                        "reason": "slide deck does not mention SQLancer",
                    })
            else:
                sources.append({"kind": "slides", "status": "unavailable",
                                "url": str(slides), "retrieved_at": timestamp})
                needs_review.append({"kind": "talk", "url": url,
                                     "reason": "slide deck could not be fetched"})

        captured = captured_transcript(url)
        if captured:
            found = findings_from_segments(captured.get("segments", []),
                                           url=url, tax=tax)
            for index, mention in enumerate(found):
                mention["id"] = f"M{len(mentions) + index + 1}"
            mentions.extend(found)
            sources.append({
                "kind": "captions", "status": "extracted", "url": url,
                "retrieved_at": captured.get("retrieved_at") or timestamp,
                "segments": captured.get("total_segments"),
                "characters": captured.get("total_characters"),
                # A transcript that was read and mentions nothing is a finding
                # rather than a blank: it says the mention is on a slide, which
                # is what the frames are for.
                "note": (captured.get("note") if found else
                         ("The whole transcript was read and SQLancer is not "
                          "spoken anywhere in it.")),
            })
        elif video_id(url):
            sources.append({"kind": "captions",
                            "status": str(item.get("captions_status")
                                          or "not_attempted"),
                            "url": url,
                            "note": item.get("captions_note")})

        for frame in (item.get("frames") or ()):
            at = int(frame["at_seconds"])
            image = frame_for(video_id(url), at)
            if not image:
                needs_review.append({
                    "kind": "talk", "url": url,
                    "reason": (f"a frame is recorded at {timestamp_of(at)} but "
                               "no image was captured for it"),
                })
                continue
            sources.append({"kind": "frame", "status": "extracted",
                            "url": deep_link(url, at), "retrieved_at": timestamp,
                            "note": f"Captured at {timestamp_of(at)}."})
            mentions.append({
                "id": f"M{len(mentions) + 1}",
                "source": "frame",
                "at_seconds": at,
                "timestamp": timestamp_of(at),
                "url": deep_link(url, at),
                "matched": list(frame.get("matched") or ["sqlancer"]),
                "heard_as": None,
                # Never an excerpt: text read off a picture cannot be checked
                # against a fetched source the way every other excerpt here is.
                "excerpt": None,
                "excerpt_is_verbatim": False,
                "note": str(frame["note"]),
                "image": image,
                "technique_ids": list(frame.get("technique_ids") or []),
            })

        watched = item.get("watched")
        if watched and not mentions:
            sources.append({"kind": "watched", "status": "extracted",
                            "url": url, "note": str(watched)})
            mentions.append({
                "id": f"M{len(mentions) + 1}",
                "source": "watched",
                "at_seconds": None,
                "timestamp": None,
                "url": url,
                "matched": ["sqlancer"],
                "heard_as": None,
                "excerpt": None,
                "excerpt_is_verbatim": False,
                "note": str(watched),
                "image": None,
                "technique_ids": [],
            })

        if not mentions:
            needs_review.append({
                "kind": "talk", "url": url,
                "reason": "no source could be read for this talk",
            })
            continue

        for checked in (item.get("no_frame") or ()):
            for mention in mentions:
                if mention.get("at_seconds") == int(checked["at_seconds"]):
                    mention["frame_checked"] = str(checked["why"])

        mentions.sort(key=lambda m: (m.get("at_seconds") is None,
                                     m.get("at_seconds") or 0))
        for index, mention in enumerate(mentions, start=1):
            mention["id"] = f"M{index}"

        techniques = list(item.get("related_techniques") or [])
        for mention in mentions:
            techniques.extend(mention.get("technique_ids") or [])
        techniques = sorted({t for t in techniques if t in tax.techniques})

        records.append({
            "id": talk_id(url),
            "title": str(item["title"]),
            "url": url,
            "video_id": video_id(url),
            "speakers": list(item.get("speakers") or []),
            "publisher": item.get("publisher"),
            "event": item.get("event"),
            "year": item.get("year"),
            "duration_seconds": item.get("duration_seconds"),
            "related_dbms": sorted(item.get("related_dbms") or []),
            "related_techniques": techniques,
            "sources": sources,
            "mentions": mentions,
            "relationship": {
                "roles": list(item.get("roles") or ["background"]),
                "summary": str(item["summary"]),
                "mention_ids": [m["id"] for m in mentions],
                "decided_by": "curator",
            },
            "provenance": {
                "collector": COLLECTOR,
                "collector_version": COLLECTOR_VERSION,
                "first_seen": timestamp,
                "last_verified": timestamp,
                "source_url": url,
                "source_type": "video",
            },
        })
    return records, needs_review
