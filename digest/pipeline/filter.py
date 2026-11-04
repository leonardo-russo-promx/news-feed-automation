from __future__ import annotations

from datetime import UTC, datetime, timedelta

from digest.models import Item, Source


def in_lookback(item: Item, days: int, now: datetime | None = None) -> bool:
    timestamp = item.updated_at or item.published_at
    if timestamp is None:
        return False
    current = now or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return current - timedelta(days=days) <= timestamp <= current


def filter_source_items(
    items: list[Item],
    source: Source,
    cap: int | None = None,
    settings_categories: list[str] | None = None,
) -> list[Item]:
    filters = source.filters
    category_filters = list(filters.include_categories)
    if filters.include_categories_from_settings == "roadmap_products":
        category_filters.extend(settings_categories or [])
    selected: list[Item] = []
    for item in items:
        haystack = f"{item.title}\n{item.summary_raw}\n{item.content_text}".casefold()
        if filters.include_keywords and not any(
            word.casefold() in haystack for word in filters.include_keywords
        ):
            continue
        if any(word.casefold() in haystack for word in filters.exclude_keywords):
            continue
        if category_filters and not any(
            wanted.casefold() in category.casefold()
            for wanted in category_filters
            for category in item.categories
        ):
            continue
        selected.append(item)
    selected.sort(
        key=lambda item: item.updated_at or item.published_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    limit = cap if cap is not None else filters.max_items
    return selected[:limit] if limit else selected
