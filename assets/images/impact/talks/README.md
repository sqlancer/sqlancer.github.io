# Frames from talks

One picture per moment where a talk shows SQLancer on a slide. Committed,
because for some talks that slide is the whole of the evidence: the name is
never spoken, so no transcript reaches it.

## Which moments want one

Ask the dataset rather than deciding by eye:

    python3 -m tools.impact.talks frames

It lists what is already captured and what is missing, and says why each missing
one matters. Two cases qualify, both where the words alone are weak:

* **the transcriber misheard the name** — the excerpt says "SQL lenser" and the
  slide is where it is spelled correctly;
* **nothing quotable was said** — which is what a slide-only mention looks like
  from the transcript's side.

A talk whose transcript was read and mentions nothing is the strongest case of
the second kind. Miryung Kim's keynote is one: 439 segments, not a word, and the
name plainly there on a table of fuzzers.

## Capturing one

A browser job. The trap is quality, not aim — a player drops to a small
rendition whenever it seeks and upgrades only a few seconds later, so a frame
grabbed as the seek lands is a blur where a table should be.

1. Open the talk at the moment: `...watch?v=<id>&t=<seconds>s`.
2. Pin the quality before playing:
   `document.querySelector('#movie_player').setPlaybackQualityRange('hd1080', 'hd1080')`.
3. Play, seek to the second, wait a few seconds for the stream to settle, then
   pause. Check `video.videoWidth` — 1920 means the good rendition is up.
4. Hide the player's own furniture, which would otherwise be in the picture:
   `.ytp-chrome-bottom`, `.ytp-gradient-bottom`, `.ytp-gradient-top`,
   `.ytp-chrome-top`, `.ytp-large-play-button`, `.ytp-pause-overlay`.
5. Screenshot the `<video>` element's rectangle. Take a plain screenshot first
   and check the page has settled — the rectangle moves while the player
   re-lays-out, and a stale one captures the wrong crop.
6. **Read the slide back off the result.** If its text cannot be read, the
   capture has failed, whatever it looks like at a glance.

Then file it, which trims the letterboxing, shrinks it and names it:

    python3 -c "from tools.impact import talks; \
      print(talks.store_frame('<screenshot>', '<video id>', <seconds>))"

Finally add the moment to the talk's entry in
`tools/impact/collectors/talks.py` under `frames`, with a note saying what is
on the slide, and re-run `collect --only talks`.

## What a frame may say

Nothing, in words. A frame never carries an excerpt: every other excerpt in this
dataset is a substring of something fetched and a check can say so, while text
read off a picture is a person's reading. The note describes what is on the
slide; the picture is the evidence. The validator enforces this.
