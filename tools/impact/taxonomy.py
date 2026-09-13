"""Reading and applying the technique taxonomy.

Collectors never hardcode technique names. They ask this module, which loads
``_data/impact/techniques.json`` and knows how to match a name in free text --
including the part that matters most in practice: refusing to treat a bare
``TLP`` or ``PQS`` as a SQLancer reference unless the surrounding text is
actually about database testing.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Sequence

from . import config


class TechniqueMatch:
    __slots__ = ("technique_id", "matched_name", "start", "end", "context",
                 "needs_context", "context_terms_found")

    def __init__(self, technique_id, matched_name, start, end, context,
                 needs_context, context_terms_found):
        self.technique_id = technique_id
        self.matched_name = matched_name
        self.start = start
        self.end = end
        self.context = context
        self.needs_context = needs_context
        self.context_terms_found = context_terms_found

    def __repr__(self):
        return (f"TechniqueMatch({self.technique_id!r}, {self.matched_name!r}, "
                f"context_terms={self.context_terms_found!r})")


class Taxonomy:
    """Loaded view over ``techniques.json`` with matching helpers."""

    # Window of characters around a hit inspected for corroborating context.
    CONTEXT_WINDOW = 400

    def __init__(self, data: dict):
        self.data = data
        self.version = data["taxonomy_version"]
        self.techniques = {t["id"]: t for t in data["sqlancer_techniques"]}
        self.excluded = {t["id"]: t for t in data.get("excluded_techniques", [])}
        # Papers the project states are its own, beyond the technique and tool
        # papers already listed above.
        self.project_publications = list(data.get("project_publications", []))
        self.project_authors = list(data.get("project_authors", []))
        self.project_contributors = list(data.get("project_contributors", []))
        self._project_author_pattern = self._compile_author_names(
            self.project_authors)
        self.finders = {f["id"]: f for f in data["finders"]}
        self.symptoms = {s["id"] for s in data.get("bug_symptoms", [])}
        self._patterns = self._compile(self.techniques)
        self._excluded_patterns = self._compile(self.excluded)
        # One combined pattern for the finders, because their names are nested:
        # matching "SQLancer" and "SQLancer++" independently would report both
        # for a text that only mentions the latter.
        self._finder_pattern, self._finder_by_name = self._compile_owned(self.finders)

    # -- construction -----------------------------------------------------

    @staticmethod
    def _compile_names(names: Sequence[str]):
        # Longest first so "Ternary Logic Partitioning" wins over "TLP" and
        # "SQLancer++" wins over "SQLancer".
        ordered = sorted(set(names), key=len, reverse=True)
        alternatives = "|".join(re.escape(n) for n in ordered)
        # \b does not fire next to '+', so SQLancer++ needs an explicit boundary.
        return re.compile(rf"(?<![\w+])({alternatives})(?![\w])", re.IGNORECASE)

    def _compile(self, entries: Dict[str, dict]):
        return {
            key: self._compile_names(entry["names"])
            for key, entry in entries.items()
        }

    @staticmethod
    def _compile_author_names(names: Sequence[str]):
        """Pattern for matching a surname inside an author name.

        Stricter at the boundary than term matching: a hyphen ends a name here,
        so a double-barrelled surname is a different person, whereas in prose
        "TLP-based" is still a reference to TLP.
        """
        if not names:
            return None
        ordered = sorted(set(names), key=len, reverse=True)
        alternatives = "|".join(re.escape(name) for name in ordered)
        return re.compile(rf"(?<![\w'-])({alternatives})(?![\w'-])",
                          re.IGNORECASE)

    def _compile_owned(self, entries: Dict[str, dict]):
        """Combined pattern over all entries plus a name -> owning id map."""
        owner = {}
        for key, entry in entries.items():
            for name in entry["names"]:
                owner[name.lower()] = key
        return self._compile_names(list(owner)), owner

    # -- accessors --------------------------------------------------------

    def technique_ids(self) -> List[str]:
        return list(self.techniques)

    def umbrella_finder_ids(self) -> List[str]:
        return [f["id"] for f in self.finders.values() if f["umbrella_member"]]

    def is_known_technique(self, technique_id: Optional[str]) -> bool:
        return technique_id is None or technique_id in self.techniques

    def display_name(self, technique_id: Optional[str]) -> str:
        if technique_id is None:
            return "Not recorded"
        entry = self.techniques.get(technique_id)
        return entry["display_name"] if entry else technique_id

    def finder_display_name(self, finder_id: str) -> str:
        entry = self.finders.get(finder_id)
        return entry["display_name"] if entry else finder_id

    def seed_ids(self) -> List[str]:
        """Every id a citation seed can be recorded under.

        A seed is usually a technique, but a tool's own paper is a seed too --
        SQLancer++'s is -- and it is recorded under the tool's id. The two id
        spaces do not overlap.
        """
        return [seed["seed_id"] for seed in self.citation_seeds()
                if seed.get("seed_id")]

    def citation_seeds(self) -> List[dict]:
        """Foundational publications whose citations are crawled for papers.

        A tool's own paper can be a seed as an oracle's is. SQLancer++ is
        cited by work that cites no oracle paper, and walking only the oracle
        papers cannot reach it.
        """
        seeds = []
        for technique in self.techniques.values():
            paper = technique.get("origin_paper")
            if paper and paper.get("is_citation_seed"):
                seeds.append({"technique_id": technique["id"],
                              "seed_id": technique["id"], **paper})
        for finder in self.finders.values():
            paper = finder.get("paper")
            if paper and paper.get("is_citation_seed"):
                # No technique: citing SQLancer++'s paper says a work cites
                # the tool, not that it cites an oracle. seed_id identifies
                # the seed either way, and the id spaces do not overlap.
                seeds.append({"technique_id": None, "finder_id": finder["id"],
                              "seed_id": finder["id"], **paper})
        return seeds

    def search_terms(self) -> List[str]:
        """Every name worth issuing a discovery query for."""
        terms: List[str] = []
        for finder in self.finders.values():
            terms.extend(finder["names"])
        for technique in self.techniques.values():
            terms.extend(technique["names"])
        seen, ordered = set(), []
        for term in terms:
            lowered = term.lower()
            if lowered not in seen:
                seen.add(lowered)
                ordered.append(term)
        return ordered

    # -- matching ---------------------------------------------------------

    def mentions_finder(self, text: str) -> List[str]:
        """Umbrella tool names explicitly present in ``text``.

        Because the combined pattern prefers the longest alternative, a text
        that says only "SQLancer++" reports ``sqlancer_pp`` and not ``sqlancer``.
        """
        if not text:
            return []
        found: List[str] = []
        for hit in self._finder_pattern.finditer(text):
            finder_id = self._finder_by_name.get(hit.group(1).lower())
            if finder_id and finder_id not in found:
                found.append(finder_id)
        return found

    def find_techniques(self, text: str, *, require_context: bool = True
                        ) -> List[TechniqueMatch]:
        """Locate SQLancer technique names in ``text``.

        Ambiguous acronyms only produce a match when a corroborating
        database-testing term appears nearby and no negative term does. This is
        what keeps ``TLP`` in a Linux power-management thread out of the data.
        """
        if not text:
            return []
        matches: List[TechniqueMatch] = []
        lowered = text.lower()
        for technique_id, pattern in self._patterns.items():
            technique = self.techniques[technique_id]
            ambiguous = technique.get("ambiguous_acronym", False)
            required = [t.lower() for t in technique.get("required_context_terms", [])]
            negative = [t.lower() for t in technique.get("negative_context_terms", [])]
            for hit in pattern.finditer(text):
                start = max(0, hit.start() - self.CONTEXT_WINDOW)
                end = min(len(text), hit.end() + self.CONTEXT_WINDOW)
                window = lowered[start:end]
                # A full spelled-out name is unambiguous on its own.
                spelled_out = len(hit.group(1)) > 6
                if any(term in window for term in negative):
                    continue
                found_terms = [term for term in required if term in window]
                if (ambiguous and require_context and not spelled_out
                        and not found_terms):
                    continue
                matches.append(TechniqueMatch(
                    technique_id=technique_id,
                    matched_name=hit.group(1),
                    start=hit.start(),
                    end=hit.end(),
                    context=text[start:end],
                    needs_context=ambiguous and not spelled_out,
                    context_terms_found=found_terms,
                ))
        matches.sort(key=lambda m: m.start)
        return matches

    def is_project_author(self, name: Optional[str]) -> bool:
        """Whether an author name belongs to the SQLancer project.

        Matched as a whole word so that initialled forms ("M. Rigger") and the
        full name both hit, without a substring matching some longer surname.
        """
        if not name or self._project_author_pattern is None:
            return False
        return bool(self._project_author_pattern.search(name))

    def is_project_contributor(self, who: Optional[str],
                               extra_handles: Optional[Sequence[str]] = None
                               ) -> bool:
        """Whether a bug reporter belongs to the project rather than adopting it.

        Matched on the display name or the GitHub handle, since the collectors
        record whichever the source gave. ``extra_handles`` carries the TEST
        lab's current members, which are read from its site rather than listed
        here so the roster does not go stale.
        """
        if not who:
            return False
        needle = who.strip().lower()
        if not needle:
            return False
        if self.is_project_author(who):
            return True
        for entry in self.project_contributors:
            if needle == (entry.get("name") or "").strip().lower():
                return True
            if needle == (entry.get("github") or "").strip().lower():
                return True
        return any(needle == (handle or "").strip().lower()
                   for handle in (extra_handles or []))

    def find_excluded_techniques(self, text: str) -> List[str]:
        """Out-of-scope techniques named in ``text`` (currently: EET)."""
        if not text:
            return []
        return [key for key, pattern in self._excluded_patterns.items()
                if pattern.search(text)]

    def technique_from_oracle_label(self, label: Optional[str]) -> Optional[str]:
        """Map an upstream oracle label such as ``TLP (WHERE)`` to a technique id.

        Labels like ``error``, ``crash`` and ``hang`` describe how the bug
        manifested rather than which technique found it, so they map to no
        technique and are handled as symptoms instead.
        """
        if not label:
            return None
        for match in self.find_techniques(label, require_context=False):
            return match.technique_id
        return None


@lru_cache(maxsize=1)
def load() -> Taxonomy:
    return Taxonomy(config.load_json(config.DATA_FILES["techniques"]))


def reload() -> Taxonomy:
    load.cache_clear()
    return load()
