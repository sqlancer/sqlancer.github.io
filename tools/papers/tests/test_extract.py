"""Extraction is not transcription, and the damage it does is repairable."""

import unittest

from tools.papers import extract


class TidyTest(unittest.TestCase):
    def test_small_caps_are_rejoined(self):
        self.assertEqual("REFERENCES", extract.tidy("R EFERENCES"))
        self.assertEqual("SQLANCER found", extract.tidy("SQL ANCER found"))
        self.assertEqual("NOREC compares", extract.tidy("N OREC compares"))

    def test_a_lost_space_before_a_keyword_is_restored(self):
        """The case that started this: "all columns areNOT NULL"."""
        self.assertEqual("all columns are NOT NULL",
                         extract.tidy("all columns areNOT NULL"))
        self.assertEqual("PQS and TLP", extract.tidy("PQS andTLP"))
        self.assertEqual("compared to SELECT", extract.tidy("compared toSELECT"))

    def test_a_product_name_is_not_split(self):
        """A name carries its capital at the front; a lost space does not."""
        for text in ("PostgreSQL and DuckDB", "CockroachDB, MariaDB, MonetDB",
                     "TiDB and SQLite", "a SQLancer run", "the NoREC oracle"):
            self.assertEqual(text, extract.tidy(text))

    def test_two_real_words_are_not_joined(self):
        """"SQL AND" is two words; joining it would invent one."""
        for text in ("SELECT AND NOT NULL", "SQL AND the rest",
                     "RELATED WORK", "USENIX ATC"):
            self.assertEqual(text, extract.tidy(text))

    def test_an_author_s_hyphen_survives_but_loses_its_spaces(self):
        self.assertEqual("Time-Series Databases",
                         extract.tidy("Time -Series Databases"))
        self.assertEqual("STATE-AWARE TESTCASE",
                         extract.tidy("STATE -AWARE TESTCASE"))

    def test_a_typesetter_s_hyphen_goes_away_with_the_break(self):
        """The second half starts lowercase, so the hyphen is not the author's."""
        self.assertEqual("our experience revealed two",
                         extract.tidy("our experience re - vealed two"))
        self.assertEqual("nonoptimizing", extract.tidy("non-\noptimizing"))

    def test_spacing_around_punctuation_is_repaired(self):
        self.assertEqual("RANDOOP, and EVOSUITE",
                         extract.tidy("RANDOOP , and EVOSUITE"))
        self.assertEqual("SQLANCER+ by adapting",
                         extract.tidy("SQLANCER+by adapting"))

    def test_control_characters_are_removed(self):
        """A form feed at a page break makes the JSON unreadable to Jekyll."""
        cleaned = extract.tidy("page one\x0cpage two\x0bhere")
        self.assertNotIn("\x0c", cleaned)
        self.assertNotIn("\x0b", cleaned)
        self.assertIn("page one", cleaned)
        self.assertIn("page two", cleaned)

    def test_characters_yaml_rejects_are_removed(self):
        """Jekyll reads data files as YAML, which is stricter than JSON.

        A broken text layer leaves U+FFFF where a ligature was -- "o<U+FFFF>cial"
        for "official" -- and one of those makes the whole site fail to build.
        """
        cleaned = extract.tidy("o\uffffcial documentation\u0085here")
        for code in (0xFFFF, 0x0085):
            self.assertNotIn(chr(code), cleaned)
        self.assertIn("cial documentation", cleaned)

    def test_newlines_survive_because_sentences_need_them(self):
        self.assertIn("\n", extract.tidy("one line\n\nanother line"))


class SectionTest(unittest.TestCase):
    def test_section_numbers_must_ascend_in_one_style(self):
        """A pseudocode step and a caption both look like headings."""
        text = ("I. Introduction\n"
                "Some prose here about the paper and what it does.\n"
                "5 Avail s selectRandomAvailableFunc (Fs,S);\n"
                "2 JackOrigin Table t0\n"
                "II. Background\n"
                "More prose follows in this section of the paper.\n")
        titles = [s["title"] for s in extract.find_sections(text)]
        self.assertEqual(["Introduction", "Background"], titles)

    def test_a_subsection_is_kept_with_its_section(self):
        text = ("1 Introduction\n"
                "Prose about the introduction of this paper.\n"
                "2 Approach\n"
                "Prose about the approach taken here.\n"
                "2.1 Overview\n"
                "Prose about the overview of the approach.\n")
        numbers = [s["number"] for s in extract.find_sections(text)]
        self.assertEqual(["1", "2", "2.1"], numbers)


class ReferenceTest(unittest.TestCase):
    def test_numbered_entries_are_split_out(self):
        text = ("Body of the paper.\n"
                "References\n"
                "[1] A. Author, Some Paper Title, 2020.\n"
                "[2] B. Writer, Another Paper, 2021.\n")
        start, entries = extract.parse_references(text)
        self.assertIsNotNone(start)
        self.assertEqual([1, 2], [e["number"] for e in entries])
        self.assertIn("Some Paper Title", entries[0]["text"])

    def test_a_bracketed_number_inside_an_entry_does_not_start_a_new_one(self):
        text = ("References\n"
                "[1] A. Author, A Paper About [3] Notation, 2020.\n"
                "[2] B. Writer, Another, 2021.\n")
        _, entries = extract.parse_references(text)
        self.assertEqual([1, 2], [e["number"] for e in entries])


if __name__ == "__main__":
    unittest.main()


