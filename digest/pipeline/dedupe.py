from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from digest.models import Item


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (key, value) for key, value in parse_qsl(parts.query) if not key.lower().startswith("utm_")
    ]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), "")
    )


def item_id(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


def deduplicate(items: list[Item], seen: dict[str, str], now: datetime | None = None) -> list[Item]:
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(days=120)
    retained = {
        key: value
        for key, value in seen.items()
        if _parse_time(value) is not None and _parse_time(value) >= cutoff
    }
    output: list[Item] = []
    for item in items:
        key = item_id(item.url)
        if key not in retained:
            item.id = key
            retained[key] = current.isoformat()
            output.append(item)
    seen.clear()
    seen.update(retained)
    return output


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
