"""Checks on the published dataset itself.

These run against the real files in ``_data/impact`` and are what stands between
a bad automated proposal and the website. They need no network and no API key.
"""

from __future__ import annotations

import json
import re
import unittest
from collections import Counter

from tools.impact import config, stats as stats_module, taxonomy, validate
from tools.impact.dataset import LIST_FIELD
from tools.impact.util import normalize_doi, normalize_url


def load(name: str) -> dict:
    return config.load_json(config.DATA_FILES[name])


def records(name: str):
    return load(name).get(LIST_FIELD[name], [])


class SchemaTest(unittest.TestCase):
    def test_every_file_validates(self):
        problems = validate.validate_all()
        self.assertEqual([], [str(p) for p in problems])

    def test_schemas_are_valid_json_schema(self):
        import jsonschema
        for path in config.SCHEMA_DIR.glob("*.schema.json"):
            with open(path, encoding="utf-8") as handle:
                schema = json.load(handle)
            jsonschema.Draft7Validator.check_schema(schema)


class IdentityTest(unittest.TestCase):
    def test_ids_are_unique_within_each_file(self):
        for name in LIST_FIELD:
            counts = Counter(record["id"] for record in records(name))
            duplicates = [key for key, count in counts.items() if count > 1]
            self.assertEqual([], duplicates, f"duplicate ids in {name}.json")

    def test_every_record_has_a_stable_id(self):
        pattern = re.compile(r"^[a-z0-9]+(:[A-Za-z0-9._+#/@-]+)+$")
        for name in LIST_FIELD:
            for record in records(name):
                if name == "dbms":
                    continue  # registry ids are bare slugs by design
                self.assertRegex(record["id"], pattern,
                                 f"{name}.json record id is not namespaced")

    def test_no_duplicate_bug_urls_with_the_same_title(self):
        seen = {}
        for record in records("bugs"):
            url = normalize_url(record.get("primary_url"))
            if not url:
                continue
            key = (url, record["title"])
            self.assertNotIn(key, seen,
                             f"duplicate bug {url} / {record['title']}")
            seen[key] = record["id"]

    def test_no_two_records_are_the_same_work(self):
        """A preprint and its published paper are one work, not two.

        Both citation graphs index them separately, and nothing downstream
        noticed: the DBMS fuzzing survey was counted twice and AMOEBA three
        times before this was checked.
        """
        from tools.impact import dedupe

        groups, review = dedupe.find_duplicates(records("papers"))
        self.assertEqual(
            [], [[p["id"] for p in group] for group in groups],
            "run `python3 -m tools.impact.run dedupe` to merge these")
        self.assertEqual(
            [], [(a["id"], b["id"]) for a, b, _ in review],
            "decide these in _data/impact/paper_decisions.json")

    def test_an_authorless_stub_sharing_a_tool_name_is_raised(self):
        """Nothing else can compare a record that carries no authors.

        The 2022 DBStorm record had none, so authorship could not link it to the
        2024 paper it was an earlier version of, and the download list kept
        asking for a paper already in hand.
        """
        from tools.impact import dedupe

        papers = [
            {"id": "paper:doi:10.1145/x", "title": "DBStorm: Generating Various "
             "Effective Workloads for Testing Isolation Levels",
             "authors": ["Keqiang Li", "Siyang Weng"], "relationships": {}},
            {"id": "paper:s2:abc", "title": "DBStorm: A Cost-effective Approach "
             "for Generating Valid Workload", "authors": [], "relationships": {}},
        ]
        _, review = dedupe.find_duplicates(papers)
        self.assertEqual(1, len(review), "the shared tool name should be raised")

    def test_a_merged_record_keeps_the_identities_it_replaced(self):
        """An old id must stay resolvable after a merge."""
        seen = set()
        for record in records("papers"):
            for alias in record.get("also_indexed_as") or []:
                self.assertNotIn(alias, seen, f"{alias} claimed twice")
                seen.add(alias)
            self.assertNotIn(record["id"], record.get("also_indexed_as") or [])

    def test_no_duplicate_dois(self):
        seen = {}
        for record in records("papers"):
            doi = normalize_doi(record.get("doi"))
            if not doi:
                continue
            self.assertNotIn(doi, seen, f"duplicate DOI {doi}")
            seen[doi] = record["id"]

    def test_no_duplicate_resource_urls(self):
        seen = set()
        for record in records("resources"):
            url = normalize_url(record["url"])
            self.assertNotIn(url, seen, f"duplicate resource {url}")
            seen.add(url)


