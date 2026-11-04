from __future__ import annotations

from urllib.parse import urlsplit


def is_safe_http_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        return False
    return (
        parts.scheme in {"http", "https"}
        and bool(parts.hostname)
        and not parts.username
        and not parts.password
    )
