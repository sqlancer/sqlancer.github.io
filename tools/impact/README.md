# The impact pipeline

This directory collects the evidence behind everything shown on
[`/impact/`](../../_pages/impact.html). The authoritative records live in
[`_data/impact/`](../../_data/impact/); everything here exists to propose changes
to them and to derive the figures and charts the website renders.

## The shape of it

```
deterministic collectors   -- GitHub, scholarly APIs, the curated bug repository
        |
        v
candidate records          -- deduplicated on stable ids and canonical URLs
        |
        v
cached evidence            -- fetched once, revalidated rather than re-fetched
        |
        v
semantic classification    -- only where judgement is genuinely required
        |
        v
schema validation          -- JSON Schema plus cross-file integrity checks
        |
        v
proposed data changes      -- a pull request a human reviews
```

Discovery is deterministic throughout. A language model is used only to answer
bounded questions about material the collectors already fetched, and it can
neither browse nor introduce a record on its own. There is no agent loop.

## Running it

```sh
make validate   # schemas + cross-file checks
make build      # regenerate stats.json and the charts, then validate
make test       # the test suite (no network, no API key)
make collect    # incremental collection run
make collect-full   # periodic full reconciliation
```

Or directly:

```sh
python3 -m tools.impact.run collect --only papers --dry-run
python3 -m tools.impact.run report --input .cache/impact/last-run.json
```

`--dry-run` never calls the classifier; candidates that would need one are
reported as needing human judgement instead.

### Environment

| Variable | Effect if unset |
| --- | --- |
| `GITHUB_TOKEN` | GitHub searches run unauthenticated and are throttled hard |
| `SEMANTIC_SCHOLAR_API_KEY` | Semantic Scholar is used on the shared pool, with long backoff; OpenAlex still covers discovery |
| `ANTHROPIC_API_KEY` | Ambiguous candidates are reported for human judgement rather than classified |
| `IMPACT_OFFLINE=1` | No network at all; everything is served from the cache |
| `IMPACT_CACHE_DIR` | Defaults to `.cache/impact` |

Every one of these is optional. The pipeline degrades rather than fails: a
missing key costs coverage that week, never correctness.

## Modules

| File | Responsibility |
| --- | --- |
| `config.py` | Paths, versions, deterministic JSON writing |
| `util.py` | Hashing, stable ids, URL/DOI normalisation, the verbatim-excerpt check |
| `cache.py` | The three cache layers and the incremental discovery state |
| `http.py` | Conditional fetching, backoff, offline mode |
| `github.py` | The GitHub REST endpoints the collectors need |
| `scholarly.py` | Semantic Scholar and OpenAlex clients |
| `taxonomy.py` | Reads `techniques.json`; technique matching, including acronym disambiguation |
| `dataset.py` | Loading, merging and idempotent writing of the records |
| `collectors/artifacts.py` | Finds a paper's artifact and inspects it for SQLancer markers; this is what decides `uses_infrastructure` |
| `collectors/` | One module per source: `sqlancer_bugs` (the curated bug repository), `github_bugs` (issue search), `nus_test` (the TEST lab bug list), `papers`, `artifacts`, `adoption`, `resources`, `talks`, `dbms_registry` |
| `talks.py` | Reads a talk four ways -- slides, a frame from the recording, captions, or a viewer's account -- and keeps them apart, because an automatic caption is not a quotation and a picture is not text |
| `repos.py` | Where a project's issues live now: rewrites an address under a former organisation, so a bug filed at `cwida/duckdb` is the same record as one found at `duckdb/duckdb` |
| `classify/` | The bounded questions and the cached, verified classifier |
| `stats.py` | Derives `stats.json` from the records |
| `plots.py` | Renders the inline SVG charts |
| `validate.py` | JSON Schema plus the checks a schema cannot express |
| `pr.py` | Renders the pull-request body |
| `run.py` | Command-line entry point |
| `seed/` | Scripts that bootstrapped the dataset |

## The three caches

Caching is an optimisation and never a correctness dependency; a test asserts
that a cold cache produces the same records as a warm one.

1. **Raw source** (`.cache/impact/{http,github,papers,artifacts}`) — fetched
   bodies with the validators needed to tell whether a source changed: ETag,
   Last-Modified, the upstream `updated_at`, and a content hash. An unchanged
   GitHub thread costs no request at all, because its `updated_at` is enough.
2. **Derived** (`.cache/impact/derived`) — deterministic extraction keyed by a
   stable source id plus the hash of the source content and the extractor
   version.
3. **Classifications** (`.cache/impact/classifications`) — every answer the
   classifier has given, including the negative and uncertain ones. The key
   covers the source id, the hash of the exact evidence sent, the prompt
   version, the policy version, the taxonomy version and the model, so a change
   to any of those forces a fresh answer and nothing else does.

`.cache/impact/state.json` records the last successful scan and the last full
reconciliation per source, which is what makes weekly runs incremental.

## What is enforced, not trusted

- **Excerpts are verbatim.** Every stored excerpt is checked to be a literal
  substring of the fetched source. An answer from the classifier whose excerpts
  fail that check loses them, and a `yes` with no surviving excerpt is
  downgraded to `uncertain`.
- **Uncertain is never promoted.** Only `yes` produces a published claim.
- **Ambiguous acronyms need context to be *accepted*, not to be *considered*.**
  A bare `TLP` or `PQS` never establishes an attribution on its own. Inside a
  registered database system's own repository it is still enough to make the
  issue a candidate, because the repository is the context -- the deterministic
  layer's job is to generate candidates for the classifier, not to decide in
  its place.
- **Excluded techniques stay excluded.** EET and DQE did not originate in
  SQLancer -- both were published elsewhere and contributed afterwards -- so
  bugs attributed to them are not counted and their publications are not
  citation seeds, even though SQLancer ships an oracle for each.
- **Manual curation is sticky.** A relationship a person decided is never
  overwritten by a later automated run.
- **A list is not an attribution.** The TEST lab publishes the lab's bugs, not
  SQLancer's. Every candidate from it is taken back to its own bug report and
  put through the same rules as any other; being on the list counts for
  nothing.
- **Derived output cannot go stale.** The tests fail if `stats.json` or any
  chart differs from what the records produce.

## Marking a paper as the project's own

Papers by the project are excluded from counts of external work building on
SQLancer. Three things mark one, all configured in `seed/techniques_seed.py`:

- `PROJECT_AUTHORS` — a paper co-authored by any of these is ours. Names are
  matched as whole words against each author, so both `Manuel Rigger` and
  `M. Rigger` hit while a longer surname does not.
- The technique origin papers and tool papers already in the taxonomy, matched
  on DOI, arXiv id, Semantic Scholar id or exact title.
- `PROJECT_PUBLICATIONS` — anything else the project states is its own, with a
  reason per entry. Use this for a project paper none of the authors above is
  on, or to record a specific decision durably.

## Adding a technique or a database system

Both come from data, not from code. Edit `seed/techniques_seed.py` and run it to
regenerate `_data/impact/techniques.json`; bump `taxonomy_version` if the change
affects how anything should be classified, which invalidates the affected cached
answers. Database systems are discovered from the SQLancer repository's provider
directories, so a newly supported system appears on the next run; presentation
metadata for it lives in `collectors/dbms_registry.py`.
