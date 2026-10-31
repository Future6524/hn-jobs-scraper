"""Async clients for the official HN Firebase API and the Algolia HN Search API."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from typing import Any

import httpx

HN_API = "https://hacker-news.firebaseio.com/v0"
ALGOLIA_API = "https://hn.algolia.com/api/v1"
THREAD_AUTHOR = "whoishiring"
THREAD_TITLE_PREFIX = "ask hn: who is hiring?"

DEFAULT_CONCURRENCY = 20
DEFAULT_RETRIES = 4
USER_AGENT = "hn-jobs-scraper/1.0"


class HNClient:
    """Thin async wrapper with retries, backoff and a shared concurrency limit."""

    def __init__(
        self,
        concurrency: int = DEFAULT_CONCURRENCY,
        retries: int = DEFAULT_RETRIES,
        timeout: float = 15.0,
    ) -> None:
        self._semaphore = asyncio.Semaphore(concurrency)
        self._retries = retries
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
            follow_redirects=True,
        )

    async def __aenter__(self) -> HNClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """GET a URL and decode JSON, retrying transient failures with exponential backoff."""
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                async with self._semaphore:
                    response = await self._client.get(url, params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"retryable status {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500 and exc.response.status_code != 429:
                    raise
                last_error = exc
                if attempt < self._retries:
                    await asyncio.sleep(0.5 * 2**attempt + random.uniform(0, 0.25))
        raise RuntimeError(f"GET {url} failed after {self._retries + 1} attempts") from last_error

    async def find_latest_hiring_thread(self) -> dict[str, Any]:
        """Use Algolia to find the newest "Who is hiring?" story by user whoishiring."""
        data = await self.get_json(
            f"{ALGOLIA_API}/search_by_date",
            params={
                "tags": f"story,author_{THREAD_AUTHOR}",
                "query": "Ask HN: Who is hiring?",
                "hitsPerPage": 30,
            },
        )
        hits = [
            hit
            for hit in data.get("hits", [])
            if (hit.get("title") or "").lower().startswith(THREAD_TITLE_PREFIX)
        ]
        if not hits:
            raise RuntimeError("No 'Ask HN: Who is hiring?' thread found via Algolia")
        newest = max(hits, key=lambda h: h.get("created_at_i", 0))
        return {
            "id": int(newest["objectID"]),
            "title": newest["title"],
            "created_at": newest.get("created_at"),
        }

    async def get_item(self, item_id: int) -> dict[str, Any] | None:
        return await self.get_json(f"{HN_API}/item/{item_id}.json")

    async def get_items(
        self,
        item_ids: list[int],
        on_done: Callable[[], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch many items concurrently; failed or missing items are skipped."""

        async def fetch(item_id: int) -> dict[str, Any] | None:
            try:
                return await self.get_item(item_id)
            except Exception:
                return None
            finally:
                if on_done:
                    on_done()

        results = await asyncio.gather(*(fetch(i) for i in item_ids))
        return [item for item in results if item]
