from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

from digest.collectors.base import build_item, get_with_retry
from digest.models import Item, Source


class PageDiffCollector:
    def __init__(self) -> None:
        self.warning: str | None = None
        self.etag: str | None = None
        self.last_modified: str | None = None
        self.not_modified = False

    async def collect(
        self,
        source: Source,
        client: httpx.AsyncClient,
        snapshots: dict[str, Any],
        validators: dict[str, str] | None = None,
    ) -> list[Item]:
        if not source.website_url:
            return []
        response = await get_with_retry(client, str(source.website_url), validators)
        self.etag = response.headers.get("ETag")
        self.last_modified = response.headers.get("Last-Modified")
        if response.status_code == 304:
            self.not_modified = True
            return []
        soup = BeautifulSoup(response.text, "html.parser")
        selector = source.page.selector
        selected = soup.select_one(selector)
        if selected is None:
            raise ValueError(f"selector {selector!r} did not match the page")
        text = re.sub(r"\s+", " ", selected.get_text(" ", strip=True)).strip()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        previous = snapshots.get(source.id)
        snapshots[source.id] = {"hash": digest, "text": text}
        if not previous:
            return []
        previous_text = str(previous.get("text", ""))
        if previous.get("hash") == digest:
            return []
        old_blocks = set(re.split(r"(?<=[.!?])\s+", previous_text))
        new_blocks = [
            block for block in re.split(r"(?<=[.!?])\s+", text) if block not in old_blocks
        ]
        extracted = [block.strip() for block in new_blocks if len(block.strip()) >= 40]
        if not extracted:
            self.warning = "Page content changed, but no dated update blocks could be extracted."
            return []
        now = datetime.now(UTC)
        item = build_item(
            source,
            title=f"Page update: {source.title}",
            url=str(source.website_url),
            published_at=None,
            summary=" ".join(extracted)[:500],
            content="\n\n".join(extracted),
            kind="page_change",
        )
        item.updated_at = now
        return [item]
