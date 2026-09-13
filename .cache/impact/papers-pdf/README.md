# Hand-supplied paper PDFs

Two thirds of the papers in this corpus are published by ACM, IEEE or Springer,
all of which refuse automated requests. For those, the only evidence the
pipeline can gather on its own is a citation index's two-sentence contexts.

Dropping a PDF here lets the same extraction run over the paper itself. Name the
file for the paper's identifier:

- by DOI, with `/` replaced by `_` — `10.1145_3764583.pdf`
- or by arXiv id — `2604.16373.pdf`
- or, for a paper the indexes give neither, by its Semantic Scholar id
  with an `s2_` prefix — `s2_c5c2bcb84acde8c7fe8964aba2d6d27f220ae80b.pdf`

The PDFs stay on your machine: everything in this folder is gitignored except
this file and WANTED.md, so a publisher copy cannot be committed by arriving
with an unexpected name. Nothing here is required: a missing PDF costs coverage
for that paper, never correctness. Claims drawn from a supplied PDF quote it
verbatim exactly as they would from a preprint, so they remain checkable by
anyone holding the same paper.

The routes that need no help: arXiv preprints, and PVLDB, which serves every
paper freely from vldb.org.
