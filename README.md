# sqlancer.github.io

The website for [SQLancer](https://github.com/sqlancer/sqlancer), built with
Jekyll and the [Minimal Mistakes](https://github.com/mmistakes/minimal-mistakes)
remote theme, and published through GitHub Pages.

## Running it locally

```sh
bundle install
bundle exec jekyll serve
```

## The impact section

[`/impact/`](_pages/impact.html) presents what SQLancer has found and what has
been built on it: bugs by database system and over time, which database system
projects use SQLancer, and the research that reuses, extends or compares against
it. Every figure on that page -- and the statistics on the homepage -- is
computed from structured, evidence-backed records rather than written by hand.

- **The records** live in [`_data/impact/`](_data/impact/), one JSON file per
  kind of claim, each validated against a schema in
  [`schemas/impact/`](schemas/impact/). Every record carries the primary source
  that justifies it.
- **The policy** that governs what is admitted is in
  [`_data/impact/policy.json`](_data/impact/policy.json) and is rendered on the
  impact page, so the rules the collectors enforce and the rules the page
  describes cannot drift apart.
- **The pipeline** that proposes changes to the records is in
  [`tools/impact/`](tools/impact/) -- see
  [its README](tools/impact/README.md) for how it works, how to run it, and what
  it enforces. It runs weekly through
  [`.github/workflows/impact.yml`](.github/workflows/impact.yml) and opens a pull
  request; it never writes to `main`.

```sh
make validate   # schemas and cross-file integrity
make build      # regenerate derived statistics and charts, then validate
make test       # the impact test suite
```

`stats.json` and the charts are generated: CI fails if they do not match the
records they are derived from.

## Corrections

If a bug, paper, integration or resource is missing or wrongly attributed,
please [open an issue](https://github.com/sqlancer/sqlancer.github.io/issues/new).
The dataset is meant to be corrected in public.
