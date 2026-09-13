"""Finds and inspects research artifacts for signs of SQLancer reuse.

``uses_infrastructure`` is meant to be the objective category, so it is decided
from the artifact rather than from how a paper describes itself. This module
locates a paper's artifact repository and looks for concrete markers: a fork of
the SQLancer repository, the ``sqlancer`` Java package layout, retained source
files, a copyright notice, a README that says so.

No single marker is treated as decisive. ``Randomly.java`` shows up in plenty of
repositories that merely vendored a snippet, so the markers are returned as a
set and the decision -- taken elsewhere -- combines them.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from ..github import GitHub
from ..util import content_hash, now, truncate

COLLECTOR = "artifacts"
COLLECTOR_VERSION = "1.0.0"
EXTRACTOR_VERSION = "artifact-markers-v1"

SQLANCER_REPO_FULL_NAME = "sqlancer/sqlancer"

# Identifiers that together fingerprint SQLancer's own Randomly.java. This is
# what catches a derivative that renamed the package -- a rename defeats every
# path-based check while leaving the source itself untouched, and renaming is
# common enough in research artifacts that path matching alone misses real
# reuse. Requiring several of these together keeps an ordinary utility class
# named Randomly from matching.
RANDOMLY_FINGERPRINT = (
    "StringGenerationStrategy",
    "SOPHISTICATED",
    "cachedLongs",
    "cachedStrings",
    "cachedDoubles",
    "getPositiveIntegerNotNull",
    "ALPHANUMERIC_SPECIALCHAR",
    "getNotCachedInteger",
)
RANDOMLY_FINGERPRINT_MINIMUM = 5

# Path segments that mark a nested copy of somebody else's project: a vendored
# baseline, a bundled comparison artifact, a third-party tree. SQLancer's source
# found under one of these belongs to that nested project, not necessarily to
# the paper whose repository it happens to sit in -- so it is evidence worth
# looking at rather than evidence that settles the question.
NESTED_ARTIFACT_SEGMENT = re.compile(
    r"(^|/)([\w.-]*(?:artifact|baseline|comparison|competitor|related[_-]?work"
    r"|third[_-]?party|vendor|external|reference[_-]?impl)[\w.-]*)/",
    re.IGNORECASE)

# Files whose presence is characteristic of the SQLancer codebase.
CHARACTERISTIC_FILES = (
    "src/sqlancer/Randomly.java",
    "src/sqlancer/Main.java",
    "src/sqlancer/DBMSExecutor.java",
    "src/sqlancer/common/oracle/TestOracle.java",
    "src/sqlancer/common/oracle/NoRECOracle.java",
    "src/sqlancer/common/oracle/TLPWhereOracle.java",
)

README_REUSE = re.compile(
    r"(built?\s+(?:on\s+top\s+of|upon)\s+sqlancer|based\s+on\s+sqlancer|"
    r"fork(?:ed)?\s+(?:of|from)\s+sqlancer|extend(?:s|ed)?\s+sqlancer|"
    r"we\s+(?:use|used|modified|extended)\s+sqlancer|"
    r"implemented?\s+(?:on\s+top\s+of|in|within)\s+sqlancer)",
    re.IGNORECASE)

COPYRIGHT_NOTICE = re.compile(
    r"copyright[^\n]{0,80}sqlancer|sqlancer[^\n]{0,40}mit\s+license", re.IGNORECASE)

ARTIFACT_HOSTS = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "bitbucket.org": "bitbucket",
    "zenodo.org": "zenodo",
    "doi.org": "other",
    "figshare.com": "figshare",
}

# Text that a marker was found in, kept short enough to be readable evidence.
EXCERPT_LIMIT = 600


# Artifact repositories a maintainer has linked to a paper by hand.
#
# Forward search cannot always get there: a repository is usually named after
# the tool, the tool is often named only in the body of the paper rather than
# its title or indexed abstract, and the README frequently says "this paper"
# without a citation. Where the link is knowledge rather than a derivation, it
# is written down. The SQLancer markers are still verified by inspecting the
# repository; only the paper-to-repository link comes from here.
KNOWN_ARTIFACTS: Dict[str, str] = {
    # Empty on purpose. The first entry tried here was wrong -- a repository
    # guessed at from a title search -- while the automatic path linked the same
    # artifact correctly from its repository description. Add an entry only when
    # the link is known, not inferred.
}


def find_sqlancer_derived_repositories(gh: GitHub, *, limit: int = 100
                                       ) -> List[str]:
    """Repositories on GitHub that contain SQLancer's own source files.

    Code search rather than repository search, because the giveaway is a file
    path inside the repository, not anything in its name or description. This
    finds artifacts whose authors never mention SQLancer anywhere a reader
    would look.

    Incomplete by construction -- GitHub excludes forks from code search and
    indexes only some repositories -- so it supplements the other routes rather
    than replacing them.
    """
    found: List[str] = []
    queries = [
        # Content first: this is the query that finds derivatives which renamed
        # the package, which path-scoped queries cannot see at all.
        "filename:Randomly.java StringGenerationStrategy",
        "filename:Randomly.java ALPHANUMERIC_SPECIALCHAR",
        "path:src/sqlancer filename:Randomly.java",
        "path:src/sqlancer filename:DBMSExecutor.java",
        "path:src/sqlancer/common/oracle filename:TestOracle.java",
    ]
    for query in queries:
        try:
            for item in gh.search_code(query, max_pages=1):
                full_name = (item.get("repository") or {}).get("full_name")
                if full_name and full_name not in found:
                    found.append(full_name)
                if len(found) >= limit:
                    return found
        except RuntimeError:
            continue
    return found


def classify_artifact_url(url: str) -> Optional[str]:
    lowered = url.lower()
    for host, kind in ARTIFACT_HOSTS.items():
        if host in lowered:
            if kind == "other" and "zenodo" in lowered:
                return "zenodo"
            return kind
    return None


# Words that look like a tool name but are just prose. A paper's tool name is
# the thing its artifact repository is usually called, so this list is what
# stops "SQL" or "DBMS" being treated as one.
_NOT_TOOL_NAMES = {
    "SQL", "DBMS", "DBMSS", "DBMSES", "NOSQL", "ACID", "API", "CPU", "GPU",
    "LLM", "LLMS", "AI", "ML", "IR", "AST", "CI", "CD", "OLAP", "OLTP", "JSON",
    "XML", "HTML", "UDF", "UDFS", "JIT", "IO", "OS", "RDBMS", "RDBMSS", "TPC",
    "ANSI", "ISO", "IEEE", "ACM", "USENIX", "ICSE", "FSE", "ASE", "OSDI",
    "SIGMOD", "VLDB", "OOPSLA", "PLDI", "ISSTA", "WHERE", "SELECT", "GROUP",
    "ORDER", "JOIN", "NULL", "AND", "OR", "NOT", "THE", "FOR", "VIA", "WITH",
    "TEST", "TESTING", "BUGS", "BUG", "NEW", "TOOL", "PAPER", "ARTIFACT",
}

# "we implement ... in a tool called X" and its common variants.
TOOL_NAME_PHRASE = re.compile(
    r"\b(?:tool|prototype|system|framework|implementation)\s+(?:called|named)\s+"
    r"([A-Z][A-Za-z0-9_+-]{2,24})"
    r"|\bwe\s+(?:call|name)\s+(?:it|our\s+\w+)\s+([A-Z][A-Za-z0-9_+-]{2,24})"
    r"|\b(?:we\s+)?(?:present|introduce|propose|implement(?:ed)?|built?)\s+"
    r"([A-Z][A-Za-z0-9_+-]{2,24})\s*,?\s*(?:a|an|the)\b")


def candidate_tool_names(title: str, abstract: Optional[str] = None) -> List[str]:
    """Tool names a paper appears to give its own implementation.

    Artifact repositories are named after the tool far more often than after
    the paper, so this is what makes a repository findable at all. Names are
    drawn from the paper's own text only.
    """
    names: List[str] = []

    def add(name: Optional[str]) -> None:
        if not name:
            return
        cleaned = name.strip().strip(".,:;")
        if len(cleaned) < 3 or cleaned.upper() in _NOT_TOOL_NAMES:
            return
        if cleaned not in names:
            names.append(cleaned)

    for match in TOOL_NAME_PHRASE.finditer(abstract or ""):
        for group in match.groups():
            add(group)
    # A leading "NAME: rest of the title" is the other common convention.
    leading = re.match(r"^([A-Z][A-Za-z0-9_+-]{2,24})\s*[:\u2013-]\s+", title or "")
    if leading:
        add(leading.group(1))
    # All-caps acronyms in the title are usually the tool.
    for token in re.findall(r"\b([A-Z][A-Z0-9]{2,15})\b", title or ""):
        add(token)
    return names[:4]


def find_repository_candidates(gh: GitHub, title: str, *,
                               tool_names: Optional[Sequence[str]] = None,
                               limit: int = 5) -> List[dict]:
    """Search GitHub for repositories that might hold a paper's artifact.

    Two queries, because artifacts are named inconsistently: the tool name as a
    repository name, and the paper title in the description or README. Results
    are only candidates -- linking one to the paper still needs its own
    evidence.
    """
    results: List[dict] = []
    seen = set()

    def collect(query: str) -> None:
        try:
            for item in gh.search("repositories", query, max_pages=1):
                full_name = item.get("full_name")
                if full_name and full_name not in seen:
                    seen.add(full_name)
                    results.append(item)
                if len(results) >= limit:
                    return
        except RuntimeError:
            return

    for name in (tool_names or [])[:2]:
        if len(results) >= limit:
            break
        collect(f"{name} in:name")

    words = [w for w in re.findall(r"[A-Za-z0-9+]{4,}", title or "")][:6]
    if len(words) >= 2 and len(results) < limit:
        collect(" ".join(words) + " in:name,description,readme")
    return results[:limit]


# Terms that show a repository is about database testing at all. Without one of
# these, a name collision is far more likely than a real artifact.
DBMS_CONTEXT = re.compile(
    r"\b(sql|dbms|database|databases|query|queries|oracle|fuzz\w*|"
    r"logic bug|test(?:ing)?|sqlite|mysql|postgres\w*|duckdb|mariadb|"
    r"cockroach\w*|tidb|clickhouse)\b", re.IGNORECASE)


def repository_text(gh: GitHub, owner: str, repo: str) -> Optional[dict]:
    """A repository's description and README, fetched once.

    Linking artifacts to papers compares every candidate repository against
    every paper, so the text each comparison needs is read once and then held
    in memory rather than re-read per paper.
    """
    metadata = gh.repo(owner, repo)
    if not metadata:
        return None
    default_branch = metadata.get("default_branch") or "HEAD"
    readme = ""
    for name in ("README.md", "readme.md", "README.rst", "README"):
        text, _ = gh.raw_file(owner, repo, name, default_branch, max_age_days=30)
        if text:
            readme = text
            break
    return {
        "full_name": f"{owner}/{repo}",
        "owner": owner,
        "repo": repo,
        "description": metadata.get("description") or "",
        "readme": readme,
        "default_branch": default_branch,
    }


def link_evidence(gh: GitHub, owner: str, repo: str, paper: dict, *,
                  tool_names: Sequence[str], timestamp: str
                  ) -> Optional[dict]:
    """Evidence that this repository is that paper's artifact, or None."""
    entry = repository_text(gh, owner, repo)
    if entry is None:
        return None
    return link_evidence_from_text(entry, paper, tool_names=tool_names,
                                   timestamp=timestamp)


