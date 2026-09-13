"""Behavioural tests for the pipeline itself.

These use temporary directories and stub sources rather than the network, so
they run in CI in under a second. They cover the properties the weekly run
depends on: repeated collection is idempotent, an empty cache changes results
not at all, unchanged evidence never reaches the model twice, and the classifier
cannot introduce an excerpt that is not in the source.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.impact import config, plots, stats as stats_module, taxonomy
from tools.impact.cache import Caches, ClassificationCache, DerivedCache, RawCache, State
from tools.impact.classify import Classifier
from tools.impact.collectors import sqlancer_bugs
from tools.impact.collectors.adoption import _best_excerpt
from tools.impact.collectors import nus_test
from tools.impact.collectors.github_bugs import deterministic_attribution
from tools.impact.collectors.papers import (Candidate, build_record,
                                            is_project_publication,
                                            looks_like_sqlancer_publication,
                                            normalise_openalex, paper_identity)
from tools.impact.dataset import Dataset, substantively_equal
from tools.impact.util import verbatim_excerpt


class FakeRawFile:
    """Minimal stand-in for the GitHub client used by the curated importer."""

    def __init__(self, files):
        self.files = files
        self.reads = 0

    def raw_file(self, owner, repo, path, ref="HEAD", **kwargs):
        self.reads += 1
        return self.files.get(path), {"status": 200}


CURATED = [
    {
        "date": "28/5/2019", "dbms": "SQLite", "oracle": "NoREC",
        "reporter": "Manuel Rigger", "status": "fixed",
        "title": "Wrong result for a simple filter",
        "links": {"bugreport": "https://sqlite.org/forum/forumpost/aaaa"},
        "test": ["CREATE TABLE t0(c0);"],
    },
    {
        "date": "01/6/2020", "dbms": "DuckDB", "oracle": "crash",
        "reporter": "Manuel Rigger", "status": "closed (not a bug)",
        "title": "Not actually a bug",
        "links": {"bugreport": "https://github.com/duckdb/duckdb/issues/1"},
        "test": ["SELECT 1;"],
    },
]

README = ("This project aims to provide a basis to study bugs in DBMS. To this "
          "end, the repository stores a list of bugs found by SQLancer with "
          "additional metadata.\n")


def fake_github():
    return FakeRawFile({
        "bugs.json": json.dumps(CURATED, indent=4) + "\n",
        "README.md": README,
    })


class CuratedImportTest(unittest.TestCase):
    def setUp(self):
        self.records = sqlancer_bugs.collect(fake_github(),
                                             timestamp="2026-01-01T00:00:00Z")

    def test_imports_every_entry(self):
        self.assertEqual(2, len(self.records))

    def test_technique_comes_from_the_taxonomy_not_a_hardcoded_map(self):
        by_title = {r["title"]: r for r in self.records}
        self.assertEqual("norec",
                         by_title["Wrong result for a simple filter"]["technique"])
        self.assertIsNone(by_title["Not actually a bug"]["technique"])

    def test_rejected_reports_are_kept_but_not_counted(self):
        by_title = {r["title"]: r for r in self.records}
        self.assertFalse(by_title["Not actually a bug"]["status_is_true_positive"])
        self.assertEqual("closed_not_a_bug", by_title["Not actually a bug"]["status"])

    def test_evidence_excerpts_are_verbatim_in_the_source(self):
        source = json.dumps(CURATED, indent=4) + "\n"
        for record in self.records:
            for item in record["attribution"]["evidence"]:
                excerpt = item.get("excerpt")
                if excerpt is None:
                    continue
                self.assertTrue(
                    verbatim_excerpt(source, excerpt)
                    or verbatim_excerpt(README, excerpt),
                    f"excerpt not found verbatim: {excerpt!r}")

    def test_per_entry_hashes_are_independent(self):
        """Adding an unrelated upstream entry must not touch existing records."""
        extended = CURATED + [{
            "date": "02/6/2020", "dbms": "DuckDB", "oracle": "TLP (WHERE)",
            "reporter": "Manuel Rigger", "status": "fixed", "title": "Another",
            "links": {"bugreport": "https://github.com/duckdb/duckdb/issues/2"},
            "test": ["SELECT 2;"],
        }]
        gh = FakeRawFile({"bugs.json": json.dumps(extended, indent=4) + "\n",
                          "README.md": README})
        after = sqlancer_bugs.collect(gh, timestamp="2026-01-01T00:00:00Z")
        before_by_id = {r["id"]: r for r in self.records}
        for record in after:
            if record["id"] in before_by_id:
                self.assertEqual(before_by_id[record["id"]]["provenance"]
                                 ["content_sha256"],
                                 record["provenance"]["content_sha256"])


class IdempotencyTest(unittest.TestCase):
    """Collecting twice with no new evidence must change nothing on disk."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.files = {"bugs": self.tmp / "bugs.json"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dataset(self):
        dataset = Dataset(self.files)
        dataset.ensure("bugs", {"schema_version": "1.0.0",
                                "policy_version": "test"})
        return dataset

    def test_second_run_writes_no_bytes(self):
        first = sqlancer_bugs.collect(fake_github(),
                                      timestamp="2026-01-01T00:00:00Z")
        dataset = self._dataset()
        dataset.merge("bugs", first, timestamp="2026-01-01T00:00:00Z")
        self.assertTrue(dataset.save(["bugs"])["bugs"])
        before = self.files["bugs"].read_text()

        # A later run: same evidence, different wall-clock timestamp.
        second = sqlancer_bugs.collect(fake_github(),
                                       timestamp="2026-02-02T00:00:00Z")
        dataset = self._dataset()
        report = dataset.merge("bugs", second, timestamp="2026-02-02T00:00:00Z")
        changed = dataset.save(["bugs"])

        self.assertEqual([], report.added)
        self.assertEqual([], report.updated)
        self.assertEqual(2, report.unchanged)
        self.assertFalse(changed["bugs"])
        self.assertEqual(before, self.files["bugs"].read_text())

    def test_first_seen_survives_an_update(self):
        first = sqlancer_bugs.collect(fake_github(),
                                      timestamp="2026-01-01T00:00:00Z")
        dataset = self._dataset()
        dataset.merge("bugs", first, timestamp="2026-01-01T00:00:00Z")
        dataset.save(["bugs"])

        changed = json.loads(json.dumps(first))
        changed[0]["status"] = "verified"
        dataset = self._dataset()
        dataset.merge("bugs", changed, timestamp="2026-03-03T00:00:00Z")
        stored = {r["id"]: r for r in dataset.records("bugs")}
        updated = stored[changed[0]["id"]]
        self.assertEqual("verified", updated["status"])
        self.assertEqual("2026-01-01T00:00:00Z",
                         updated["provenance"]["first_seen"])
        self.assertEqual("2026-03-03T00:00:00Z",
                         updated["provenance"]["last_verified"])

    def test_timestamps_alone_are_not_a_change(self):
        left = {"id": "bug:x:1", "last_verified": "2026-01-01T00:00:00Z", "a": 1}
        right = {"id": "bug:x:1", "last_verified": "2026-09-09T00:00:00Z", "a": 1}
        self.assertTrue(substantively_equal(left, right))
        right["a"] = 2
        self.assertFalse(substantively_equal(left, right))


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cold_cache_produces_the_same_records(self):
        """Caching is an optimisation; correctness must not depend on it."""
        warm = sqlancer_bugs.collect(fake_github(),
                                     timestamp="2026-01-01T00:00:00Z")
        cold = sqlancer_bugs.collect(fake_github(),
                                     timestamp="2026-01-01T00:00:00Z")
        self.assertEqual(warm, cold)

    def test_derived_cache_only_recomputes_on_a_content_change(self):
        cache = DerivedCache(self.tmp / "derived")
        calls = []

        def produce():
            calls.append(1)
            return {"value": len(calls)}

        first = cache.compute("src", "sha256:aaa", "v1", produce)
        second = cache.compute("src", "sha256:aaa", "v1", produce)
        self.assertEqual(first, second)
        self.assertEqual(1, len(calls))

        cache.compute("src", "sha256:bbb", "v1", produce)
        self.assertEqual(2, len(calls))
        cache.compute("src", "sha256:aaa", "v2", produce)
        self.assertEqual(3, len(calls))

    def test_raw_cache_reuses_an_unchanged_upstream_timestamp(self):
        cache = RawCache(self.tmp / "raw")
        cache.store("issue:1", body="hello", url="https://example.org/1",
                    upstream_updated_at="2026-01-01T00:00:00Z")
        self.assertTrue(cache.is_fresh(
            "issue:1", upstream_updated_at="2026-01-01T00:00:00Z"))
        self.assertFalse(cache.is_fresh(
            "issue:1", upstream_updated_at="2026-02-01T00:00:00Z"))

    def test_state_tracks_incremental_and_full_scans(self):
        state = State(self.tmp / "state.json")
        self.assertIsNone(state.last_scan("github"))
        state.record_scan("github")
        state.save()
        self.assertIsNotNone(State(self.tmp / "state.json").last_scan("github"))
        self.assertIsNone(State(self.tmp / "state.json").last_reconcile("github"))
        state.record_scan("github", full=True)
        state.save()
        self.assertIsNotNone(State(self.tmp / "state.json").last_reconcile("github"))


class StubClassifierClient:
    """Records every request so the tests can count real model calls."""

    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    class _Block:
        def __init__(self, text):
            self.type = "text"
            self.text = text

    class _Response:
        def __init__(self, blocks):
            self.content = blocks
            self.stop_reason = "end_turn"

    class _Messages:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kwargs):
            self.outer.requests.append(kwargs)
            return StubClassifierClient._Response(
                [StubClassifierClient._Block(json.dumps(self.outer.payload))])

    @property
    def messages(self):
        return StubClassifierClient._Messages(self)


class ClassifierTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cache = ClassificationCache(
            self.tmp / "classifications", model="test-model",
            policy_version="policy-v1", taxonomy_version="taxonomy-v1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _classifier(self, payload):
        """A classifier wired to a stub client rather than the real API.

        Injecting the client is enough: `available` reports True once a client
        is present and a key is configured, so nothing about the class needs to
        be monkeypatched.
        """
        classifier = Classifier(self.cache, model="test-model", api_key="test")
        classifier._client = StubClassifierClient(payload)
        return classifier

    def _classify(self, classifier, text="found by SQLancer while testing"):
        return classifier.classify(
            "bug_attribution", source_id="github:x/y#1", source_text=text,
            source_label="test", technique_names=["NoREC"],
            finder_names=["SQLancer"], excluded_names=["EET"])

    def test_unchanged_evidence_is_not_sent_to_the_model_twice(self):
        payload = {"answer": "yes", "finder": "SQLancer", "technique": None,
                   "excerpts": ["found by SQLancer"], "reason": "states it"}
        classifier = self._classifier(payload)
        self.assertTrue(classifier.available)
        first = self._classify(classifier)
        second = self._classify(classifier)
        self.assertIsNotNone(first)
        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertEqual(1, len(classifier._client.requests))
        self.assertEqual(1, classifier.calls_made)
        self.assertEqual(1, classifier.cache_hits)

    def test_negative_answers_are_cached_too(self):
        payload = {"answer": "no", "finder": None, "technique": None,
                   "excerpts": [], "reason": "unrelated"}
        classifier = self._classifier(payload)
        self._classify(classifier)
        second = self._classify(classifier)
        self.assertTrue(second.from_cache)
        self.assertEqual("no", second.answer)
        self.assertEqual(1, len(classifier._client.requests))

    def test_a_changed_prompt_version_forces_a_fresh_answer(self):
        key_a = self.cache.key("src", "sha256:aaa", "bug-attribution-v1")
        key_b = self.cache.key("src", "sha256:aaa", "bug-attribution-v2")
        self.assertNotEqual(key_a, key_b)

    def test_a_changed_policy_version_forces_a_fresh_answer(self):
        other = ClassificationCache(self.tmp / "c2", model="test-model",
                                    policy_version="policy-v2",
                                    taxonomy_version="taxonomy-v1")
        self.assertNotEqual(self.cache.key("s", "h", "v"),
                            other.key("s", "h", "v"))

    def test_an_excerpt_not_in_the_source_is_discarded(self):
        classifier = Classifier(self.cache, model="test-model", api_key="test")
        cleaned = classifier._sanitise(
            {"answer": "yes", "excerpts": ["a sentence that was never there"],
             "reason": "made up"},
            "the real source text says something else entirely")
        self.assertEqual([], cleaned["excerpts"])
        self.assertEqual("uncertain", cleaned["answer"],
                         "a positive answer with no usable evidence must not stand")

    def test_a_verbatim_excerpt_is_kept(self):
        classifier = Classifier(self.cache, model="test-model", api_key="test")
        cleaned = classifier._sanitise(
            {"answer": "yes", "excerpts": ["found by SQLancer"], "reason": "ok"},
            "This bug was found by SQLancer during a testing campaign.")
        self.assertEqual(["found by SQLancer"], cleaned["excerpts"])
        self.assertEqual("yes", cleaned["answer"])

    def test_an_unknown_answer_becomes_uncertain(self):
        classifier = Classifier(self.cache, model="test-model", api_key="test")
        cleaned = classifier._sanitise(
            {"answer": "probably", "excerpts": [], "reason": ""}, "text")
        self.assertEqual("uncertain", cleaned["answer"])

    def test_no_api_key_yields_no_classification_rather_than_a_guess(self):
        classifier = Classifier(self.cache, model="test-model", api_key=None)
        self.assertFalse(classifier.available)
        self.assertIsNone(self._classify(classifier))
        self.assertEqual(1, classifier.stats()["skipped_no_key"])


class AttributionRuleTest(unittest.TestCase):
    def setUp(self):
        self.tax = taxonomy.load()

    def test_a_discovery_statement_is_accepted(self):
        result = deterministic_attribution(
            "This wrong result was found by SQLancer while testing DuckDB.",
            self.tax)
        self.assertIsNotNone(result)
        self.assertEqual("sqlancer", result[2])

    def test_an_oracle_attribution_is_accepted_without_the_word_sqlancer(self):
        result = deterministic_attribution(
            "The NoREC oracle reports a mismatch between the two queries.",
            self.tax)
        self.assertIsNotNone(result)
        self.assertEqual("norec", result[1])

    def test_a_feature_request_mentioning_sqlancer_is_not_a_bug(self):
        self.assertIsNone(deterministic_attribution(
            "SQLancer does not support window functions yet, please add them.",
            self.tax))

    def test_an_excluded_technique_is_never_attributed(self):
        self.assertIsNone(deterministic_attribution(
            "This bug was found using EET on the query.", self.tax))

    def test_an_ambiguous_acronym_needs_database_context(self):
        self.assertIsNone(deterministic_attribution(
            "Please raise the TLP power management timeout on my laptop.",
            self.tax))
        self.assertIsNotNone(deterministic_attribution(
            "Detected by TLP: the SELECT query returns a wrong result set.",
            self.tax))


class TaxonomyTest(unittest.TestCase):
    def setUp(self):
        self.tax = taxonomy.load()

    def test_umbrella_tools_are_distinguished(self):
        self.assertEqual(["sqlancer_pp"],
                         self.tax.mentions_finder("we ran SQLancer++"))
        self.assertEqual(["sqlancer"], self.tax.mentions_finder("we ran SQLancer"))
        self.assertEqual(["shqvel"], self.tax.mentions_finder("ShQveL synthesised"))

    def test_oracle_labels_map_to_technique_ids(self):
        self.assertEqual("tlp",
                         self.tax.technique_from_oracle_label("TLP (aggregate)"))
        self.assertIsNone(self.tax.technique_from_oracle_label("crash"))

    def test_every_seed_publication_is_reachable(self):
        seeds = self.tax.citation_seeds()
        self.assertTrue(seeds)
        for seed in seeds:
            self.assertTrue(seed.get("doi") or seed.get("arxiv_id")
                            or seed.get("s2_paper_id") or seed.get("title"))


class PaperIdentityTest(unittest.TestCase):
    def test_an_arxiv_doi_collapses_onto_the_arxiv_id(self):
        """The same preprint reached through either index must be one record."""
        from_openalex, _, _ = paper_identity(
            {"DOI": "https://doi.org/10.48550/arxiv.2505.02012"}, "fallback")
        from_s2, _, _ = paper_identity({"ArXiv": "2505.02012"}, "fallback")
        self.assertEqual(from_openalex, from_s2)

    def test_a_real_doi_wins_over_an_arxiv_id(self):
        record_id, doi, arxiv = paper_identity(
            {"DOI": "10.1145/3428279", "ArXiv": "1234.5678"}, "fallback")
        self.assertEqual("paper:doi:10.1145/3428279", record_id)
        self.assertEqual("1234.5678", arxiv)

    def test_openalex_works_are_projected_onto_a_common_shape(self):
        work = normalise_openalex({
            "id": "https://openalex.org/W123", "doi": "https://doi.org/10.1/x",
            "display_name": "A paper", "publication_year": 2024,
            "authorships": [{"author": {"display_name": "A. Person"}}],
            "primary_location": {"source": {"display_name": "A venue"}},
        })
        self.assertEqual("A paper", work["title"])
        self.assertEqual("W123", work["openalexId"])
        self.assertEqual("A venue", work["venue"])

    def test_an_unclassified_paper_is_never_counted_as_building_on_sqlancer(self):
        candidate = Candidate({"paperId": "a" * 40, "title": "T", "year": 2024,
                               "externalIds": {"DOI": "10.1/y"}})
        candidate.add_edge("tlp", {"contexts": []}, "semantic_scholar")
        record = build_record(candidate, timestamp="2026-01-01T00:00:00Z")
        self.assertEqual("yes", record["relationships"]["references"]["value"])
        for key in ("uses_infrastructure", "extends_technique", "compares_with"):
            self.assertEqual("insufficient_evidence",
                             record["relationships"][key]["value"])


class ProjectPublicationTest(unittest.TestCase):
    """Which papers are the project's own is stated, not inferred."""

    def setUp(self):
        self.tax = taxonomy.load()

    def _candidate(self, **external):
        return Candidate({"paperId": None, "openalexId": "W1",
                          "externalIds": external, "title": "A paper",
                          "year": 2024})

    def test_a_listed_publication_is_recognised_by_doi(self):
        for entry in self.tax.project_publications:
            if not entry.get("doi"):
                continue
            self.assertTrue(
                is_project_publication(self._candidate(DOI=entry["doi"]), self.tax),
                f"{entry['title']} is listed but was not recognised")

    def test_technique_and_tool_papers_are_still_recognised(self):
        seeds = self.tax.citation_seeds()
        checked = 0
        for seed in seeds:
            if not seed.get("doi"):
                continue
            checked += 1
            self.assertTrue(
                is_project_publication(self._candidate(DOI=seed["doi"]), self.tax))
        self.assertGreater(checked, 0)

    def test_an_unrelated_paper_is_not_a_project_publication(self):
        self.assertFalse(
            is_project_publication(self._candidate(DOI="10.1145/9999999"),
                                   self.tax))

    def test_a_paper_by_a_project_author_is_ours(self):
        """Co-authorship settles it, whatever the paper is about."""
        candidate = Candidate({
            "paperId": None, "openalexId": "W1", "externalIds": {},
            "title": "Something entirely unrelated to databases", "year": 2025,
            "authors": [{"name": "Manuel Rigger"}, {"name": "A. Person"}],
        })
        self.assertTrue(looks_like_sqlancer_publication(candidate))

    def test_an_initialled_project_author_still_matches(self):
        candidate = Candidate({
            "paperId": None, "openalexId": "W2", "externalIds": {},
            "title": "A paper", "year": 2025,
            "authors": [{"name": "M. Rigger"}],
        })
        self.assertTrue(looks_like_sqlancer_publication(candidate))

    def test_a_longer_surname_is_not_a_false_match(self):
        for name in ("Riggers", "Triggerman", "Rigger-Smith Industries"):
            self.assertFalse(self.tax.is_project_author(name), name)

    def test_a_paper_without_a_project_author_stays_external(self):
        candidate = Candidate({
            "paperId": None, "openalexId": "W3",
            "externalIds": {"DOI": "10.1145/9999999"},
            "title": "An independent paper", "year": 2025,
            "authors": [{"name": "Someone Else"}],
        })
        self.assertFalse(looks_like_sqlancer_publication(candidate))

    def test_project_authors_are_configured_not_hardcoded(self):
        self.assertTrue(self.tax.project_authors,
                        "project_authors is empty; the rule would never fire")

    def test_no_published_paper_by_a_project_author_counts_as_external(self):
        for record in config.load_json(config.DATA_FILES["papers"])["papers"]:
            if record.get("is_sqlancer_publication"):
                continue
            for author in record.get("authors", []):
                self.assertFalse(
                    self.tax.is_project_author(author),
                    f"{record['title']!r} is counted as external work but "
                    f"{author} is a project author")

    def test_every_listed_publication_carries_a_reason(self):
        for entry in self.tax.project_publications:
            self.assertTrue(entry.get("reason", "").strip(),
                            f"{entry['title']} is listed without a reason")

    def test_listed_publications_are_excluded_from_external_counts(self):
        records = {p["id"]: p for p in
                   config.load_json(config.DATA_FILES["papers"])["papers"]}
        for entry in self.tax.project_publications:
            if not entry.get("doi"):
                continue
            record = records.get(f"paper:doi:{entry['doi'].lower()}")
            if record is None:
                continue
            self.assertTrue(
                record.get("is_sqlancer_publication"),
                f"{entry['title']} is listed as ours but still counts as external")


class AdoptionEvidenceTest(unittest.TestCase):
    def test_a_prose_statement_is_preferred_over_a_bare_link(self):
        text = ("DataFusion uses the [SQLancer] for fuzz testing.\n\n"
                "[sqlancer]: https://github.com/sqlancer/sqlancer\n")
        excerpt, kind = _best_excerpt(text, "docs/testing.md")
        self.assertEqual("statement", kind)
        self.assertIn("uses the [SQLancer]", excerpt)

    def test_a_bare_repository_link_is_not_treated_as_an_invocation(self):
        text = "See [SQLancer]: https://github.com/sqlancer/sqlancer for details\n"
        self.assertIsNone(_best_excerpt(text, "docs/other.md"))

    def test_a_command_line_counts_as_an_invocation(self):
        text = "RUN java -jar sqlancer-2.0.0.jar --num-threads 4 duckdb\n"
        excerpt, kind = _best_excerpt(text, "test/Dockerfile")
        self.assertEqual("invocation", kind)


class ArtifactTest(unittest.TestCase):
    """uses_infrastructure is decided from the artifact, not from the paper."""

    class FakeGitHub:
        """Stands in for GitHub with one repository's files in memory."""

        def __init__(self, *, description="", files=None, parent=None,
                     tree=None):
            self.metadata = {"default_branch": "main", "description": description}
            if parent:
                self.metadata["parent"] = {"full_name": parent}
            self.files = files or {}
            self._tree = tree or list(self.files)

        def repo(self, owner, name, **kwargs):
            return self.metadata

        def tree(self, owner, repo, ref="HEAD", **kwargs):
            return [{"path": path, "type": "blob"} for path in self._tree]

        def raw_file(self, owner, repo, path, ref="HEAD", **kwargs):
            return self.files.get(path), {"status": 200}

    def test_a_tool_name_is_taken_from_the_paper_text(self):
        from tools.impact.collectors.artifacts import candidate_tool_names
        names = candidate_tool_names(
            "Detecting Logic Bugs in DBMSs via Equivalent Data Construction",
            "We implement our approach in a tool called Radar and evaluate it.")
        self.assertIn("Radar", names)

    def test_a_leading_tool_name_in_the_title_is_found(self):
        from tools.impact.collectors.artifacts import candidate_tool_names
        self.assertIn("Pinolo", candidate_tool_names(
            "Pinolo: Detecting Logical Bugs in Database Management Systems", ""))

    def test_generic_acronyms_are_not_tool_names(self):
        from tools.impact.collectors.artifacts import candidate_tool_names
        names = candidate_tool_names("Testing DBMS via SQL and JSON", "")
        self.assertEqual([], names)

    def test_markers_alone_are_not_conclusive(self):
        from tools.impact.collectors.artifacts import markers_are_conclusive
        self.assertFalse(markers_are_conclusive(["randomly_java_present"]),
                         "one vendored file must not settle infrastructure reuse")
        self.assertTrue(markers_are_conclusive(
            ["randomly_java_present", "sqlancer_package_structure"]))
        self.assertTrue(markers_are_conclusive(["fork_of_sqlancer_repository"]))

    def test_a_fork_of_sqlancer_is_detected(self):
        from tools.impact.collectors.artifacts import inspect_repository
        gh = self.FakeGitHub(parent="sqlancer/sqlancer")
        markers, evidence = inspect_repository(gh, "someone", "their-fork",
                                               timestamp="2026-01-01T00:00:00Z")
        self.assertIn("fork_of_sqlancer_repository", markers)
        self.assertTrue(evidence)

    def test_retained_source_files_are_detected(self):
        from tools.impact.collectors.artifacts import inspect_repository
        gh = self.FakeGitHub(files={
            "src/sqlancer/Randomly.java": "package sqlancer;",
            "src/sqlancer/Main.java": "package sqlancer;",
            "pom.xml": "<groupId>com.sqlancer</groupId>",
        })
        markers, _ = inspect_repository(gh, "someone", "artifact",
                                        timestamp="2026-01-01T00:00:00Z")
        self.assertIn("retained_sqlancer_source_files", markers)
        self.assertIn("sqlancer_package_structure", markers)
        self.assertIn("sqlancer_build_file_reference", markers)

    def test_a_repository_naming_the_paper_is_linked(self):
        from tools.impact.collectors.artifacts import link_evidence
        paper = {"title": "Detecting Logic Bugs via Equivalent Data Construction",
                 "doi": "10.1145/1234567", "arxiv_id": None}
        gh = self.FakeGitHub(files={
            "README.md": ("Artifact for Detecting Logic Bugs via Equivalent "
                          "Data Construction.\n")})
        link = link_evidence(gh, "o", "r", paper, tool_names=[],
                             timestamp="2026-01-01T00:00:00Z")
        self.assertIsNotNone(link)
        self.assertIn("names this paper", link["note"])

    def test_a_repository_citing_the_doi_is_linked(self):
        from tools.impact.collectors.artifacts import link_evidence
        paper = {"title": "Some paper", "doi": "10.1145/1234567", "arxiv_id": None}
        gh = self.FakeGitHub(files={"README.md": "See https://doi.org/10.1145/1234567"})
        link = link_evidence(gh, "o", "r", paper, tool_names=[],
                             timestamp="2026-01-01T00:00:00Z")
        self.assertIsNotNone(link)

    def test_a_name_collision_without_database_context_is_not_linked(self):
        from tools.impact.collectors.artifacts import link_evidence
        paper = {"title": "Some paper", "doi": None, "arxiv_id": None}
        gh = self.FakeGitHub(description="A radar chart library for Android",
                             files={"README.md": "Draw radar charts."})
        self.assertIsNone(link_evidence(gh, "o", "radar", paper,
                                        tool_names=["radar"],
                                        timestamp="2026-01-01T00:00:00Z"))

    def test_a_tool_named_repository_about_databases_is_linked(self):
        from tools.impact.collectors.artifacts import link_evidence
        paper = {"title": "Some paper", "doi": None, "arxiv_id": None}
        gh = self.FakeGitHub(description="Logic bug detection for SQL DBMSs",
                             files={"README.md": "Tests SQLite and MySQL."})
        link = link_evidence(gh, "o", "radar", paper, tool_names=["Radar"],
                             timestamp="2026-01-01T00:00:00Z")
        self.assertIsNotNone(link)
        self.assertIn("named after", link["note"])

    def test_an_unrelated_repository_is_not_linked(self):
        from tools.impact.collectors.artifacts import link_evidence
        paper = {"title": "Some paper", "doi": None, "arxiv_id": None}
        gh = self.FakeGitHub(description="Unrelated project",
                             files={"README.md": "Nothing to do with it."})
        self.assertIsNone(link_evidence(gh, "o", "other", paper,
                                        tool_names=["Radar"],
                                        timestamp="2026-01-01T00:00:00Z"))


class FullTextTest(unittest.TestCase):
    """Judging a paper from its own words, not from a citation index."""

    def setUp(self):
        from tools.impact.collectors import fulltext
        self.find = fulltext.find_signals

    def test_a_claim_of_reuse_is_found(self):
        found = self.find("We implemented our approach on top of SQLancer.")
        self.assertEqual(1, len(found["uses_infrastructure"]))

    def test_related_work_is_not_the_authors_claim(self):
        """"Previous work built on SQLancer" says nothing about this paper."""
        for sentence in (
                "Previous work implemented their tool on top of SQLancer.",
                "Existing approaches are built on SQLancer.",
                "Prior work extended TLP to graph databases."):
            found = self.find(sentence)
            self.assertEqual([], found["uses_infrastructure"], sentence)
            self.assertEqual([], found["extends_technique"], sentence)

    def test_a_figure_caption_is_not_a_claim_of_reuse(self):
        """The pattern needs `we` or `our`; an adjective is not a verb."""
        found = self.find(
            "Fig. 2b presents the GA for WHERE Extended case of Ternary Logic "
            "Partitioning (TLP) [21] oracle from SQLancer.")
        self.assertEqual([], found["uses_infrastructure"])

    def test_a_comparison_is_found(self):
        found = self.find(
            "We compare the performance of our tool against SQLancer on five "
            "database systems.")
        self.assertEqual(1, len(found["compares_with"]))

    def test_an_extension_claim_is_found(self):
        found = self.find("We extend TLP to graph database systems.")
        self.assertEqual(1, len(found["extends_technique"]))

    def test_an_excluded_technique_alone_establishes_nothing(self):
        found = self.find("We compare our approach against EET on five DBMSs.")
        self.assertEqual([], found["compares_with"])

    def test_sentences_are_kept_verbatim(self):
        sentence = "We implemented our prototype on top of SQLancer."
        found = self.find(sentence)
        self.assertEqual(sentence, found["uses_infrastructure"][0])

    def test_a_pdf_that_is_not_a_pdf_is_rejected(self):
        from tools.impact.collectors import fulltext

        class NotAPdf:
            def fetch_binary(self, url, **kwargs):
                return b"<html>not a pdf</html>", False

        self.assertIsNone(fulltext.pdf_text(NotAPdf(), "https://example.org/x"))


class PreprintTest(unittest.TestCase):
    """Matching a paywalled paper to its preprint has to be strict."""

    ATOM = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>http://arxiv.org/abs/2604.16373v1</id>
        <title>DIRT: Database-Integrated Random Testing</title>
        <summary>A paper.</summary>
        <author><name>Alperen Keles</name></author>
        <author><name>Ethan Chou</name></author>
        <link title="pdf" href="http://arxiv.org/pdf/2604.16373v1"/>
      </entry>
    </feed>"""

    class FakeCache:
        def __init__(self, body):
            self.body = body

        def get(self, key):
            return None

        def is_fresh(self, key, **kwargs):
            return False

        def put(self, key, value):
            pass

        def store(self, key, **kwargs):
            return {}

    def _arxiv(self):
        from tools.impact.scholarly import ArXiv
        arxiv = ArXiv(self.FakeCache(self.ATOM))
        arxiv._entries = lambda query, limit: __import__(
            "tools.impact.scholarly", fromlist=["_parse_atom"]
        )._parse_atom(self.ATOM)
        return arxiv

    def test_an_exact_title_with_a_shared_author_matches(self):
        found = self._arxiv().find_preprint(
            "DIRT: Database-Integrated Random Testing", ["Alperen Keles"])
        self.assertIsNotNone(found)
        self.assertEqual("2604.16373v1", found["arxiv_id"])

    def test_a_different_paper_by_the_same_author_does_not_match(self):
        self.assertIsNone(self._arxiv().find_preprint(
            "Some Entirely Different Paper Title", ["Alperen Keles"]))

    def test_the_same_title_by_different_authors_does_not_match(self):
        """Titles repeat across communities; an author must corroborate."""
        self.assertIsNone(self._arxiv().find_preprint(
            "DIRT: Database-Integrated Random Testing", ["Someone Unrelated"]))

    def test_a_title_too_short_to_identify_anything_is_refused(self):
        self.assertIsNone(self._arxiv().find_preprint("DIRT", ["Keles"]))

    def test_atom_parsing_reads_what_it_needs(self):
        from tools.impact.scholarly import _parse_atom
        entries = _parse_atom(self.ATOM)
        self.assertEqual(1, len(entries))
        self.assertEqual(["Alperen Keles", "Ethan Chou"], entries[0]["authors"])
        self.assertIn("pdf", entries[0]["pdf_url"])


class LabMemberTest(unittest.TestCase):
    """Author-scoped search, because term search is capped at 1000 results."""

    def test_handles_are_extracted_from_the_people_page(self):
        from tools.impact.collectors import lab_members

        class FakeFetcher:
            def fetch(self, url, **kwargs):
                return {"status": 200, "body":
                        '<a href="https://github.com/bajinsheng">x</a>'
                        '<a href="https://github.com/suyZhong">y</a>'
                        '<a href="https://github.com/sqlancer/sqlancer">z</a>'}, False

        found = lab_members.from_people_page(FakeFetcher())
        self.assertIn("bajinsheng", found)
        self.assertIn("suyZhong", found)
        self.assertNotIn("sqlancer", found,
                         "the project's own account is not a lab member")

    def test_one_query_per_member_covers_every_term(self):
        from tools.impact.collectors import lab_members
        queries = lab_members.search_queries(["alice", "bob"], ["SQLancer", "TLP"])
        self.assertEqual(2, len(queries))
        self.assertIn("author:alice", queries[0])
        self.assertIn("SQLancer OR TLP", queries[0])

    def test_display_names_are_not_usable_as_handles(self):
        """The bug list mixes handles and full names; only handles can search."""
        from tools.impact.collectors import lab_members

        class FakeGitHub:
            def raw_file(self, *args, **kwargs):
                import json
                return json.dumps([
                    {"reported_by": "bajinsheng"},
                    {"reported_by": "Suyang Zhong"},
                ]), {}

        found = lab_members.from_bug_list(FakeGitHub())
        self.assertEqual(["bajinsheng"], found)


class ReproducerSignatureTest(unittest.TestCase):
    """The reproducer's own shape, which is what reaches reports that say nothing."""

    def _excerpt(self, text):
        from tools.impact.collectors.github_bugs import reproducer_excerpt
        return reproducer_excerpt(text)

    def test_a_generated_schema_is_recognised(self):
        self.assertTrue(self._excerpt(
            "CREATE TABLE t0(c0 BOOLEAN, c1 INT); INSERT INTO t0 VALUES (0, 1);"))

    def test_another_generator_s_columns_are_not_sqlancer_s(self):
        """Other tools number their tables too; the column names differ."""
        self.assertIsNone(self._excerpt(
            "CREATE TABLE t2 (c_pk INTEGER, c_int INTEGER); SELECT c_pk FROM t2;"))
        self.assertIsNone(self._excerpt(
            "create table t0 (c_0 int); create table t1 (c_1 int);"))

    def test_a_handwritten_reproducer_is_not_matched(self):
        self.assertIsNone(self._excerpt(
            "CREATE TABLE users (id INT, name TEXT); SELECT * FROM users;"))

    def test_the_excerpt_is_copied_from_the_report(self):
        text = "Some prose.\nCREATE TABLE t0(c0 INT);\nSELECT * FROM t0;\nMore."
        excerpt = self._excerpt(text)
        self.assertIn(excerpt, text)

    def test_the_reproducer_alone_never_attributes_a_stranger_s_report(self):
        """It is evidence only for someone already known to run campaigns."""
        from tools.impact import taxonomy
        from tools.impact.collectors.github_bugs import deterministic_attribution
        tax = taxonomy.load()
        issue = {"title": "Wrong result", "state": "open",
                 "body": "CREATE TABLE t0(c0 INT); SELECT * FROM t0;", "labels": []}
        text = f"{issue['title']}\n{issue['body']}"
        self.assertIsNone(deterministic_attribution(text, tax, issue))
        found = deterministic_attribution(text, tax, issue,
                                          reporter_runs_campaigns=True)
        self.assertIsNotNone(found)
        self.assertEqual("campaign_evidence", found[0])


class SearchOrderTest(unittest.TestCase):
    """Which queries run first decides which ones the candidate cap silences."""

    def test_author_searches_run_before_term_searches(self):
        from tools.impact import taxonomy
        from tools.impact.collectors import github_bugs

        class FakeGitHub:
            def search_issues(self, query, **kwargs):
                seen.append(query)
                return []

        seen = []
        github_bugs.collect(FakeGitHub(), classifier=None, full=True)
        authored = [i for i, q in enumerate(seen) if q.startswith("author:")]
        others = [i for i, q in enumerate(seen) if not q.startswith("author:")]
        if authored and others:
            self.assertLess(max(authored), min(others),
                            "author searches must not be behind the term searches")


class CampaignReporterTest(unittest.TestCase):
    """Admitting a report on the strength of who filed it.

    The weakest rule in the policy, so what it must never do matters as much as
    what it does.
    """

    def setUp(self):
        from tools.impact import taxonomy
        from tools.impact.collectors import github_bugs
        self.g = github_bugs
        self.tax = taxonomy.load()

    def _issue(self, body, title="Wrong result", **kwargs):
        issue = {"title": title, "body": body, "state": "open", "labels": [],
                 "html_url": "https://github.com/duckdb/duckdb/issues/1",
                 "user": {"login": "Yibo-Dong"}, "created_at": "2026-01-01T00:00:00Z"}
        issue.update(kwargs)
        return issue

    def _attribute(self, issue, member=True):
        text = self.g._issue_source_text(issue)
        return self.g.deterministic_attribution(
            text, self.tax, issue, reporter_runs_campaigns=member)

    def test_a_member_s_defect_report_is_admitted_without_a_tool_mention(self):
        issue = self._issue(
            "## What happened\n\nDuckDB returns the list in reverse order for "
            "a window aggregate.\n\n## Environment\n\nv1.2.0")
        found = self._attribute(issue)
        self.assertIsNotNone(found)
        self.assertEqual("campaign_reporter", found[0])
        self.assertEqual("low", self.g._confidence_for(found[0]))

    def test_the_same_report_from_a_stranger_is_not_admitted(self):
        issue = self._issue(
            "## What happened\n\nDuckDB returns the list in reverse order.")
        self.assertIsNone(self._attribute(issue, member=False))

    def test_the_quoted_excerpt_is_the_reporter_s_words_not_the_template_s(self):
        issue = self._issue(
            "## What happened\n\nDolt panics when evaluating LOCATE with a "
            "negative start position.\n\n## Environment")
        found = self._attribute(issue)
        self.assertNotIn("What happened", found[3])
        self.assertIn("Dolt panics", found[3])
        self.assertIn(found[3], self.g._issue_source_text(issue))

    def test_a_carefully_written_report_is_not_rejected_for_its_vocabulary(self):
        """The reports that matter describe the misbehaviour instead of naming it."""
        issue = self._issue(
            "Dolt evaluates the frame bound before the ORDER BY is applied, so "
            "the window sees rows in the wrong order and returns a value from "
            "the wrong partition. This reproduces on a fresh database.",
            title="Dolt ignores `LIMIT` inside a correlated `EXISTS` subquery.",
            labels=[{"name": "customer issue"}])
        self.assertFalse(self.g.looks_like_a_defect(issue),
                         "precondition: the old gate rejects this")
        self.assertEqual("campaign_reporter", self._attribute(issue)[0])

    def test_a_pull_request_is_never_a_bug_report(self):
        issue = self._issue("A description long enough to pass the length test, "
                            "with plenty of words in it to be sure.",
                            pull_request={})
        self.assertIsNone(self._attribute(issue))

    def test_a_one_line_issue_is_not_admitted(self):
        issue = self._issue("hm?", title="Something odd")
        self.assertIsNone(self._attribute(issue))

    def test_a_feature_request_from_a_member_is_still_not_a_bug(self):
        issue = self._issue(
            "It would be nice if this were configurable in some way here.",
            title="Support configuring the window frame")
        self.assertIsNone(self._attribute(issue))

    def test_a_report_crediting_an_excluded_technique_is_still_rejected(self):
        """Membership does not override the exclusions."""
        excluded = next(iter(self.tax.excluded.values()))
        name = excluded.get("display_name") or excluded.get("name") or ""
        if not name:
            self.skipTest("no excluded technique with a name")
        issue = self._issue(f"We found this with {name}, a wrong result here.")
        self.assertIsNone(self._attribute(issue))

    def test_the_record_says_it_rests_on_the_roster(self):
        issue = self._issue(
            "## What happened\n\nDuckDB returns the list in reverse order for "
            "a window aggregate.")
        rule, technique, finder, excerpt = self._attribute(issue)
        record = self.g.build_record(
            issue, "duckdb", rule=rule, technique=technique, finder=finder,
            excerpt=excerpt, source_text=self.g._issue_source_text(issue),
            timestamp="2026-01-01T00:00:00Z",
            confidence=self.g._confidence_for(rule))
        notes = " ".join(item["note"] for item in record["attribution"]["evidence"])
        self.assertIn("membership", notes)
        self.assertIn(self.g.ROSTER_URL,
                      [item["source_url"] for item in record["attribution"]["evidence"]])

    def test_a_reproducer_still_wins_over_the_weaker_rule(self):
        """A SQLancer-shaped reproducer is better evidence; keep it."""
        issue = self._issue("CREATE TABLE t0(c0 INT); SELECT * FROM t0;")
        self.assertEqual("campaign_evidence", self._attribute(issue)[0])


class PaperNoteTest(unittest.TestCase):
    """A summary is a convenience; the evidence under it is the record."""

    def _paper(self):
        return {
            "id": "paper:doi:10.1145/1",
            "title": "A Paper",
            "authors": ["A. Author"],
            "year": 2025,
            "venue": "A Venue",
            "doi": "10.1145/1",
            "url": "https://doi.org/10.1145/1",
            "abstract": "We built a tool on top of SQLancer. It found bugs.",
            "relationships": {
                "references": {
                    "value": "yes", "method": "citation_graph", "techniques": [],
                    "evidence": [{
                        "source_url": "https://doi.org/10.1145/1",
                        "source_type": "paper_citation_context",
                        "excerpt": "We use SQLancer [12].",
                        "excerpt_is_verbatim": True,
                        "note": "Citing sentence.",
                        "retrieved_at": "2026-01-01T00:00:00Z",
                    }],
                },
            },
        }

    def setUp(self):
        import tempfile, pathlib as _pathlib
        from tools.impact import notes
        self.notes = notes
        self._real_dir = notes.NOTES_DIR
        self._tmp = tempfile.TemporaryDirectory()
        notes.NOTES_DIR = _pathlib.Path(self._tmp.name)

    def tearDown(self):
        self.notes.NOTES_DIR = self._real_dir
        self._tmp.cleanup()

    def test_evidence_is_written_without_any_model(self):
        note = self.notes.build_note(self._paper(), classifier=None, fetcher=None)
        self.assertIsNone(note["summary"])
        self.assertEqual(1, len(note["evidence"]))
        self.assertEqual("We use SQLancer [12].",
                         note["evidence"][0]["sources"][0]["excerpt"])

    def test_a_summary_must_quote_the_source_it_claims(self):
        paper = self._paper()
        with self.assertRaises(self.notes.UngroundedSummary):
            self.notes.apply_summary(
                paper, text="A summary.", relationship_to_sqlancer="Uses it.",
                excerpts=["a sentence that is not in the abstract"],
                written_from="abstract", source_text=paper["abstract"],
                model="claude-opus-5")

    def test_a_grounded_summary_is_stored_and_marked_generated(self):
        paper = self._paper()
        note = self.notes.apply_summary(
            paper, text="A summary.", relationship_to_sqlancer="Uses it.",
            excerpts=["We built a tool on top of SQLancer."],
            written_from="abstract", source_text=paper["abstract"],
            model="claude-opus-5")
        self.assertTrue(note["summary"]["is_model_generated"])
        self.assertTrue(note["summary"]["grounded_in_source"])
        self.assertEqual("abstract", note["summary"]["written_from"])
        # And the evidence is still there, untouched by the summary.
        self.assertEqual("We use SQLancer [12].",
                         note["evidence"][0]["sources"][0]["excerpt"])

    def test_regenerating_evidence_never_drops_an_existing_summary(self):
        """A run with no API key must not erase what an earlier run wrote."""
        import json
        paper = self._paper()
        self.notes.apply_summary(
            paper, text="A summary.", relationship_to_sqlancer="Uses it.",
            excerpts=["We built a tool on top of SQLancer."],
            written_from="abstract", source_text=paper["abstract"],
            model="claude-opus-5")
        path = self.notes.note_path(paper)

        # Now rebuild the note the way a keyless run would, and write it.
        note = self.notes.build_note(paper, classifier=None, fetcher=None)
        self.assertIsNone(note["summary"])
        counts = self._write_all_over([paper])
        kept = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsNotNone(kept["summary"], "the summary was erased")
        self.assertEqual("A summary.", kept["summary"]["text"])

    def _write_all_over(self, papers):
        """Run write_all against a fixed paper list."""
        return self.notes.write_all(papers=papers)

    def test_a_supplied_pdf_is_read_even_without_a_fetcher(self):
        """It is on disk; needing a fetcher for it would silently lose it."""
        from unittest import mock
        from tools.impact.collectors import fulltext

        paper = self._paper()
        with mock.patch.object(fulltext, "local_pdf",
                               return_value="file:///tmp/x.pdf"), \
             mock.patch.object(fulltext, "pdf_text",
                               return_value="Full text of the paper."):
            text, label, _ = self.notes.summary_source(paper)
        self.assertEqual("supplied PDF", label)
        self.assertEqual("Full text of the paper.", text)

    def test_the_note_is_named_for_the_paper_id_not_its_title(self):
        paper = self._paper()
        first = self.notes.note_path(paper)
        paper["title"] = "A Paper, Renamed On Publication"
        self.assertEqual(first, self.notes.note_path(paper))


class PageFurnitureTest(unittest.TestCase):
    """A quotation must be the author's sentence, not the publisher's stamp."""

    def test_an_ieee_licence_stamp_is_removed(self):
        from tools.impact.collectors import fulltext
        text = ("Rigger et al. proposed NoREC, which restructures SQL "
                "statements to inhibit query 125 Authorized licensed use "
                "limited to the terms of the applicable license agreement "
                "with IEEE. Restrictions apply. The next sentence.")
        cleaned = fulltext._strip_page_furniture(text)
        self.assertNotIn("Authorized licensed", cleaned)
        self.assertIn("proposed NoREC", cleaned)
        self.assertIn("The next sentence.", cleaned)

    def test_an_acm_permission_block_is_removed(self):
        from tools.impact.collectors import fulltext
        text = ("We implemented SRS on top of SQLancer. Permission to make "
                "digital or hard copies of part or all of this work for "
                "personal use is granted without fee. Our evaluation follows.")
        cleaned = fulltext._strip_page_furniture(text)
        self.assertNotIn("Permission to make", cleaned)
        self.assertIn("We implemented SRS on top of SQLancer.", cleaned)

    def test_ordinary_text_is_untouched(self):
        from tools.impact.collectors import fulltext
        text = "We compare against TLP and NoREC on five widely used systems."
        self.assertEqual(text, fulltext._strip_page_furniture(text))


class BaselineSentenceTest(unittest.TestCase):
    """Taking a tool up as a baseline is the opposite of building on it."""

    def signals(self, sentence):
        from tools.impact.collectors import fulltext
        return {k: len(v) for k, v in fulltext.find_signals(sentence).items()}

    def test_adapting_a_tool_as_a_baseline_is_only_a_comparison(self):
        found = self.signals(
            "To evaluate the effectiveness of TSGuard in detecting logic bugs "
            "in TSMSs, we adapted the open-source relational database testing "
            "tool SQLancer as a baseline for comparison.")
        self.assertEqual(1, found["compares_with"])
        self.assertEqual(0, found["extends_technique"],
                         "adapting a tool to measure against is not extending it")
        self.assertEqual(0, found["uses_infrastructure"])

    def test_a_real_extension_still_counts(self):
        found = self.signals(
            "We extended TLP to support graph queries in our implementation.")
        self.assertEqual(1, found["extends_technique"])

    def test_real_reuse_still_counts(self):
        found = self.signals("We implemented our prototype on top of SQLancer.")
        self.assertEqual(1, found["uses_infrastructure"])


class IntakeTest(unittest.TestCase):
    """Filing a downloaded PDF as the paper it actually is."""

    GDSMITH = ("GDsmith: Detecting Bugs in Cypher Graph Database Engines\n"
               "Ziyue Hua, Wei Lin, Luyao Ren\n"
               "Peking University\n\nABSTRACT\nGraph database engines ...")

    def setUp(self):
        from tools.impact import intake
        self.intake = intake

    def test_a_generic_title_does_not_match_everything(self):
        """The failure this guards against: short titles made of common words.

        Counting how many of a candidate's words appear on the page scored
        "Test Data Generation for Complex SQL Queries" at 100% against an
        unrelated graph database paper.
        """
        right = self.intake.score(
            "GDsmith: Detecting Bugs in Cypher Graph Database Engines", self.GDSMITH)
        wrong = self.intake.score(
            "Test Data Generation for Complex SQL Queries", self.GDSMITH)
        self.assertGreater(right, 0.8)
        self.assertLess(wrong, 0.5)

    def test_a_paper_and_its_preprint_are_refused_rather_than_guessed(self):
        candidates = [
            {"title": "GDsmith: Detecting Bugs in Cypher Graph Database Engines",
             "filename": "a.pdf"},
            {"title": "GDsmith: Detecting Bugs in Graph Database Engines",
             "filename": "b.pdf"},
        ]
        match, best, runner_up = self.intake.identify(self.GDSMITH, candidates)
        self.assertIsNone(match)
        self.assertTrue(self.intake.is_ambiguous(best, runner_up))

    def test_a_usenix_cover_sheet_does_not_hide_the_title(self):
        """USENIX runs the title onto the end of its cover boilerplate."""
        page = ("This paper is included in the Proceedings of the\n"
                "31st USENIX Security Symposium.\n"
                "August 10-12, 2022 - Boston, MA, USA\n"
                "Open access to the Proceedings of the\n"
                "31st USENIX Security Symposium is\n"
                "sponsored by USENIX.Detecting Logical Bugs of DBMS with\n"
                "Coverage-based Guidance\n"
                "Yu Liang, Pennsylvania State University")
        self.assertIn("Detecting Logical Bugs of DBMS with Coverage-based Guidance",
                      self.intake.title_candidates(page))

    def test_a_title_wrapping_onto_a_short_line_keeps_that_line(self):
        """"DBMS" on its own line is the end of the title, not noise."""
        page = ("Unveiling Logic Bugs in SPJG Query Optimizations within\n"
                "DBMS\n"
                "XIU TANG ,Zhejiang University, Hangzhou, China")
        self.assertIn("Unveiling Logic Bugs in SPJG Query Optimizations within DBMS",
                      self.intake.title_candidates(page))

    def test_a_tie_resolves_to_the_one_still_wanted(self):
        """A paper and its preprint share a title; only one needs the file."""
        candidates = [
            {"title": "A Comprehensive Survey on DBMS Fuzzing", "filename": "doi.pdf"},
            {"title": "A Comprehensive Survey on DBMS Fuzzing", "filename": "arxiv.pdf"},
        ]
        page = "A Comprehensive Survey on DBMS Fuzzing\nAuthor, University"
        match, _, _ = self.intake.identify(page, candidates)
        self.assertIsNone(match, "with no wanted set, a tie stays a tie")
        match, _, _ = self.intake.identify(page, candidates, wanted={"doi.pdf"})
        self.assertEqual("doi.pdf", match["filename"])
        match, _, _ = self.intake.identify(page, candidates,
                                           wanted={"doi.pdf", "arxiv.pdf"})
        self.assertIsNone(match, "if both are wanted, only a person can choose")

    def test_a_tie_between_two_papers_that_need_nothing_is_not_a_decision(self):
        """Usually a paper already filed under one of its two identities."""
        candidates = [
            {"title": "A Comprehensive Survey on DBMS Fuzzing", "filename": "doi.pdf"},
            {"title": "A Comprehensive Survey on DBMS Fuzzing", "filename": "arxiv.pdf"},
        ]
        page = "A Comprehensive Survey on DBMS Fuzzing\nAuthor, University"
        match, best, runner_up = self.intake.identify(page, candidates, wanted=set())
        self.assertIsNotNone(match, "nothing is wanted, so nothing to decide")

    def test_front_matter_above_the_title_does_not_swallow_it(self):
        """An author line looked like front matter and moved the start past it."""
        page = ("Semantic Hint-Based Fuzzing for Time-Series\n"
                "Databases\n"
                "1st Panta Kittisatra, Some University\n")
        best = self.intake.title_candidates(page)[0]
        self.assertTrue(best.startswith("Semantic Hint-Based Fuzzing"), best)

    def test_a_declared_title_identifies_a_pdf_whose_page_lacks_one(self):
        """A magazine extract can begin mid-sentence, with no title anywhere."""
        page = "Our experience at AWS with TLA+ revealed two advantages."
        self.assertGreater(
            self.intake.score("Systems Correctness Practices at AWS", page,
                              ("Systems Correctness Practices at AWS",)), 0.9)

    def test_a_publisher_filename_identifies_the_paper_exactly(self):
        import pathlib
        rows = [{"title": "Systems Correctness Practices at Amazon Web Services",
                 "filename": "10.1145_3729175.pdf"}]
        found = self.intake.identifier_from_filename(
            pathlib.Path("/tmp/3729175.pdf"), rows)
        self.assertEqual("10.1145_3729175.pdf", found["filename"])
        self.assertIsNone(self.intake.identifier_from_filename(
            pathlib.Path("/tmp/notes.pdf"), rows))
        self.assertIsNone(self.intake.identifier_from_filename(
            pathlib.Path("/tmp/9999999.pdf"), rows))

    def test_a_doi_printed_on_the_page_identifies_the_paper(self):
        """A journal masthead can push the title past any layout rule."""
        rows = [{"title": "Benchmarking Autonomy", "filename": "x.pdf",
                 "doi": "10.22399/ijcesen.5462"},
                {"title": "Something Else", "filename": "y.pdf",
                 "doi": "10.1145/3769828"}]
        page = ("Copyright IJCESEN\nInternational Journal\n"
                "Article Info:\nDOI:  10.22399/ijcesen. 5462\n")
        self.assertEqual("x.pdf", self.intake.doi_on_page(page, rows)["filename"])
        self.assertIsNone(self.intake.doi_on_page("no doi here", rows))

    def test_an_unrelated_pdf_is_not_ambiguous_merely_unmatched(self):
        candidates = [{"title": "Testing Database Systems via Differential "
                                "Query Execution", "filename": "a.pdf"}]
        page = "Konditionen fur die Buchung\nHotelreservierung 2026"
        match, best, runner_up = self.intake.identify(page, candidates)
        self.assertIsNone(match)
        self.assertFalse(self.intake.is_ambiguous(best, runner_up))

    def test_a_clear_match_is_returned(self):
        candidates = [
            {"title": "GDsmith: Detecting Bugs in Cypher Graph Database Engines",
             "filename": "a.pdf"},
            {"title": "Testing DBMSs via Equivalent Expression Transformation",
             "filename": "b.pdf"},
        ]
        match, best, _ = self.intake.identify(self.GDSMITH, candidates)
        self.assertIsNotNone(match)
        self.assertEqual("a.pdf", match["filename"])
        self.assertGreater(best, 0.8)


class WorklistTest(unittest.TestCase):
    """The download list must ask only for papers nothing else can reach."""

    def _paper(self, **kwargs):
        record = {"title": "T", "url": "https://example.org/p", "year": 2025,
                  "doi": None, "arxiv_id": None, "s2_paper_id": None,
                  "relationships": {key: {"value": "uncertain"} for key in
                                    ("uses_infrastructure", "extends_technique",
                                     "compares_with")}}
        record.update(kwargs)
        return record

    def test_a_preprint_is_never_requested(self):
        from tools.impact import worklist
        rows = worklist.wanted([self._paper(arxiv_id="2501.00001",
                                            doi="10.1145/1")])
        self.assertEqual([], rows)

    def test_a_pvldb_paper_is_never_requested(self):
        from tools.impact import worklist
        rows = worklist.wanted([self._paper(doi="10.14778/3695624.3695625")])
        self.assertEqual([], rows)

    def test_sqlancer_s_own_papers_are_never_requested(self):
        from tools.impact import worklist
        rows = worklist.wanted([self._paper(doi="10.1145/1",
                                            is_sqlancer_publication=True)])
        self.assertEqual([], rows)

    def test_the_filename_is_the_one_the_pipeline_looks_for(self):
        from tools.impact import worklist
        from tools.impact.collectors import fulltext
        # A deliberately unreal DOI: a test must not change its meaning when
        # somebody downloads the paper it named.
        rows = worklist.wanted([self._paper(doi="10.9999/not-a-real-paper")])
        self.assertEqual("10.9999_not-a-real-paper.pdf", rows[0]["filename"])
        self.assertEqual(rows[0]["filename"],
                         fulltext.wanted_filename("10.9999/not-a-real-paper", None))

    def test_a_paper_whose_contexts_hint_at_more_outranks_a_bare_citation(self):
        from tools.impact import worklist
        plain = self._paper(doi="10.1145/1", title="Plain", year=2026)
        hinting = self._paper(doi="10.1145/2", title="Hinting", year=2020)
        hinting["relationships"]["references"] = {
            "value": "yes",
            "evidence": [{"excerpt": "We implemented our prototype on top of "
                                     "SQLancer for this evaluation."}],
        }
        rows = worklist.wanted([plain, hinting])
        self.assertEqual("Hinting", rows[0]["title"],
                         "a hint should outrank a merely newer paper")

    def test_a_paper_with_no_doi_can_still_be_supplied(self):
        """A dozen papers have neither DOI nor arXiv id; they are still wanted."""
        from tools.impact import worklist
        from tools.impact.collectors import fulltext
        paper = self._paper(doi=None, arxiv_id=None,
                            s2_paper_id="0000000000000000000000000000000000000000")
        rows = worklist.wanted([paper])
        self.assertEqual(1, len(rows))
        self.assertEqual(
            "s2_0000000000000000000000000000000000000000.pdf", rows[0]["filename"])
        self.assertEqual(rows[0]["filename"],
                         fulltext.wanted_filename(None, None, paper["s2_paper_id"]))

    def test_every_download_goes_through_the_proxy(self):
        """The proxy is unconditional; the path behind it is per-publisher.

        A paywalled paper needs the proxy and an open-access one is unharmed by
        it, so the link never asks the reader to judge which kind it is.
        """
        from tools.impact import worklist
        ieee = worklist.download_url("10.1109/icse55347.2025.00003", None, "IEEE")
        self.assertEqual(worklist.PROXY + "https://doi.org/"
                         "10.1109/icse55347.2025.00003", ieee)
        acm = worklist.download_url("10.1145/3769828", None, "ACM")
        self.assertEqual(worklist.PROXY + "https://dl.acm.org/doi/pdf/"
                         "10.1145/3769828", acm)
        springer = worklist.download_url("10.1007/978-3-031-1", None, "Springer")
        self.assertEqual(worklist.PROXY + "https://link.springer.com/content/"
                         "pdf/10.1007/978-3-031-1.pdf", springer)
        other = worklist.download_url(None, "https://example.org/p", "other")
        self.assertEqual(worklist.PROXY + "https://example.org/p", other)
        self.assertEqual("", worklist.download_url(None, None, "other"))

    def test_papers_with_a_known_relationship_come_first(self):
        from tools.impact import worklist
        settled = self._paper(doi="10.1145/2", title="Settled")
        settled["relationships"]["uses_infrastructure"]["value"] = "yes"
        rows = worklist.wanted([self._paper(doi="10.1145/1", title="Unknown"),
                                settled])
        self.assertEqual("Settled", rows[0]["title"])


class PeopleRosterTest(unittest.TestCase):
    """Resolving a reporter to a GitHub profile, which the searches depend on."""

    def _gh(self, authors):
        class FakeGitHub:
            def issue(self, owner, repo, number, **kwargs):
                login = authors.get(f"{owner}/{repo}#{number}")
                return ({"user": {"login": login}} if login else None), False
        return FakeGitHub()

    def _urls(self, count, repo="a/b"):
        return [f"https://github.com/{repo}/issues/{i}" for i in range(1, count + 1)]

    def test_a_handle_is_read_off_the_person_s_own_issues(self):
        from tools.impact.collectors import people
        urls = self._urls(4)
        authors = {u.replace("https://github.com/", "").replace("/issues/", "#"):
                   "suyZhong" for u in urls}
        handle, evidence = people.resolve_handle(self._gh(authors), urls)
        self.assertEqual("suyZhong", handle)
        self.assertEqual(1, len(evidence))
        self.assertIn(evidence[0], urls)

    def test_disagreeing_samples_resolve_to_nothing(self):
        """Whoever is credited need not be whoever filed it; do not pick a winner."""
        from tools.impact.collectors import people
        urls = self._urls(4)
        keys = [u.replace("https://github.com/", "").replace("/issues/", "#")
                for u in urls]
        authors = dict(zip(keys, ["alice", "alice", "alice", "bob"]))
        handle, evidence = people.resolve_handle(self._gh(authors), urls)
        self.assertIsNone(handle)
        self.assertEqual([], evidence)

    def test_too_few_samples_resolve_to_nothing(self):
        from tools.impact.collectors import people
        urls = self._urls(2)
        keys = [u.replace("https://github.com/", "").replace("/issues/", "#")
                for u in urls]
        handle, _ = people.resolve_handle(self._gh(dict.fromkeys(keys, "alice")), urls)
        self.assertIsNone(handle)

    def test_a_bot_is_never_resolved_to(self):
        from tools.impact.collectors import people
        urls = self._urls(4)
        keys = [u.replace("https://github.com/", "").replace("/issues/", "#")
                for u in urls]
        handle, _ = people.resolve_handle(
            self._gh(dict.fromkeys(keys, "dependabot[bot]")), urls)
        self.assertIsNone(handle)

    def test_author_searches_carry_no_search_term(self):
        """The whole point: reach reports whose text never names SQLancer."""
        from tools.impact.collectors import people
        queries = people.search_queries(["suyZhong", "DerZc"])
        self.assertEqual(["author:suyZhong type:issue", "author:DerZc type:issue"],
                         queries)
        for query in queries:
            self.assertNotIn("SQLancer", query)

    def test_one_person_recorded_twice_becomes_one_entry(self):
        from tools.impact.collectors import people
        merged = people._merge_by_handle([
            {"id": "person:x", "name": "xhandle", "github": "x",
             "handle_is_verified": True, "reports_on_lab_list": 3,
             "evidence": [{"source_url": "https://example.org/a"}]},
            {"id": "person:x", "name": "X Name", "github": "x",
             "handle_is_verified": False, "reports_on_lab_list": 2,
             "evidence": [{"source_url": "https://example.org/b"}]},
        ])
        self.assertEqual(1, len(merged))
        self.assertEqual(5, merged[0]["reports_on_lab_list"])
        self.assertEqual(["X Name"], merged[0]["also_recorded_as"])
        self.assertEqual(2, len(merged[0]["evidence"]))


class StateOfTheArtTest(unittest.TestCase):
    """Recognition is read off the citing sentence, which is the evidence."""

    def setUp(self):
        from tools.impact.collectors.papers import state_of_the_art_contexts
        self.detect = state_of_the_art_contexts
        self.tax = taxonomy.load()

    def _detect(self, context, technique="tlp"):
        return self.detect([(technique, context)], self.tax)

    def test_a_technique_named_among_state_of_the_art_approaches_counts(self):
        found = self._detect(
            "We further compared ERIQ with four state-of-the-art DBMS logic bug "
            "detection approaches: EDC [4], Radar [34], EET [11], and TLP [30].")
        self.assertEqual(1, len(found))
        self.assertEqual("tlp", found[0][0])

    def test_the_tool_being_called_state_of_the_art_counts(self):
        found = self._detect(
            "SQLancer is the state-of-the-art tool for discovering logic bugs "
            "in DBMS using metamorphic testing [40-42].")
        self.assertEqual(1, len(found))

    def test_alternative_hyphenation_is_matched(self):
        self.assertEqual(1, len(self._detect(
            "NoREC is a state of the art oracle for optimisation bugs.")))

    def test_a_sentence_without_the_phrase_does_not_count(self):
        self.assertEqual([], self._detect(
            "We compared our approach with TLP and NoREC on five systems."))

    def test_a_sentence_without_a_sqlancer_name_does_not_count(self):
        self.assertEqual([], self._detect(
            "We compare against three state-of-the-art fuzzers.", "tlp"))

    def test_recognition_of_an_excluded_technique_is_not_recognition_of_sqlancer(self):
        """A sentence naming only EET or DQE says nothing about SQLancer."""
        self.assertEqual([], self._detect(
            "We compare with two state-of-the-art approaches: EET [11] and "
            "EDC [4].", "tlp"))

    def test_an_ambiguous_acronym_still_needs_database_context(self):
        self.assertEqual([], self._detect(
            "We use the state-of-the-art TLP power management daemon.", "tlp"))

    def test_published_records_carry_the_sentence_as_evidence(self):
        for record in config.load_json(config.DATA_FILES["papers"])["papers"]:
            entry = record["relationships"].get("describes_as_state_of_the_art")
            if not entry or entry.get("value") != "yes":
                continue
            self.assertTrue(entry.get("evidence"))
            for item in entry["evidence"]:
                self.assertTrue(item.get("excerpt"),
                                f"{record['id']} has recognition without a quote")

    def test_recognition_is_not_counted_as_building_on(self):
        from tools.impact.stats import BUILDING_ON
        self.assertNotIn("describes_as_state_of_the_art", BUILDING_ON)


class AuthoritativeMergeTest(unittest.TestCase):
    """A record can leave the dataset, but only on a run that re-derived it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.files = {"papers": self.tmp / "papers.json"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dataset(self):
        dataset = Dataset(self.files)
        dataset.ensure("papers", {"schema_version": "1.0.0",
                                  "policy_version": "test"})
        return dataset

    def _record(self, number):
        return {"id": f"paper:doi:10.1/{number}", "title": f"Paper {number}",
                "year": 2024, "cites_seed_techniques": ["tlp"],
                "relationships": {}, "provenance": {}}

    def test_an_incremental_run_never_removes(self):
        dataset = self._dataset()
        dataset.merge("papers", [self._record(1), self._record(2)],
                      timestamp="2026-01-01T00:00:00Z")
        dataset.save(["papers"])

        dataset = self._dataset()
        report = dataset.merge("papers", [self._record(1)],
                               timestamp="2026-02-01T00:00:00Z")
        self.assertEqual([], report.removed)
        self.assertEqual(2, len(dataset.records("papers")))

    def test_an_authoritative_run_retires_what_it_no_longer_finds(self):
        dataset = self._dataset()
        dataset.merge("papers", [self._record(1), self._record(2)],
                      timestamp="2026-01-01T00:00:00Z")
        dataset.save(["papers"])

        dataset = self._dataset()
        report = dataset.merge("papers", [self._record(1)],
                               timestamp="2026-02-01T00:00:00Z",
                               authoritative=True)
        self.assertEqual(1, len(report.removed))
        self.assertEqual("paper:doi:10.1/2", report.removed[0]["id"])
        self.assertEqual(["paper:doi:10.1/1"],
                         [r["id"] for r in dataset.records("papers")])

    def test_an_authoritative_run_that_finds_everything_removes_nothing(self):
        dataset = self._dataset()
        candidates = [self._record(1), self._record(2)]
        dataset.merge("papers", candidates, timestamp="2026-01-01T00:00:00Z")
        dataset.save(["papers"])

        dataset = self._dataset()
        report = dataset.merge("papers", candidates,
                               timestamp="2026-02-01T00:00:00Z",
                               authoritative=True)
        self.assertEqual([], report.removed)
        self.assertFalse(dataset.save(["papers"])["papers"],
                         "an unchanged authoritative run must rewrite nothing")


class BackoffTest(unittest.TestCase):
    """A 403 is only a throttle when the response says so."""

    def test_a_github_style_throttle_is_retried(self):
        from tools.impact.http import _is_rate_limited
        self.assertTrue(_is_rate_limited({"Retry-After": "60"}))
        self.assertTrue(_is_rate_limited({"X-RateLimit-Remaining": "0"}))

    def test_a_plain_refusal_is_not_retried(self):
        from tools.impact.http import _is_rate_limited
        self.assertFalse(_is_rate_limited({}))
        self.assertFalse(_is_rate_limited({"X-RateLimit-Remaining": "4999"}))
        self.assertFalse(_is_rate_limited({"Server": "Apache"}))


class LabImportTest(unittest.TestCase):
    """The TEST lab list is a source of candidates, never of attributions."""

    ENTRIES = [
        {"url": "https://github.com/duckdb/duckdb/issues/1",
         "title": "Wrong result with a filter", "created_at": "01/02/2023",
         "state": "closed", "resolution": "fixed", "domain": "dbms",
         "system": "DuckDB", "reported_by": "someone"},
        {"url": "https://github.com/duckdb/duckdb/issues/2",
         "title": "Crash in the parser", "created_at": "02/02/2023",
         "state": "closed", "resolution": "fixed", "domain": "dbms",
         "system": "DuckDB", "reported_by": "someone"},
        {"url": "https://github.com/php/php-src/issues/3",
         "title": "Not a database bug", "created_at": "03/02/2023",
         "state": "open", "resolution": "open", "domain": "compiler&interpreter",
         "system": "php-src", "reported_by": "someone"},
    ]

    BODIES = {
        1: "Wrong result with a filter\n\nFound by SQLancer using the NoREC "
           "oracle while testing DuckDB.",
        2: "Crash in the parser\n\nReduced from a fuzzing run with AFL.",
    }

    class FakeGitHub:
        def __init__(self, bodies):
            self.bodies = bodies
            self.fetched = []

        def issue(self, owner, repo, number, **kwargs):
            self.fetched.append(number)
            body = self.bodies.get(number)
            if body is None:
                return None, False
            return {"title": "", "body": body}, False

    def _collect(self, entries=None, gh=None):
        gh = gh or self.FakeGitHub(self.BODIES)
        return nus_test.collect(gh, None, fetcher=None, entries=entries or self.ENTRIES,
                                timestamp="2026-01-01T00:00:00Z")

    def test_only_reports_naming_sqlancer_are_imported(self):
        records, rejected, _ = self._collect()
        self.assertEqual(1, len(records))
        self.assertEqual("norec", records[0]["technique"])
        self.assertEqual("duckdb", records[0]["dbms"])
        self.assertTrue(any("no SQLancer tool" in entry["reason"]
                            for entry in rejected))

    def test_a_lab_bug_found_with_another_tool_is_not_attributed(self):
        records, rejected, _ = self._collect(entries=[self.ENTRIES[1]])
        self.assertEqual([], records)
        self.assertEqual(1, len(rejected))

    def test_non_database_domains_are_skipped_without_a_fetch(self):
        gh = self.FakeGitHub(self.BODIES)
        index = [entry for entry in self.ENTRIES
                 if entry["domain"] == nus_test.DBMS_DOMAIN]
        self._collect(entries=index, gh=gh)
        self.assertNotIn(3, gh.fetched)

    def test_the_lab_page_is_recorded_as_corroborating_evidence(self):
        records, _, _ = self._collect()
        sources = [item["source_url"] for item in
                   records[0]["attribution"]["evidence"]]
        self.assertIn(nus_test.PAGE_URL, sources)
        self.assertIn("https://github.com/duckdb/duckdb/issues/1", sources)

    def test_the_run_budget_defers_rather_than_drops(self):
        records, _, needs_review = nus_test.collect(
            self.FakeGitHub(self.BODIES), None, fetcher=None,
            entries=self.ENTRIES[:2], max_reports=1,
            timestamp="2026-01-01T00:00:00Z")
        deferred = [entry for entry in needs_review
                    if "run budget" in entry["reason"]]
        self.assertEqual(1, len(deferred))

    def test_upstream_resolutions_map_onto_the_status_vocabulary(self):
        allowed = {"fixed", "fixed_in_documentation", "verified", "open",
                   "closed_not_a_bug", "closed_duplicate", "unknown"}
        for status, _ in nus_test.RESOLUTION_STATUS.values():
            self.assertIn(status, allowed)
        self.assertEqual(("unknown", False),
                         nus_test._status_of({"resolution": "???", "state": ""}))


class PlotTest(unittest.TestCase):
    def setUp(self):
        self.stats = config.load_json(config.DATA_FILES["stats"])
        self.charts = plots.build_all(self.stats)

    def test_every_chart_is_well_formed_svg(self):
        for name, markup in self.charts.items():
            self.assertTrue(markup.startswith("<svg"), name)
            self.assertTrue(markup.rstrip().endswith("</svg>"), name)
            import xml.etree.ElementTree as ET
            ET.fromstring(markup)

    def test_bar_counts_match_the_records(self):
        """Every value drawn must appear in the statistics it came from."""
        expected = {str(row["count"]) for row in self.stats["bugs"]["by_dbms"]}
        markup = self.charts["bugs-by-dbms"]
        for count in expected:
            self.assertIn(f">{count}</text>", markup)

    def test_the_paper_relationship_chart_matches_the_paper_records(self):
        """Every bar's tooltip carries the count the statistics hold.

        The series drawn are the reported ones: reuse and extension share a
        bar, because a paper can do both and two bars would count it twice.
        The halves are still in the data and are checked to sum to at least
        the combined figure, which they must if the union is deduplicated.
        """
        yearly = self.stats["papers"]["by_relationship_year"]
        markup = self.charts["papers-relationships-by-year"]
        drawn = ("reusing_or_extending", "compares_with",
                 "describes_as_state_of_the_art")
        for key in drawn:
            for entry in yearly.get(key, []):
                if entry["count"] > 0:
                    self.assertIn(f": {entry['count']}</title>", markup)
        for index, entry in enumerate(yearly["reusing_or_extending"]):
            halves = (yearly["uses_infrastructure"][index]["count"]
                      + yearly["extends_technique"][index]["count"])
            self.assertGreaterEqual(halves, entry["count"], entry["label"])
            self.assertLessEqual(entry["count"], halves, entry["label"])

    def test_committed_plots_are_up_to_date(self):
        for name, markup in self.charts.items():
            path = config.PLOT_DIR / f"{name}.svg"
            self.assertTrue(path.exists(), f"{name}.svg is missing")
            self.assertEqual(markup, path.read_text(encoding="utf-8"),
                             f"{name}.svg is stale; run "
                             f"`python -m tools.impact.run plots`")


class RoundingTest(unittest.TestCase):
    def test_headline_rounds_down(self):
        cases = {0: "0", 7: "7", 12: "10+", 99: "90+", 456: "400+",
                 1483: "1,000+", 2000: "2,000+"}
        for total, expected in cases.items():
            self.assertEqual(expected, stats_module.round_down_headline(total))


if __name__ == "__main__":
    unittest.main()


class IntakeReportingTest(unittest.TestCase):
    """Every downloaded PDF is accounted for by name.

    A file the intake cannot place used to be counted and then dropped from the
    report. To the person who downloaded it that is indistinguishable from a
    file that was filed, so they download it again -- which is exactly what
    happened before this was fixed.
    """

    def _pdf(self, tmp, name, text):
        """A one-page PDF whose page text is ``text``.

        Skips rather than fails without pypdf. Every production caller treats
        the package as optional and degrades to "no full text", so a checkout
        without it is a supported state, not a broken one.
        """
        try:
            import pypdf
            from pypdf.generic import DecodedStreamObject, NameObject
        except ImportError:
            self.skipTest("pypdf is not installed; PDF reading is optional")
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject()
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream.set_data(f"BT /F1 12 Tf 72 700 Td ({escaped}) Tj ET".encode())
        page = writer.pages[0]
        page[NameObject("/Contents")] = writer._add_object(stream)
        path = tmp / name
        with open(path, "wb") as handle:
            writer.write(handle)
        return path

    def test_a_pdf_matching_nothing_is_named_in_the_report(self):
        import io, contextlib, tempfile, pathlib
        from tools.impact import intake
        with tempfile.TemporaryDirectory() as raw:
            tmp = pathlib.Path(raw)
            self._pdf(tmp, "mystery.pdf", "An unrelated paper about nothing")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                counts = intake.run(tmp, dry_run=True, limit=5,
                                    destination=tmp / "filed",
                                    candidates=[{"title": "Some Other Paper",
                                                 "filename": "other.pdf",
                                                 "doi": "10.1/other"}],
                                    wanted={"other.pdf"})
            self.assertEqual(1, counts["unmatched"])
            self.assertIn("mystery.pdf", buf.getvalue())
            self.assertEqual(1, len(counts["_unresolved"]))


class DbmsPagesTest(unittest.TestCase):
    """One page per database system, generated from the statistics."""

    def _stats(self, rows):
        return {"dbms": {"rows": rows}}

    def test_a_system_with_nothing_recorded_gets_no_page(self):
        """An empty page is worse than no link to one."""
        from tools.impact import pages
        rows = [{"id": "known", "name": "Known", "bugs": 3, "supported": False,
                 "adoption_relationships": [], "planned_adoption": False},
                {"id": "silent", "name": "Silent", "bugs": 0, "supported": False,
                 "adoption_relationships": [], "planned_adoption": False}]
        wanted = [row["id"] for row in pages.wanted(self._stats(rows))]
        self.assertEqual(["known"], wanted)

    def test_a_supported_system_without_bugs_still_gets_one(self):
        from tools.impact import pages
        rows = [{"id": "fresh", "name": "Fresh", "bugs": 0, "supported": True,
                 "adoption_relationships": [], "planned_adoption": False}]
        self.assertEqual(["fresh"],
                         [row["id"] for row in pages.wanted(self._stats(rows))])

    def test_the_excerpt_keeps_a_systems_own_capitalisation(self):
        """``capitalize`` would turn DuckDB into duckdb."""
        from tools.impact import pages
        text = pages.excerpt_for({"id": "duckdb", "name": "DuckDB", "bugs": 12,
                                  "adoption_relationships": [],
                                  "planned_adoption": False})
        self.assertIn("DuckDB", text)
        self.assertTrue(text.startswith("12 bugs"), text)

    def test_a_system_that_leaves_the_dataset_loses_its_page(self):
        import tempfile, pathlib
        from tools.impact import pages
        with tempfile.TemporaryDirectory() as raw:
            tmp = pathlib.Path(raw)
            (tmp / "gone.html").write_text("stale", encoding="utf-8")
            rows = [{"id": "here", "name": "Here", "bugs": 1, "supported": False,
                     "adoption_relationships": [], "planned_adoption": False}]
            outcome = pages.write_all(self._stats(rows), tmp)
            self.assertEqual("removed", outcome["gone"])
            self.assertFalse((tmp / "gone.html").exists())
            self.assertTrue((tmp / "here.html").exists())

    def test_the_chart_links_each_bar_to_its_system(self):
        from tools.impact import plots
        svg = plots.stacked_horizontal_bars(
            [{"key": "duckdb", "label": "DuckDB", "count": 4,
              "segments": [{"key": "project", "label": "Project", "count": 4}]}],
            title="t", value_label="v", link_base="/impact/dbms/")
        self.assertIn('<a href="/impact/dbms/duckdb/"', svg)
        self.assertEqual(1, svg.count("</a>"))

    def test_a_chart_without_a_link_base_has_no_links(self):
        from tools.impact import plots
        svg = plots.stacked_horizontal_bars(
            [{"key": "duckdb", "label": "DuckDB", "count": 4,
              "segments": [{"key": "project", "label": "Project", "count": 4}]}],
            title="t", value_label="v")
        self.assertNotIn("<a ", svg)


class AffiliationRulingTest(unittest.TestCase):
    """A person can be on the lab's roster and still count as external.

    The roster can see that someone is listed on the lab's people page. It
    cannot see whether their work is this project's, and a lab lists everyone
    working in it -- so that judgement is recorded rather than inferred.
    """

    def test_without_a_ruling_the_roster_decides(self):
        """An empty decisions file leaves every record exactly as it was."""
        from tools.impact import validate
        from tools.impact.collectors import people
        original = people.decisions
        people.decisions = lambda: {"counts_as_external": []}
        try:
            problems = validate.check_affiliation_rulings(
                [{"id": "bug:x:1", "reporter": "someone",
                  "reporter_affiliation": "project"}],
                [{"github": "someone", "name": "Someone"}])
        finally:
            people.decisions = original
        self.assertEqual([], problems)

    def test_the_ruling_in_force_puts_its_person_outside_the_project(self):
        from tools.impact.collectors import people
        self.assertIn("joyemang33", people.counted_as_external())

    def test_a_stale_record_is_reported(self):
        from tools.impact import validate
        from tools.impact.collectors import people
        ruling = {"counts_as_external": [
            {"github": "someone", "reason": "x" * 25}]}
        original = people.decisions
        people.decisions = lambda: ruling
        try:
            problems = validate.check_affiliation_rulings(
                [{"id": "bug:x:1", "reporter": "someone",
                  "reporter_affiliation": "project"}],
                [{"github": "someone", "name": "Someone"}])
        finally:
            people.decisions = original
        self.assertEqual(1, len(problems))
        self.assertIn("re-run collect", str(problems[0]))

    def test_a_misspelled_handle_is_reported(self):
        """A ruling that matches nobody changes nothing and says nothing."""
        from tools.impact import validate
        from tools.impact.collectors import people
        ruling = {"counts_as_external": [
            {"github": "nobody-here", "reason": "x" * 25}]}
        original = people.decisions
        people.decisions = lambda: ruling
        try:
            problems = validate.check_affiliation_rulings(
                [], [{"github": "someone", "name": "Someone"}])
        finally:
            people.decisions = original
        self.assertEqual(1, len(problems))
        self.assertIn("check the spelling", str(problems[0]))


class GitHubCredentialTest(unittest.TestCase):
    """A rejected token fails loudly.

    A 401 used to surface as an empty result set: `_request` raised
    RuntimeError, `search` caught it and returned, and the run reported no new
    bugs. An expired token and a world with no bugs looked exactly alike.
    """

    class _Fetcher:
        def __init__(self, status):
            self.status = status
            self.calls = 0

        def fetch(self, url, **kwargs):
            self.calls += 1
            return {"status": self.status, "body": "{}"}, False

    def _client(self, status):
        from tools.impact.github import GitHub
        gh = GitHub.__new__(GitHub)
        gh.search_fetcher = self._Fetcher(status)
        gh.failed_searches = []
        return gh

    def test_a_rejected_token_raises(self):
        from tools.impact.github import BadCredentials
        gh = self._client(401)
        with self.assertRaises(BadCredentials):
            list(gh.search("issues", "anything"))

    def test_another_failure_is_recorded_rather_than_raised(self):
        """A single dead search must not abort a run, but must leave a trace."""
        gh = self._client(422)
        self.assertEqual([], list(gh.search("issues", "anything")))
        self.assertEqual([("issues", "anything", 422)], gh.failed_searches)

    def test_a_repo_scope_rejects_an_unregistered_repository(self):
        from tools.impact.collectors import github_bugs
        with self.assertRaises(ValueError):
            github_bugs.collect(None, only_repos=["nobody/nothing"])


class EmailedBugTest(unittest.TestCase):
    """Bugs the lab reported privately, which have no link to follow."""

    ENTRIES = [
        {"system": "Umbra", "domain": "dbms", "url": None,
         "title": "Unexpected Results when Comparing Boolean Values",
         "created_at": "11/27/2023", "resolution": "fixed",
         "reported_by": "Suyang Zhong"},
        {"system": "Nowhere", "domain": "dbms", "url": None,
         "title": "Something", "created_at": "01/02/2024",
         "resolution": "fixed", "reported_by": "Someone"},
    ]

    def test_a_report_without_a_link_still_becomes_a_record(self):
        from tools.impact.collectors import nus_test
        records, review = nus_test.emailed_records(
            self.ENTRIES, {"umbra": "umbra"}, timestamp="2026-01-01T00:00:00Z")
        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual("umbra", record["dbms"])
        self.assertEqual("curated_primary_source", record["attribution"]["rule"])
        self.assertEqual("fixed", record["status"])
        self.assertTrue(record["status_is_true_positive"])
        # No report to point at, so no primary_url; pointing every one of them
        # at the list page would make many bugs look like one duplicated.
        self.assertIsNone(record.get("primary_url"))
        self.assertEqual(nus_test.PAGE_URL, record["links"]["record"])

    def test_an_unregistered_system_is_reported_not_guessed(self):
        from tools.impact.collectors import nus_test
        _, review = nus_test.emailed_records(
            self.ENTRIES, {"umbra": "umbra"}, timestamp="2026-01-01T00:00:00Z")
        self.assertEqual(1, len(review))
        self.assertIn("Nowhere", review[0]["reason"])


class SlashDateTest(unittest.TestCase):
    """Slash dates carry no order, and sources are not consistent about it.

    The TEST lab's list has 735 rows that can only be day-first and 147 that
    can only be month-first. The month-first ones matched no format at all and
    came back as no date, which is how 55 Umbra bugs arrived undated.
    """

    def test_a_day_above_twelve_settles_the_order(self):
        from tools.impact.util import parse_date
        self.assertEqual("2023-11-27", parse_date("11/27/2023"))
        self.assertEqual("2025-06-30", parse_date("06/30/2025"))

    def test_a_month_above_twelve_still_reads_day_first(self):
        from tools.impact.util import parse_date
        self.assertEqual("2023-11-27", parse_date("27/11/2023"))

    def test_an_ambiguous_date_keeps_the_previous_reading(self):
        """Nothing in the value can settle it, so the behaviour is unchanged."""
        from tools.impact.util import parse_date
        self.assertEqual("2024-08-02", parse_date("2/8/2024"))

    def test_an_impossible_date_is_no_date(self):
        from tools.impact.util import parse_date
        self.assertIsNone(parse_date("13/13/2020"))


class TechniqueOnlyReportTest(unittest.TestCase):
    """A report naming an oracle but never the tool.

    Every sentence pattern is anchored on the tool's own names, so a report
    saying only "the NoREC-transformed query returns 0" had techniques matched
    and then no sentence to quote, and was thrown away. MariaDB's Jira is full
    of these: the reporters describe the transformation, not the tool.
    """

    def _issue(self, title, body):
        return {"title": title, "body": body, "labels": [{"name": "Bug"}]}

    def test_an_oracle_name_is_its_own_anchor(self):
        from tools.impact import taxonomy
        from tools.impact.collectors import github_bugs
        text = ("NoREC logical bug: the original query returns COUNT(*) = 4, "
                "while the NoREC-transformed query returns 0.")
        found = github_bugs.deterministic_attribution(
            text, taxonomy.load(), self._issue("NoREC logical bug", text))
        self.assertIsNotNone(found)
        rule, technique, _, excerpt = found
        self.assertEqual("norec", technique)
        self.assertIn(excerpt, text)

    def test_an_ambiguous_acronym_still_needs_the_tool_named(self):
        """CERT in a cluster report is a TLS certificate, not an oracle.

        Ambiguous acronyms need database context to count as a technique, and
        in a DBMS tracker that context is always present -- every issue is
        about a database. Twenty-three Galera issues from 2013 to 2017, years
        before SQLancer existed, were admitted as cardinality estimation
        testing before this was required.
        """
        from tools.impact import taxonomy
        from tools.impact.collectors import github_bugs
        text = ("Galera: WSREP fails to accept state transfer because the "
                "cert index and the database query log disagree.")
        found = github_bugs.deterministic_attribution(
            text, taxonomy.load(), self._issue("Galera: WSREP fails", text))
        self.assertIsNone(found)


class ReviewQueueTest(unittest.TestCase):
    """Candidates a collector could not place, kept between runs.

    The list used to live only in the run report. Three real gaps were found
    by hand that it had already flagged and thrown away: MatrixOne's bugs,
    Oxla's, and a TimescaleDB provider in a fork.
    """

    ITEM = {"kind": "bug", "url": "https://github.com/a/b/issues/1",
            "reason": "repository is not a registered database system",
            "title": "Something"}

    def _queue(self):
        return {"schema_version": "1.0.0", "open": [], "dismissed": []}

    def test_an_item_is_kept_and_recognised_next_run(self):
        from tools.impact import review
        queue = self._queue()
        first = review.merge(queue, [self.ITEM], source="github_bugs",
                             timestamp="2026-01-01T00:00:00Z")
        second = review.merge(queue, [self.ITEM], source="github_bugs",
                              timestamp="2026-02-01T00:00:00Z")
        self.assertEqual(1, first["added"])
        self.assertEqual(1, second["seen_again"])
        self.assertEqual(1, len(queue["open"]))
        row = queue["open"][0]
        self.assertEqual("2026-01-01T00:00:00Z", row["first_seen"])
        self.assertEqual("2026-02-01T00:00:00Z", row["last_seen"])

    def test_an_item_leaves_when_its_record_is_admitted(self):
        from tools.impact import review
        queue = self._queue()
        review.merge(queue, [self.ITEM], source="github_bugs")
        counts = review.merge(queue, [], source="github_bugs",
                              admitted={"https://github.com/a/b/issues/1"})
        self.assertEqual(1, counts["resolved"])
        self.assertEqual([], queue["open"])

    def test_absence_alone_does_not_resolve_an_item(self):
        """A run that did not reach an item looks like one that resolved it."""
        from tools.impact import review
        queue = self._queue()
        review.merge(queue, [self.ITEM], source="github_bugs")
        review.merge(queue, [], source="github_bugs")
        self.assertEqual(1, len(queue["open"]))

    def test_a_dismissed_item_is_never_raised_again(self):
        from tools.impact import review
        queue = self._queue()
        review.merge(queue, [self.ITEM], source="github_bugs")
        queue["dismissed"] = [{"id": queue["open"][0]["id"],
                               "reason": "not a database system"}]
        queue["open"] = []
        counts = review.merge(queue, [self.ITEM], source="github_bugs")
        self.assertEqual(0, counts["added"])
        self.assertEqual([], queue["open"])


class ReproducerBackedAcronymTest(unittest.TestCase):
    """An ambiguous acronym may anchor when the reproducer backs it.

    "TLP equivalence verification" over a generated t0(c0 ...) schema is a
    SQLancer oracle; "cert index" in a Galera cluster report is a TLS
    certificate. The reproducer is what separates them, and it works in any
    language -- openGauss's tracker is in Chinese, where no English sentence
    pattern can reach.
    """

    def _issue(self, title, body):
        return {"title": title, "body": body, "labels": [{"name": "Bug"}]}

    def test_a_generated_schema_lets_tlp_anchor(self):
        from tools.impact import taxonomy
        from tools.impact.collectors import github_bugs
        text = ("TLP equivalence verification reports inconsistent results\n"
                "Test type: SQL functionality. Database version: 7.0.0\n"
                "CREATE UNLOGGED TABLE t0(c0 boolean PRIMARY KEY UNIQUE);\n"
                "CREATE TABLE IF NOT EXISTS t1(c0 TEXT PRIMARY KEY, c1 money);\n"
                "SELECT SUM(t1.c1) FROM t1 HAVING true;\n")
        found = github_bugs.deterministic_attribution(
            text, taxonomy.load(), self._issue("TLP equivalence", text))
        self.assertIsNotNone(found)
        self.assertEqual("tlp", found[1])

    def test_without_a_reproducer_the_acronym_is_still_refused(self):
        from tools.impact import taxonomy
        from tools.impact.collectors import github_bugs
        text = ("Galera: WSREP fails to accept state transfer because the "
                "cert index and the database query log disagree.")
        self.assertIsNone(github_bugs.deterministic_attribution(
            text, taxonomy.load(), self._issue("Galera: WSREP fails", text)))


class GiteeCollectorTest(unittest.TestCase):
    """Gitee hosts trackers the GitHub search cannot see.

    openGauss's is the case this exists for: a report titled "TLP 等价验证出现
    结果内容不一致" over a generated t0(c0 ...) schema, invisible to every
    other route.
    """

    def test_only_registered_repositories_are_searched(self):
        """An unscoped sweep put a thousand throwaway repositories into the
        review queue and reached no real report."""
        from tools.impact.collectors import gitee_bugs
        index = gitee_bugs.repository_index()
        self.assertIn("opengauss/opengauss-server", index)
        self.assertEqual("opengauss", index["opengauss/opengauss-server"])

    def test_the_scope_carries_the_full_repository_name(self):
        from tools.impact.collectors import gitee_bugs

        seen = []

        class _Fetcher:
            def fetch(self, url, **kwargs):
                seen.append(url)
                return {"status": 200, "body": "[]"}, False

        gitee_bugs.search(_Fetcher(), "TLP",
                          repository="opengauss/openGauss-server")
        self.assertIn("owner=opengauss", seen[0])
        # The full owner/name, which is the form the API accepts; a bare
        # repository name returns nothing.
        self.assertIn("repo=opengauss/openGauss-server", seen[0])

    def test_a_throttled_search_is_not_an_empty_one(self):
        from tools.impact.collectors import gitee_bugs

        class _Fetcher:
            def fetch(self, url, **kwargs):
                return {"status": 403, "body": ""}, False

        del gitee_bugs.FAILED_SEARCHES[:]
        self.assertEqual([], gitee_bugs.search(_Fetcher(), "TLP",
                                               repository="a/b"))
        self.assertEqual(1, len(gitee_bugs.FAILED_SEARCHES))

    def test_a_rejected_issue_is_not_a_true_positive(self):
        from tools.impact.collectors import gitee_bugs
        self.assertEqual(("closed_not_a_bug", False),
                         gitee_bugs.status_of({"state": "rejected"}))
        self.assertEqual(("fixed", True),
                         gitee_bugs.status_of({"state": "closed"}))
        self.assertEqual(("open", True),
                         gitee_bugs.status_of({"state": "progressing"}))

    def test_a_run_stops_once_it_is_blocked(self):
        """Gitee blocks by address, so continuing costs and gains nothing."""
        from tools.impact.collectors import gitee_bugs
        del gitee_bugs.FAILED_SEARCHES[:]
        self.assertFalse(gitee_bugs._blocked())
        for _ in range(gitee_bugs.MAX_CONSECUTIVE_FAILURES):
            gitee_bugs.FAILED_SEARCHES.append(("TLP", "a/b", 403))
        self.assertTrue(gitee_bugs._blocked())
        del gitee_bugs.FAILED_SEARCHES[:]


class UnreadableTrackerTest(unittest.TestCase):
    """A link a reader cannot follow is not published as the report.

    Oxla tracks its bugs in a private Jira, so twelve records pointed at
    oxla.atlassian.net pages that return 404 to everybody. The public evidence
    is the bug file in the fork, and that is what the record links.
    """

    ENTRY = {"constant": "bugOxla8408", "urls": ["https://x.atlassian.net/browse/OXLA-8408"],
             "note": "See: https://x.atlassian.net/browse/OXLA-8408 A query fails.",
             "excerpt": "/// A query fails."}
    FILE = "https://github.com/o/r/blob/main/src/sqlancer/oxla/OxlaBugs.java"

    def _record(self, readable):
        from tools.impact.collectors import provider_bugs
        return provider_bugs.build_record(
            self.ENTRY, "oxla", url=self.ENTRY["urls"][0], source_url=self.FILE,
            repository="o/r", timestamp="2026-01-01T00:00:00Z",
            status=("unknown", True), readable=readable)

    def test_an_unreadable_tracker_is_not_the_primary_url(self):
        record = self._record(False)
        self.assertIsNone(record.get("primary_url"))
        self.assertEqual(self.FILE, record["links"]["record"])
        self.assertEqual(self.ENTRY["urls"][0], record["links"]["tracker"])
        self.assertIn("not public", record["attribution"]["evidence"][0]["note"])

    def test_a_readable_tracker_is_linked_as_the_report(self):
        record = self._record(True)
        self.assertEqual(self.ENTRY["urls"][0], record["primary_url"])
        self.assertEqual(self.ENTRY["urls"][0], record["links"]["report"])
        self.assertNotIn("tracker", record["links"])

    def test_only_a_definite_refusal_counts_as_unreadable(self):
        """A 403 means this client is refused, not that nobody can read it."""
        from tools.impact.collectors import provider_bugs

        class _Fetcher:
            def __init__(self, status):
                self.status = status

            def fetch(self, url, **kwargs):
                return {"status": self.status}, False

        self.assertFalse(provider_bugs.is_publicly_readable("u", _Fetcher(404)))
        self.assertTrue(provider_bugs.is_publicly_readable("u", _Fetcher(403)))
        self.assertTrue(provider_bugs.is_publicly_readable("u", _Fetcher(200)))
        self.assertTrue(provider_bugs.is_publicly_readable("u", None))


class ProviderBugUrlTest(unittest.TestCase):
    """The field name says which of several cited issues the entry is about.

    CockroachDB's file has an entry citing a closed bug and the underlying one
    it waits on. Taking the first URL recorded issue 84078 for a field named
    bug84154, and titled it with the maintainers' note above the field.
    """

    def test_the_field_name_picks_the_issue(self):
        from tools.impact.collectors import provider_bugs
        entry = {
            "constant": "bug84154",
            "urls": ["https://github.com/cockroachdb/cockroach/issues/84078",
                     "https://github.com/cockroachdb/cockroach/issues/84154"],
            "note": "The following bug is closed, but leave it enabled.",
            "excerpt": "// The following bug is closed",
        }
        self.assertEqual("https://github.com/cockroachdb/cockroach/issues/84154",
                         provider_bugs.bug_url(entry))

    def test_a_single_url_is_used_whatever_the_name(self):
        from tools.impact.collectors import provider_bugs
        entry = {"constant": "bugOxla8408", "note": "x", "excerpt": "y",
                 "urls": ["https://x.atlassian.net/browse/OXLA-8408"]}
        self.assertEqual("https://x.atlassian.net/browse/OXLA-8408",
                         provider_bugs.bug_url(entry))

    def test_the_reports_own_title_wins(self):
        """The comment above a field is sometimes a note, not a bug title."""
        from tools.impact.collectors import provider_bugs
        entry = {"constant": "bug84154", "note": "The following bug is closed",
                 "excerpt": "// The following bug is closed",
                 "urls": ["https://github.com/cockroachdb/cockroach/issues/84154"]}
        record = provider_bugs.build_record(
            entry, "cockroachdb", url=entry["urls"][0], source_url="file",
            repository="sqlancer/sqlancer", timestamp="2026-01-01T00:00:00Z",
            status=("fixed", True), title="sql: interval overflow panics")
        self.assertEqual("sql: interval overflow panics", record["title"])


class RecognitionHighlightTest(unittest.TestCase):
    """The featured quotes are ones the records actually hold.

    The highlights file names a paper and a mention id rather than copying the
    sentence, so a quote on the page cannot say something the paper did not.
    What it can do is go stale, and then the page quotes nothing.
    """

    def test_every_highlight_resolves(self):
        from tools.impact import config, validate
        papers = config.load_json(config.DATA_FILES["papers"])["papers"]
        self.assertEqual([], [str(p) for p in
                              validate.check_recognition_highlights(papers)])

    def test_a_highlight_on_a_paper_that_no_longer_qualifies_is_caught(self):
        from tools.impact import validate
        papers = [{"id": "paper:doi:10.1109/icde65706.2026.00240",
                   "relationships": {"describes_as_state_of_the_art":
                                     {"value": "no"}}}]
        problems = validate.check_recognition_highlights(papers)
        self.assertTrue(any("no longer describes" in str(p) for p in problems))

    def test_a_highlight_naming_an_unknown_paper_is_caught(self):
        from tools.impact import validate
        problems = validate.check_recognition_highlights([])
        self.assertTrue(problems)
        self.assertTrue(all("no paper with this id" in str(p)
                            for p in problems))

    def test_every_highlighted_paper_still_says_it(self):
        """The nine are a subset of the papers the data classifies as such."""
        import json
        from tools.impact import config
        highlights = config.load_json(
            config.DATA_DIR / "recognition_highlights.json")["highlights"]
        papers = {p["id"]: p for p in
                  config.load_json(config.DATA_FILES["papers"])["papers"]}
        for entry in highlights:
            paper = papers[entry["paper_id"]]
            self.assertEqual(
                "yes",
                paper["relationships"]["describes_as_state_of_the_art"]["value"],
                entry["paper_id"])


class TalkTranscriptTest(unittest.TestCase):
    """A talk's words are read, not fetched, and the reading has to be honest.

    Three things here are the whole point of the module, and each has already
    gone wrong once: a mention has to land on the second it was said, distant
    parts of a transcript must not run together into one remark, and a name the
    transcriber got wrong has to be recorded as misheard rather than quietly
    presented as what the speaker said.
    """

    def test_a_misheard_name_is_matched_and_recorded_as_misheard(self):
        from tools.impact import talks
        found = talks.findings_from_segments(
            [{"start_ms": 295000,
              "text": "as another example SQL lenser which is a fuzzer for "
                      "database Management Systems"}],
            url="https://www.youtube.com/watch?v=6YGqFRTe2D0")
        self.assertEqual(1, len(found))
        self.assertIn("sqlancer", found[0]["matched"])
        self.assertEqual("SQL lenser", found[0]["heard_as"])
        self.assertEqual("captions", found[0]["source"])

    def test_distant_segments_do_not_become_one_remark(self):
        """Captions carry no full stops, so only the clock can end a sentence."""
        from tools.impact import talks
        found = talks.findings_from_segments(
            [{"start_ms": 295000, "text": "one example is SQL lenser"},
             {"start_ms": 1509000, "text": "a quote about Manuel Rigger here"}],
            url="https://www.youtube.com/watch?v=x")
        self.assertEqual(2, len(found))
        self.assertEqual([295, 1509], [m["at_seconds"] for m in found])
        self.assertEqual(["4:55", "25:09"], [m["timestamp"] for m in found])

    def test_the_link_points_at_the_moment_not_at_the_start(self):
        from tools.impact import talks
        long_lead = "and so " * 120
        found = talks.findings_from_segments(
            [{"start_ms": 0, "text": long_lead},
             {"start_ms": 600000, "text": "this is where SQLancer comes up"}],
            url="https://www.youtube.com/watch?v=x")
        self.assertEqual(1, len(found))
        self.assertEqual(600, found[0]["at_seconds"])
        self.assertIn("t=600s", found[0]["url"])

    def test_slides_are_quoted_as_written(self):
        from tools.impact import talks
        found = talks.findings_from_slides(
            "Tests new code for edge cases. SQLancer — logical SQL fuzzer "
            "Developed by Manuel Rigger at ETH Zurich. Added to ClickHouse.",
            url="https://presentations.clickhouse.com/2021-cpp-siberia/index.html")
        self.assertEqual(1, len(found))
        self.assertEqual("slides", found[0]["source"])
        self.assertIsNone(found[0]["heard_as"])
        self.assertTrue(found[0]["excerpt"].startswith("SQLancer — logical"))

    def test_every_stored_excerpt_is_in_its_captured_transcript(self):
        """The excerpts on the site are substrings of what was captured."""
        import json
        from tools.impact import config, talks
        for talk in talks.load()["talks"]:
            for mention in talk["mentions"]:
                if mention["source"] != "captions" or not mention["excerpt"]:
                    continue
                path = (talks.TRANSCRIPT_CACHE
                        / f"{talk['video_id']}.json")
                self.assertTrue(path.exists(), f"no capture for {talk['id']}")
                with path.open() as handle:
                    captured = json.load(handle)
                haystack = " ".join(s["text"] for s in captured["segments"])
                self.assertIn(mention["excerpt"], haystack)

    def test_the_records_validate(self):
        from tools.impact import talks, validate
        self.assertEqual([], [str(p) for p in
                              validate.check_talks(talks.load()["talks"])])

    def test_a_caption_mention_without_its_moment_is_caught(self):
        from tools.impact import validate
        problems = validate.check_talks([{
            "id": "talk:youtube:x",
            "sources": [{"kind": "captions", "status": "extracted"}],
            "mentions": [{"id": "M1", "source": "captions", "at_seconds": 295,
                          "url": "https://www.youtube.com/watch?v=x",
                          "matched": ["sqlancer"], "excerpt": "…",
                          "excerpt_is_verbatim": True}],
        }])
        self.assertTrue(any("does not point at the moment" in str(p)
                            for p in problems))

    def test_a_talk_nobody_read_cannot_be_quoted(self):
        from tools.impact import validate
        problems = validate.check_talks([{
            "id": "talk:youtube:x",
            "sources": [{"kind": "watched", "status": "extracted"}],
            "mentions": [{"id": "M1", "source": "watched",
                          "url": "https://www.youtube.com/watch?v=x",
                          "matched": ["sqlancer"],
                          "excerpt": "words nobody transcribed",
                          "excerpt_is_verbatim": True}],
        }])
        self.assertTrue(any("cannot carry a quotation" in str(p)
                            for p in problems))

    def test_every_talk_reaches_the_resource_list_and_a_page(self):
        from tools.impact import config, pages, talks
        urls = {r["url"] for r in
                config.load_json(config.DATA_FILES["resources"])["resources"]
                if r["type"] == "talk"}
        for talk in talks.load()["talks"]:
            self.assertIn(talk["url"], urls)
            self.assertTrue(
                (pages.TALK_PAGE_DIR / f"{pages.talk_slug(talk)}.html").exists(),
                f"no page for {talk['id']}")


class PeerToolTest(unittest.TestCase):
    """The README's links section lists peer tools, not only derived ones.

    Jepsen, SQLsmith and Squirrel are other people's database testing tools.
    They belong in SQLancer's README -- they are what a reader should look at
    next -- but a resource list that counts them is padding itself with work
    that says nothing about SQLancer's reach.
    """

    def test_a_peer_tool_is_not_a_resource(self):
        from tools.impact import config
        urls = {r["url"] for r in
                config.load_json(config.DATA_FILES["resources"])["resources"]}
        for peer in ("https://github.com/jepsen-io",
                     "https://github.com/anse1/sqlsmith",
                     "https://github.com/s3team/Squirrel"):
            self.assertNotIn(peer, urls)

    def test_a_tool_the_readme_connects_to_sqlancer_is_kept(self):
        """SQLRight earns its place: the line says it supports NoREC and TLP."""
        from tools.impact import config
        records = {r["url"]: r for r in
                   config.load_json(config.DATA_FILES["resources"])["resources"]}
        kept = records["https://github.com/PSU-Security-Universe/sqlright"]
        self.assertIn("NoREC", kept["evidence"][0]["excerpt"])


class TalkFrameTest(unittest.TestCase):
    """Some talks name SQLancer only on a slide, which no transcript reaches.

    A frame is the evidence there, and it is held to a rule the rest of the
    dataset cannot enforce on it: nothing is transcribed off a picture. Every
    other excerpt here is a substring of something fetched, and a check can say
    so; text read off an image is a person's reading, so the image is shown and
    a note says what is in it.
    """

    def test_no_frame_carries_a_quotation(self):
        from tools.impact import talks
        for talk in talks.load()["talks"]:
            for mention in talk["mentions"]:
                if mention["source"] == "frame":
                    self.assertIsNone(mention["excerpt"], mention["id"])
                    self.assertTrue(mention.get("note"))

    def test_every_frame_image_is_in_the_repository(self):
        from tools.impact import config, talks
        seen = 0
        for talk in talks.load()["talks"]:
            for mention in talk["mentions"]:
                if mention.get("image"):
                    seen += 1
                    self.assertTrue(
                        (config.REPO_ROOT / mention["image"].lstrip("/")).exists(),
                        mention["image"])
        self.assertGreater(seen, 0)

    def test_a_frame_without_its_image_is_caught(self):
        from tools.impact import validate
        problems = validate.check_talks([{
            "id": "talk:youtube:x",
            "sources": [{"kind": "frame", "status": "extracted"}],
            "mentions": [{"id": "M1", "source": "frame", "at_seconds": 10,
                          "url": "https://www.youtube.com/watch?v=x&t=10s",
                          "matched": ["sqlancer"], "note": "a slide",
                          "image": "/assets/images/impact/talks/nope-10.jpg"}],
        }])
        self.assertTrue(any("not in the repository" in str(p) for p in problems))

    def test_mentions_run_in_the_order_the_talk_does(self):
        from tools.impact import talks
        for talk in talks.load()["talks"]:
            timed = [m["at_seconds"] for m in talk["mentions"]
                     if m.get("at_seconds") is not None]
            self.assertEqual(sorted(timed), timed, talk["id"])

    def test_a_viewers_account_yields_to_a_better_source(self):
        """Once a caption or a frame reaches a talk, the account is not evidence."""
        from tools.impact import talks
        for talk in talks.load()["talks"]:
            kinds = {m["source"] for m in talk["mentions"]}
            if kinds & {"captions", "frame", "slides"}:
                self.assertNotIn("watched", kinds, talk["id"])


class SuppliedTranscriptTest(unittest.TestCase):
    """A transcript somebody got out by hand has to be accepted as it comes.

    YouTube will not serve some talks' captions to anything but its own player,
    so the way in is a person: the panel's copy button, a downloaded .vtt,
    yt-dlp. Which of those it was is not something they should have to care
    about, and the rolling repetition auto-captions carry is not something they
    should have to clean up.
    """

    VTT = ("WEBVTT\n\n"
           "00:04:55.000 --> 00:04:58.120 align:start position:0%\n"
           "as another example <00:04:56.000><c> SQL</c><c> lenser</c>\n\n"
           "00:04:58.120 --> 00:05:01.000\n"
           "as another example SQL lenser\n"
           "which is a fuzzer for database systems\n")

    def test_a_vtt_is_read_and_its_repetition_dropped(self):
        from tools.impact import talks
        segments = talks.parse_transcript(self.VTT)
        self.assertEqual(["as another example SQL lenser",
                          "which is a fuzzer for database systems"],
                         [s["text"] for s in segments])
        self.assertEqual([295000, 298000], [s["start_ms"] for s in segments])

    def test_an_srt_is_read_too(self):
        from tools.impact import talks
        srt = ("1\n00:04:55,000 --> 00:04:58,120\n"
               "as another example SQL lenser\n")
        segments = talks.parse_transcript(srt)
        self.assertEqual(1, len(segments))
        self.assertEqual(295000, segments[0]["start_ms"])

    def test_a_transcript_copied_out_of_the_panel_is_read(self):
        from tools.impact import talks
        for pasted in ("3:25\nSo the first one is pivoted query synthesis.\n",
                       "3:25 So the first one is pivoted query synthesis.\n"):
            segments = talks.parse_transcript(pasted)
            self.assertEqual([205000], [s["start_ms"] for s in segments])

    def test_an_hour_long_talk_keeps_its_hours(self):
        from tools.impact import talks
        segments = talks.parse_transcript("1:00:36\nsorry about that\n")
        self.assertEqual(3636000, segments[0]["start_ms"])

    def test_only_the_windows_around_a_mention_are_kept(self):
        """A whole talk is not stored; the sentences that mention it are."""
        from tools.impact import talks
        segments = [{"start_ms": i * 5000, "text": f"line {i}"}
                    for i in range(40)]
        segments[20]["text"] = "and this is where SQLancer comes up"
        mentions = talks.findings_from_segments(
            segments, url="https://www.youtube.com/watch?v=x")
        kept = talks.windows_around(segments, mentions)
        self.assertEqual(5, len(kept))
        self.assertEqual(40 - 5, len(segments) - len(kept))
        self.assertIn("SQLancer", kept[2]["text"])


class MisspelledAuthorTest(unittest.TestCase):
    """Transcribers get the first name wrong as often as they get it right.

    "Manual Rigger" is what SQLite's creator's talk came back as, and matching
    on the full name missed the only mention in it. The surname carries enough
    on its own here -- "trigger" is the one word that would collide, and a word
    boundary keeps it out.
    """

    def test_the_surname_alone_is_a_mention(self):
        from tools.impact import talks
        for spelling in ("Manual Rigger came up with this idea",
                         "Manuel Rigger came up with this idea",
                         "Rigger's fuzzers find wrong answers here"):
            found = talks.findings_from_segments(
                [{"start_ms": 0, "text": spelling}],
                url="https://www.youtube.com/watch?v=x")
            self.assertEqual(["author"], found[0]["matched"], spelling)

    def test_trigger_is_not_the_author(self):
        from tools.impact import talks
        self.assertEqual([], talks.findings_from_segments(
            [{"start_ms": 0, "text": "inputs that trigger many known errors"}],
            url="https://www.youtube.com/watch?v=x"))

    def test_a_short_mention_carries_the_thought_it_introduces(self):
        """A name in one breath, what it is for in the next."""
        from tools.impact import talks
        found = talks.findings_from_segments(
            [{"start_ms": 0, "text": "Then Rigger came up with this idea."},
             {"start_ms": 5000,
              "text": "He tested for inconsistencies in SQL instead."}],
            url="https://www.youtube.com/watch?v=x")
        self.assertIn("inconsistencies in SQL", found[0]["excerpt"])

    def test_a_transcript_that_says_nothing_says_so(self):
        """Miryung Kim's keynote never speaks the name; the slide carries it."""
        from tools.impact import talks
        talk = next(t for t in talks.load()["talks"]
                    if t["video_id"] == "L90MBb6NLBE")
        captions = next(s for s in talk["sources"] if s["kind"] == "captions")
        self.assertEqual("extracted", captions["status"])
        self.assertIn("not spoken anywhere", captions["note"])
        self.assertEqual(["frame"], [m["source"] for m in talk["mentions"]])


class FrameWorklistTest(unittest.TestCase):
    """Which moments want a picture is a rule, not a judgement call.

    Choosing them by eye missed two: both were moments where the transcript had
    misheard the name, which is exactly when the slide is worth having. The
    worklist asks the data instead.
    """

    def test_a_misheard_name_wants_a_frame(self):
        from tools.impact import talks
        wanted = talks.frames_wanted({
            "url": "https://www.youtube.com/watch?v=x",
            "sources": [{"kind": "captions", "status": "extracted"}],
            "mentions": [{"id": "M1", "source": "captions", "at_seconds": 100,
                          "timestamp": "1:40", "heard_as": "SQL lenser",
                          "excerpt": "one example is SQL lenser",
                          "url": "https://www.youtube.com/watch?v=x&t=100s"}],
        })
        self.assertEqual(1, len(wanted))
        self.assertIn("misheard", wanted[0]["why"])

    def test_a_moment_already_captured_is_not_asked_for(self):
        from tools.impact import talks
        wanted = talks.frames_wanted({
            "url": "https://www.youtube.com/watch?v=x",
            "sources": [{"kind": "captions", "status": "extracted"}],
            "mentions": [
                {"id": "M1", "source": "captions", "at_seconds": 100,
                 "timestamp": "1:40", "heard_as": "SQL lenser",
                 "excerpt": "…", "url": "https://www.youtube.com/watch?v=x&t=100s"},
                {"id": "M2", "source": "frame", "at_seconds": 104,
                 "timestamp": "1:44", "image": "/assets/x.jpg",
                 "url": "https://www.youtube.com/watch?v=x&t=104s"}],
        })
        self.assertEqual([], wanted)

    def test_a_moment_somebody_checked_is_not_asked_for_again(self):
        """The closing contact slide was up; there was nothing to capture."""
        from tools.impact import talks
        wanted = talks.frames_wanted({
            "url": "https://www.youtube.com/watch?v=x",
            "sources": [{"kind": "captions", "status": "extracted"}],
            "mentions": [{"id": "M1", "source": "captions", "at_seconds": 100,
                          "timestamp": "1:40", "heard_as": "SQL answer",
                          "excerpt": "…", "frame_checked": "nothing on screen",
                          "url": "https://www.youtube.com/watch?v=x&t=100s"}],
        })
        self.assertEqual([], wanted)

    def test_a_silent_transcript_wants_a_moment_from_a_person(self):
        from tools.impact import talks
        wanted = talks.frames_wanted({
            "url": "https://www.youtube.com/watch?v=x",
            "sources": [{"kind": "captions", "status": "extracted",
                         "note": "The whole transcript was read and SQLancer "
                                 "is not spoken anywhere in it."}],
            "mentions": [],
        })
        self.assertEqual(1, len(wanted))
        self.assertIsNone(wanted[0]["at_seconds"])

    def test_the_dataset_has_no_outstanding_frames(self):
        from tools.impact import talks
        outstanding = {t["title"]: talks.frames_wanted(t)
                       for t in talks.load()["talks"]
                       if talks.frames_wanted(t)}
        self.assertEqual({}, outstanding)

    def test_a_talk_either_shows_something_or_says_why_not(self):
        """Words alone are allowed, but only once somebody has looked.

        Most talks put SQLancer on a slide. A few mention it in the questions,
        with whatever slide happened to be left up, and there is nothing to
        capture -- which is a finding, recorded on the mention, not an absence.
        """
        from tools.impact import talks
        for talk in talks.load()["talks"]:
            shows = any(m.get("image") for m in talk["mentions"])
            looked = any(m.get("frame_checked") for m in talk["mentions"])
            self.assertTrue(shows or looked, talk["id"])

    def test_a_deck_slide_keeps_its_words_as_well_as_its_picture(self):
        """A frame may not be quoted; a published slide may -- it is text."""
        from tools.impact import talks
        for talk in talks.load()["talks"]:
            for mention in talk["mentions"]:
                if mention["source"] == "slides" and mention.get("image"):
                    self.assertTrue(mention["excerpt"])
                    self.assertTrue(mention["excerpt_is_verbatim"])


class GraphSystemTest(unittest.TestCase):
    """A lab member's name does not vouch for a graph database system.

    The reporter rule infers that a roster member's database bug came from a
    SQLancer campaign. For graph systems that inference is simply false: the
    lab tests them with tools developed independently of SQLancer, and SQLancer
    has no provider for any of them. 38 records reached the dataset this way
    before the rule learned the difference.
    """

    def test_no_graph_bug_rests_on_the_reporter(self):
        from tools.impact import config
        from tools.impact.collectors.github_bugs import OUTSIDE_SQLANCER_CAMPAIGNS
        bugs = config.load_json(config.DATA_FILES["bugs"])["bugs"]
        offenders = [b["id"] for b in bugs
                     if b.get("dbms") in OUTSIDE_SQLANCER_CAMPAIGNS
                     and (b.get("attribution") or {}).get("rule")
                     == "campaign_reporter"]
        self.assertEqual([], offenders)

    def test_the_rule_is_off_for_those_systems(self):
        """The gate is at the call site, so assert on what it lets through."""
        from tools.impact.collectors.github_bugs import (
            OUTSIDE_SQLANCER_CAMPAIGNS, deterministic_attribution)
        from tools.impact import taxonomy
        issue = {"title": "Wrong result for a MATCH query",
                 "body": "MATCH (n) RETURN n returns the wrong rows.",
                 "labels": [{"name": "bug"}]}
        text = f"{issue['title']}\n{issue['body']}"
        tax = taxonomy.load()
        self.assertIn("neo4j", OUTSIDE_SQLANCER_CAMPAIGNS)
        # With the reporter's name allowed to vouch, this is admitted...
        self.assertIsNotNone(deterministic_attribution(
            text, tax, issue, reporter_runs_campaigns=True))
        # ...and without it, nothing here says SQLancer at all.
        self.assertIsNone(deterministic_attribution(
            text, tax, issue, reporter_runs_campaigns=False))

    def test_the_policy_says_so_on_the_page(self):
        from tools.impact import config
        policy = config.load_json(config.DATA_FILES["policy"])
        rules = [r for section in policy["sections"]
                 for r in section.get("rules", [])]
        graph = next(r for r in rules if r["id"] == "bug-graph-systems")
        self.assertEqual("exclude", graph["kind"])


class UnreadableQuoteTest(unittest.TestCase):
    """A quotation nobody can read is worse than no quotation.

    Some PDFs set a passage with letter-spacing, and extraction returns it one
    character at a time: "w i t h i n S Q L a n c e r". The gaps between words
    are the same width as the gaps inside them, in the file as well as on the
    page, so the word breaks are not there to put back -- rejoining gives
    "systemwithinSQLancer" and guessing them from a dictionary would be writing
    text rather than quoting it. Six mentions were affected, and every one had
    other evidence for the same claim.
    """

    def test_nothing_published_quotes_an_unreadable_passage(self):
        from tools.impact import config, validate
        papers = config.load_json(config.DATA_FILES["papers"])["papers"]
        self.assertEqual([], [str(p) for p in
                              validate.check_readable_excerpts(papers)])

    def test_the_check_catches_one(self):
        from tools.impact import validate
        problems = validate.check_readable_excerpts([{
            "id": "paper:doi:x",
            "relationships": {"uses_infrastructure": {"value": "yes", "evidence": [
                {"excerpt": "We implemented this s y s t e m w i t h i n "
                            "S Q L a n c e r and tested it."}]}},
        }])
        self.assertTrue(any("word breaks" in str(p) for p in problems))

    def test_the_mention_is_kept_and_marked(self):
        """The mention is real; only its text is unusable."""
        import json
        import pathlib
        marked = 0
        for path in pathlib.Path("_data/papers").glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            for mention in record.get("mentions", []):
                if mention.get("text_is_letter_spaced"):
                    marked += 1
                    self.assertTrue(mention.get("sentence"))
        self.assertEqual(6, marked)


class MovedRepositoryTest(unittest.TestCase):
    """A project that changed organisation must not get two records per bug.

    DuckDB was cwida/duckdb before it was duckdb/duckdb, and GitHub redirects
    the old links, so both forms reach the same issue and both look canonical.
    The curated list kept the address a bug was filed under while an issue
    search found the address it lives at now: 77 DuckDB bugs were counted
    twice.
    """

    def test_a_former_organisation_resolves_to_the_current_one(self):
        from tools.impact.repos import canonical_issue_url
        self.assertEqual("https://github.com/duckdb/duckdb/issues/490",
                         canonical_issue_url("https://github.com/cwida/duckdb/issues/490"))

    def test_an_unrelated_url_is_left_alone(self):
        from tools.impact.repos import canonical_issue_url
        for url in ("https://github.com/cockroachdb/cockroach/issues/1",
                    "https://github.com/duckdb/duckdb",
                    "https://bugs.mysql.com/bug.php?id=1"):
            self.assertEqual(url, canonical_issue_url(url))

    def test_no_bug_is_recorded_at_two_addresses(self):
        from tools.impact import config
        from tools.impact.repos import canonical_issue_url
        seen = {}
        for bug in config.load_json(config.DATA_FILES["bugs"])["bugs"]:
            url = canonical_issue_url(bug.get("primary_url") or "")
            if not url:
                continue
            key = (bug["dbms"], url)
            self.assertNotIn(key, seen,
                             f"{bug['id']} and {seen.get(key)} are the same report")
            seen[key] = bug["id"]


class RepeatedTitleTest(unittest.TestCase):
    """Reports sharing a title are not thereby the same bug.

    A fuzzer files what it finds under whatever title its harness writes:
    CockroachDB's nightly roachtest files "roachtest: tlp failed" every time,
    and StarRocks' campaigns file "[sqlancer] query of result set mismatch".
    Collapsing those would undercount -- the CockroachDB set spans 21 distinct
    days over four years, and the same-day sets are consecutive issue numbers
    from one sitting, each accepted and fixed separately.

    What does settle it is the project's own triage, and that is already
    honoured: a report the maintainers closed as a duplicate never counts.
    """

    def test_a_report_the_project_called_a_duplicate_is_not_counted(self):
        from tools.impact import config
        bugs = config.load_json(config.DATA_FILES["bugs"])["bugs"]
        declared = [b for b in bugs if b["status"] == "closed_duplicate"]
        self.assertTrue(declared, "expected some declared duplicates on file")
        self.assertEqual([], [b["id"] for b in declared
                              if b.get("status_is_true_positive")])

    def test_repeated_titles_each_carry_their_own_address(self):
        """Each is its own issue, which is what makes it its own record."""
        import collections
        from tools.impact import config
        bugs = config.load_json(config.DATA_FILES["bugs"])["bugs"]
        addressed = collections.defaultdict(list)
        for bug in bugs:
            title = (bug.get("title") or "").strip().lower()
            if title and bug.get("primary_url"):
                addressed[(bug["dbms"], title)].append(bug["primary_url"])
        for (dbms, title), urls in addressed.items():
            self.assertEqual(len(urls), len(set(urls)),
                             f"{dbms} '{title[:40]}' repeats an address")

    def test_a_report_without_an_address_is_not_a_second_copy(self):
        """Umbra's bugs were emailed, so they have no URL -- but a record with
        no address must not shadow a curated one for the same report."""
        import collections
        from tools.impact import config
        bugs = config.load_json(config.DATA_FILES["bugs"])["bugs"]
        clusters = collections.defaultdict(list)
        for bug in bugs:
            title = (bug.get("title") or "").strip().lower()
            if title and bug.get("reported_date"):
                clusters[(bug["dbms"], title, bug["reported_date"])].append(bug)
        for key, rows in clusters.items():
            if len(rows) < 2:
                continue
            with_url = [b for b in rows if b.get("primary_url")]
            self.assertFalse(
                with_url and len(with_url) != len(rows),
                f"{key[0]} '{key[1][:38]}' on {key[2]} has a copy with no address")


class PaperRecordSchemaTest(unittest.TestCase):
    """The analysed papers are checked like everything else now.

    These 190 files hold the sentences behind every claim the impact page makes
    about research, and they were the one part of the dataset with no schema at
    all -- nothing would have noticed a renamed field or a lost quotation.
    """

    def test_every_record_matches_the_schema(self):
        from tools.impact import validate
        self.assertEqual([], [str(p) for p in validate.check_paper_records()])

    def test_the_schema_covers_what_is_on_disk(self):
        import json
        import pathlib
        from tools.impact import config
        records = sorted(pathlib.Path("_data/papers").glob("*.json"))
        self.assertGreater(len(records), 100)
        schema = config.load_json(
            config.REPO_ROOT / "schemas" / "papers" / "paper-analysis.schema.json")
        # The sentinel that stands where a repository, not a sentence, carries
        # the claim must be allowed -- one paper depends on it.
        pattern = (schema["properties"]["analysis"]["properties"]["relationships"]
                   ["additionalProperties"]["properties"]["mention_ids"]
                   ["items"]["pattern"])
        self.assertIn("ARTIFACT", pattern)
        with records[0].open() as handle:
            self.assertIn("mentions", json.load(handle))


class NonEnglishContextTest(unittest.TestCase):
    """An ambiguous acronym has to be corroborated in the report's own language.

    "TLP" means thread-level parallelism and a laptop power tool, so the
    taxonomy only accepts it beside a database-testing term. Those terms were
    all English, and openGauss reports in Chinese on Gitee -- so a title reading
    "TLP 等价验证" (TLP equivalence verification) corroborated nothing and was
    thrown away.
    """

    def test_a_chinese_report_can_corroborate_an_acronym(self):
        from tools.impact import taxonomy
        found = taxonomy.load().find_techniques(
            "执行包含 SUM(REAL) 聚合与恒真 HAVING 条件的查询时，"
            "TLP 等价验证出现结果内容不一致")
        self.assertEqual(["tlp"], [m.technique_id for m in found])

    def test_the_power_management_sense_is_still_refused(self):
        from tools.impact import taxonomy
        self.assertEqual([], taxonomy.load().find_techniques(
            "tlp-stat shows the laptop battery is in power management mode"))

    def test_a_chinese_certificate_thread_is_not_the_cert_oracle(self):
        from tools.impact import taxonomy
        self.assertEqual([], taxonomy.load().find_techniques(
            "配置数据库连接时 CERT 证书校验失败"))
