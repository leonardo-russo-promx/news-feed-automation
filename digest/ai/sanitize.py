from __future__ import annotations

import re
from html import unescape

URL_PATTERN = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
BLOCK_PATTERN = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>")
TAG_PATTERN = re.compile(r"</?[A-Za-z!][^>]*>")


def strip_html(value: str) -> str:
    text = BLOCK_PATTERN.sub("", value)
    text = TAG_PATTERN.sub("", text)
    return unescape(text)


def sanitize_text(value: str, valid_ids: set[str] | None = None) -> str:
    text = URL_PATTERN.sub("[link omitted]", strip_html(value))
    if valid_ids is not None:
        text = re.sub(
            r"\b[a-f0-9]{64}\b",
            lambda match: match.group(0) if match.group(0) in valid_ids else "[unknown item]",
            text,
            flags=re.IGNORECASE,
        )
    return text.strip()
