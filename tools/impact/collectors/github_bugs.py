"""Discovers SQLancer-attributed bug reports on GitHub.

Searching is deterministic and incremental: the taxonomy supplies the terms,
GitHub's ``created:``/``updated:`` qualifiers restrict a weekly run to threads
that actually changed, and the raw cache keys issue bodies on the thread's
``updated_at`` so an unchanged thread costs no request at all.

Attribution then happens in two tiers. A report that states in so many words
that SQLancer or one of its oracles found the bug is accepted deterministically,
with the sentence stored verbatim. Everything else -- a passing mention, a bare
acronym, a report about SQLancer rather than from it -- goes to the classifier,
and is dropped if the classifier is unavailable or unconvinced. Nothing is
admitted on a keyword match alone.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from .. import config, taxonomy
from ..classify import (Classifier, excluded_names, finder_names,
                        resolve_finder, resolve_technique, technique_names)
from ..github import GitHub
from ..util import (content_hash, normalize_url, now, truncate,
                    verbatim_excerpt, year_of)

COLLECTOR = "github_bugs"
COLLECTOR_VERSION = "1.0.0"
EXTRACTOR_VERSION = "github-bug-signals-v1"

# Phrasings that state a discovery rather than merely name a tool. Accepting on
# these alone is safe in a way that "the issue contains the word SQLancer" is
# not: "SQLancer does not support window functions" matches the latter.
DISCOVERY_STATEMENT = re.compile(
    r"("
    # Filler is allowed between the preposition and the tool: real reports say
    # "Found via automated fuzzing with SQLancer", not "found by SQLancer".
    # Requiring them adjacent was losing reports that state the discovery
    # perfectly clearly.
    r"(?:found|discovered|detected|caught|reported|identified|reproduced)\s+"
    r"(?:this\s+|the\s+)?(?:bug\s+|issue\s+|problem\s+)?"
    r"(?:by|with|using|via|through|in|while|during)\s+[^.\n]{0,60}?"
    r"(?<![\w+])(?P<tool_a>sqlancer\+\+|sqlancer|shqvel|norec|tlp|pqs|qpg|dqe|dqp|cert|coddtest)(?![\w])"
    r"|"
    r"(?P<tool_b>sqlancer\+\+|sqlancer|shqvel|norec|tlp|pqs|qpg|dqe|dqp|cert|coddtest)"
    # `reports` only in its verb form: "SQLancer reports a mismatch" is a
    # finding, "the sqlancer report is not useful" is a feature request.
    r"\s+(?:found|discovered|detected|caught|reported|identified|flagged|"
    r"reports|complains|got|hits|hit|triggers|triggered|produced|produces|"
    r"raised|raises|crashed|crashes)"
    r"|"
    r"(?:while|when)\s+(?:testing|fuzzing|running)\s+[^.\n]{0,60}?"
    r"(?:with|using)\s+(?:the\s+)?"
    r"(?P<tool_c>sqlancer\+\+|sqlancer|shqvel|norec|tlp|pqs|qpg)"
    r"|"
    r"(?:the\s+)?(?P<tool_d>norec|tlp|pqs|qpg|dqe|dqp|cert|coddtest)\s+"
    r"(?:test\s+)?oracle"
    r")",
    re.IGNORECASE)

# Projects that test with SQLancer routinely mark the resulting reports rather
# than describing the discovery in prose: a component tag in the title, or a
# mention that the failure showed up in a SQLancer run. That is the policy's
# campaign-evidence clause -- the report plainly came out of a SQLancer campaign
# -- and without it a project's whole SQLancer backlog is invisible.
CAMPAIGN_STATEMENT = re.compile(
    r"\[\s*(?:sqlancer\+\+|sqlancer|shqvel)\s*\]"
    r"|\b(?:in|during|from|via|by)\s+(?:a\s+|an\s+|the\s+|our\s+)?"
    r"(?:sqlancer\+\+|sqlancer)\s+"
    r"(?:test|tests|testing|run|runs|campaign|workload|workloads|job|jobs|"
    r"fuzzing|session)\b"
    r"|\b(?:sqlancer\+\+|sqlancer)\s+(?:test|tests|run|runs|job|jobs)\s+"
    r"(?:fail\w*|crash\w*|error\w*)",
    re.IGNORECASE)

# A campaign tag says where the report came from, not that it is a defect: the
# same tag appears on issues about the testing setup itself. These say it is.
DEFECT_LABEL = re.compile(r"\b(?:kind/bug|type/bug|bug|defect|crash)\b",
                          re.IGNORECASE)
DEFECT_TITLE = re.compile(
    r"\b(?:crash\w*|segv|sigsegv|core\s?dump\w*|assert\w*|panic\w*|"
    r"fatal|abort\w*|wrong\s+result\w*|incorrect\s+result\w*|"
    r"unexpected\s+result\w*|inconsistent\w*|error|exception|"
    r"internal\s+error|corrupt\w*|hang\w*|deadlock\w*|leak\w*|oom)\b",
    re.IGNORECASE)


def looks_like_a_defect(issue: dict) -> bool:
    """Whether an issue reports a defect rather than test-harness work."""
    labels = issue.get("labels") or []
    names = [label.get("name", "") if isinstance(label, dict) else str(label)
             for label in labels]
    if any(DEFECT_LABEL.search(name) for name in names):
        return True
    return bool(DEFECT_TITLE.search(issue.get("title") or ""))


# SQLancer names the tables it generates t0, t1, ... and their columns c0, c1,
# ..., and that convention survives into the reduced test case people paste into
# a bug report. It is a real fingerprint: 398 of the 499 bugs in SQLancer's own
# curated repository carry it.
#
# On its own it says a SQLancer-family generator produced the reproducer, not
# who ran it, so it is only ever used together with something that establishes
# the campaign -- a reporter known to work on SQLancer, or a curated list saying
# where the report came from.
REPRODUCER_SIGNATURE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`\[]?t\d{1,2}[\"`\]]?\s*\(",
    re.IGNORECASE)

# The other half of the convention: SQLancer names generated columns c0, c1.
# The table names alone are too weak -- other generators also number their
# tables t0, t1, but pair them with their own column names (c_pk, c_int, c_0),
# and without this those reports were being read as SQLancer's. On the
# project's own 499 curated bugs, requiring this as well costs exactly one.
REPRODUCER_COLUMN = re.compile(r"(?<![\w_])c\d{1,2}(?![\w])", re.IGNORECASE)


def reproducer_excerpt(text: str) -> Optional[str]:
    """The generated schema from a report, quoted verbatim."""
    text = text or ""
    match = REPRODUCER_SIGNATURE.search(text)
    if not match or not REPRODUCER_COLUMN.search(text):
        return None
    start = text.rfind("\n", 0, match.start())
    start = 0 if start < 0 else start + 1
    end = text.find("\n", match.end())
    for _ in range(3):  # a few statements give the convention room to show
        if end < 0:
            break
        nxt = text.find("\n", end + 1)
        if nxt < 0:
            break
        end = nxt
    end = len(text) if end < 0 else end
    return truncate(text[start:end].strip(), 700)


# Corroborating context required before an ambiguous acronym is trusted at all.
# The same test in the languages projects actually report in. openGauss files
# in Chinese, and an English-only guard threw away a report whose title names
# the oracle outright.
DB_CONTEXT = re.compile(
    r"\b(sql|query|queries|database|dbms|table|select|oracle|optimiz|optimis|"
    r"result set|logic bug)\b|数据库|查询|等价验证|优化器|聚合|结果集",
    re.IGNORECASE)

# Phrasing that marks the thread as a request about SQLancer rather than a bug
# SQLancer found. Without a classifier these have to be excluded outright.
FEATURE_REQUEST = re.compile(
    r"(would\s+be\s+(?:nice|good|great|helpful)|please\s+add\s+support|"
    r"feature\s+request|is\s+not\s+(?:really\s+)?useful|"
    r"improve\s+the\s+report|can\s+(?:we|you)\s+add|"
    r"(?:sqlancer|shqvel)\s+(?:does\s+not|doesn't)\s+support|"
    r"add\s+support\s+for\s+[^.\n]{0,40}(?:to|in)\s+sqlancer)",
    re.IGNORECASE)

MAX_BODY_CHARS = 24_000
# Longest excerpt stored as attribution evidence.
LIMIT = 700
MAX_SEARCH_PAGES = 10

STATE_TO_STATUS = {
    "open": ("open", True),
    "closed": ("fixed", True),
}


def _repo_index() -> Dict[str, str]:
    """Map ``owner/repo`` of each registered DBMS to its registry id.

    Every organisation the project is known to operate under is mapped, not
    just the owner of its main repository. Databend is the case that showed why:
    it moved from ``datafuselabs`` to ``databendlabs``, and its issues under the
    new organisation were being dropped as belonging to an unregistered system.
    """
    registry = config.load_json(config.DATA_FILES["dbms"])["dbms"]
    index: Dict[str, str] = {}
    for entry in registry:
        repository = entry.get("repository")
        if not repository:
            continue
        parts = repository.rstrip("/").split("/")
        if len(parts) < 2:
            continue
        owner, name = parts[-2], parts[-1]
        index[f"{owner}/{name}".lower()] = entry["id"]
        for other in entry.get("github_owners") or []:
            index[f"{other}/{name}".lower()] = entry["id"]
        # A project may file bugs in a repository of its own beside the main
        # one; DuckDB's fuzzer robot is the case this exists for.
        for extra in entry.get("github_repositories") or []:
            index[extra.lower()] = entry["id"]
    return index


def search_queries(tax: taxonomy.Taxonomy, repos: List[str]) -> List[str]:
    """Build the issue searches to run.

    Searching per repository rather than globally is the whole point here.
    GitHub returns at most 1000 results for a search and pages far fewer than
    that in practice, while "SQLancer" alone appears in well over a thousand
    issues -- so a global query silently sees a fraction of them and the rest
    are never even considered. Scoped to one repository at a time, each search
    returns a complete set: yugabyte-db has fifty, comfortably under any cap.

    A global query is still issued as a catch-all, since that is how a bug in a
    system the registry does not know yet gets noticed at all.
    """
    unambiguous = [name for name in tax.search_terms()
                   if len(name) > 6 or name.lower() in ("norec", "shqvel")]
    ambiguous = [technique["names"][0] for technique in tax.techniques.values()
                 if technique.get("ambiguous_acronym")]

    queries: List[str] = []
    for repo in repos:
        # One query for the unambiguous names, which need no context, and one
        # for the acronyms, which the attribution rules then check in context.
        names = " OR ".join(f'"{term}"' for term in unambiguous[:6])
        queries.append(f"repo:{repo} ({names}) in:body,title type:issue")
        if ambiguous:
            acronyms = " OR ".join(f'"{term}"' for term in ambiguous)
            queries.append(f"repo:{repo} ({acronyms}) in:body,title type:issue")

    for term in unambiguous[:4]:
        queries.append(f'"{term}" in:body,title type:issue')

    # Labels are the strongest signal there is: a project that labels an issue
    # `sqlancer` is itself saying where the report came from. Searched globally
    # because the whole label fits well inside one search.
    for label in ("sqlancer", "SQLancer", "sqlancer++", "norec", "tlp", "pqs"):
        queries.append(f'label:"{label}" type:issue')
    return queries


def sqlancer_labels(issue: dict, tax: taxonomy.Taxonomy) -> List[str]:
    """Labels on an issue that name SQLancer or one of its techniques."""
    found = []
    for label in issue.get("labels") or []:
        name = label.get("name", "") if isinstance(label, dict) else str(label)
        if not name:
            continue
        if tax.mentions_finder(name) or tax.find_techniques(
                name, require_context=False):
            found.append(name)
    return found


def _issue_source_text(issue: dict, comments: Optional[List[dict]] = None) -> str:
    """Assemble the text a decision may be based on, newest context last."""
    parts = [issue.get("title") or "", issue.get("body") or ""]
    for comment in (comments or [])[:10]:
        parts.append(comment.get("body") or "")
    return truncate("\n\n".join(part for part in parts if part), MAX_BODY_CHARS)


def _unambiguous(matches, tax: taxonomy.Taxonomy):
    """The first matched technique whose name is not an ambiguous acronym."""
    for match in matches:
        entry = tax.techniques.get(match.technique_id) or {}
        if not entry.get("ambiguous_acronym"):
            return match
    return None


def _span_excerpt(text: str, start: int, end: int) -> str:
    """Verbatim text around a span, for a match that is not a regex match."""
    class _Span:
        def __init__(self, a, b):
            self._a, self._b = a, b

        def start(self):
            return self._a

        def end(self):
            return self._b

    return _statement_excerpt(text, _Span(start, end))


def _statement_excerpt(text: str, match: re.Match) -> str:
    """Verbatim text around a discovery statement.

    Anchored on the line the match sits in rather than a fixed character
    window, so a mention buried in a stack trace quotes that line instead of
    several hundred characters of unrelated trace.
    """
    start = text.rfind("\n", 0, match.start())
    start = 0 if start < 0 else start + 1
    end = text.find("\n", match.end())
    if end < 0:
        end = len(text)
    elif end - start < 120:
        # Include one continuation line when the first is short, so a wrapped
        # sentence is not cut in half.
        following = text.find("\n", end + 1)
        end = len(text) if following < 0 else following
    # Keep the matched phrase inside the excerpt: on a very long line, trimming
    # from the start is what preserves the sentence the attribution rests on.
    if match.end() - start > LIMIT:
        start = max(start, match.start() - 200)
    return truncate(text[start:end].strip(), LIMIT)


def deterministic_attribution(text: str, tax: taxonomy.Taxonomy,
                              issue: Optional[dict] = None, *,
                              reporter_runs_campaigns: bool = False
                              ) -> Optional[Tuple[str, Optional[str], str, str]]:
    """Try to settle attribution without a model.

    Returns ``(rule, technique_id, finder_id, excerpt)`` when the text states a
    discovery outright or the report is plainly the output of a SQLancer
    campaign, otherwise ``None``.

    ``reporter_runs_campaigns`` says the report was reached by searching a known
    campaign reporter's issues rather than by searching for the tool. That
    changes what counts as evidence: such a report need not name SQLancer, and
    usually does not, so the reproducer's own shape is allowed to establish it.
    """
    if tax.find_excluded_techniques(text):
        # The report credits a technique that did not originate in SQLancer.
        return None
    match = DISCOVERY_STATEMENT.search(text)
    if not match:
        campaign = _campaign_attribution(text, tax, issue)
        if campaign is not None:
            return campaign
        if reporter_runs_campaigns:
            return (_reproducer_attribution(text, issue)
                    or _campaign_reporter_attribution(text, issue))
        return None
    tool = next((match.group(name) for name in
                 ("tool_a", "tool_b", "tool_c", "tool_d")
                 if match.group(name)), None)
    if not tool:
        return None

    excerpt = _statement_excerpt(text, match)
    if FEATURE_REQUEST.search(excerpt):
        # Reads like a request about SQLancer rather than a bug it found. Not
        # accepted here, but the candidate still goes on to the classifier.
        return None
    finder = resolve_finder(tax, tool) or "sqlancer"
    technique = resolve_technique(tax, tool)

    if technique is not None:
        entry = tax.techniques[technique]
        if entry.get("ambiguous_acronym") and not DB_CONTEXT.search(excerpt):
            return None
        return "technique_attribution", technique, finder, excerpt
    if tax.mentions_finder(tool):
        # Named the tool but not the oracle; look for an oracle elsewhere.
        matches = tax.find_techniques(text)
        technique = matches[0].technique_id if matches else None
        return "explicit_tool_statement", technique, finder, excerpt
    return None


def _reproducer_attribution(text: str, issue: Optional[dict]
                            ) -> Optional[Tuple[str, Optional[str], str, str]]:
    """Attribution from the reproducer's own schema, for a campaign reporter.

    SQLancer generates the schema it tests against, so its reproducers create
    tables named t0, t1 with columns c0, c1. That shape survives into the bug
    report even when the reporter never says what produced it -- which is the
    normal case, because a bug report is about the bug. On the project's own
    curated list of 499 bugs this signature is present in 398.

    It is only trusted for someone already known to run SQLancer campaigns.
    On its own the convention is suggestive, not conclusive; paired with a
    reporter whose reports are SQLancer output, it is the evidence.
    """
    if not looks_like_a_defect(issue or {}):
        return None
    excerpt = reproducer_excerpt(text)
    if not excerpt:
        return None
    return "campaign_evidence", None, "sqlancer", excerpt


def _confidence_for(rule: str) -> str:
    """How far the evidence goes, kept in one place so records stay comparable."""
    if rule == "campaign_reporter":
        return "low"      # rests on who reported it, not on what they wrote
    if rule == "campaign_evidence":
        return "medium"   # the report carries a SQLancer-shaped reproducer
    return "high"         # the report names the tool or the oracle


def _campaign_reporter_attribution(text: str, issue: Optional[dict]
                                   ) -> Optional[Tuple[str, Optional[str], str, str]]:
    """Attribution from who filed the report, when the report says nothing.

    The weakest rule here, and deliberately the last one tried. It exists
    because the people who run SQLancer campaigns file database bugs for that
    reason, and their reports usually give no other sign: the tool is not
    mentioned, and the reproducer has been minimised by hand before filing,
    which removes the generated schema along with everything else incidental.

    Only a defect report counts, and only in a database system's own tracker --
    both checked by the caller. What is quoted is the report's own description
    of the defect, so the record still carries the reporter's words rather than
    an assertion of ours; the roster that admitted it is attached separately by
    ``build_record``.
    """
    issue = issue or {}
    if not _is_a_bug_report(issue, text):
        return None
    excerpt = _defect_excerpt(text)
    if not excerpt:
        return None
    return "campaign_reporter", None, "sqlancer", excerpt


# Things a member files that are not bug reports.
NOT_A_BUG_REPORT = re.compile(
    r"\b(?:feature\s+request|support\s+for|add\s+support|please\s+add|"
    r"proposal|rfc|tracking\s+issue|meta\s+issue|umbrella|roadmap|"
    r"documentation|docs?\s+typo|typo|question|how\s+(?:do|to|can)\b|"
    r"release\s+notes?|discussion)\b", re.IGNORECASE)


def _is_a_bug_report(issue: dict, text: str) -> bool:
    """Whether a roster member's issue is a bug report rather than something else.

    ``looks_like_a_defect`` asks the opposite question -- does this say "crash"
    or "wrong result" -- and that is the wrong test here. A carefully written
    report describes the misbehaviour instead of naming it ("Dolt ignores LIMIT
    inside a correlated EXISTS subquery"), and carries whatever triage label the
    project happens to use ("reproduced", "customer issue"), so demanding the
    vocabulary of a crash report threw away most of these.

    So this asks whether the issue is plainly something else instead: a pull
    request, a feature request, a question, a tracking issue. A defect label or
    a defect-shaped title still settles it outright.
    """
    if "pull_request" in issue:
        return False   # presence, not truthiness: the field can be empty
    if looks_like_a_defect(issue):
        return True
    title = issue.get("title") or ""
    if NOT_A_BUG_REPORT.search(title) or FEATURE_REQUEST.search(text):
        return False
    # A bug report describes something; a one-line ask usually does not.
    return len((issue.get("body") or "").strip()) >= 120


# Boilerplate a report template puts above the actual description.
TEMPLATE_HEADING = re.compile(
    r"^\s*(#{1,4}\s*)?(what happened|what happens|describe the bug|bug "
    r"description|summary|steps? to reproduce|how to reproduce|environment|"
    r"expected behaviou?r|actual behaviou?r|to reproduce)\s*:?\s*$",
    re.IGNORECASE)


def _defect_excerpt(text: str) -> Optional[str]:
    """The report's own description of the defect, quoted verbatim.

    Skips the headings a report template supplies, since "## What happened" is
    the template's words rather than the reporter's and says nothing about the
    bug.
    """
    for line in (text or "").splitlines():
        stripped = line.strip()
        if len(stripped) < 25 or TEMPLATE_HEADING.match(stripped):
            continue
        if stripped.startswith(("```", "|", ">", "<!--")):
            continue
        return truncate(stripped, LIMIT)
    return None


# Where the tool is named, for picking a sentence to quote when nothing more
# specific matched.
TOOL_MENTION = re.compile(r"(?<![\w+])(sqlancer\+\+|sqlancer|shqvel)(?![\w])",
                          re.IGNORECASE)


def _campaign_attribution(text: str, tax: taxonomy.Taxonomy,
                          issue: Optional[dict]
                          ) -> Optional[Tuple[str, Optional[str], str, str]]:
    """Attribution from a defect report, in a DBMS tracker, that names SQLancer.

    Pattern-matching how people phrase a discovery was the wrong shape. Reports
    say "Found via automated fuzzing with SQLancer", "[YSQL][SQLancer]",
    "SQLancer got:", "Test case generated from SQLancer" -- and every pattern
    written to catch them missed more than it caught.

    So the rule is the plain one: a defect report, in a database system's own
    tracker, that names SQLancer. There is little else such a report would be.
    The phrasing patterns are still consulted, but only to choose which sentence
    to quote, since one that states the discovery is better evidence than the
    first mention anywhere in the text.

    Two things still disqualify: language marking the issue as a request about
    SQLancer rather than a bug it found, and crediting a technique that did not
    originate in SQLancer.
    """
    if issue is None:
        return None

    # A project's own label settles it, whatever the issue text says and
    # whether or not the title reads like a defect: the project applied it.
    labels = sqlancer_labels(issue, tax)
    if labels:
        match = (DISCOVERY_STATEMENT.search(text)
                 or CAMPAIGN_STATEMENT.search(text)
                 or TOOL_MENTION.search(text))
        excerpt = (_statement_excerpt(text, match) if match
                   else f"Labelled {', '.join(labels)}")
        named = tax.mentions_finder(" ".join(labels)) or tax.mentions_finder(text)
        matched = (tax.find_techniques(" ".join(labels), require_context=False)
                   or tax.find_techniques(text))
        return ("campaign_evidence",
                matched[0].technique_id if matched else None,
                named[0] if named else "sqlancer", excerpt)

    if not looks_like_a_defect(issue):
        return None

    finders = tax.mentions_finder(text)
    technique_matches = tax.find_techniques(text)
    if not finders and not technique_matches:
        return None
    if tax.find_excluded_techniques(text) and not finders:
        return None

    match = (DISCOVERY_STATEMENT.search(text)
             or CAMPAIGN_STATEMENT.search(text)
             or TOOL_MENTION.search(text))
    if match is not None:
        excerpt = _statement_excerpt(text, match)
    elif _unambiguous(technique_matches, tax) or (
            technique_matches and reproducer_excerpt(text)):
        # The report names an oracle but never the tool, so none of the
        # sentence patterns has anything to anchor on -- TOOL_MENTION knows
        # only the tool's own names. An unambiguous oracle name is its own
        # anchor: "the NoREC-transformed query using SUM ... returns 0" is a
        # report of a SQLancer oracle finding a bug, whether or not the word
        # SQLancer appears anywhere in it.
        #
        # Only unambiguous ones. An acronym like CERT or TLP needs database
        # context to be a technique at all, and in a DBMS tracker that context
        # is always present -- every issue is about a database. Requiring the
        # tool to be named is what keeps TLS certificates in a Galera cluster
        # report from being read as cardinality estimation testing.
        # An ambiguous acronym is allowed to anchor only when the report also
        # carries SQLancer's generated-schema fingerprint. That is what tells
        # "TLP equivalence verification" over a t0(c0 ...) schema apart from a
        # TLS certificate in a cluster report, and it works in any language:
        # the reproducer is SQL either way. openGauss's tracker is in Chinese,
        # so no English sentence pattern can reach it.
        first = _unambiguous(technique_matches, tax) or technique_matches[0]
        excerpt = _span_excerpt(text, first.start, first.end)
    else:
        return None
    if FEATURE_REQUEST.search(excerpt):
        return None

    finder = finders[0] if finders else "sqlancer"
    technique = technique_matches[0].technique_id if technique_matches else None
    return "campaign_evidence", technique, finder, excerpt


# Where the roster of campaign reporters is published, cited as the evidence
# for a record admitted on the strength of who filed it.
ROSTER_URL = "https://nus-test.github.io/people/"

# Systems a lab member's name does not vouch for. The lab tests graph database
# systems as well, with tools developed independently of SQLancer, and SQLancer
# itself has no provider for any of them -- so a defect report filed against one
# by someone on the roster is not the output of a SQLancer campaign, which is
# the whole of what the reporter rule infers. Ruled by Manuel Rigger,
# 2026-09-12, after 38 records reached the dataset this way.
OUTSIDE_SQLANCER_CAMPAIGNS = {"neo4j", "redisgraph"}


def _evidence_note(rule: str) -> str:
    if rule == "campaign_reporter":
        return ("The reporter runs SQLancer campaigns; this is their "
                "description of the defect.")
    if rule == "campaign_evidence":
        return "The project marks this report as coming from a SQLancer run."
    return "The report states which tool or oracle found the bug."


def _roster_evidence(issue: dict, timestamp: str) -> dict:
    """Why this reporter's database bugs count, stated openly."""
    reporter = (issue.get("user") or {}).get("login") or "the reporter"
    return {
        "source_url": ROSTER_URL,
        "source_type": "website",
        "note": (f"{reporter} is listed as a member of the TEST lab, which "
                 f"runs SQLancer campaigns against database systems. This "
                 f"record rests on that membership, not on anything the "
                 f"report says."),
        "retrieved_at": timestamp,
        "first_seen": timestamp,
        "last_verified": timestamp,
    }


def build_record(issue: dict, dbms_id: str, *, rule: str, technique: Optional[str],
                 finder: str, excerpt: str, source_text: str, timestamp: str,
                 classifier_record: Optional[dict] = None,
                 confidence: str = "high") -> dict:
    """Assemble a bug record from a GitHub issue."""
    url = issue["html_url"]
    status, is_true_positive = STATE_TO_STATUS.get(
        issue.get("state", ""), ("unknown", False))
    # A closed issue is only a fixed bug if something closed it as completed.
    if issue.get("state") == "closed" and issue.get("state_reason") == "not_planned":
        status, is_true_positive = "closed_not_a_bug", False

    created = (issue.get("created_at") or "")[:10] or None
    from ..util import stable_digest
    from ..repos import canonical_issue_url
    # The address the project is at today, so a bug filed under a former
    # organisation is the same record as the one found under the current name.
    url = canonical_issue_url(url) or url
    record_id = f"bug:{dbms_id}:{stable_digest(url)}"

    evidence = [{
        "source_url": url,
        "source_type": ("github_pull_request" if issue.get("pull_request")
                        else "github_issue"),
        "excerpt": excerpt,
        "excerpt_is_verbatim": True,
        "note": _evidence_note(rule),
        "content_sha256": content_hash(source_text),
        "retrieved_at": timestamp,
        "first_seen": timestamp,
        "last_verified": timestamp,
    }]

    if rule == "campaign_reporter":
        evidence.append(_roster_evidence(issue, timestamp))

    attribution = {
        "rule": rule,
        "confidence": confidence,
        "evidence": evidence,
    }
    if classifier_record:
        attribution["classifier"] = classifier_record

    return {
        "id": record_id,
        "dbms": dbms_id,
        "title": truncate(issue.get("title") or "(untitled report)", 500),
        "reported_date": created,
        "reported_year": year_of(created),
        "status": status,
        "status_is_true_positive": is_true_positive,
        "finder": finder,
        "technique": technique,
        "symptom": "unknown",
        "reporter": (issue.get("user") or {}).get("login"),
        "links": {"report": url},
        "primary_url": normalize_url(url),
        "attribution": attribution,
        "provenance": {
            "collector": COLLECTOR,
            "collector_version": COLLECTOR_VERSION,
            "policy_version": config.policy_version(),
            "first_seen": timestamp,
            "last_verified": timestamp,
            "source_url": url,
            "source_type": "github_issue",
            "content_sha256": content_hash(source_text),
        },
    }


def member_queries(gh: GitHub, tax: taxonomy.Taxonomy) -> List[str]:
    """Author-scoped searches for the people who run SQLancer campaigns.

    Term search is capped at 1000 results and "SQLancer" comfortably exceeds
    that, so reports get lost. Searching by author does not hit the same wall.

    No search term is attached. Asking for a person's issues *that mention
    SQLancer* cannot reach a report whose text never names it, and most do not:
    a bug report is written about the bug, not about what found it. What comes
    back is filtered by the same rules as everything else -- the query only
    decides what gets looked at.
    """
    from . import people

    try:
        return people.search_queries(people.handles(gh))
    except Exception:
        return []


def collect(gh: GitHub, classifier: Optional[Classifier] = None, *,
            since: Optional[str] = None, timestamp: Optional[str] = None,
            full: bool = False, max_candidates: int = 25_000,
            include_members: bool = True,
            only_repos: Optional[Sequence[str]] = None
            ) -> Tuple[List[dict], List[dict], List[dict]]:
    """Return ``(records, rejected, needs_review)``.

    ``rejected`` holds candidates the evidence did not support; ``needs_review``
    holds candidates that would need a classifier run that could not happen.
    Both are reported in the pull request so a human can see what was left out.

    ``only_repos`` restricts the repository searches to the ``owner/name`` pairs
    given. A newly registered system needs its own repository searched and
    nothing else -- the global catch-all query cannot reach it, since "SQLancer"
    is far past GitHub's result cap -- and re-running every repository to pick
    up one is a great deal of work for a known answer.
    """
    timestamp = timestamp or now()
    tax = taxonomy.load()
    repo_ids = _repo_index()
    repos = sorted(_repo_index())
    if only_repos is not None:
        wanted = {name.lower() for name in only_repos}
        repos = [name for name in repos if name.lower() in wanted]
        unknown = wanted - {name.lower() for name in repo_ids}
        if unknown:
            raise ValueError(
                "not a registered database system's repository: "
                + ", ".join(sorted(unknown)))

    records: List[dict] = []
    rejected: List[dict] = []
    needs_review: List[dict] = []
    seen_urls = set()

    # Author searches run first. The candidate cap is a bound on total work,
    # and whichever queries run last are the ones it silences -- so the queries
    # that reach reports nothing else can find must not be the ones at the back.
    queries = [(query, True) for query in member_queries(gh, tax)] \
        if include_members else []
    queries += [(query, False) for query in search_queries(tax, repos)]

    for query, by_author in queries:
        for item in gh.search_issues(query, since=None if full else since,
                                     max_pages=MAX_SEARCH_PAGES,
                                     sort="updated"):
            url = item.get("html_url") or ""
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            if len(seen_urls) > max_candidates:
                break

            owner_repo = _owner_repo(item.get("repository_url") or url)
            dbms_id = repo_ids.get(owner_repo.lower()) if owner_repo else None
            if dbms_id is None:
                if by_author:
                    # An author search returns everything the person ever
                    # filed, most of it nothing to do with database systems.
                    # Reporting each one as needing review would bury the
                    # candidates that do.
                    continue
                # A bug in a system not in the registry cannot be filed against
                # a DBMS id; surface it rather than guessing one.
                needs_review.append({
                    "kind": "bug",
                    "url": url,
                    "reason": "repository is not a registered database system",
                    "title": item.get("title"),
                })
                continue

            source_text = _issue_source_text(item)
            deterministic = deterministic_attribution(
                source_text, tax, item,
                reporter_runs_campaigns=(
                    by_author and dbms_id not in OUTSIDE_SQLANCER_CAMPAIGNS))
            if deterministic is not None:
                rule, technique, finder, excerpt = deterministic
                if not verbatim_excerpt(source_text, excerpt):
                    continue
                records.append(build_record(
                    item, dbms_id, rule=rule, technique=technique,
                    finder=finder, excerpt=excerpt, source_text=source_text,
                    timestamp=timestamp,
                    confidence=_confidence_for(rule)))
                continue

            # Inside a registered database system's own repository, a bare
            # acronym needs no further corroboration to be worth a question:
            # the repository is the database-testing context. Requiring it here
            # as well is what turns the deterministic layer into a gatekeeper,
            # and it belongs upstream of the classifier, not in place of it.
            mentions = (tax.mentions_finder(source_text)
                        or tax.find_techniques(source_text, require_context=False)
                        or sqlancer_labels(item, tax))
            if not mentions:
                if not by_author:
                    rejected.append({
                        "kind": "bug", "url": url,
                        "reason": "no SQLancer tool or technique mentioned at all",
                    })
                continue
            if tax.find_excluded_techniques(source_text) and not tax.mentions_finder(
                    source_text):
                rejected.append({
                    "kind": "bug", "url": url,
                    "reason": "credits a technique that did not originate in SQLancer",
                })
                continue

            # Ask even when no key is present: the answer may already be in
            # the cache, and a cached answer needs no API call. Checking
            # availability here instead meant a warm cache was unusable
            # offline, which is the opposite of what the cache is for.
            result = None if classifier is None else classifier.classify(
                "bug_attribution",
                source_id=f"github:{owner_repo}#{item.get('number')}",
                source_text=source_text,
                source_label=f"GitHub issue {owner_repo}#{item.get('number')}",
                technique_names=technique_names(tax),
                finder_names=finder_names(tax),
                excluded_names=excluded_names(tax))

            if result is None:
                needs_review.append({
                    "kind": "bug", "url": url, "title": item.get("title"),
                    "reason": "needs semantic classification; classifier unavailable",
                })
                continue
            if not result.is_positive:
                rejected.append({
                    "kind": "bug", "url": url, "answer": result.answer,
                    "reason": result.reason or "classifier did not confirm attribution",
                })
                continue

            technique = resolve_technique(tax, result.extra.get("technique"))
            finder = resolve_finder(tax, result.extra.get("finder")) or "sqlancer"
            records.append(build_record(
                item, dbms_id,
                rule="technique_attribution" if technique else "explicit_tool_statement",
                technique=technique, finder=finder,
                excerpt=result.excerpts[0], source_text=source_text,
                timestamp=timestamp,
                classifier_record=result.classifier_record(),
                confidence="medium"))

    return records, rejected, needs_review


def _owner_repo(url: str) -> Optional[str]:
    match = re.search(r"repos/([\w.-]+/[\w.-]+)", url)
    if match:
        return match.group(1)
    match = re.search(r"github\.com/([\w.-]+/[\w.-]+)/", url)
    return match.group(1) if match else None
