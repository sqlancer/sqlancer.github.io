"""Talks in which someone stands up and discusses SQLancer.

A recorded talk is evidence no written source gives: a researcher naming
SQLancer as the example of a fuzzer that works, a database developer walking an
audience through how they run it, a keynote quoting SQLite's page about the
project's author. None of that is in the page's HTML -- it is in the audio --
so a talk cannot be collected the way a blog post can.

A talk can be read three ways, and they are not equally trustworthy:

* **Slides.** The speaker's own words, in text, fetchable like any other page.
  Where a deck exists this is the best source there is, and its sentences are
  quotable exactly as a blog post's are.
* **Automatic captions.** A machine's guess at speech. In the talks collected so
  far they render "SQLancer" as "SQL answer" and as "SQL lenser" -- which is
  also why ``ASR_VARIANTS`` exists, and why matching here is deliberately more
  permissive than anywhere else in the pipeline. An excerpt taken from captions
  is verbatim with respect to the caption track and *not* with respect to the
  speaker, so it carries the misheard form and a deep link to the second it was
  said. The link is the real evidence: a reader can listen.
* **A person who watched it.** No quote, but a note about what was said and
  where. Weakest, and honest about being so.

Captions are not fetched here. YouTube serves its caption tracks only to its own
player, so getting one means opening the talk in a browser, opening the
transcript panel, and reading the segments out of the page -- a manual step,
whose result ``findings_from_segments`` turns into mentions. Slides are fetched:
``findings_from_slides`` does it. Either way ``merge`` files the result, and the
talks file is curated data kept across runs: a run that cannot see a source
today must never delete what a person captured yesterday.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import config, taxonomy
from .util import now, stable_digest

MODULE_VERSION = "1.0.0"

TALKS_FILE = config.DATA_DIR / "talks.json"

# Where a hand-captured transcript is kept. Uncommitted, like every other cache:
# what belongs in the dataset is the handful of sentences that mention
# SQLancer, not a full transcription of somebody else's talk.
TRANSCRIPT_CACHE = config.CACHE_DIR / "talk-transcripts"

# Frames captured from a talk at the moment SQLancer comes up. Committed,
# because for some talks the mention is on a slide and never spoken: no
# transcript can reach it, and the frame is the only evidence there is.
#
# Capturing one is a browser job, and the trap is quality rather than aim: a
# player drops to a small rendition whenever it seeks and only upgrades a few
# seconds later, so a frame grabbed the moment the seek lands is a blur where a
# slide's table should be. Pin the quality first
# (``setPlaybackQualityRange('hd1080', 'hd1080')`` on ``#movie_player``), let it
# play for a second or two past the mark, then pause, then capture -- and read
# the slide back off the result before filing it.
FRAME_DIR = config.REPO_ROOT / "assets" / "images" / "impact" / "talks"
FRAME_URL_BASE = "/assets/images/impact/talks/"
FRAME_MAX_WIDTH = 960
FRAME_QUALITY = 82

YOUTUBE_ID = re.compile(
    r"(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/)([\w-]{11})", re.IGNORECASE)

# How transcribers hear "SQLancer". Every one of these was observed in a real
# caption track; they are not guesses.
ASR_VARIANTS = re.compile(
    r"\bsql\s*[- ]?\s*(?:lancer|lenser|lanza|answer|ancer|dancer|launcher)\b"
    r"|\bsequel\s*[- ]?\s*(?:lancer|lenser|answer)\b"
    r"|\bsqlancer\b",
    re.IGNORECASE)

# The project's author, as a talk is as likely to name the person as the tool.
# The surname alone is enough: transcribers spell the first name as "Manual" as
# often as "Manuel", and a talk about database testing that says "Rigger" means
# this one. The word boundary keeps "trigger" out, which is the only word that
# would otherwise collide.
AUTHOR = re.compile(r"\bri(?:gg|g|eg)er(?:'s)?\b|\bmanuel\s+rigor\b",
                    re.IGNORECASE)

# Spelled-out technique names. Acronyms are left to the taxonomy, which knows
# which of them are ambiguous.
TECHNIQUE_PHRASES: Tuple[Tuple[str, re.Pattern], ...] = (
    ("pqs", re.compile(r"\bpivoted query synthesis\b", re.I)),
    ("norec", re.compile(r"\bnon-?optimi[sz]ing reference engine\b", re.I)),
    ("tlp", re.compile(r"\bternary logic partitioning\b", re.I)),
    ("qpg", re.compile(r"\bquery plan guidance\b", re.I)),
    ("cert", re.compile(r"\bcardinality estimation restriction\b", re.I)),
    ("dqp", re.compile(r"\bdifferential query plans?\b", re.I)),
    ("coddtest", re.compile(r"\bconstant[- ]optimi[sz]ation[- ]driven\b", re.I)),
)

SENTENCE = re.compile(r"[^.!?]+[.!?]+|\S[^.!?]*$")

# Seconds of silence that end a sentence regardless of punctuation. Captions
# arrive without full stops, so without this every segment in a file runs
# together -- and a file holding windows from two ends of a talk would read as
# one continuous remark, which is exactly the sort of thing this module exists
# to prevent.
GAP_SECONDS = 30

# How much of a run to keep around a mention. Unpunctuated speech has no
# sentence to stop at, so the excerpt is a window instead: enough to see what
# was being said, not so much that it becomes a transcript.
EXCERPT_RADIUS = 340

# Below this, a matched sentence is extended with the one after it. Speech
# introduces a name in one breath and says what it is for in the next -- "then
# Rigger came up with this idea." on its own is a name-drop, and the sentence
# following it is the evidence.
SHORT_SENTENCE = 220


def video_id(url: str) -> Optional[str]:
    match = YOUTUBE_ID.search(url or "")
    return match.group(1) if match else None


def talk_id(url: str) -> str:
    found = video_id(url)
    return f"talk:youtube:{found}" if found else f"talk:{stable_digest(url)}"


def timestamp_of(seconds: int) -> str:
    """``25:09``, or ``1:00:36`` once a talk passes an hour."""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def deep_link(url: str, seconds: int) -> str:
    """The talk's URL pointing at the moment a mention was made."""
    base = url.split("&t=")[0].split("#")[0]
    joiner = "&" if "?" in base else "?"
    return f"{base}{joiner}t={int(seconds)}s"