def link_evidence_from_text(entry: dict, paper: dict, *,
                            tool_names: Sequence[str], timestamp: str
                            ) -> Optional[dict]:
    """Evidence that this repository is that paper's artifact, or None.

    A repository is only linked when something ties it to the paper in
    particular: its text names the paper, its DOI or its arXiv id, or it is
    named after the tool the paper says it built and is recognisably about
    database testing. Marker evidence says a repository derives from SQLancer;
    it says nothing about whose artifact it is, so the two are kept separate.
    """
    owner, repo = entry["owner"], entry["repo"]
    description, readme = entry["description"], entry["readme"]
    haystack = f"{description}\n{readme}"
    url = f"https://github.com/{owner}/{repo}"

    title = (paper.get("title") or "").strip()
    normalised_title = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    normalised_hay = re.sub(r"[^a-z0-9]+", " ", haystack.lower())

    def evidence(excerpt: Optional[str], note: str) -> dict:
        item = {
            "source_url": url,
            "source_type": "github_repository",
            "excerpt": excerpt,
            "note": note,
            "retrieved_at": timestamp,
            "first_seen": timestamp,
            "last_verified": timestamp,
        }
        if excerpt is not None:
            item["excerpt_is_verbatim"] = True
        return item

    if normalised_title and normalised_title in normalised_hay:
        # Locate the title in the raw text, allowing for the whitespace and
        # punctuation that normalisation removed. Without this the quote can
        # come from somewhere else in the README entirely, which is worse than
        # no quote at all: it looks like evidence and is not.
        flexible = r"\s*".join(re.escape(word) for word in title.split())
        match = re.search(flexible, haystack, re.IGNORECASE)
        excerpt = _sentence_around(haystack, match.start()) if match else None
        return evidence(excerpt, "Repository names this paper.")

    for identifier in filter(None, [paper.get("doi"), paper.get("arxiv_id")]):
        if identifier.lower() in haystack.lower():
            index = haystack.lower().find(identifier.lower())
            return evidence(_sentence_around(haystack, index),
                            f"Repository cites the paper's {identifier}.")

    for name in tool_names:
        if repo.lower() != name.lower():
            continue
        if not DBMS_CONTEXT.search(haystack):
            continue
        return evidence(
            truncate(description or readme.strip().splitlines()[0], EXCERPT_LIMIT)
            if (description or readme.strip()) else None,
            (f"Repository is named after {name}, the tool this paper says it "
             f"built, and is about database testing."))
    return None


