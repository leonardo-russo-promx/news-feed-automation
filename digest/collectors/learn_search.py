from __future__ import annotations

from urllib.parse import urlencode

import httpx

from digest.collectors.base import get_with_retry
from digest.collectors.rss import parse_content
from digest.models import Item, Source


class LearnSearchCollector:
    def __init__(self) -> None:
        self.etag: str | None = None
        self.last_modified: str | None = None
        self.warning: str | None = None

    async def collect(
        self,
        source: Source,
        client: httpx.AsyncClient,
        validators: dict[str, str] | None = None,
    ) -> list[Item]:
        if not source.learn_scope or source.learn_scope.startswith("TODO"):
            return []
        query = urlencode(
            {
                "search": source.learn_search_terms or source.title,
                "locale": "en-us",
                "$filter": f"scopes/any(t: t eq '{source.learn_scope}')",
            }
        )
        url = f"https://learn.microsoft.com/api/search/rss?{query}"
        response = await get_with_retry(client, url, validators)
        self.etag = response.headers.get("ETag")
        self.last_modified = response.headers.get("Last-Modified")
        if response.status_code == 304:
            return []
        warnings: list[str] = []
        items = parse_content(source, response.content, warnings)
        if warnings:
            self.warning = (
                f"Skipped {len(warnings)} Learn search entries without a usable date or URL."
            )
        return items