def _sentences(segments: Sequence[dict]) -> List[dict]:
    """Caption segments rejoined into sentences that keep their timestamps.

    Captions break on line length rather than on grammar, so a mention often
    straddles two segments and neither one reads as a sentence on its own.
    """
    runs: List[List[dict]] = []
    for segment in segments:
        body = re.sub(r"\s+", " ", str(segment.get("text") or "")).strip()
        if not body:
            continue
        start = int(segment.get("start_ms") or 0) // 1000
        if runs and start - runs[-1][-1]["end"] <= GAP_SECONDS:
            runs[-1].append({"text": body, "start": start, "end": start})
        else:
            runs.append([{"text": body, "start": start, "end": start}])

    out = []
    for run in runs:
        text, owner = [], []
        for piece in run:
            if text:
                text.append(" ")
                owner.append(piece["start"])
            text.append(piece["text"])
            owner.extend([piece["start"]] * len(piece["text"]))
        joined = "".join(text)
        for match in SENTENCE.finditer(joined):
            body = match.group(0).strip()
            if body:
                out.append({"text": body, "offsets": owner,
                            "start": match.start(),
                            "at_seconds": owner[match.start()]})
    return out


def _matches(sentence: str, tax
             ) -> Tuple[List[str], Optional[str], List[str], int]:
    """``(matched, heard_as, technique_ids, position)`` for one sentence."""
    matched: List[str] = []
    techniques: List[str] = []
    heard_as = None
    position = -1

    name = ASR_VARIANTS.search(sentence)
    if name:
        position = name.start()
        matched.append("sqlancer")
        surface = name.group(0)
        if surface.replace(" ", "").lower() not in ("sqlancer", "sqlancer"):
            heard_as = surface
    author = AUTHOR.search(sentence)
    if author:
        matched.append("author")
        position = author.start() if position < 0 else position
    for technique_id, pattern in TECHNIQUE_PHRASES:
        found = pattern.search(sentence)
        if found:
            techniques.append(technique_id)
            position = found.start() if position < 0 else position
    # Acronyms only where the taxonomy says they are unambiguous, and only in a
    # sentence that is already about databases -- "TLP" and "CERT" are common
    # words elsewhere.
    for found in tax.find_techniques(sentence):
        if getattr(found, "ambiguous_acronym", False):
            continue
        if found.technique_id not in techniques:
            techniques.append(found.technique_id)
    matched.extend(t for t in techniques if t not in matched)
    return matched, heard_as, techniques, max(position, 0)


