from __future__ import annotations

import asyncio

import httpx
from bs4 import BeautifulSoup

from digest.collectors.base import USER_AGENT
from digest.models import Item


async def enrich_content(items: list[Item], client: httpx.AsyncClient) -> None:
    semaphore = asyncio.Semaphore(8)

    async def fetch(item: Item) -> None:
        if not item.url.startswith(("http://", "https://")):
            return
        async with semaphore:
            try:
                response = await client.get(item.url, headers={"User-Agent": USER_AGENT})
                response.raise_for_status()
            except httpx.HTTPError:
                return
            soup = BeautifulSoup(response.text, "html.parser")
            for node in soup.select("script, style, nav, header, footer, form, noscript"):
                node.decompose()
            main = soup.select_one("article") or soup.select_one("main") or soup.body
            if main is not None:
                extracted = " ".join(main.get_text(" ", strip=True).split())[:6000]
                if extracted:
                    item.content_text = extracted

    await asyncio.gather(*(fetch(item) for item in items))
