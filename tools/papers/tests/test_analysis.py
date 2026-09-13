"""The analysis is the one part written by a model, so it is the part checked."""

import pathlib
import tempfile
import unittest

from tools.papers import analysis, store


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._real = store.DIRECTORY
        store.DIRECTORY = pathlib.Path(self._dir.name)
        store.save({
            "schema_version": "1.0.0",
            "paper": {"id": "paper:doi:10.1145/x", "title": "A Paper"},
            "document": {"has_fulltext": True, "page_count": 1,
                         "has_outline": False, "sections": []},
            "mentions": [
                {"id": "M1", "sentence": "We implemented our tool on top of "
                 "SQLancer.", "section": "4 Implementation", "page": 4,
                 "found_by_all": ["name"], "char_offset": 10},
                {"id": "M2", "sentence": "We compare against NoREC.",
                 "section": "5 Evaluation", "page": 6,
                 "found_by_all": ["technique"], "char_offset": 90},
            ],
            "references": [], "sqlancer_references": [], "checks": {},
            "artifact": None, "analysis": None, "sources": [], "provenance": {},
        })

    def tearDown(self):
        store.DIRECTORY = self._real
        self._dir.cleanup()

    def _answer(self, **overrides):
        answer = {
            "summary": "A paper about testing.",
            "narrative": "It builds on SQLancer.",
            "roles": {"M1": "reuse_implementation", "M2": "baseline"},
            "relationships": {
                "uses_infrastructure": {"value": "yes", "mention_ids": ["M1"],
                                        "reasoning": "It says so."},
                "extends_technique": {"value": "no", "mention_ids": [],
                                      "reasoning": ""},
                "compares_with": {"value": "yes", "mention_ids": ["M2"],
                                  "reasoning": "It says so."},
                "describes_as_state_of_the_art": {"value": "insufficient_evidence",
                                                  "mention_ids": [],
                                                  "reasoning": ""},
            },
            "disagreements": [], "unresolved": [],
        }
        answer.update(overrides)
        return answer

    def test_a_good_answer_is_stored_with_quotes_from_the_inventory(self):
        record = analysis.apply("paper:doi:10.1145/x", self._answer(),
                                model="claude-opus-5")
        uses = record["analysis"]["relationships"]["uses_infrastructure"]
        self.assertEqual(["M1"], uses["mention_ids"])
        self.assertEqual("We implemented our tool on top of SQLancer.",
                         uses["quotes"][0]["sentence"])
        self.assertTrue(record["analysis"]["is_model_written"])

    def test_a_claim_citing_a_mention_that_does_not_exist_is_rejected(self):
        answer = self._answer()
        answer["relationships"]["uses_infrastructure"]["mention_ids"] = ["M9"]
        with self.assertRaises(analysis.Rejected) as caught:
            analysis.apply("paper:doi:10.1145/x", answer, model="m")
        self.assertIn("M9", str(caught.exception))

    def test_a_yes_with_nothing_cited_is_rejected(self):
        answer = self._answer()
        answer["relationships"]["compares_with"]["mention_ids"] = []
        with self.assertRaises(analysis.Rejected):
            analysis.apply("paper:doi:10.1145/x", answer, model="m")

    def test_an_unknown_role_is_rejected(self):
        answer = self._answer(roles={"M1": "invented_role"})
        with self.assertRaises(analysis.Rejected):
            analysis.apply("paper:doi:10.1145/x", answer, model="m")

    def test_a_role_for_a_mention_that_does_not_exist_is_rejected(self):
        answer = self._answer(roles={"M7": "baseline"})
        with self.assertRaises(analysis.Rejected):
            analysis.apply("paper:doi:10.1145/x", answer, model="m")

    def test_a_missing_relationship_is_rejected(self):
        answer = self._answer()
        del answer["relationships"]["extends_technique"]
        with self.assertRaises(analysis.Rejected):
            analysis.apply("paper:doi:10.1145/x", answer, model="m")

    def test_an_unknown_reuse_kind_is_rejected(self):
        answer = self._answer()
        answer["relationships"]["uses_infrastructure"]["reuse_kind"] = "somehow"
        with self.assertRaises(analysis.Rejected):
            analysis.apply("paper:doi:10.1145/x", answer, model="m")


class ArtifactCitationTest(ApplyTest):
    """A repository can be the only evidence a paper reuses SQLancer."""

    def test_the_artifact_is_citable_when_it_was_inspected(self):
        record = store.load("paper:doi:10.1145/x")
        record["artifact"] = {"url": "https://github.com/x/y",
                              "markers": ["sqlancer_source_content_match"]}
        store.save(record)
        answer = self._answer()
        answer["relationships"]["uses_infrastructure"]["mention_ids"] = ["ARTIFACT"]
        stored = analysis.apply("paper:doi:10.1145/x", answer, model="m")
        uses = stored["analysis"]["relationships"]["uses_infrastructure"]
        self.assertEqual(["ARTIFACT"], uses["mention_ids"])
        self.assertIn("sqlancer_source_content_match", uses["quotes"][0]["sentence"])

    def test_the_artifact_cannot_be_cited_when_none_was_inspected(self):
        answer = self._answer()
        answer["relationships"]["uses_infrastructure"]["mention_ids"] = ["ARTIFACT"]
        with self.assertRaises(analysis.Rejected):
            analysis.apply("paper:doi:10.1145/x", answer, model="m")


class TaskTest(unittest.TestCase):
    def test_the_mentions_a_check_fired_on_are_never_dropped(self):
        """A survey can mention SQLancer eighty times; which are kept matters."""
        mentions = [{"id": f"M{i}", "sentence": f"sentence {i}",
                     "found_by_all": ["citation_marker"], "char_offset": i,
                     "section": "7 Related Work"} for i in range(1, 60)]
        record = {"paper": {"id": "p", "title": "t"},
                  "document": {"has_fulltext": True},
                  "mentions": mentions,
                  "checks": {"suggests": {"compares_with": ["M55"]}},
                  "sqlancer_references": []}
        task = analysis.task(record, max_mentions=10)
        kept = {m["id"] for m in task["mentions"]}
        self.assertIn("M55", kept)
        self.assertEqual(49, task["mentions_omitted"])


if __name__ == "__main__":
    unittest.main()