def _window(text: str, position: int) -> Tuple[str, int]:
    """``EXCERPT_RADIUS`` characters either side of a mention, on word bounds."""
    if len(text) <= 2 * EXCERPT_RADIUS:
        return text, position
    start = max(0, position - EXCERPT_RADIUS)
    end = min(len(text), position + EXCERPT_RADIUS)
    if start:
        space = text.find(" ", start)
        start = space + 1 if 0 <= space < position else start
    if end < len(text):
        space = text.rfind(" ", position, end)
        end = space if space > position else end
    return text[start:end].strip(), position - start


def findings_from_segments(segments: Sequence[dict], *, url: str,
                           tax=None) -> List[dict]:
    """Mentions found in a captured transcript, in the order they were said."""
    tax = tax or taxonomy.load()
    mentions: List[dict] = []
    sentences = _sentences(segments)
    for index, sentence in enumerate(sentences):
        matched, heard_as, techniques, position = _matches(sentence["text"], tax)
        if not matched:
            continue
        text = sentence["text"]
        following = sentences[index + 1] if index + 1 < len(sentences) else None
        # Same run means the two were spoken together, with no gap of silence
        # between them; ``_sentences`` gives every sentence in a run the same
        # offsets list.
        if (len(text) < SHORT_SENTENCE and following is not None
                and following["offsets"] is sentence["offsets"]):
            text = f"{text} {following['text']}"
        excerpt, position = _window(text, position)
        # The timestamp follows the excerpt: a long unpunctuated run can span
        # minutes, and a link to its first word would not land on the mention.
        offsets = sentence["offsets"]
        index = min(sentence["start"] + position, len(offsets) - 1)
        at = int(offsets[index]) if offsets else int(sentence["at_seconds"])
        mentions.append({
            "id": f"M{len(mentions) + 1}",
            "source": "captions",
            "at_seconds": at,
            "timestamp": timestamp_of(at),
            "url": deep_link(url, at),
            "matched": matched,
            "heard_as": heard_as,
            "excerpt": excerpt,
            "excerpt_is_verbatim": True,
            "technique_ids": techniques,
        })
    return mentions


# Slides punctuate loosely -- a heading, a dash, then bullets with no full
# stop -- so the excerpt runs to the next sentence end or to the end of the
# deck's text, and an em dash is content rather than a boundary.
SLIDE_SENTENCE = re.compile(r"[^.!?]{0,240}\bSQLancer\b[^.!?]{0,300}(?:[.!?]|$)")


def findings_from_slides(text: str, *, url: str, tax=None,
                         start_at: int = 0) -> List[dict]:
    """Mentions read off a slide deck's own text.

    Slides are written, not spoken, so the name is spelled correctly and the
    excerpt is quotable as it stands. They carry no clock, so a mention points
    at the deck rather than at a moment.
    """
    tax = tax or taxonomy.load()
    mentions: List[dict] = []
    seen = set()
    for match in SLIDE_SENTENCE.finditer(text or ""):
        excerpt = re.sub(r"\s+", " ", match.group(0)).strip()
        if not excerpt or excerpt in seen:
            continue
        seen.add(excerpt)
        matched, _, techniques, _position = _matches(excerpt, tax)
        if "sqlancer" not in matched:
            continue
        mentions.append({
            "id": f"M{start_at + len(mentions) + 1}",
            "source": "slides",
            "at_seconds": None,
            "timestamp": None,
            "url": url,
            "matched": matched,
            "heard_as": None,
            "excerpt": excerpt[:2000],
            "excerpt_is_verbatim": True,
            "technique_ids": techniques,
        })
    return mentions


def frame_name(key: str, seconds: Optional[int] = None) -> str:
    """``<video id>-<second>.jpg`` for a recording, ``<key>.jpg`` for a deck."""
    return f"{key}-{int(seconds)}.jpg" if seconds is not None else f"{key}.jpg"