class EvidenceTest(unittest.TestCase):
    URL = re.compile(r"^https?://[^\s<>\"]+$")

    def _evidence(self, record):
        found = []

        def walk(node):
            if isinstance(node, dict):
                if "source_url" in node and "source_type" in node:
                    found.append(node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(record)
        return found

    def test_every_bug_has_evidence_and_a_link(self):
        for record in records("bugs"):
            self.assertTrue(record.get("links"),
                            f"{record['id']} has no links")
            self.assertTrue(record["attribution"]["evidence"],
                            f"{record['id']} has no attribution evidence")

    def test_every_adoption_record_has_evidence(self):
        for record in records("adoption"):
            self.assertTrue(record.get("evidence"),
                            f"{record['id']} has no evidence")

    def test_every_positive_paper_relationship_has_evidence(self):
        for record in records("papers"):
            for key, entry in record["relationships"].items():
                if entry.get("value") == "yes":
                    self.assertTrue(
                        entry.get("evidence"),
                        f"{record['id']} claims {key} without evidence")

    def test_evidence_urls_are_well_formed(self):
        for name in LIST_FIELD:
            for record in records(name):
                for item in self._evidence(record):
                    self.assertRegex(item["source_url"], self.URL,
                                     f"{name}.json {record['id']}")

    def test_excerpts_are_flagged_verbatim(self):
        """An excerpt is source text; the flag records that we checked."""
        for name in LIST_FIELD:
            for record in records(name):
                for item in self._evidence(record):
                    if item.get("excerpt") is not None:
                        self.assertTrue(
                            item.get("excerpt_is_verbatim"),
                            f"{name}.json {record['id']} excerpt not marked verbatim")
                        self.assertTrue(item["excerpt"].strip())


class VocabularyTest(unittest.TestCase):
    def setUp(self):
        self.tax = taxonomy.load()
        self.registry = {entry["id"] for entry in records("dbms")}

    def test_bug_techniques_are_known(self):
        for record in records("bugs"):
            technique = record.get("technique")
            self.assertTrue(self.tax.is_known_technique(technique),
                            f"{record['id']} references unknown {technique!r}")
            self.assertNotIn(technique, self.tax.excluded,
                             f"{record['id']} uses an excluded technique")

    def test_bug_finders_are_umbrella_members(self):
        for record in records("bugs"):
            finder = record["finder"]
            self.assertIn(finder, self.tax.finders)
            self.assertTrue(self.tax.finders[finder]["umbrella_member"])

    def test_all_dbms_references_resolve(self):
        for name in ("bugs", "adoption"):
            for record in records(name):
                self.assertIn(record["dbms"], self.registry,
                              f"{name}.json {record['id']}")

    def test_paper_relationship_techniques_are_known(self):
        """The three judged relationships name techniques.

        "references" is different: it records which seed publication the paper
        cites, and a seed can be a tool's own paper rather than an oracle's.
        SQLancer++'s is, and papers citing it cite no technique at all.
        """
        seeds = set(self.tax.seed_ids())
        for record in records("papers"):
            for key, entry in record["relationships"].items():
                allowed = (set(self.tax.techniques) | seeds
                           if key == "references" else set(self.tax.techniques))
                for technique in entry.get("techniques", []):
                    self.assertIn(technique, allowed, f"{record['id']}/{key}")

    def test_adoption_relationships_use_the_controlled_vocabulary(self):
        allowed = {"official_ci", "official_testing", "developer_use",
                   "integration_contributed_by_dbms_team", "planned_adoption"}
        for record in records("adoption"):
            self.assertIn(record["relationship"], allowed)

    def test_a_proposal_is_never_counted_as_use(self):
        """An open proposal is an intention; counting it would overstate reach."""
        stats = load("stats")
        using = {record["dbms"] for record in records("adoption")
                 if record["relationship"] != "planned_adoption"}
        self.assertEqual(len(using),
                         stats["headline"]["dbms_projects_using_sqlancer"])
        planned = {record["dbms"] for record in records("adoption")
                   if record["relationship"] == "planned_adoption"}
        self.assertEqual(len(planned - using),
                         stats["headline"]["dbms_projects_planning_adoption"])

    def test_a_proposal_never_reaches_the_use_column(self):
        """The systems table must not present an open issue as adoption."""
        planned = {record["dbms"] for record in records("adoption")
                   if record["relationship"] == "planned_adoption"}
        using = {record["dbms"] for record in records("adoption")
                 if record["relationship"] != "planned_adoption"}
        for row in load("stats")["dbms"]["rows"]:
            self.assertNotIn("planned_adoption", row["adoption_relationships"],
                             f"{row['id']} lists a proposal as use")
            self.assertEqual(row["id"] in planned and row["id"] not in using,
                             row["planned_adoption"], row["id"])

    def test_a_campaign_reporter_is_never_counted_as_external(self):
        """The same person is a handle in one tracker and a name in another.

        Matching only handles marked every SQLite forum report as somebody
        else's work, though the same people filed them.

        A ruling in ``people_decisions.json`` is the one thing that overrides
        this: appearing on the lab's people page says someone is in the lab,
        not that their work is this project's, and only a person can tell the
        two apart.
        """
        from tools.impact.collectors import people

        known = {name.strip().lower() for name in people.identities()}
        ruled_external = people.counted_as_external()
        for record in records("bugs"):
            reporter = (record.get("reporter") or "").strip().lower()
            if not reporter or reporter not in known:
                continue
            expected = "external" if reporter in ruled_external else "project"
            self.assertEqual(expected, record["reporter_affiliation"],
                             f"{record['id']}: {record['reporter']} is on the "
                             f"roster and {'is' if reporter in ruled_external else 'is not'} "
                             f"ruled external")

    def test_excluded_techniques_carry_a_reason_and_evidence(self):
        for entry in self.tax.excluded.values():
            self.assertTrue(entry["reason"])
            self.assertTrue(entry.get("evidence"))


class PolicyTest(unittest.TestCase):
    def test_policy_version_matches_the_data_files(self):
        policy_version = load("policy")["policy_version"]
        for name in ("bugs", "papers", "adoption"):
            self.assertEqual(policy_version, load(name)["policy_version"],
                             f"{name}.json is on a different policy version")

    def test_adoption_is_not_inferred_from_support(self):
        """Supporting a DBMS must never be recorded as that project using it."""
        supported = {entry["id"] for entry in records("dbms")
                     if entry.get("supported_by_sqlancer")}
        for record in records("adoption"):
            evidence_urls = " ".join(item["source_url"]
                                     for item in record["evidence"])
            self.assertNotIn("github.com/sqlancer/sqlancer", evidence_urls,
                             f"{record['id']} rests on SQLancer's own repository")
            if record["dbms"] in supported:
                self.assertTrue(record["evidence"])

    def test_every_collected_paper_at_least_references_sqlancer(self):
        for record in records("papers"):
            self.assertEqual("yes",
                             record["relationships"]["references"]["value"])


class ReviewQueueTest(unittest.TestCase):
    """The queue is the record of what did not get in, so it is checked too."""

    def queue(self, **changes) -> dict:
        base = {"schema_version": "1.0.0", "description": "x",
                "open": [{"id": "review:aaa", "kind": "bug", "source": "bugs",
                          "url": None, "title": None, "reason": "unplaced",
                          "first_seen": "2026-01-01T00:00:00Z",
                          "last_seen": "2026-01-02T00:00:00Z"}],
                "dismissed": [{"id": "review:bbb", "url": None,
                               "reason": "not a database system",
                               "decided_by": "Manuel Rigger",
                               "decided_on": "2026-01-01"}]}
        base.update(changes)
        return base

    def test_a_well_formed_queue_passes(self):
        self.assertEqual([], validate.check_review_queue(self.queue()))

    def test_an_item_cannot_be_dismissed_and_still_open(self):
        queue = self.queue()
        queue["open"][0]["id"] = queue["dismissed"][0]["id"]
        problems = validate.check_review_queue(queue)
        self.assertTrue(any("also in open" in str(p) for p in problems), problems)

    def test_a_dismissal_must_say_who_made_it(self):
        queue = self.queue()
        queue["dismissed"][0].pop("decided_by")
        problems = validate.check_review_queue(queue)
        self.assertTrue(any("decided_by" in str(p) for p in problems), problems)

class StatsTest(unittest.TestCase):
    def setUp(self):
        self.stats = load("stats")
        self.recomputed = stats_module.compute(stats_module.load_data())

    def test_stats_file_matches_the_records(self):
        """The published figures must be exactly what the records produce."""
        self.assertEqual(self.recomputed, self.stats,
                         "stats.json is stale; run `python -m tools.impact.run stats`")

    def test_bug_total_counts_only_accepted_reports(self):
        accepted = [r for r in records("bugs") if r["status_is_true_positive"]]
        self.assertEqual(len(accepted), self.stats["headline"]["bugs_total"])
        self.assertEqual(len(records("bugs")),
                         self.stats["bugs"]["total_including_rejected"])

    def test_by_dbms_sums_to_the_total(self):
        total = sum(row["count"] for row in self.stats["bugs"]["by_dbms"])
        self.assertEqual(self.stats["bugs"]["total"], total)

    def test_headline_rounding_never_overstates(self):
        headline = self.stats["headline"]
        digits = headline["bugs_total_rounded"].rstrip("+").replace(",", "")
        self.assertLessEqual(int(digits), headline["bugs_total"])

    def test_papers_building_on_is_deduplicated(self):
        """A paper in several categories must be counted once."""
        building = 0
        per_category = 0
        for record in records("papers"):
            if record.get("is_sqlancer_publication"):
                continue
            hits = [key for key in ("uses_infrastructure", "extends_technique",
                                    "compares_with")
                    if record["relationships"][key]["value"] == "yes"]
            per_category += len(hits)
            if hits:
                building += 1
        self.assertEqual(building,
                         self.stats["headline"]["papers_building_on_sqlancer"])
        self.assertLessEqual(building, max(per_category, building))

    def test_supported_and_bug_counts_are_separate_concepts(self):
        supported = {entry["id"] for entry in records("dbms")
                     if entry.get("supported_by_sqlancer")}
        with_bugs = {record["dbms"] for record in records("bugs")
                     if record["status_is_true_positive"]}
        self.assertEqual(len(supported), self.stats["dbms"]["supported"])
        self.assertEqual(len(with_bugs), self.stats["dbms"]["with_bugs"])
        adopting = {record["dbms"] for record in records("adoption")
                    if record["relationship"] != "planned_adoption"}
        self.assertEqual(len(adopting), self.stats["dbms"]["with_adoption"])


if __name__ == "__main__":
    unittest.main()


class UndercountTest(unittest.TestCase):
    """The bug count is a floor, and the page has to be able to say so.

    The figure behind that claim is derived rather than written: projects whose
    own evidence shows them running SQLancer, and whose bugs never reach this
    dataset because no public report ties one to it. Materialize is the clearest
    case -- it maintains its own SQLancer fork and contributes no bug at all.
    """

    def test_the_blind_spot_is_measured_not_asserted(self):
        from tools.impact import config
        stats = config.load_json(config.DATA_FILES["stats"])
        rows = {row["id"]: row for row in stats["dbms"]["rows"]}
        silent = [row for row in rows.values()
                  if row.get("adoption_relationships") and not row.get("bugs")]
        self.assertEqual(
            len(silent),
            stats["headline"]["dbms_using_sqlancer_without_counted_bugs"])

    def test_the_named_systems_are_the_measured_ones(self):
        """The page names them, so the names have to come from the same rows."""
        from tools.impact import config
        stats = config.load_json(config.DATA_FILES["stats"])
        named = stats["dbms"]["using_without_bugs"]
        expected = [{"id": row["id"], "name": row["name"]}
                    for row in stats["dbms"]["rows"]
                    if row.get("adoption_relationships") and not row.get("bugs")]
        self.assertEqual(expected, named)
        self.assertEqual(
            len(named),
            stats["headline"]["dbms_using_sqlancer_without_counted_bugs"])

    def test_each_named_system_has_a_page_to_link_to(self):
        from tools.impact import config, pages
        stats = config.load_json(config.DATA_FILES["stats"])
        for row in stats["dbms"]["using_without_bugs"]:
            self.assertTrue((pages.PAGE_DIR / f"{row['id']}.html").exists(),
                            row["id"])

    def test_a_project_running_it_can_still_have_no_bugs(self):
        from tools.impact import config
        stats = config.load_json(config.DATA_FILES["stats"])
        row = next(r for r in stats["dbms"]["rows"] if r["id"] == "materialize")
        self.assertTrue(row["adoption_relationships"])
        self.assertFalse(row.get("bugs"))
