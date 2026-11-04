from __future__ import annotations

from datetime import UTC, datetime

import feedparser
import httpx

from digest.collectors.base import build_item, get_with_retry
from digest.collectors.discovery import discover_feed
from digest.models import Item, Source


def parse_content(source: Source, content: bytes, warnings: list[str] | None = None) -> list[Item]:
    parsed_feed = feedparser.parse(content)
    if parsed_feed.bozo and not parsed_feed.entries:
        raise ValueError(f"invalid feed: {parsed_feed.bozo_exception}")
    items: list[Item] = []
    for entry in parsed_feed.entries:
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        if stamp is None:
            if warnings is not None:
                warnings.append(str(entry.get("title", "(untitled)")))
            continue
        published = datetime(*stamp[:6], tzinfo=UTC)
        link = entry.get("link")
        if not link:
            continue
        categories = [str(tag.get("term")) for tag in entry.get("tags", []) if tag.get("term")]
        summary = entry.get("summary", "")
        try:
            item = build_item(
                source,
                title=entry.get("title", ""),
                url=str(link),
                published_at=published,
                summary=summary,
                content=summary,
                categories=categories,
                kind=(
                    "roadmap"
                    if source.id == "ai-at-work-roadmap"
                    else "message_center"
                    if source.id == "message-center"
                    else "docs_change"
                    if source.method == "learn_search"
                    else "blog_post"
                ),
            )
        except ValueError:
            if warnings is not None:
                warnings.append(str(entry.get("title", "(invalid URL)")))
            continue
        items.append(item)
    return items


class RssCollector:
    def __init__(self) -> None:
        self.resolved_feed_url: str | None = None
        self.etag: str | None = None
        self.last_modified: str | None = None
        self.not_modified = False
        self.warning: str | None = None

    async def collect(
        self,
        source: Source,
        client: httpx.AsyncClient,
        validators: dict[str, str] | None = None,
    ) -> list[Item]:
        candidates = [str(url) for url in ([source.feed_url] if source.feed_url else [])]
        candidates.extend(str(url) for url in source.alt_feed_urls)
        if source.website_url:
            candidates.append("")
        errors: list[str] = []
        content = None
        for candidate in candidates:
            try:
                if not candidate:
                    found = await discover_feed(str(source.website_url), client)
                    if not found and source.tc_board_id:
                        found = (
                            "https://techcommunity.microsoft.com/t5/s/gxcuf89792/rss/board"
                            f"?board.id={source.tc_board_id}"
                        )
                    if not found:
                        raise ValueError("no RSS/Atom alternate link discovered")
                    candidate = found
                response = await get_with_retry(client, candidate, validators)
                self.resolved_feed_url = candidate
                self.etag = response.headers.get("ETag")
                self.last_modified = response.headers.get("Last-Modified")
                if response.status_code == 304:
                    self.not_modified = True
                    return []
                content = response.content
                break
            except Exception as exc:
                errors.append(f"{candidate or source.website_url}: {exc}")
        if content is None:
            raise RuntimeError("; ".join(errors) or "no feed URL available")
        warnings: list[str] = []
        items = parse_content(source, content, warnings)
        if warnings:
            self.warning = f"Skipped {len(warnings)} feed entries without a usable date or URL."
        return items