def store_frame(source: str, key: str, seconds: Optional[int] = None,
                trim_bottom: int = 0) -> str:
    """File a captured frame, trimmed and shrunk; returns its site path.

    A browser screenshot arrives as a full-width PNG with the page's own
    letterboxing around the video. Both are cut back here so what lands in the
    repository is the frame itself, at a size that suits a page rather than a
    display.
    """
    from PIL import Image, ImageChops

    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    image = Image.open(source).convert("RGB")
    # A capture of the player often catches the page's own title line just
    # below it; ``trim_bottom`` cuts that off before anything else.
    if trim_bottom:
        image = image.crop((0, 0, image.width, image.height - trim_bottom))
    # Trim the black letterboxing: a difference against a black image leaves
    # only the picture, and its bounding box is where the frame really starts.
    background = Image.new("RGB", image.size, (0, 0, 0))
    box = ImageChops.difference(image, background).getbbox()
    if box:
        image = image.crop(box)
    if image.width > FRAME_MAX_WIDTH:
        height = round(image.height * FRAME_MAX_WIDTH / image.width)
        image = image.resize((FRAME_MAX_WIDTH, height), Image.LANCZOS)
    name = frame_name(key, seconds)
    image.save(FRAME_DIR / name, "JPEG", quality=FRAME_QUALITY, optimize=True)
    return f"{FRAME_URL_BASE}{name}"


def frame_for(key: Optional[str], seconds: Optional[int] = None
              ) -> Optional[str]:
    """The site path of a stored frame for this moment, if one was captured."""
    if not key:
        return None
    name = frame_name(key, seconds)
    return f"{FRAME_URL_BASE}{name}" if (FRAME_DIR / name).exists() else None


# ---------------------------------------------------------------------------
# Getting a transcript in by hand
#
# YouTube serves its caption tracks only to its own player, and for some talks
# it will not fill the transcript panel at all. Both dead ends are worked
# around the same way: somebody gets the text out however they can -- the
# panel's own copy button, yt-dlp, a downloaded .vtt -- and hands it here. The
# parsers below take whichever shape it arrives in, because the shape is not
# something the person supplying it should have to care about.
# ---------------------------------------------------------------------------

VTT_TIME = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*"
    r"(?:\d{1,2}):(?:\d{2}):(?:\d{2})[.,]\d{3}")
VTT_SHORT_TIME = re.compile(
    r"(\d{1,2}):(\d{2})[.,](\d{3})\s*-->\s*(?:\d{1,2}):(?:\d{2})[.,]\d{3}")

# Auto-captions carry per-word timing inside the cue, and a cue number and
# position line around it. None of that is speech.
CUE_MARKUP = re.compile(r"<[^>]*>")
CUE_SETTINGS = re.compile(r"\s(align|position|line|size):\S+")

# A transcript copied out of the panel is "12:34" followed by the line, either
# on one line or two.
PANEL_LINE = re.compile(r"^\s*((?:\d{1,2}:)?\d{1,2}:\d{2})\s*(.*)$")


def _seconds(*parts: str) -> int:
    values = [int(p) for p in parts]
    while len(values) < 3:
        values.insert(0, 0)
    return values[0] * 3600 + values[1] * 60 + values[2]


def segments_from_cues(text: str) -> List[dict]:
    """Segments from a WebVTT or SubRip file.

    Auto-generated captions repeat themselves: each cue restates the tail of
    the one before it so the words can roll up the screen. Emitting that as
    written would trip the matcher into reporting the same sentence several
    times, so a line already carried by the previous cue is dropped.
    """
    segments: List[dict] = []
    previous: List[str] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        match = VTT_TIME.search(block) or VTT_SHORT_TIME.search(block)
        if not match:
            continue
        groups = [g for g in match.groups() if g is not None]
        start = (_seconds(*groups[:3]) if len(groups) >= 4
                 else _seconds(*groups[:2]))
        lines = []
        for line in block.split("\n"):
            if VTT_TIME.search(line) or VTT_SHORT_TIME.search(line):
                continue
            line = CUE_SETTINGS.sub(" ", CUE_MARKUP.sub("", line))
            # Collapse spacing before comparing: stripping the per-word timing
            # tags leaves gaps where they were, and a line that differs from
            # the one before only by those gaps is the same line.
            line = re.sub(r"\s+", " ", line).strip()
            if line and not line.isdigit() and line.upper() != "WEBVTT":
                lines.append(line)
        fresh = [line for line in lines if line not in previous]
        previous = lines
        body = re.sub(r"\s+", " ", " ".join(fresh)).strip()
        if body:
            segments.append({"start_ms": start * 1000, "text": body})
    return segments