def inspect_repository(gh: GitHub, owner: str, repo: str, *,
                       timestamp: Optional[str] = None
                       ) -> Tuple[List[str], List[dict]]:
    """Return ``(markers, evidence)`` for a candidate artifact repository."""
    timestamp = timestamp or now()
    url = f"https://github.com/{owner}/{repo}"
    markers: List[str] = []
    evidence: List[dict] = []

    metadata = gh.repo(owner, repo)
    if not metadata:
        return markers, evidence

    parent = (metadata.get("parent") or {}).get("full_name", "").lower()
    source = (metadata.get("source") or {}).get("full_name", "").lower()
    if SQLANCER_REPO_FULL_NAME in (parent, source):
        markers.append("fork_of_sqlancer_repository")
        evidence.append({
            "source_url": url,
            "source_type": "github_repository",
            "excerpt": None,
            "note": (f"GitHub reports {owner}/{repo} as a fork of "
                     f"{SQLANCER_REPO_FULL_NAME}."),
            "retrieved_at": timestamp,
            "first_seen": timestamp,
            "last_verified": timestamp,
        })

    default_branch = metadata.get("default_branch") or "HEAD"
    tree = gh.tree(owner, repo, default_branch)
    paths = {entry["path"] for entry in tree if entry.get("type") == "blob"}
    directories = {entry["path"] for entry in tree if entry.get("type") == "tree"}

    if any(path.startswith("src/sqlancer/") for path in paths):
        markers.append("sqlancer_package_structure")
    if any(directory.startswith("src/sqlancer/") for directory in directories):
        markers.append("sqlancer_provider_directory")

    present = [path for path in CHARACTERISTIC_FILES if path in paths]
    if present:
        markers.append("retained_sqlancer_source_files")
        evidence.append({
            "source_url": f"{url}/blob/{default_branch}/{present[0]}",
            "source_type": "github_code",
            "excerpt": None,
            "note": ("Repository retains SQLancer source files: "
                     + ", ".join(present[:5])),
            "retrieved_at": timestamp,
            "first_seen": timestamp,
            "last_verified": timestamp,
        })
    if "src/sqlancer/Randomly.java" in paths:
        markers.append("randomly_java_present")

    # Any Randomly.java, wherever it sits and whatever package it declares.
    for path in sorted(p for p in paths
                       if p == "Randomly.java" or p.endswith("/Randomly.java")):
        if path == "src/sqlancer/Randomly.java":
            continue
        text, _ = gh.raw_file(owner, repo, path, default_branch)
        if not text:
            continue
        present = [token for token in RANDOMLY_FINGERPRINT if token in text]
        if len(present) < RANDOMLY_FINGERPRINT_MINIMUM:
            continue
        nested = NESTED_ARTIFACT_SEGMENT.search(path)
        markers.append("sqlancer_source_in_nested_artifact" if nested
                       else "sqlancer_source_content_match")
        package = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.MULTILINE)
        if package and not package.group(1).startswith("sqlancer"):
            markers.append("renamed_sqlancer_package")
        snippet = _first_match_line(text, "stringgenerationstrategy")
        evidence.append({
            "source_url": f"{url}/blob/{default_branch}/{path}",
            "source_type": "github_code",
            "excerpt": snippet,
            "excerpt_is_verbatim": True,
            "note": ((f"{path} is SQLancer's Randomly.java, but it sits under "
                      f"{nested.group(2)}, which looks like a bundled copy of "
                      f"another project"
                      ) if nested else
                     f"{path} is SQLancer's Randomly.java"
                     + (f", with the package renamed to "
                        f"{package.group(1)}" if package and
                        not package.group(1).startswith("sqlancer") else "")
                     + f" ({len(present)} of {len(RANDOMLY_FINGERPRINT)} "
                       f"identifiers match: {', '.join(present[:5])})."),
            "content_sha256": content_hash(text),
            "retrieved_at": timestamp,
            "first_seen": timestamp,
            "last_verified": timestamp,
        })
        break

    for build_file in ("pom.xml", "build.gradle", "build.gradle.kts"):
        if build_file not in paths:
            continue
        text, _ = gh.raw_file(owner, repo, build_file, default_branch)
        if text and "sqlancer" in text.lower():
            markers.append("sqlancer_build_file_reference")
            snippet = _first_match_line(text, "sqlancer")
            if snippet:
                evidence.append({
                    "source_url": f"{url}/blob/{default_branch}/{build_file}",
                    "source_type": "github_code",
                    "excerpt": snippet,
                    "excerpt_is_verbatim": True,
                    "note": f"{build_file} identifies the project as SQLancer.",
                    "content_sha256": content_hash(text),
                    "retrieved_at": timestamp,
                    "first_seen": timestamp,
                    "last_verified": timestamp,
                })
            break

    for readme in ("README.md", "readme.md", "README.rst", "README"):
        if readme not in paths:
            continue
        text, _ = gh.raw_file(owner, repo, readme, default_branch)
        if not text:
            break
        match = README_REUSE.search(text)
        if match:
            markers.append("readme_states_reuse")
            evidence.append({
                "source_url": f"{url}/blob/{default_branch}/{readme}",
                "source_type": "documentation",
                "excerpt": _sentence_around(text, match.start()),
                "excerpt_is_verbatim": True,
                "note": "README states that the artifact reuses SQLancer.",
                "content_sha256": content_hash(text),
                "retrieved_at": timestamp,
                "first_seen": timestamp,
                "last_verified": timestamp,
            })
        break

    for licence in ("LICENSE", "LICENSE.md", "LICENSE.txt", "NOTICE"):
        if licence not in paths:
            continue
        text, _ = gh.raw_file(owner, repo, licence, default_branch)
        if text and COPYRIGHT_NOTICE.search(text):
            markers.append("sqlancer_copyright_or_license_notice")
            match = COPYRIGHT_NOTICE.search(text)
            evidence.append({
                "source_url": f"{url}/blob/{default_branch}/{licence}",
                "source_type": "github_code",
                "excerpt": _sentence_around(text, match.start()),
                "excerpt_is_verbatim": True,
                "note": "Licence file carries a SQLancer copyright notice.",
                "content_sha256": content_hash(text),
                "retrieved_at": timestamp,
                "first_seen": timestamp,
                "last_verified": timestamp,
            })
        break

    return sorted(set(markers)), evidence


