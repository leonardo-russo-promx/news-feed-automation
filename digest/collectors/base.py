from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from digest.models import Item, Source

USER_AGENT = "TechnologyDigestBot/1.0 (+https://github.com)"


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.25, min=0.25, max=2),
    reraise=True,
)
async def get_with_retry(
    client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None
) -> httpx.Response:
    response = await client.get(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    if response.status_code != 304:
        response.raise_for_status()
    return response


class Collector(Protocol):
    async def collect(self, source: Source, client: httpx.AsyncClient) -> list[Item]: ...


def build_item(
    source: Source,
    *,
    title: str,
    url: str,
    published_at: datetime | None,
    summary: str = "",
    content: str = "",
    categories: list[str] | None = None,
    kind: str = "blog_post",
) -> Item:
    from digest.pipeline.dedupe import item_id

    return Item(
        id=item_id(url),
        source_id=source.id,
        source_title=source.title,
        area=source.area,
        kind=kind,
        title=title.strip() or "(untitled)",
        url=url,
        published_at=published_at,
        summary_raw=summary,
        content_text=content[:6000],
        categories=categories or [],
    )


def now_utc() -> datetime:
    return datetime.now(UTC)
