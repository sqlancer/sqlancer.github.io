"""Thin GitHub REST client on top of the caching fetcher.

Only the handful of endpoints the collectors need. Two things matter here
beyond plain requests: issue and PR metadata carry an ``updated_at`` that lets
the raw cache skip a request entirely for unchanged threads, and the search
endpoints support ``created:``/``updated:`` qualifiers that make weekly scans
incremental instead of full rescans.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .cache import RawCache
from .http import Fetcher, with_params

API = "https://api.github.com"

# GitHub's search API is capped at 1000 results per query and rate limited far
# more tightly than the core API, so searches are kept narrow and paged.
SEARCH_PAGE_SIZE = 100
SEARCH_MAX_PAGES = 10


class BadCredentials(RuntimeError):
    """The token GitHub was given is not a valid one."""


def _token() -> Optional[str]:
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "IMPACT_GITHUB_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value
    return None


class GitHub:
    def __init__(self, cache: RawCache, *, token: Optional[str] = None):
        self.token = token or _token()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        # GitHub rate limits search far more tightly than the rest of the API:
        # 30 searches per minute authenticated, against 5000 core requests per
        # hour. A single interval for both would make reading a few hundred
        # issue bodies take an hour for no reason, so the two get their own
        # budgets.
        self.fetcher = Fetcher(cache, min_interval=0.8 if self.token else 3.0,
                               headers=headers)
        self.search_fetcher = Fetcher(
            cache, min_interval=2.5 if self.token else 6.0, headers=headers)
        self.authenticated = bool(self.token)
        # Searches that came back non-200. A run that silently found nothing
        # and a run that genuinely found nothing look identical otherwise.
        self.failed_searches: List[Tuple[str, str, Optional[int]]] = []

    # -- primitive --------------------------------------------------------

    def get(self, path: str, *, params: Optional[Dict[str, Any]] = None,
            cache_key: Optional[str] = None, max_age_days: Optional[int] = None,
            upstream_updated_at: Optional[str] = None, force: bool = False):
        url = API + path if path.startswith("/") else path
        if params:
            url = with_params(url, params)
        data, from_cache = self.fetcher.fetch_json(
            url, cache_key=cache_key or url, max_age_days=max_age_days,
            upstream_updated_at=upstream_updated_at, force=force)
        return data, from_cache

    # -- repository content ----------------------------------------------

    def tree(self, owner: str, repo: str, ref: str = "HEAD",
             *, max_age_days: int = 7) -> List[dict]:
        data, _ = self.get(f"/repos/{owner}/{repo}/git/trees/{ref}",
                           params={"recursive": "1"}, max_age_days=max_age_days)
        return (data or {}).get("tree", [])

    def raw_file(self, owner: str, repo: str, path: str, ref: str = "HEAD",
                 *, max_age_days: int = 30):
        """Fetch a file's text from raw.githubusercontent.com.

        The raw host is used rather than the contents API because it returns the
        bytes directly, which keeps the stored evidence excerpt and its hash
        aligned with what a reviewer sees when following the URL.
        """
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
        entry, _ = self.fetcher.fetch(url, max_age_days=max_age_days)
        if entry.get("status") != 200:
            return None, entry
        return entry["body"], entry

    def repo(self, owner: str, name: str, *, max_age_days: int = 7):
        data, _ = self.get(f"/repos/{owner}/{name}", max_age_days=max_age_days)
        return data

    def issue(self, owner: str, repo: str, number: int, *,
              upstream_updated_at: Optional[str] = None,
              max_age_days: Optional[int] = None):
        data, from_cache = self.get(
            f"/repos/{owner}/{repo}/issues/{number}",
            upstream_updated_at=upstream_updated_at, max_age_days=max_age_days)
        return data, from_cache

    def issue_comments(self, owner: str, repo: str, number: int, *,
                       upstream_updated_at: Optional[str] = None,
                       max_age_days: Optional[int] = None) -> List[dict]:
        data, _ = self.get(f"/repos/{owner}/{repo}/issues/{number}/comments",
                           params={"per_page": 100},
                           upstream_updated_at=upstream_updated_at,
                           max_age_days=max_age_days)
        return data or []

    # -- search -----------------------------------------------------------

    def search(self, kind: str, query: str, *, sort: Optional[str] = None,
               order: str = "desc", max_pages: int = SEARCH_MAX_PAGES,
               max_age_days: Optional[int] = 7,
               accept: Optional[str] = None) -> Iterator[dict]:
        """Page through ``/search/<kind>``.

        ``kind`` is one of ``issues``, ``repositories`` or ``code``. Code search
        needs a token and a preview Accept header for text matches, which are
        what make a hit usable as evidence.
        """
        for page in range(1, max_pages + 1):
            params = {"q": query, "per_page": SEARCH_PAGE_SIZE, "page": page,
                      "order": order}
            if sort:
                params["sort"] = sort
            url = with_params(f"{API}/search/{kind}", params)
            extra = {"Accept": accept} if accept else None
            try:
                entry, _ = self.search_fetcher.fetch(
                    url, max_age_days=max_age_days, extra_headers=extra)
            except RuntimeError:
                # A failing search must not abort the whole run; the periodic
                # reconciliation will pick up anything missed.
                return
            if entry.get("status") == 401:
                # Not transient, and not something reconciliation fixes: every
                # search in the run is broken the same way. Returning quietly
                # here made an expired token look like a world with no bugs --
                # the run reported "0 new" and nothing said why.
                raise BadCredentials(
                    "GitHub rejected the token (401). Set a valid GITHUB_TOKEN "
                    "or unset it to search unauthenticated.")
            if entry.get("status") != 200:
                self.failed_searches.append((kind, query, entry.get("status")))
                return
            import json
            try:
                payload = json.loads(entry["body"])
            except ValueError:
                return
            items = payload.get("items", [])
            for item in items:
                yield item
            if len(items) < SEARCH_PAGE_SIZE:
                return
            if page * SEARCH_PAGE_SIZE >= min(payload.get("total_count", 0), 1000):
                return

    def search_issues(self, query: str, *, since: Optional[str] = None, **kwargs):
        """Issue search, optionally restricted to threads updated since a date."""
        if since:
            query = f"{query} updated:>={since[:10]}"
        return self.search("issues", query, **kwargs)

    def search_code(self, query: str, **kwargs):
        return self.search(
            "code", query,
            accept="application/vnd.github.text-match+json", **kwargs)

    def rate_limit(self) -> dict:
        data, _ = self.get("/rate_limit", max_age_days=None, force=True)
        return data or {}