class DottedReferenceTest(unittest.TestCase):
    """Springer numbers its bibliography "1." rather than "[1]".

    The bracketed marker finds nothing in an LNCS paper, which is how three
    papers arrived with zero references and so no citation-marker mentions at
    all.
    """

    SPRINGER = """References
1. https://www.wolfram.com/mathematica/
2. Abdul Khalek, S., Khurshid, S.: Automated SQL query generation. ASE 2010
3. Rigger, M., Su, Z.: Finding bugs in database systems via query partitioning
"""

    BRACKETED = """REFERENCES
[1] M. Someone, "A paper," vol. 22, no. 2, pp. 20-23, 2003.
[2] M. Rigger and Z. Su, "Finding bugs via query partitioning," 2020.
"""

    def test_a_dotted_bibliography_is_parsed(self):
        from tools.papers import extract
        _, refs = extract.parse_references(self.SPRINGER)
        self.assertEqual(3, len(refs))
        self.assertIn("query partitioning", refs[2]["text"])

    def test_a_bracketed_bibliography_still_wins(self):
        """The dotted fallback must not displace a reading that worked."""
        from tools.papers import extract
        _, refs = extract.parse_references(self.BRACKETED)
        self.assertEqual(2, len(refs))
        self.assertIn("Rigger", refs[1]["text"])

    def test_page_and_volume_numbers_do_not_start_entries(self):
        from tools.papers import extract
        _, refs = extract.parse_references(
            "References\n1. Someone, A.: A title. In: Venue, vol. 5, pp. 12. 2011\n")
        self.assertEqual(1, len(refs))

    def test_a_page_number_glued_to_the_heading(self):
        """"894REFERENCES" is what an IEEE page break leaves behind."""
        from tools.papers import extract
        _, refs = extract.parse_references(
            "some prose\n894REFERENCES \n[1] A. Someone, \"A paper,\" 2003.\n"
            "[2] M. Rigger and Z. Su, \"Query partitioning,\" 2020.\n")
        self.assertEqual(2, len(refs))
        self.assertIn("Rigger", refs[1]["text"])

    def test_a_bibliography_with_no_matchable_heading(self):
        """One paper runs the heading into the conclusion: "... environments.
        REFERENCE". The entries' own shape is what locates it."""
        from tools.papers import extract
        _, refs = extract.parse_references(
            "cloud-native environments. REFERENCE  \n"
            '[1]  C. Y. Jeong, H. C. Shin, "Sensor-data augmentation," 2020.\n'
            '[2] M. Rigger and Z. Su. "Finding Bugs in Database Systems via '
            'Query Partitioning". OOPSLA (2020).\n')
        self.assertEqual(2, len(refs))
        self.assertIn("Rigger", refs[1]["text"])

    def test_prose_citing_one_is_not_a_bibliography(self):
        from tools.papers import extract
        start, refs = extract.parse_references(
            "Relational databases are essential [1]. Oracle dominates [2].\n")
        self.assertIsNone(start)
        self.assertEqual([], refs)

    def test_a_bibliography_spelling_first_names_in_full(self):
        """"Djallel Bouneffouf. 2016." carries no initial to match on, so the
        year is what identifies the entry as a reference."""
        from tools.papers import extract
        _, refs = extract.parse_references(
            "will be developed as a more in-depth study.REFERENCES\n"
            "[1] Djallel Bouneffouf. 2016. Finite-time analysis. In ICML.\n"
            "[2] Manuel Rigger and Zhendong Su. 2020. Testing database engines "
            "via pivoted query synthesis. In OSDI. 667-682.\n")
        self.assertEqual(2, len(refs))
        self.assertIn("pivoted query synthesis", refs[1]["text"])

    def test_a_mid_sentence_citation_is_not_a_bibliography(self):
        """A bibliography lays entries out at the start of a line."""
        from tools.papers import extract
        start, refs = extract.parse_references(
            "As shown by earlier work [1] in 2016, this holds generally.\n")
        self.assertIsNone(start)
        self.assertEqual([], refs)


class PaperPageTest(unittest.TestCase):
    """One page per record, generated from the records themselves."""

    def _record(self, key, narrative=None):
        record = {"_key": key,
                  "paper": {"title": "Testing Databases via Something"},
                  "analysis": {"narrative": narrative} if narrative else None}
        return record

    def test_a_page_is_named_for_its_record(self):
        from tools.papers import pages
        markup = pages.render(self._record("paper_doi_10_1145_1"))
        self.assertIn("permalink: /impact/papers/paper_doi_10_1145_1/", markup)
        self.assertIn("paper_key: paper_doi_10_1145_1", markup)

    def test_a_quote_in_a_title_does_not_break_the_front_matter(self):
        from tools.papers import pages
        record = self._record("k")
        record["paper"]["title"] = 'Testing "Databases" via Something'
        markup = pages.render(record)
        self.assertIn(r'title: "Testing \"Databases\" via Something"', markup)

    def test_the_excerpt_is_the_sqlancer_relationship(self):
        """Not the paper's own abstract: that is what distinguishes the page."""
        from tools.papers import pages
        record = self._record("k", "DQE is built on SQLancer. More follows.")
        self.assertEqual("DQE is built on SQLancer.",
                         pages.excerpt_for(record))

    def test_a_record_that_leaves_loses_its_page(self):
        import tempfile, pathlib
        from tools.papers import pages
        with tempfile.TemporaryDirectory() as raw:
            tmp = pathlib.Path(raw)
            (tmp / "gone.html").write_text("stale", encoding="utf-8")
            outcome = pages.write_all(tmp, [self._record("here")])
            self.assertEqual("removed", outcome["gone"])
            self.assertTrue((tmp / "here.html").exists())
