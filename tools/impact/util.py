"""Small shared helpers: hashing, stable ids, timestamps, URL normalisation."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Iterable, Optional
from urllib.parse import urlsplit, urlunsplit


def now() -> str:
    """Current UTC time in the timestamp format the schemas require."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    """Hash in the ``sha256:<hex>`` form used throughout the schemas."""
    return "sha256:" + sha256_hex(text)


def stable_digest(*parts: object, length: int = 12) -> str:
    """Short digest over a tuple of values, used to build stable record ids."""
    joined = " ".join("" if p is None else str(p) for p in parts)
    return sha256_hex(joined)[:length]


def slugify(value: str) -> str:
    """Lowercase identifier slug matching the ``slug`` schema definition."""
    normalised = unicodedata.normalize("NFKD", value)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_only.lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if not slug:
        slug = "unknown"
    if not slug[0].isalnum():
        slug = "x" + slug
    return slug[:64]


_TRACKING_PARAMS = re.compile(r"^(utm_|ref$|ref_|fbclid$|gclid$)")


def normalize_url(url: Optional[str]) -> Optional[str]:
    """Canonicalise a URL so the same source deduplicates to one record.

    Lowercases the host, drops a default port, strips tracking parameters and
    fragments, and removes a trailing slash. Deliberately conservative: it never
    touches the path's case, because many paths are case sensitive.
    """
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    host = parts.netloc.lower()
    if parts.scheme == "http" and host.endswith(":80"):
        host = host[:-3]
    if parts.scheme == "https" and host.endswith(":443"):
        host = host[:-4]
    if host.startswith("www."):
        host = host[4:]
    query = "&".join(
        piece for piece in parts.query.split("&")
        if piece and not _TRACKING_PARAMS.match(piece)
    )
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit(("https", host, path, query, ""))


_GITHUB_ISSUE = re.compile(
    r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/(issues|pull)/(\d+)",
    re.IGNORECASE,
)


def github_issue_ref(url: Optional[str]):
    """Return ``(owner, repo, kind, number)`` for a GitHub issue or PR URL."""
    if not url:
        return None
    match = _GITHUB_ISSUE.match(url)
    if not match:
        return None
    owner, repo, kind, number = match.groups()
    return owner, repo, "pull_request" if kind == "pull" else "issue", int(number)


def normalize_doi(doi: Optional[str]) -> Optional[str]:
    """Strip resolver prefixes and lowercase a DOI for use as a stable key."""
    if not doi:
        return None
    doi = doi.strip()
    for prefix in ("https://doi.org/", "http://doi.org/",
                   "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
            break
    doi = doi.strip().lower()
    return doi if doi.startswith("10.") else None


_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%y")


# A slash date whose order has to be worked out from the numbers themselves.
_SLASH_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def parse_date(value: Optional[str]) -> Optional[str]:
    """Parse the date spellings seen in upstream sources into ISO YYYY-MM-DD.

    Slash dates carry no indication of their order, and sources are not even
    internally consistent: the TEST lab's bug list has 735 rows that can only
    be day-first and 147 that can only be month-first. Where one component
    exceeds twelve the order is settled by that, which is what recovers the
    month-first rows -- they used to fail every format and come back as no date
    at all. Where both components could be a month nothing in the value can
    settle it, and the day-first reading is kept, as before.
    """
    if not value:
        return None
    value = value.strip()
    if not value or value.lower() in ("unknown", "n/a", "none"):
        return None
    if "T" in value:
        value = value.split("T", 1)[0]
    slash = _SLASH_DATE.match(value)
    if slash and int(slash.group(1)) <= 12 < int(slash.group(2)):
        month, day, year = slash.groups()
        try:
            return datetime(int(year), int(month), int(day)).strftime("%Y-%m-%d")
        except ValueError:
            return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def year_of(iso_date: Optional[str]) -> Optional[int]:
    if not iso_date:
        return None
    try:
        return int(iso_date[:4])
    except (TypeError, ValueError):
        return None


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def verbatim_excerpt(source_text: str, excerpt: Optional[str]) -> bool:
    """Check that an excerpt really is a literal substring of the source.

    The no-fabrication rule is enforced here rather than trusted: anything that
    is not found verbatim in the fetched text is rejected before it is stored.
    Only whitespace runs are normalised, since extraction reflows line breaks.
    """
    if excerpt is None:
        return True
    if not source_text:
        return False
    return _collapse(excerpt) in _collapse(source_text)


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def dedupe_preserving_order(items: Iterable[str]):
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
