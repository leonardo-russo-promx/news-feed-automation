from __future__ import annotations

import httpx
from bs4 import BeautifulSoup

from digest.collectors.base import get_with_retry

RSS_TYPES = {
    "application/rss+xml",
    "application/atom+xml",
    "application/rdf+xml",
}


async def discover_feed(website_url: str, client: httpx.AsyncClient) -> str | None:
    response = await get_with_retry(client, website_url)
    soup = BeautifulSoup(response.text, "html.parser")
    for link in soup.find_all("link", href=True):
        rel = link.get("rel", [])
        rel_values = [rel] if isinstance(rel, str) else rel
        media_type = str(link.get("type", "")).split(";", 1)[0].strip().lower()
        if "alternate" in rel_values and media_type in RSS_TYPES:
            return str(response.url.join(str(link["href"])))
    return None
