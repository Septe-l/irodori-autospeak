"""SearXNG search plus article-body extraction.

SearXNG snippets alone are too thin to talk from for several minutes, so the top
few hits are downloaded and run through trafilatura to get the actual prose.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx
import trafilatura

from .config import Settings

logger = logging.getLogger(__name__)

# Sites that reliably yield nothing useful or block automated fetches.
SKIP_HOSTS = ("youtube.com", "youtu.be", "twitter.com", "x.com", "instagram.com", "tiktok.com")


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str
    body: str = ""

    def as_context(self, char_cap: int) -> str:
        text = self.body or self.snippet
        return f"# {self.title}\n{text[:char_cap]}"


@dataclass
class SearchResult:
    query: str
    hits: list[SearchHit] = field(default_factory=list)

    def context(self, char_cap: int) -> str:
        return "\n\n".join(h.as_context(char_cap) for h in self.hits if h.body or h.snippet)

    def sources(self) -> list[dict[str, str]]:
        return [{"title": h.title, "url": h.url} for h in self.hits]


class Searcher:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    async def search(self, query: str) -> SearchResult:
        hits = await self._query_searxng(query)
        if not hits:
            return SearchResult(query=query, hits=[])

        fetchable = [h for h in hits if not any(d in h.url for d in SKIP_HOSTS)]
        targets = fetchable[: self.s.search_fetch_pages]
        async with httpx.AsyncClient(
            timeout=self.s.search_timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
        ) as client:
            await asyncio.gather(*(self._fill_body(client, h) for h in targets))

        # Keep pages we actually extracted, then pad with snippet-only hits.
        with_body = [h for h in hits if h.body]
        without = [h for h in hits if not h.body]
        return SearchResult(query=query, hits=(with_body + without)[: self.s.search_results])

    async def _query_searxng(self, query: str) -> list[SearchHit]:
        params = {
            "q": query,
            "format": "json",
            "language": self.s.search_language,
            "safesearch": "0",
        }
        try:
            async with httpx.AsyncClient(timeout=self.s.search_timeout) as client:
                resp = await client.get(f"{self.s.searxng_url}/search", params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("searxng query failed for %r: %s: %s", query, type(e).__name__, e)
            return []

        hits: list[SearchHit] = []
        seen: set[str] = set()
        for r in data.get("results", []):
            url = r.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            hits.append(
                SearchHit(
                    title=(r.get("title") or "").strip(),
                    url=url,
                    snippet=(r.get("content") or "").strip(),
                )
            )
            if len(hits) >= self.s.search_results * 2:
                break
        return hits

    async def _fill_body(self, client: httpx.AsyncClient, hit: SearchHit) -> None:
        try:
            resp = await client.get(hit.url)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype and "xml" not in ctype:
                return
            extracted = trafilatura.extract(
                resp.text,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )
            if extracted:
                hit.body = " ".join(extracted.split())
        except Exception as e:  # noqa: BLE001
            logger.info("page fetch failed %s: %s: %s", hit.url, type(e).__name__, e)
