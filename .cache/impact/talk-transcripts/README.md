# Captured talk transcripts

The input behind the caption mentions in `_data/impact/talks.json`. Committed,
small and few on purpose: keeping them makes that file reproducible, and keeping
only windows around the moments SQLancer comes up avoids storing a transcription
of somebody else's whole talk.

## Getting one

YouTube serves its caption tracks only to its own player, and for some talks it
will not even fill its own transcript panel. So this step is done by a person.
Any of these works — the importer takes whichever shape the text arrives in:

1. **The transcript panel.** Open the talk, expand the description, click
   *Show transcript*, then use the panel's ⋮ menu (*Toggle timestamps* off is
   not needed) — select the transcript and copy it. Save it to a file.
2. **A caption file.** `yt-dlp --write-auto-subs --sub-lang en --skip-download
   --sub-format vtt <url>` leaves a `.vtt` beside itself. A `.srt` works too.
3. **The JSON shape this directory holds**, if you already have segments.

Then:

    python3 -m tools.impact.talks import <file> <talk url>
    python3 -m tools.impact.run collect --only talks

The first command prints every mention it found, with its timestamp and how the
transcriber heard the name, and writes the windows here. The second folds them
into the dataset. Rolling repetition in auto-captions — each cue restating the
tail of the one before — is dropped on the way in.

## What the text is worth

A machine transcription, wrong often enough to matter: it renders "SQLancer" as
"SQL answer", "SQL lenser" and "SQLanswer" in the talks collected so far. The
records built from it carry the misheard form and a link to the second it was
said, and the page says plainly that the words are the transcriber's rather than
the speaker's. Where a mention is on a slide and never spoken, no transcript
reaches it at all — that is what the frames in `assets/images/impact/talks/` are
for.
