# Collector caches

Everything here is an optimisation. The pipeline produces the same records with
this directory empty as with it warm, and a test asserts that.

What is committed is decided by one question: could a fresh checkout get this
back by itself? A fetched page could, so it is not committed. An answer that
cost tokens, a transcript a person had to read out of a browser, and the record
of how far each source has been scanned could not.

| Path | Committed | What it holds |
| --- | --- | --- |
| `state.json` | yes | Last successful scan and last full reconciliation per source. This is what makes weekly runs incremental. |
| `classifications/` | yes | Every classifier answer, including the negative and uncertain ones, keyed so that a change to the evidence, policy, taxonomy, prompt version or model forces a fresh answer and nothing else does. Only the answer and a hash of the evidence are stored, never the evidence itself. |
| `talk-transcripts/` | yes | Windows of caption text around the moments a talk mentions SQLancer. Committed because a person had to obtain them by hand -- YouTube serves caption tracks only to its own player -- so no rerun can recover them. See that directory's README. |
| `derived/` | yes | Deterministic extraction results, keyed by source id + content hash + extractor version. Empty so far: nothing has cost enough to keep. |
| `http/`, `github/`, `papers/`, `artifacts/` | no | Fetched bodies with their ETag / Last-Modified / upstream `updated_at` validators. Large, re-fetchable, and restored from the GitHub Actions cache instead. |
| `papers-pdf/`, `papers-text/` | no | Supplied PDFs and the text extracted from them. Deliberately never committed; only the two Markdown files explaining the directory are. |

Committing the classification cache is what keeps the weekly token cost
proportional to genuinely new evidence: a paper that turned out to only cite
SQLancer, or an issue that turned out to be unrelated, is never asked about
again.
