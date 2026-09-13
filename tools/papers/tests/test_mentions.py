"""Finding the places a paper refers to SQLancer, however it refers to it."""

import unittest

from tools.papers import mentions


class CitationMarkerTest(unittest.TestCase):
    def test_ranges_expand(self):
        """"[9]-[11]" names three references; the middle one is invisible."""
        self.assertEqual([9, 10, 11], mentions.expand_markers("9-11"))
        self.assertEqual([9, 10, 11], mentions.expand_markers("9–11"))
        self.assertEqual([3, 7], mentions.expand_markers("3, 7"))

    def test_an_implausible_range_is_not_expanded(self):
        self.assertEqual([], mentions.expand_markers("1-500"))

    def test_markers_are_found_however_they_are_spaced(self):
        """"[ 1,2]" is what some publishers produce, and it found nothing."""
        for text in ("cited [11] here", "cited [ 11] here", "cited [ 1,2] here",
                     "cited [ 9,10,\n11] here"):
            self.assertTrue(mentions.CITATION_GROUP.search(text), text)


class ReferenceMatchingTest(unittest.TestCase):
    def test_a_sqlancer_paper_is_recognised(self):
        found = mentions.sqlancer_references([
            {"number": 11, "text": "M. Rigger and Z. Su, Testing Database "
             "Engines via Pivoted Query Synthesis, OSDI 2020."}])
        self.assertEqual("sqlancer_publication", found[11]["matched_as"])
        self.assertEqual("pqs", found[11]["technique"])

    def test_another_paper_by_the_same_author_is_not_a_sqlancer_paper(self):
        """FlowFusion is a PHP fuzzer Rigger co-authored, and is not SQLancer."""
        found = mentions.sqlancer_references([
            {"number": 7, "text": "Y. Jiang, C. Zhang, M. Rigger, and Z. Liang, "
             "Fuzzing the PHP interpreter via dataflow fusion, USENIX 2025."}])
        self.assertEqual("project_authored", found[7]["matched_as"])

    def test_a_reference_whose_words_are_glued_together_is_still_matched(self):
        """Extraction runs words together in some bibliographies.

        "ManuelRiggerandZhendongSu.2020. FindingBugsinDatabaseSystemsvia
        QueryPartitioning" is a real entry, and it left the TLP paper
        unrecognised in the bibliography of a paper that cites it.
        """
        found = mentions.sqlancer_references([
            {"number": 43, "text": "ManuelRiggerandZhendongSu.2020. "
             "FindingBugsinDatabaseSystemsvia QueryPartitioning. PACMPL 4."}])
        self.assertEqual("sqlancer_publication", found[43]["matched_as"])
        self.assertEqual("tlp", found[43]["technique"])

    def test_an_unrelated_reference_is_ignored(self):
        found = mentions.sqlancer_references([
            {"number": 3, "text": "A. Author, Something About Compilers, 2019."}])
        self.assertEqual({}, found)


class InventoryTest(unittest.TestCase):
    def _document(self, text, references=None):
        return {"text": text, "pages": [{"page": 1, "start": 0,
                                         "end": len(text)}],
                "page_count": 1, "sections": [], "references": references or [],
                "references_start": None, "has_outline": False}

    def test_a_name_split_by_small_caps_is_still_found(self):
        doc = self._document("For example, SQL ANCER relies on JDBC to run queries.")
        found = mentions.build(doc)["mentions"]
        self.assertEqual(1, len(found))
        self.assertEqual(["name"], found[0]["found_by_all"])

    def test_a_sentence_citing_a_sqlancer_reference_is_found_without_the_name(self):
        """The point of the whole route: no name appears in this sentence."""
        doc = self._document(
            "Existing works [9]-[11] also face the same problem here.",
            [{"number": 10, "text": "M. Rigger and Z. Su, Testing Database "
              "Engines via Pivoted Query Synthesis, OSDI 2020."}])
        found = mentions.build(doc)["mentions"]
        self.assertEqual(1, len(found))
        self.assertEqual(["citation_marker"], found[0]["found_by_all"])
        self.assertEqual(10, found[0]["cited_reference"]["number"])

    def test_an_author_year_citation_is_found(self):
        """ACM's current format has no numbers to resolve."""
        doc = self._document("for databases [Rigger and Su 2020; Wang 2021].")
        found = mentions.build(doc)["mentions"]
        self.assertEqual(["author_year_citation"], found[0]["found_by_all"])

    def test_a_sentence_is_recorded_once_however_many_ways_it_was_found(self):
        doc = self._document(
            "SQLancer [11] is a popular tool for testing databases.",
            [{"number": 11, "text": "M. Rigger, Testing Database Engines via "
              "Pivoted Query Synthesis, OSDI 2020."}])
        found = mentions.build(doc)["mentions"]
        self.assertEqual(1, len(found))
        self.assertEqual({"name", "citation_marker"},
                         set(found[0]["found_by_all"]))

    def test_the_bibliography_itself_yields_no_mentions(self):
        text = ("Body cites nothing.\nReferences\n"
                "[1] M. Rigger, Testing Database Engines via Pivoted Query "
                "Synthesis, OSDI 2020.\n")
        doc = self._document(text)
        doc["references_start"] = text.index("References")
        doc["references"] = [{"number": 1, "text": "M. Rigger, Testing Database "
                              "Engines via Pivoted Query Synthesis."}]
        self.assertEqual([], mentions.build(doc)["mentions"])


