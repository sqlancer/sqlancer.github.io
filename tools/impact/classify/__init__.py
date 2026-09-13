"""The semantic classifier: a cached, verified wrapper around one API call.

Three properties matter more than anything else here, and each is enforced
rather than trusted:

1. **Unchanged evidence is never reclassified.** The cache key covers the source
   id, the hash of the exact text sent, the prompt version, the policy version,
   the taxonomy version and the model. Negative and uncertain answers are cached
   just like positive ones, so a paper that turned out to only cite SQLancer
   costs nothing on subsequent runs.
2. **The model cannot invent evidence.** Every excerpt it returns is checked
   against the source text it was given. An excerpt that is not literally there
   is dropped, and a "yes" that loses all its excerpts is downgraded to
   ``uncertain`` -- a positive claim without evidence is not published.
3. **Absence of an API key is not a failure mode.** A cached answer is returned
   whatever the key situation: the cache is consulted before availability is,
   so a question answered once stays answered offline. Only a question with no
   cached answer returns ``None``, and the caller then records the candidate as
   needing review. The pipeline still runs, and the dataset grows by whatever
   the cache can already answer.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from .. import config, taxonomy
from ..cache import ClassificationCache
from ..util import content_hash, now, truncate, verbatim_excerpt
from . import prompts

# Guard against sending an entire paper or repository to the model. Sources are
# assembled from targeted excerpts upstream, so this is a backstop.
MAX_SOURCE_CHARS = 60_000
MAX_EXCERPT_CHARS = 2_000


class ClassificationResult:
    __slots__ = ("answer", "excerpts", "reason", "extra", "from_cache",
                 "classifier_version", "evidence_hash", "model")

    def __init__(self, answer: str, excerpts: List[str], reason: str,
                 extra: Dict[str, Any], *, from_cache: bool,
                 classifier_version: str, evidence_hash: str, model: str):
        self.answer = answer
        self.excerpts = excerpts
        self.reason = reason
        self.extra = extra
        self.from_cache = from_cache
        self.classifier_version = classifier_version
        self.evidence_hash = evidence_hash
        self.model = model

    @property
    def is_positive(self) -> bool:
        return self.answer == "yes"

    def classifier_record(self, *, method: str = "llm_classification") -> dict:
        """The audit trail stored alongside an accepted claim."""
        return {
            "method": method,
            "classifier_version": self.classifier_version,
            "policy_version": config.policy_version(),
            "taxonomy_version": config.taxonomy_version(),
            "evidence_sha256": self.evidence_hash,
            "classified_at": now(),
            "model": self.model,
            "rationale": truncate(self.reason, 1000) if self.reason else None,
        }

    def __repr__(self):
        return (f"ClassificationResult({self.answer!r}, "
                f"{len(self.excerpts)} excerpt(s), "
                f"cached={self.from_cache})")


class Classifier:
    """Answers the bounded questions in :mod:`prompts`, with caching."""

    def __init__(self, cache: ClassificationCache, *, model: Optional[str] = None,
                 api_key: Optional[str] = None, dry_run: bool = False):
        self.cache = cache
        self.model = model or config.DEFAULT_MODEL
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.dry_run = dry_run
        self._client = None
        self.calls_made = 0
        self.cache_hits = 0
        self.skipped_no_key = 0

    @property
    def available(self) -> bool:
        """Whether a live classification is possible at all."""
        if self.dry_run or not self.api_key:
            return False
        if self._client is not None:
            return True
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    # -- public API -------------------------------------------------------

    def classify(self, question: str, *, source_id: str, source_text: str,
                 **kwargs) -> Optional[ClassificationResult]:
        """Answer ``question`` about ``source_text``.

        Returns ``None`` only when no answer could be obtained -- no API key, no
        SDK, or an API failure. ``None`` is not a negative answer: the caller
        must treat it as "not yet classified" and surface the candidate for
        human review.
        """
        builder = prompts.BUILDERS.get(question)
        if builder is None:
            raise KeyError(f"unknown classifier question {question!r}")

        source_text = truncate(source_text or "", MAX_SOURCE_CHARS)
        version = prompts.VERSIONS[question]
        evidence_hash = content_hash(f"{question}\n{source_text}")

        cached = self.cache.lookup(source_id, evidence_hash, version)
        if cached is not None:
            self.cache_hits += 1
            payload = cached["result"]
            return ClassificationResult(
                payload["answer"], payload.get("excerpts", []),
                payload.get("reason", ""), payload.get("extra", {}),
                from_cache=True, classifier_version=version,
                evidence_hash=evidence_hash, model=cached.get("model", self.model))

        if not self.available:
            self.skipped_no_key += 1
            return None

        request = builder(source_text=source_text, **kwargs)
        raw = self._ask(request)
        if raw is None:
            return None

        payload = self._sanitise(raw, source_text)
        self.cache.store(source_id, evidence_hash, version, payload)
        self.calls_made += 1
        return ClassificationResult(
            payload["answer"], payload["excerpts"], payload["reason"],
            payload["extra"], from_cache=False, classifier_version=version,
            evidence_hash=evidence_hash, model=self.model)

    # -- internals --------------------------------------------------------

    def _ask(self, request: dict) -> Optional[dict]:
        try:
            response = self._get_client().messages.create(
                model=self.model,
                max_tokens=2000,
                system=request["system"],
                messages=[{"role": "user", "content": request["user"]}],
                output_config={"format": request["output_format"]},
            )
        except Exception:
            # A failed call is not a negative answer; leave the candidate
            # unclassified so the next run retries it rather than recording
            # a decision that was never made.
            return None
        if getattr(response, "stop_reason", None) == "refusal":
            return None
        for block in response.content:
            if getattr(block, "type", None) == "text":
                try:
                    return json.loads(block.text)
                except ValueError:
                    return None
        return None

    def _sanitise(self, raw: dict, source_text: str) -> dict:
        """Enforce the no-fabrication rule on the model's output.

        Excerpts that are not literally present in the source are discarded. A
        "yes" whose excerpts all fail that check is downgraded to "uncertain",
        because a positive claim without quotable evidence is exactly what this
        dataset must not contain.
        """
        answer = raw.get("answer")
        if answer not in prompts.ANSWER_ENUM:
            answer = "uncertain"

        verified: List[str] = []
        for excerpt in raw.get("excerpts") or []:
            if not isinstance(excerpt, str):
                continue
            excerpt = excerpt.strip()
            if not excerpt or len(excerpt) > MAX_EXCERPT_CHARS:
                continue
            if verbatim_excerpt(source_text, excerpt) and excerpt not in verified:
                verified.append(excerpt)

        if answer == "yes" and not verified:
            answer = "uncertain"

        extra = {key: value for key, value in raw.items()
                 if key not in ("answer", "excerpts", "reason")}

        return {
            "answer": answer,
            "excerpts": verified,
            "reason": truncate(str(raw.get("reason") or ""), 1000),
            "extra": extra,
            "unverified_excerpt_count": len(raw.get("excerpts") or []) - len(verified),
        }

    def stats(self) -> Dict[str, int]:
        return {
            "api_calls": self.calls_made,
            "cache_hits": self.cache_hits,
            "skipped_no_key": self.skipped_no_key,
        }


def technique_names(tax: Optional[taxonomy.Taxonomy] = None) -> List[str]:
    tax = tax or taxonomy.load()
    return [entry["display_name"] for entry in tax.techniques.values()]


def finder_names(tax: Optional[taxonomy.Taxonomy] = None) -> List[str]:
    tax = tax or taxonomy.load()
    return [entry["display_name"] for entry in tax.finders.values()
            if entry["umbrella_member"]]


def excluded_names(tax: Optional[taxonomy.Taxonomy] = None) -> List[str]:
    tax = tax or taxonomy.load()
    return [entry.get("display_name") or entry["id"]
            for entry in tax.excluded.values()]


def resolve_technique(tax: taxonomy.Taxonomy, name: Optional[str]) -> Optional[str]:
    """Map a technique name the model returned back onto a taxonomy id.

    Anything that does not resolve is dropped rather than invented: the schema
    only accepts ids that exist in ``techniques.json``.
    """
    if not name:
        return None
    matches = tax.find_techniques(name, require_context=False)
    return matches[0].technique_id if matches else None


def resolve_finder(tax: taxonomy.Taxonomy, name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    found = tax.mentions_finder(name)
    return found[0] if found else None