def segments_from_panel(text: str) -> List[dict]:
    """Segments from a transcript copied out of YouTube's own panel."""
    segments: List[dict] = []
    pending: Optional[int] = None
    for raw in text.replace("\r\n", "\n").split("\n"):
        match = PANEL_LINE.match(raw)
        if match:
            start = _seconds(*match.group(1).split(":"))
            body = match.group(2).strip()
            if body:
                segments.append({"start_ms": start * 1000, "text": body})
                pending = None
            else:
                pending = start
        elif raw.strip() and pending is not None:
            segments.append({"start_ms": pending * 1000,
                             "text": re.sub(r"\s+", " ", raw).strip()})
            pending = None
        elif raw.strip() and segments:
            segments[-1]["text"] += " " + re.sub(r"\s+", " ", raw).strip()
    return segments


def parse_transcript(text: str) -> List[dict]:
    """Segments from whichever of the accepted shapes ``text`` is in."""
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return json.loads(text).get("segments", [])
    if VTT_TIME.search(text) or VTT_SHORT_TIME.search(text):
        return segments_from_cues(text)
    return segments_from_panel(text)


def windows_around(segments: Sequence[dict], mentions: Sequence[dict],
                   radius: int = 2) -> List[dict]:
    """The segments near a mention, and only those.

    What belongs in the repository is the handful of sentences that mention
    SQLancer, not a transcription of somebody else's whole talk. A couple of
    segments either side is enough to see that the matcher read it right, and
    to catch the thought the speaker was in the middle of -- the name often
    lands a sentence before the point being made.
    """
    wanted = {int(m["at_seconds"]) for m in mentions}
    keep = set()
    for index, segment in enumerate(segments):
        if int(segment.get("start_ms") or 0) // 1000 in wanted:
            keep.update(range(max(0, index - radius),
                              min(len(segments), index + radius + 1)))
    return [segments[i] for i in sorted(keep)]


# How near a frame has to be to count as covering a moment. Slides change on
# their own schedule, and a capture a few seconds either side of a sentence is
# still the slide that was up while it was said.
FRAME_NEARBY_SECONDS = 20


def frames_wanted(talk: dict) -> List[dict]:
    """Moments in one talk that want a frame captured, and why.

    Not every mention needs a picture. Two do, and both are cases where the
    words on their own are weak:

    * the transcriber misheard the name, so the excerpt says "SQL lenser" and
      the slide is where it is spelled correctly;
    * nothing quotable was said at all, which is what a slide-only mention
      looks like from the transcript's side.

    A moment somebody looked at and found nothing on screen for carries
    ``frame_checked`` and is not asked for again.

    A talk whose transcript was read and mentions nothing is the strongest case
    of the second kind: the mention exists, and only a frame can show it.
    """
    have = [m.get("at_seconds") for m in talk.get("mentions") or []
            if m.get("source") == "frame" and m.get("at_seconds") is not None]
    wanted: List[dict] = []
    for mention in talk.get("mentions") or []:
        at = mention.get("at_seconds")
        if mention.get("source") == "frame" or at is None:
            continue
        if any(abs(at - other) <= FRAME_NEARBY_SECONDS for other in have):
            continue
        if mention.get("frame_checked"):
            continue
        if mention.get("heard_as"):
            why = (f"the transcript misheard the name as "
                   f"{mention['heard_as']!r} here")
        elif not mention.get("excerpt"):
            why = "nothing quotable was said here"
        else:
            continue
        wanted.append({
            "at_seconds": at,
            "timestamp": mention.get("timestamp") or timestamp_of(at),
            "url": mention.get("url"),
            "why": why,
            "mention_id": mention.get("id"),
        })

    read_and_silent = any(
        source.get("kind") == "captions" and source.get("status") == "extracted"
        and "not spoken" in (source.get("note") or "")
        for source in talk.get("sources") or [])
    if read_and_silent and not have:
        wanted.append({
            "at_seconds": None, "timestamp": None, "url": talk["url"],
            "why": ("the transcript was read and says nothing: the moment has "
                    "to come from watching it"),
            "mention_id": None,
        })
    return wanted


def load() -> dict:
    if TALKS_FILE.exists():
        return config.load_json(TALKS_FILE)
    return {"schema_version": "1.0.0", "talks": []}


def save(payload: dict) -> bool:
    payload["talks"] = sorted(
        payload.get("talks", []),
        key=lambda t: (-(t.get("year") or 0), (t.get("title") or "").lower(),
                       t["id"]))
    return config.write_json(TALKS_FILE, payload)