if __name__ == "__main__":
    unittest.main()


class PublishedIdentifierTest(unittest.TestCase):
    """A reference is recognised by the identifiers a bibliography prints.

    The taxonomy carries each origin paper under its preprint title, and a
    published version is often retitled: CERT's preprint is "Cardinality
    Estimation Restriction Testing: ..." and the ICSE version is "CERT:
    Finding Performance Issues ... Through the Lens of Cardinality Estimation".
    Matching on opening words alone recognised the entry only as a paper by one
    of the project's authors, which understates it.
    """

    def test_a_retitled_paper_is_matched_by_its_doi(self):
        from tools.papers import mentions
        entry = {"number": 13, "text":
                 'J. Ba and M. Rigger, "CERT: Finding performance issues in '
                 'database systems th rough the lens of cardinality '
                 'estimation," in Proc. ACM SIGMOD Int. Conf. Manage. Data, '
                 '2023, pp. 1600 -1612, doi: 10.1145/3597503.3639076.'}
        found = mentions.sqlancer_references([entry])[13]
        self.assertEqual("sqlancer_publication", found["matched_as"])
        self.assertEqual("cert", found["technique"])

    def test_an_authors_other_project_is_not_a_sqlancer_paper(self):
        from tools.papers import mentions
        entry = {"number": 9, "text":
                 'Y. Liu and M. Rigger, "FlowFusion: fuzzing the PHP '
                 'interpreter via dataflow fusion," 2025.'}
        found = mentions.sqlancer_references([entry])[9]
        self.assertEqual("project_authored", found["matched_as"])
        self.assertIsNone(found["technique"])


class MixedCitationGroupTest(unittest.TestCase):
    """A group citing both kinds is labelled by the stronger one.

    "[2-5,8,13,14,17]" cites a paper by one of the project's authors and the
    PQS paper. Taking whichever resolved first filed a sentence citing PQS
    under project_authored.
    """

    REFS = {
        5: {"number": 5, "matched_as": "project_authored", "technique": None,
            "text": "A paper by a project author on something else."},
        13: {"number": 13, "matched_as": "sqlancer_publication",
             "technique": "pqs",
             "text": "Manuel Rigger and Zhendong Su. Testing database engines "
                     "via pivoted query synthesis."},
    }

    def test_the_sqlancer_publication_decides(self):
        from tools.papers import mentions
        document = {
            "text": "Their effectiveness remains constrained for connectors "
                    "[2-5,8,13,14,17]. More text follows here.\nREFERENCES\n",
            "references": [
                {"number": 5, "text": "Jingzhou Fu, Jie Liang, and Manuel "
                                      "Rigger. 2025. SQL function bugs."},
                {"number": 13, "text": "Manuel Rigger and Zhendong Su. 2020. "
                                       "Testing database engines via pivoted "
                                       "query synthesis. In OSDI. 667-682."},
            ],
        }
        document["references_start"] = document["text"].index("REFERENCES")
        found = mentions.build(document)
        marker = [m for m in found["mentions"]
                  if m["found_by"].startswith("citation_marker")]
        self.assertEqual(1, len(marker))
        self.assertEqual("citation_marker", marker[0]["found_by"])
        self.assertEqual("pqs", marker[0].get("technique"))