def _first_match_line(text: str, needle: str) -> Optional[str]:
    for line in text.splitlines():
        if needle in line.lower():
            stripped = line.strip()
            if stripped:
                return truncate(stripped, EXCERPT_LIMIT)
    return None


def _sentence_around(text: str, index: int) -> str:
    """Verbatim slice of ``text`` around ``index``, snapped to line breaks."""
    start = text.rfind("\n", 0, max(0, index - 200))
    start = 0 if start < 0 else start + 1
    end = text.find("\n", index + 200)
    end = len(text) if end < 0 else end
    return truncate(text[start:end].strip(), EXCERPT_LIMIT)


# Markers that on their own are strong enough to settle the question, and the
# weaker ones that need corroboration.
DECISIVE_MARKERS = {
    "fork_of_sqlancer_repository",
    "sqlancer_copyright_or_license_notice",
    "readme_states_reuse",
    "git_history_derived_from_sqlancer",
    # SQLancer's own source, identified by its content rather than its path.
    "sqlancer_source_content_match",
}
SUPPORTING_MARKERS = {
    "sqlancer_source_in_nested_artifact",
    "renamed_sqlancer_package",
    "sqlancer_package_structure",
    "retained_sqlancer_source_files",
    "sqlancer_build_file_reference",
    "sqlancer_provider_directory",
    "randomly_java_present",
}


def markers_are_conclusive(markers: Sequence[str]) -> bool:
    """Whether the marker set settles infrastructure reuse without a model.

    One decisive marker suffices; otherwise at least two supporting markers are
    required, so a lone ``Randomly.java`` never carries the decision.
    """
    marker_set = set(markers)
    if marker_set & DECISIVE_MARKERS:
        return True
    return len(marker_set & SUPPORTING_MARKERS) >= 2
