"""HTTP access with conditional requests, backoff and a hard offline mode.

Everything the pipeline fetches goes through here so that three properties hold
uniformly: unchanged sources are revalidated rather than re-downloaded, rate
limits are respected with backoff, and the whole pipeline can be run with
``IMPACT_OFFLINE=1`` against nothing but the cache, which is what the tests do.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from . import config
from .cache import RawCache
from .util import now

OFFLINE = os.environ.get("IMPACT_OFFLINE", "") not in ("", "0", "false")


class OfflineError(RuntimeError):
    """Raised when a fetch is required but the pipeline is running offline."""


class Fetcher:
    """Cache-aware HTTP client.

    ``cache`` is the raw-source layer for this kind of source; passing separate
    caches for GitHub, papers and generic HTTP keeps the cache directories
    meaningful when a human inspects them.
    """

    def __init__(self, cache: RawCache, *, min_interval: float = 0.0,
                 headers: Optional[Dict[str, str]] = None,
                 max_retries: int = 5, initial_backoff: float = 2.0,
                 max_backoff: float = 60.0, timeout: float = 30.0):
        self.cache = cache
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff
        # Bounded on purpose: several trackers linked from historic bug lists
        # no longer resolve, and a generous socket timeout multiplied by the
        # retry count is what turns a handful of dead links into a stalled run.
        self.timeout = timeout
        self.headers = {"User-Agent": config.USER_AGENT, "Accept": "*/*"}
        if headers:
            self.headers.update(headers)
        self._last_request = 0.0
        self.requests_made = 0

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request = time.monotonic()

    def fetch(self, url: str, *, cache_key: Optional[str] = None,
              upstream_updated_at: Optional[str] = None,
              max_age_days: Optional[int] = None,
              force: bool = False,
              extra_headers: Optional[Dict[str, str]] = None
              ) -> Tuple[Dict[str, Any], bool]:
        """Return ``(entry, from_cache)`` for ``url``.

        When the caller knows an upstream modification timestamp, an unchanged
        timestamp short-circuits the request entirely. Otherwise a cached ETag
        or Last-Modified is replayed as a conditional request, so a ``304`` also
        avoids re-downloading the body.
        """
        key = cache_key or url
        cached = self.cache.get(key)

        if not force and cached is not None:
            if self.cache.is_fresh(key, upstream_updated_at=upstream_updated_at,
                                   max_age_days=max_age_days):
                return cached["value"], True
        if OFFLINE:
            if cached is not None:
                return cached["value"], True
            raise OfflineError(f"offline and no cached copy of {url}")

        headers = dict(self.headers)
        if extra_headers:
            headers.update(extra_headers)
        if cached is not None and not force:
            if cached["value"].get("etag"):
                headers["If-None-Match"] = cached["value"]["etag"]
            if cached["value"].get("last_modified"):
                headers["If-Modified-Since"] = cached["value"]["last_modified"]

        body, status, response_headers = self._request(url, headers)
        if status == 304 and cached is not None:
            value = dict(cached["value"])
            value["fetched_at"] = now()
            if upstream_updated_at is not None:
                value["upstream_updated_at"] = upstream_updated_at
            self.cache.put(key, value)
            return value, True

        value = self.cache.store(
            key,
            body=body,
            url=url,
            status=status,
            etag=response_headers.get("ETag"),
            last_modified=response_headers.get("Last-Modified"),
            upstream_updated_at=upstream_updated_at,
        )
        return value, False

    def fetch_binary(self, url: str, *, cache_key: Optional[str] = None,
                     max_age_days: Optional[int] = None
                     ) -> Tuple[Optional[bytes], bool]:
        """Fetch a binary resource, such as a PDF.

        Kept separate from `fetch` because that decodes the response as text,
        which silently destroys anything that is not text -- a PDF fetched
        through it comes back as replacement characters. Bodies are cached
        base64-encoded so the cache stays plain JSON.
        """
        import base64

        key = cache_key or f"binary:{url}"
        cached = self.cache.get(key)
        if cached is not None and self.cache.is_fresh(key, max_age_days=max_age_days):
            payload = cached["value"].get("body_base64")
            if payload is None:
                return None, True
            return base64.b64decode(payload), True
        if OFFLINE:
            if cached is not None:
                payload = cached["value"].get("body_base64")
                return (base64.b64decode(payload) if payload else None), True
            raise OfflineError(f"offline and no cached copy of {url}")

        delay = self.initial_backoff
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            self._throttle()
            request = urllib.request.Request(url, headers=dict(self.headers))
            try:
                self.requests_made += 1
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                self.cache.put(key, {
                    "url": url,
                    "status": 200,
                    "body_base64": base64.b64encode(raw).decode("ascii"),
                    "content_sha256": "sha256:" + __import__("hashlib")
                                      .sha256(raw).hexdigest(),
                    "fetched_at": now(),
                })
                return raw, False
            except urllib.error.HTTPError as error:
                if error.code in (404, 403):
                    self.cache.put(key, {"url": url, "status": error.code,
                                         "body_base64": None,
                                         "fetched_at": now()})
                    return None, False
                last_error = error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = error
            if attempt < self.max_retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, self.max_backoff)
        raise RuntimeError(f"failed to fetch {url}: {last_error}")

    def fetch_json(self, url: str, **kwargs) -> Tuple[Any, bool]:
        value, from_cache = self.fetch(url, **kwargs)
        if value.get("status") not in (200, 304):
            return None, from_cache
        try:
            return json.loads(value["body"]), from_cache
        except ValueError:
            return None, from_cache

    def _request(self, url: str, headers: Dict[str, str]):
        delay = self.initial_backoff
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            self._throttle()
            request = urllib.request.Request(url, headers=headers)
            try:
                self.requests_made += 1
                with urllib.request.urlopen(request,
                                            timeout=self.timeout) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    body = response.read().decode(charset, errors="replace")
                    return body, response.status, dict(response.headers)
            except urllib.error.HTTPError as error:
                headers = dict(error.headers or {})
                if error.code == 304:
                    return "", 304, headers
                if error.code == 404:
                    return "", 404, headers
                if error.code == 401:
                    # Rejected credentials. Returned rather than raised so the
                    # caller sees the status and can say what is wrong: raising
                    # here reached a `except RuntimeError: return` that made an
                    # expired token indistinguishable from an empty result.
                    return "", 401, headers
                if error.code in (429, 500, 502, 503, 504) or (
                        error.code == 403 and _is_rate_limited(headers)):
                    # Rate limiting and transient upstream failures: back off.
                    retry_after = headers.get("Retry-After")
                    wait = (float(retry_after)
                            if retry_after and retry_after.isdigit() else delay)
                    last_error = error
                    if attempt < self.max_retries - 1:
                        time.sleep(min(wait, self.max_backoff))
                        delay = min(delay * 2, self.max_backoff)
                        continue
                if error.code == 403:
                    # A plain 403 is a site that will not serve us, not a
                    # request to slow down. Retrying it just burns the run's
                    # budget, so it is reported like any other dead source.
                    return "", 403, headers
                last_error = error
                break
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = error
                if attempt < self.max_retries - 1:
                    time.sleep(delay)
                    delay = min(delay * 2, self.max_backoff)
                    continue
                break
        raise RuntimeError(f"failed to fetch {url}: {last_error}")


def _is_rate_limited(headers: Dict[str, str]) -> bool:
    """Whether a 403 is a throttle rather than a refusal.

    GitHub answers a rate limit with 403 plus a retry hint or an exhausted
    remaining count; a site that simply blocks the client sends neither.
    """
    if headers.get("Retry-After"):
        return True
    remaining = headers.get("X-RateLimit-Remaining") or headers.get(
        "x-ratelimit-remaining")
    return remaining is not None and remaining.strip() == "0"


def with_params(url: str, params: Dict[str, Any]) -> str:
    clean = {k: v for k, v in params.items() if v is not None}
    return url + ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