def merge(entry: dict, *, timestamp: Optional[str] = None) -> str:
    """File one talk, keeping anything a person recorded about it before.

    Returns ``"added"`` or ``"updated"``.
    """
    timestamp = timestamp or now()
    payload = load()
    talks = payload.setdefault("talks", [])
    existing = next((t for t in talks if t["id"] == entry["id"]), None)
    entry.setdefault("provenance", {})
    entry["provenance"].update({
        "collector": "talks",
        "collector_version": MODULE_VERSION,
        "source_url": entry["url"],
        "source_type": "video",
        "last_verified": timestamp,
    })
    if existing is None:
        entry["provenance"]["first_seen"] = timestamp
        talks.append(entry)
        outcome = "added"
    else:
        entry["provenance"]["first_seen"] = (
            existing.get("provenance", {}).get("first_seen") or timestamp)
        # A run with no transcript in hand must not erase the mentions of one
        # that was captured earlier.
        if not entry.get("mentions") and existing.get("mentions"):
            entry["mentions"] = existing["mentions"]
            entry["sources"] = existing.get("sources", entry["sources"])
        if entry.get("relationship") is None and existing.get("relationship"):
            entry["relationship"] = existing["relationship"]
        talks[talks.index(existing)] = entry
        outcome = "updated"
    save(payload)
    return outcome


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``list`` what is on file, or ``import`` a transcript somebody got out.

        python3 -m tools.impact.talks list
        python3 -m tools.impact.talks frames
        python3 -m tools.impact.talks import <file> <talk url>

    The file may be a .vtt or .srt, a transcript copied out of YouTube's panel,
    or the JSON shape this module writes. Only the windows around the mentions
    are kept; run ``collect --only talks`` afterwards to fold them in.
    """
    argv = list(argv if argv is not None else sys.argv[1:])
    command = argv[0] if argv else "list"

    if command == "list":
        for talk in load()["talks"]:
            status = "/".join(f"{s['kind']}:{s['status']}"
                              for s in talk["sources"])
            roles = ", ".join((talk.get("relationship") or {}).get("roles", []))
            print(f"{talk['id']}  [{status}]  {talk['title'][:70]}")
            print(f"    {len(talk['mentions'])} mentions"
                  f"{'  roles: ' + roles if roles else ''}")
        return 0

    if command == "frames":
        for talk in load()["talks"]:
            have = [m for m in talk["mentions"] if m["source"] == "frame"]
            wanted = frames_wanted(talk)
            if not have and not wanted:
                continue
            print(f"{talk['title'][:70]}")
            for mention in have:
                print(f"    have  {mention['timestamp']:>8}  "
                      f"{mention['image'].split('/')[-1]}")
            for entry in wanted:
                print(f"    want  {entry['timestamp'] or '       ?':>8}  "
                      f"{entry['why']}")
                print(f"                    {entry['url']}")
        print("\nCapturing one: assets/images/impact/talks/README.md")
        return 0

    if command == "import" and len(argv) > 2:
        path, url = argv[1], argv[2]
        video = video_id(url)
        if not video:
            print(f"not a YouTube url: {url}", file=sys.stderr)
            return 1
        with open(path, encoding="utf-8", errors="replace") as handle:
            segments = parse_transcript(handle.read())
        if not segments:
            print(f"no captions found in {path}", file=sys.stderr)
            return 1
        mentions = findings_from_segments(segments, url=url)
        kept = windows_around(segments, mentions)
        TRANSCRIPT_CACHE.mkdir(parents=True, exist_ok=True)
        target = TRANSCRIPT_CACHE / f"{video}.json"
        with target.open("w", encoding="utf-8") as handle:
            json.dump({
                "video_id": video,
                "url": deep_link(url, 0).split("?t=")[0].split("&t=")[0],
                "source": "supplied_transcript",
                "kind": "auto_captions",
                "retrieved_at": now(),
                "total_segments": len(segments),
                "total_characters": sum(len(s.get("text") or "")
                                        for s in segments),
                "note": ("Windows around the moments SQLancer is mentioned, cut "
                         "from a transcript supplied by hand. Not the whole "
                         "talk."),
                "segments": kept,
            }, handle, indent=2, sort_keys=True)
            handle.write("\n")
        print(f"{len(segments)} segments read, {len(mentions)} mention(s):")
        for mention in mentions:
            heard = f" (heard as {mention['heard_as']!r})" if mention["heard_as"] else ""
            print(f"  {mention['timestamp']:>8}  {', '.join(mention['matched'])}{heard}")
            print(f"            {mention['excerpt'][:100]}")
        print(f"wrote {target}")
        print("next: python3 -m tools.impact.run collect --only talks")
        return 0

    print(main.__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
